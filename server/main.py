"""
main.py  — RKS Remote Keyboard System
FastAPI 主程式，整合 KBM（Keyboard+Mouse）與 SRV（Servo）

環境變數：
  CAMERA_INDEX   Webcam 裝置索引（預設 0）
  JPEG_QUALITY   MJPEG 品質 0–100（預設 75）
  HOST           監聽位址（預設 0.0.0.0）
  PORT           監聽埠號（預設 8000）
"""

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from collections import deque
from typing import Optional, Any

import cv2
import serial
import serial.tools.list_ports
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from keyboard_serial import KeyboardSerial
from servo_router import create_servo_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── 設定 ─────────────────────────────────────────────────────
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "75"))
HOST         = os.getenv("HOST", "0.0.0.0")
PORT         = int(os.getenv("PORT", "8000"))
AUTO_CONNECT_SERIAL = os.getenv("AUTO_CONNECT_SERIAL", "1").strip().lower() in ("1", "true", "yes", "on")

# ── 全域物件 ─────────────────────────────────────────────────
kb_serial = KeyboardSerial()
srv_router, srv_serial, srv_ws = create_servo_router()
executor   = ThreadPoolExecutor(max_workers=4)
camera: Optional[cv2.VideoCapture] = None

# 混合腳本執行 task / queue 控制
_script_queue: asyncio.Queue["MixedScriptRequest"] = asyncio.Queue()
_script_runner_task: Optional[asyncio.Task] = None

DEFAULT_EVT_TIMEOUT_MS = int(os.getenv("DEFAULT_EVT_TIMEOUT_MS", "30000"))


class EventBus:
    def __init__(self):
        self._events = deque()
        self._lock = asyncio.Lock()
        self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    @staticmethod
    def _normalize(value: str) -> str:
        value = (value or "").strip()
        return value[4:].strip() if value.upper().startswith("EVT:") else value

    def _match(self, expected: str, actual: str) -> bool:
        return self._normalize(expected) == self._normalize(actual)

    async def emit(self, event_line: str):
        async with self._lock:
            self._events.append(event_line)

    async def clear(self):
        async with self._lock:
            self._events.clear()

    async def consume_if_available(self, expected: str) -> Optional[str]:
        async with self._lock:
            for idx, actual in enumerate(self._events):
                if self._match(expected, actual):
                    match = self._events[idx]
                    del self._events[idx]
                    return match
        return None

    def emit_sync(self, event_line: str):
        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self.emit(event_line), self._loop)
        except Exception:
            pass

    async def wait_for(self, expected: str, timeout_ms: Optional[int] = None) -> str:
        timeout_s = (timeout_ms or DEFAULT_EVT_TIMEOUT_MS) / 1000
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            async with self._lock:
                for idx, actual in enumerate(self._events):
                    if self._match(expected, actual):
                        match = self._events[idx]
                        del self._events[idx]
                        return match
            remain = deadline - asyncio.get_running_loop().time()
            if remain <= 0:
                raise TimeoutError(f"EVT timeout waiting for {expected}")
            await asyncio.sleep(min(0.05, remain))


event_bus = EventBus()


class PendingScriptPool:
    def __init__(self):
        self._buckets: dict[str, deque[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._seq = 0

    @staticmethod
    def _gate_evt(req: "MixedScriptRequest") -> Optional[str]:
        for step in req.steps:
            if step.type == "evt":
                evt = (step.evt or "").strip()
                if evt:
                    return evt
        return None

    @staticmethod
    def _summary(req: "MixedScriptRequest") -> str:
        parts = [step.type.upper() for step in req.steps[:5]]
        if len(req.steps) > 5:
            parts.append("...")
        return " → ".join(parts) if parts else "(empty)"

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _consume_gate_step(req: "MixedScriptRequest", gate_evt: str) -> "MixedScriptRequest":
        new_steps: list[MixedStep] = []
        consumed = False
        for step in req.steps:
            cur_evt = (step.evt or "").strip()
            if not consumed and step.type == "evt" and cur_evt == gate_evt:
                consumed = True
                continue
            new_steps.append(step)
        if not consumed:
            return req.model_copy(deep=True)
        return req.model_copy(update={"steps": new_steps}, deep=True)

    @staticmethod
    def _restore_gate_step(req: "MixedScriptRequest", gate_evt: str) -> "MixedScriptRequest":
        steps = [MixedStep(type="evt", delay_ms=0, evt=gate_evt)]
        steps.extend(step.model_copy(deep=True) for step in req.steps)
        return req.model_copy(update={"steps": steps}, deep=True)

    async def hold(self, req: "MixedScriptRequest") -> Optional[str]:
        gate = self._gate_evt(req)
        if not gate:
            return None
        req_ready = self._consume_gate_step(req, gate)
        req_editor = req.model_copy(deep=True)
        async with self._lock:
            bucket = self._buckets.setdefault(gate, deque())
            bucket.append({
                "id": self._next_id(),
                "gate_evt": gate,
                "created_at": time.time(),
                "req": req_ready,
                "req_editor": req_editor,
                "summary": self._summary(req),
            })
        return gate

    async def drain(self, evt: str) -> list[dict[str, Any]]:
        async with self._lock:
            bucket = self._buckets.pop(evt, deque())
            return list(bucket)

    async def peek(self, evt: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            bucket = self._buckets.get(evt)
            if not bucket:
                return None
            item = bucket[0]
            req_editor = item.get("req_editor")
            if req_editor is None:
                req_editor = self._restore_gate_step(item["req"], evt)
            return {
                "id": item["id"],
                "evt": evt,
                "req": req_editor.model_dump(),
            }

    async def delete_bucket(self, evt: str) -> int:
        async with self._lock:
            bucket = self._buckets.pop(evt, deque())
            return len(bucket)

    async def clear(self):
        async with self._lock:
            self._buckets.clear()

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            now = time.time()
            buckets = []
            total = 0
            for evt in sorted(self._buckets.keys()):
                items = list(self._buckets[evt])
                total += len(items)
                buckets.append({
                    "evt": evt,
                    "count": len(items),
                    "scripts": [
                        {
                            "id": item["id"],
                            "gate_evt": item["gate_evt"],
                            "steps": len(item["req"].steps),
                            "loop": item["req"].loop,
                            "age_s": round(now - item["created_at"], 1),
                            "summary": item["summary"],
                        }
                        for item in items
                    ],
                })
            return {"total": total, "buckets": buckets}


pending_evt_pool = PendingScriptPool()

# ── KBM WebSocket 管理器 ──────────────────────────────────────
class KbmWSManager:
    def __init__(self):
        self._clients: list[WebSocket] = []
        self._lock  = asyncio.Lock()
        self._loop  = None

    def set_loop(self, loop): self._loop = loop

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._clients.append(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._clients = [c for c in self._clients if c is not ws]

    async def broadcast(self, data: dict):
        data["channel"] = "kbm"
        msg  = json.dumps(data, ensure_ascii=False)
        dead = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    def broadcast_sync(self, data: dict):
        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self.broadcast(data), self._loop)
        except Exception:
            pass

kbm_ws = KbmWSManager()

def _setup_kbm_callbacks():
    def on_line(line: str):
        kbm_ws.broadcast_sync({"type": "serial", "line": line})
        if line.startswith("EVT:"):
            evt = line[4:].strip()
            kbm_ws.broadcast_sync({"type": "event", "evt": evt, "raw": line})
            try:
                if kbm_ws._loop and kbm_ws._loop.is_running():
                    asyncio.run_coroutine_threadsafe(_emit_event_and_release(evt), kbm_ws._loop)
            except Exception:
                pass
    def on_send(cmd: str):
        kbm_ws.broadcast_sync({"type": "serial", "line": f"→ {cmd}"})
    def on_disconnect():
        kbm_ws.broadcast_sync({"type": "status", "state": "disconnected", "port": None})
    kb_serial.on_line(on_line)
    kb_serial.on_send(on_send)
    kb_serial.on_disconnect(on_disconnect)

_setup_kbm_callbacks()


async def _notify_pool_snapshot():
    snap = await pending_evt_pool.snapshot()
    await kbm_ws.broadcast({"type": "script_pool", **snap})
    srv_ws.broadcast_sync({"type": "script_pool", **snap})


async def _activate_script(req: "MixedScriptRequest") -> int:
    await _ensure_script_runner()
    await _script_queue.put(req)
    return _script_queue.qsize()


async def _release_pending_for_event(evt: str, source: str = "event"):
    released = await pending_evt_pool.drain(evt)
    if not released:
        return 0
    released_ids = []
    released_cnt = 0
    for item in released:
        await _activate_script(item["req"])
        released_cnt += 1
        released_ids.append(item["id"])
    await _notify_pool_snapshot()
    await kbm_ws.broadcast({
        "type": "script_pool_released",
        "evt": evt,
        "source": source,
        "released": released_cnt,
        "ids": released_ids,
        "queued": _script_queue.qsize(),
    })
    return released_cnt


async def _emit_event_and_release(evt: str):
    await event_bus.emit(evt)
    await _release_pending_for_event(evt, source="event")


def _probe_serial_port(port: str, baud: int, probe_cmd: str, ok_prefixes: tuple[str, ...]) -> bool:
    """Open a serial port temporarily and check whether probe command gets expected ACK."""
    ser = None
    try:
        ser = serial.Serial(port, baudrate=baud, timeout=0.3)
        # Most Arduino boards reset on serial open.
        time.sleep(1.8)
        ser.reset_input_buffer()
        ser.write(f"{probe_cmd}\n".encode("utf-8"))
        ser.flush()
        deadline = time.monotonic() + 1.8
        while time.monotonic() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip().upper()
            if any(line.startswith(prefix) for prefix in ok_prefixes):
                return True
    except Exception:
        return False
    finally:
        try:
            if ser and ser.is_open:
                ser.close()
        except Exception:
            pass
    return False


async def _auto_connect_serial_devices():
    if not AUTO_CONNECT_SERIAL:
        logger.info("AUTO_CONNECT_SERIAL disabled")
        return
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        logger.info("Auto connect: no serial ports found")
        return

    loop = asyncio.get_running_loop()
    srv_port = None
    kbm_port = None

    # Probe SRV first (115200, supports PING -> OK PONG / READY family).
    for port in ports:
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(
                    executor,
                    lambda p=port: _probe_serial_port(p, 115200, "PING", ("OK PONG", "OK READY", "OK ")),
                ),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Auto probe SRV timeout: {port}")
            ok = False
        if ok:
            srv_port = port
            break

    # Probe KBM on remaining ports (38400, PING should return OK/ERR).
    for port in ports:
        if port == srv_port:
            continue
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(
                    executor,
                    lambda p=port: _probe_serial_port(p, 38400, "PING", ("OK", "ERR")),
                ),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Auto probe KBM timeout: {port}")
            ok = False
        if ok:
            kbm_port = port
            break

    if srv_port and not srv_serial.is_connected:
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(executor, srv_serial.connect, srv_port),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Auto connect SRV timeout: {srv_port}")
            ok = False
        if ok:
            srv_serial.send("STATUS")
            srv_ws.broadcast_sync({"type": "status", "state": "idle", "port": srv_port, "attached": srv_serial.attached})
            logger.info(f"Auto connected SRV: {srv_port}")
        else:
            logger.warning(f"Auto connect SRV failed: {srv_port}")
    else:
        logger.info("Auto connect SRV: no matched port")

    if kbm_port and not kb_serial.is_connected:
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(executor, kb_serial.connect, kbm_port),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Auto connect KBM timeout: {kbm_port}")
            ok = False
        if ok:
            kbm_ws.broadcast_sync({"type": "status", "state": "idle", "port": kbm_port})
            logger.info(f"Auto connected KBM: {kbm_port}")
        else:
            logger.warning(f"Auto connect KBM failed: {kbm_port}")
    else:
        logger.info("Auto connect KBM: no matched port")

# ── Lifespan ─────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global camera
    loop = asyncio.get_running_loop()
    srv_ws.set_loop(loop)
    kbm_ws.set_loop(loop)
    event_bus.set_loop(loop)
    # Run serial auto-connect in background so startup is never blocked by COM probing.
    asyncio.create_task(_auto_connect_serial_devices())
    logger.info(f"Opening camera index: {CAMERA_INDEX}")
    camera = cv2.VideoCapture(CAMERA_INDEX)
    yield
    kb_serial.disconnect()
    srv_serial.disconnect()
    if camera:
        camera.release()
    executor.shutdown(wait=False)
    logger.info("Shutdown complete.")

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="RKS — Remote Keyboard System", lifespan=lifespan)
app.include_router(srv_router, prefix="/srv")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ═══════════════════════════════════════════════════════════════
#  Pydantic Models
# ═══════════════════════════════════════════════════════════════
class KeyEvent(BaseModel):
    cmd: str

class ConnectRequest(BaseModel):
    port: str

# ── 混合腳本步驟 ──────────────────────────────────────────────
class MixedStep(BaseModel):
    type:        str              = Field(..., pattern="^(srv|kbd|mse|evt)$")
    delay_ms:    int              = Field(0,   ge=0)
    evt:         Optional[str]    = None   # EVT step name; legacy non-EVT steps are normalized on input
    # SRV
    servo_id:    Optional[int]   = Field(None, ge=1, le=6)
    angle:       Optional[int]   = Field(None, ge=0, le=180)
    speed:       Optional[int]   = Field(None, ge=1, le=100)
    duration_ms: Optional[int]   = Field(None, ge=0)
    home:        Optional[int]   = Field(None, ge=0, le=1)
    # KBD — 可以直接給 cmd 字串，也可以給結構化欄位（前端 Export 格式）
    cmd:         Optional[str]   = None   # 完整指令，例如 "COMBO:CTRL+C"
    cmd_type:    Optional[str]   = None   # "COMBO" | "TYPE" | "KEY"
    mod1:        Optional[str]   = None
    mod2:        Optional[str]   = None
    key:         Optional[str]   = None
    text:        Optional[str]   = None
    # MSE
    action:      Optional[str]   = None   # MOVE|MOVE_TO|CLICK|DBLCLICK|SCROLL
    btn:         Optional[str]   = None   # L|R|M
    x:           Optional[int]   = None
    y:           Optional[int]   = None
    amount:      Optional[int]   = None

class MixedScriptRequest(BaseModel):
    steps:       list[MixedStep] = Field(default_factory=list, max_length=200)
    loop:        bool            = False
    # auto-attach: 執行前自動 attach 需要的 servo
    servos:      dict[str, int]  = Field(default_factory=dict)
    attach_cmds: list[str]       = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
#  Helper: 把步驟結構化欄位轉成協議字串
# ═══════════════════════════════════════════════════════════════
def _resolve_kbd_cmd(step: MixedStep) -> str:
    """把 KBD 步驟轉成 Pro Micro 指令字串"""
    # 1. 直接給了 cmd 字串就用它
    if step.cmd:
        return step.cmd.strip()
    # 2. 結構化欄位
    ct = (step.cmd_type or "TYPE").upper()
    if ct == "COMBO":
        mods = [m for m in [step.mod1, step.mod2] if m]
        key  = step.key or "C"
        return f"COMBO:{'+'.join(mods + [key])}"
    elif ct == "KEY":
        return f"KEY:{step.key or 'ENTER'}"
    else:  # TYPE
        return f"TYPE:{step.text or ''}"

def _resolve_mse_cmd(step: MixedStep) -> str:
    """把 MSE 步驟轉成 Pro Micro 指令字串"""
    if step.cmd:
        return step.cmd.strip()
    action = (step.action or "MOVE").upper()
    if action in ("MOVE", "MOVE_TO"):
        return f"MOUSE:MOVE {step.x or 0} {step.y or 0}"
    elif action == "CLICK":
        return f"MOUSE:CLICK {step.btn or 'L'}"
    elif action == "DBLCLICK":
        return f"MOUSE:DBLCLICK {step.btn or 'L'}"
    elif action == "SCROLL":
        return f"MOUSE:SCROLL {step.amount or 3}"
    elif action == "DOWN":
        return f"MOUSE:DOWN {step.btn or 'L'}"
    elif action == "UP":
        return f"MOUSE:UP {step.btn or 'L'}"
    return f"MOUSE:MOVE 0 0"


def _normalize_script_request(req: MixedScriptRequest) -> MixedScriptRequest:
    """Convert legacy per-step evt gates into standalone EVT steps."""
    normalized_steps: list[MixedStep] = []
    for step in req.steps:
        evt_name = (step.evt or "").strip()
        if step.type != "evt" and evt_name:
            normalized_steps.append(MixedStep(type="evt", delay_ms=0, evt=evt_name))
            step = step.model_copy(update={"evt": None})
        normalized_steps.append(step)
    return req.model_copy(update={"steps": normalized_steps})


# ═══════════════════════════════════════════════════════════════
#  Mixed Script 後端執行引擎
# ═══════════════════════════════════════════════════════════════
async def _wait_for_step_event(step: MixedStep, idx: int, total: int) -> bool:
    if not step.evt:
        return True
    await kbm_ws.broadcast({
        "type": "script_wait",
        "step": idx,
        "total": total,
        "evt": step.evt,
    })
    srv_ws.broadcast_sync({
        "type": "status",
        "state": "waiting",
        "step": idx,
        "total": total,
    })
    try:
        matched = await event_bus.wait_for(step.evt)
        await kbm_ws.broadcast({
            "type": "script_event",
            "step": idx,
            "total": total,
            "evt": matched,
        })
        return True
    except TimeoutError as exc:
        await kbm_ws.broadcast({
            "type": "script_error",
            "step": idx,
            "message": str(exc),
        })
        srv_ws.broadcast_sync({"type": "status", "state": "idle"})
        return False


async def _run_mixed_script(req: MixedScriptRequest):
    """後端逐步執行混合腳本，透過 WS 推播進度"""
    loop = asyncio.get_event_loop()
    total = len(req.steps)

    # 廣播開始
    await kbm_ws.broadcast({
        "type": "script_start",
        "total": total,
        "loop": req.loop,
    })

    iteration = 0
    try:
        while True:
            for i, step in enumerate(req.steps):
                # 進度廣播
                await kbm_ws.broadcast({
                    "type":      "script_progress",
                    "step":      i,
                    "total":     total,
                    "step_type": step.type,
                    "iteration": iteration,
                })
                # 也透過 srv_ws 廣播（讓 Progress panel 更新）
                srv_ws.broadcast_sync({
                    "type":  "status",
                    "state": "running",
                    "step":  i,
                    "total": total,
                })

                if step.type == "evt":
                    ok_evt = await _wait_for_step_event(step, i, total)
                    if not ok_evt:
                        return

                # delay
                if step.delay_ms > 0:
                    await asyncio.sleep(step.delay_ms / 1000)

                if step.type == "evt":
                    continue

                # 執行
                if step.type == "srv":
                    params = {
                        "delay_ms":    0,
                        "servo_id":    step.servo_id or 1,
                        "angle":       step.angle if step.angle is not None else 90,
                        "speed":       step.speed or 60,
                        "duration_ms": step.duration_ms if step.duration_ms is not None else 300,
                        "home":        step.home if step.home is not None else 1,
                    }
                    ok = await loop.run_in_executor(
                        executor, lambda p=params: srv_serial.send_command(p)
                    )
                    # 等 Servo 完成
                    hold = (step.duration_ms or 300)
                    angle = (step.angle or 90)
                    wait_s = hold / 1000 + angle * 0.01 + 0.25
                    await asyncio.sleep(wait_s)
                    if not ok:
                        await kbm_ws.broadcast({
                            "type": "script_error",
                            "step": i,
                            "message": f"SRV step {i} failed",
                        })

                elif step.type == "kbd":
                    cmd = _resolve_kbd_cmd(step)
                    result = await loop.run_in_executor(
                        executor, kb_serial.send, cmd
                    )
                    if result != "OK":
                        await kbm_ws.broadcast({
                            "type": "script_error",
                            "step": i,
                            "message": f"KBD step {i} ACK: {result} cmd: {cmd}",
                        })

                elif step.type == "mse":
                    cmd = _resolve_mse_cmd(step)
                    result = await loop.run_in_executor(
                        executor, kb_serial.send, cmd
                    )
                    if result != "OK":
                        await kbm_ws.broadcast({
                            "type": "script_error",
                            "step": i,
                            "message": f"MSE step {i} ACK: {result} cmd: {cmd}",
                        })

            iteration += 1
            if not req.loop:
                break

        # 完成
        await kbm_ws.broadcast({"type": "script_done", "iterations": iteration})
        srv_ws.broadcast_sync({"type": "done", "state": "idle"})
        logger.info(f"Script done: {total} steps × {iteration} iterations")

    except asyncio.CancelledError:
        await kbm_ws.broadcast({"type": "script_stopped"})
        srv_ws.broadcast_sync({"type": "status", "state": "idle"})
        logger.info("Script cancelled")


async def _script_runner():
    global _script_runner_task
    try:
        while True:
            req = await _script_queue.get()
            try:
                await _run_mixed_script(req)
            finally:
                _script_queue.task_done()
            if _script_queue.empty():
                break
    except asyncio.CancelledError:
        raise
    finally:
        _script_runner_task = None


async def _ensure_script_runner():
    global _script_runner_task
    if _script_runner_task is None or _script_runner_task.done():
        _script_runner_task = asyncio.create_task(_script_runner())


async def _drain_script_queue():
    while True:
        try:
            _script_queue.get_nowait()
        except asyncio.QueueEmpty:
            break


# ═══════════════════════════════════════════════════════════════
#  Routes: 頁面
# ═══════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()

@app.get("/arch", response_class=HTMLResponse)
async def arch():
    with open(os.path.join(STATIC_DIR, "arch.html"), encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
#  Routes: KBM serial
# ═══════════════════════════════════════════════════════════════
@app.get("/kbm/api/ports")
async def kbm_ports():
    return {"ports": KeyboardSerial.scan_ports()}

@app.get("/kbm/api/status")
async def kbm_status():
    return {
        "type":    "status",
        "channel": "kbm",
        "state":   "idle" if kb_serial.is_connected else "disconnected",
        "port":    kb_serial.port,
    }

@app.post("/kbm/api/connect")
async def kbm_connect(req: ConnectRequest):
    ok = await asyncio.get_event_loop().run_in_executor(
        executor, kb_serial.connect, req.port
    )
    if not ok:
        raise HTTPException(500, f"無法連線到 {req.port}")
    kbm_ws.broadcast_sync({"type": "status", "state": "idle", "port": req.port})
    return {"ok": True, "port": req.port}

@app.post("/kbm/api/disconnect")
async def kbm_disconnect():
    kb_serial.disconnect()
    return {"ok": True}

@app.post("/kbm/api/send")
async def kbm_send(event: KeyEvent):
    cmd = event.cmd.strip()
    if not cmd:
        raise HTTPException(400, "cmd 不能為空")
    result = await asyncio.get_event_loop().run_in_executor(
        executor, kb_serial.send, cmd
    )
    if result == "OK":
        return {"status": "ok", "cmd": cmd}
    raise HTTPException(500, {"status": result, "cmd": cmd})


# ═══════════════════════════════════════════════════════════════
#  Routes: 混合腳本執行（支援 curl --data-binary @file.json）
# ═══════════════════════════════════════════════════════════════
@app.post("/script/run")
async def script_run(req: MixedScriptRequest):
    """
    執行混合腳本（SRV + KBD + MSE 步驟）。

    curl 範例：
      curl -X POST http://127.0.0.1:8000/script/run \\
           -H "Content-Type: application/json" \\
           --data-binary "@exported_script.json"
    """
    req = _normalize_script_request(req)

    if not req.steps:
        raise HTTPException(400, "steps 不能為空")
    if any(step.type == "evt" and not (step.evt or "").strip() for step in req.steps):
        raise HTTPException(400, "EVT step 需要 evt 名稱")

    gate_evt = None
    for step in req.steps:
        if step.type == "evt":
            gate_evt = (step.evt or "").strip()
            if gate_evt:
                break

    if (_script_runner_task is None or _script_runner_task.done()) and _script_queue.empty():
        await event_bus.clear()

    if gate_evt:
        immediate = await event_bus.consume_if_available(gate_evt)
        if immediate is None:
            await pending_evt_pool.hold(req)
            await _notify_pool_snapshot()
            return {
                "ok": True,
                "steps": len(req.steps),
                "loop": req.loop,
                "pending": True,
                "gate_evt": gate_evt,
                "queued": _script_queue.qsize(),
            }
            req = PendingScriptPool._consume_gate_step(req, gate_evt)

    queued = await _activate_script(req)
    await _notify_pool_snapshot()

    # auto-attach servos 需要的 sid
    if srv_serial.is_connected:
        loop_ = asyncio.get_event_loop()
        used_sids = {s.servo_id for s in req.steps if s.type == "srv" and s.servo_id}
        pin_map   = {int(k): v for k, v in (req.servos or {}).items()}
        for sid in sorted(used_sids):
            if sid in srv_serial.attached:
                continue
            pin = pin_map.get(sid)
            if pin is None:
                continue
            ok = await loop_.run_in_executor(
                executor,
                lambda s=sid, p=pin: srv_serial.attach_servo_and_wait(s, p)
            )
            if not ok:
                raise HTTPException(500, f"Auto ATTACH 失敗 sid={sid}")

    return {
        "ok":    True,
        "steps": len(req.steps),
        "loop":  req.loop,
        "pending": False,
        "queued": queued,
    }

@app.post("/script/stop")
async def script_stop():
    global _script_runner_task
    if _script_runner_task and not _script_runner_task.done():
        _script_runner_task.cancel()
        try:
            await _script_runner_task
        except asyncio.CancelledError:
            pass
    await _drain_script_queue()
    await event_bus.clear()
    await pending_evt_pool.clear()
    await _notify_pool_snapshot()
    # 也停 Servo
    if srv_serial.is_connected:
        srv_serial.send("STOP")
    return {"ok": True}

@app.get("/script/status")
async def script_status():
    running = bool(_script_runner_task and not _script_runner_task.done())
    pending = await pending_evt_pool.snapshot()
    return {"running": running, "queued": _script_queue.qsize(), "pending": pending}


@app.get("/script/pool/{evt}")
async def script_pool_peek(evt: str):
    item = await pending_evt_pool.peek(evt)
    if not item:
        raise HTTPException(404, "找不到對應 EVT bucket")
    return {"ok": True, **item}


@app.delete("/script/pool/{evt}")
async def script_pool_delete(evt: str):
    deleted = await pending_evt_pool.delete_bucket(evt)
    if deleted == 0:
        raise HTTPException(404, "找不到對應 EVT bucket")
    await _notify_pool_snapshot()
    return {"ok": True, "evt": evt, "deleted": deleted}


@app.post("/script/pool/{evt}/queue_now")
async def script_pool_queue_now(evt: str):
    released = await _release_pending_for_event(evt, source="manual")
    if released == 0:
        raise HTTPException(404, "找不到對應 EVT bucket")
    return {
        "ok": True,
        "evt": evt,
        "released": released,
        "queued": _script_queue.qsize(),
    }


# ═══════════════════════════════════════════════════════════════
#  Routes: WebSocket KBM
# ═══════════════════════════════════════════════════════════════
@app.websocket("/ws/kbm")
async def ws_kbm(ws: WebSocket):
    await kbm_ws.connect(ws)
    await ws.send_text(json.dumps({
        "type":    "status",
        "channel": "kbm",
        "state":   "idle" if kb_serial.is_connected else "disconnected",
        "port":    kb_serial.port,
    }))
    try:
        while True:
            data = await ws.receive_text()
            msg  = json.loads(data)
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong", "channel": "kbm"}))
    except WebSocketDisconnect:
        await kbm_ws.disconnect(ws)


@app.websocket("/ws/srv")
async def ws_srv(ws: WebSocket):
    await srv_ws.connect(ws)
    await ws.send_text(json.dumps({
        "type":     "status",
        "channel":  "srv",
        "state":    "idle" if srv_serial.is_connected else "disconnected",
        "port":     srv_serial.port,
        "attached": srv_serial.attached,
    }, ensure_ascii=False))
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong", "channel": "srv"}))
    except WebSocketDisconnect:
        await srv_ws.disconnect(ws)


# ═══════════════════════════════════════════════════════════════
#  Routes: health + Webcam
# ═══════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    running = bool(_script_runner_task and not _script_runner_task.done())
    pending = await pending_evt_pool.snapshot()
    return {
        "kbm": {
            "connected": kb_serial.is_connected,
            "port":      kb_serial.port,
        },
        "srv": {
            "connected": srv_serial.is_connected,
            "port":      srv_serial.port,
            "attached":  srv_serial.attached,
        },
        "camera": {
            "opened": camera.isOpened() if camera else False,
            "index":  CAMERA_INDEX,
        },
        "script": {
            "running": running,
            "queued": _script_queue.qsize(),
            "pending": pending,
        },
    }

def _generate_frames():
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
    while True:
        if not camera or not camera.isOpened():
            break
        ret, frame = camera.read()
        if not ret:
            cv2.waitKey(100)
            continue
        ok2, buf = cv2.imencode(".jpg", frame, encode_params)
        if not ok2:
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + buf.tobytes()
            + b"\r\n"
        )

@app.get("/stream")
def video_stream():
    if not camera or not camera.isOpened():
        raise HTTPException(503, "Camera not available")
    return StreamingResponse(
        _generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False, log_level="info")
