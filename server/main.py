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
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional, Any

import cv2
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

# ── 全域物件 ─────────────────────────────────────────────────
kb_serial = KeyboardSerial()
srv_router, srv_serial, srv_ws = create_servo_router()
executor   = ThreadPoolExecutor(max_workers=4)
camera: Optional[cv2.VideoCapture] = None

# 混合腳本執行 task 控制
_script_task: Optional[asyncio.Task] = None

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
    def on_disconnect():
        kbm_ws.broadcast_sync({"type": "status", "state": "disconnected", "port": None})
    kb_serial.on_line(on_line)
    kb_serial.on_disconnect(on_disconnect)

_setup_kbm_callbacks()

# ── Lifespan ─────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global camera
    loop = asyncio.get_running_loop()
    srv_ws.set_loop(loop)
    kbm_ws.set_loop(loop)
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
    type:        str              = Field(..., pattern="^(srv|kbd|mse)$")
    delay_ms:    int              = Field(0,   ge=0)
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


# ═══════════════════════════════════════════════════════════════
#  Mixed Script 後端執行引擎
# ═══════════════════════════════════════════════════════════════
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

                # delay
                if step.delay_ms > 0:
                    await asyncio.sleep(step.delay_ms / 1000)

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
        kbm_ws.broadcast_sync({"type": "serial", "line": f"→ {cmd}"})
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
    global _script_task

    if not req.steps:
        raise HTTPException(400, "steps 不能為空")

    # 停掉舊的（若有）
    if _script_task and not _script_task.done():
        _script_task.cancel()
        try:
            await _script_task
        except asyncio.CancelledError:
            pass

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

    _script_task = asyncio.create_task(_run_mixed_script(req))
    return {
        "ok":    True,
        "steps": len(req.steps),
        "loop":  req.loop,
    }

@app.post("/script/stop")
async def script_stop():
    global _script_task
    if _script_task and not _script_task.done():
        _script_task.cancel()
        try:
            await _script_task
        except asyncio.CancelledError:
            pass
    # 也停 Servo
    if srv_serial.is_connected:
        srv_serial.send("STOP")
    return {"ok": True}

@app.get("/script/status")
async def script_status():
    running = bool(_script_task and not _script_task.done())
    return {"running": running}


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


# ═══════════════════════════════════════════════════════════════
#  Routes: health + Webcam
# ═══════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    running = bool(_script_task and not _script_task.done())
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
