"""
main.py  — RKS Remote Keyboard System
FastAPI 主程式，整合 KBM（Keyboard+Mouse）與 SRV（Servo）

環境變數：
  CAMERA_INDEX   Webcam 裝置索引（預設 0）
  CAMERA_WIDTH   Webcam 請求寬度（預設 1280）
  CAMERA_HEIGHT  Webcam 請求高度（預設 720）
  JPEG_QUALITY   MJPEG 品質 0–100（預設 75）
  HOST           監聽位址（預設 0.0.0.0）
  PORT           監聽埠號（預設 8000）
"""

import asyncio
import socket
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from collections import deque
from typing import Optional, Any

import base64
import dataclasses
import re
import threading
import uuid

import numpy as np
import cv2
import serial
import serial.tools.list_ports
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
CAMERA_INDEX  = int(os.getenv("CAMERA_INDEX",  "0"))
CAMERA_WIDTH  = int(os.getenv("CAMERA_WIDTH",  "1280"))
CAMERA_HEIGHT = int(os.getenv("CAMERA_HEIGHT", "720"))
JPEG_QUALITY  = int(os.getenv("JPEG_QUALITY",  "75"))
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


def _data_dir() -> str:
    """Returns persistent data directory: next to EXE when frozen, else next to main.py."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _find_available_port(host: str, preferred_port: int, max_tries: int = 30) -> int:
    """Return the first bindable TCP port starting from preferred_port."""
    for offset in range(max_tries):
        port = preferred_port + offset
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError(f"No available port from {preferred_port} to {preferred_port + max_tries - 1}")


def _resolve_lan_ipv4s() -> list[str]:
    """Best-effort resolve of LAN IPv4 addresses for connection hints."""
    ips: set[str] = set()

    # Route-based local IP (doesn't send packets, just asks OS route table).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip:
                ips.add(ip)
        finally:
            s.close()
    except Exception:
        pass

    # Hostname-based IPs.
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if ip:
                ips.add(ip)
    except Exception:
        pass

    # Filter out loopback/link-local placeholders.
    out = [
        ip for ip in sorted(ips)
        if not ip.startswith("127.") and not ip.startswith("169.254.") and ip != "0.0.0.0"
    ]
    return out


def _log_access_urls(host: str, port: int):
    if host == "0.0.0.0":
        logger.info(f"Access URL (local): http://127.0.0.1:{port}")
        lan_ips = _resolve_lan_ipv4s()
        if lan_ips:
            for ip in lan_ips:
                logger.info(f"Access URL (LAN): http://{ip}:{port}")
        else:
            logger.warning("No LAN IPv4 detected automatically. Please run ipconfig to check IPv4 address.")
        return
    logger.info(f"Access URL: http://{host}:{port}")


# ── FrameBuffer ───────────────────────────────────────────────
class FrameBuffer:
    """Daemon thread reads camera at fixed fps; MJPEG stream and visual monitor share this buffer."""

    def __init__(self, capture_fps: int = 15):
        self._lock = threading.Lock()
        self._frame: Optional[Any] = None  # latest BGR numpy array
        self._capture_fps = capture_fps
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._cap: Optional[cv2.VideoCapture] = None

    def start(self, cap: cv2.VideoCapture):
        # Stop existing thread before starting a new one
        if self._running:
            self._running = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.0)
        with self._lock:
            self._frame = None
        self._cap = cap
        self._running = True
        self._thread = threading.Thread(target=self._reader_loop, daemon=True, name="FrameBuffer")
        self._thread.start()

    def stop(self):
        self._running = False
        with self._lock:
            self._frame = None

    def _reader_loop(self):
        interval = 1.0 / self._capture_fps
        while self._running:
            cap = self._cap
            if cap is None or not cap.isOpened():
                time.sleep(interval)
                continue
            ret, frame = cap.read()
            if ret:
                with self._lock:
                    self._frame = frame
            time.sleep(interval)

    def read(self) -> Optional[Any]:
        """Return a copy of the latest BGR frame, or None if camera not yet ready."""
        with self._lock:
            return None if self._frame is None else self._frame.copy()


# ── SnapshotPool ──────────────────────────────────────────────
class SnapshotPool:
    """In-memory JPEG snapshot store with TTL. Thread-safe."""
    MAX_ITEMS = 100
    TTL_SECONDS = 1800  # 30 minutes

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict = {}  # snap_id -> dict

    def _store_jpeg_bytes(self, jpeg_bytes: bytes, snap_id: Optional[str] = None) -> dict:
        img = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Failed to decode JPEG snapshot")
        h, w = img.shape[:2]
        created_at = time.time()
        item = {
            "id": snap_id or uuid.uuid4().hex[:8],
            "jpeg_b64": base64.b64encode(jpeg_bytes).decode(),
            "width": w,
            "height": h,
            "created_at": created_at,
        }
        with self._lock:
            now = time.time()
            expired = [k for k, v in self._items.items() if now - v["created_at"] > self.TTL_SECONDS]
            for k in expired:
                del self._items[k]
            if len(self._items) >= self.MAX_ITEMS:
                oldest = min(self._items, key=lambda k: self._items[k]["created_at"])
                del self._items[oldest]
            self._items[item["id"]] = item
        return {"id": item["id"], "width": w, "height": h, "created_at": created_at}

    def add(self, frame: Any) -> dict:
        """Encode BGR frame to JPEG, store in pool, return metadata (without full b64)."""
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise ValueError("Failed to encode frame to JPEG")
        return self._store_jpeg_bytes(buf.tobytes())

    def add_b64(self, jpeg_b64: str) -> dict:
        try:
            jpeg_bytes = base64.b64decode(jpeg_b64)
        except Exception as exc:
            raise ValueError("Invalid base64 snapshot payload") from exc
        return self._store_jpeg_bytes(jpeg_bytes)

    def get(self, snap_id: str) -> Optional[dict]:
        with self._lock:
            return self._items.get(snap_id)

    def delete(self, snap_id: str) -> bool:
        with self._lock:
            if snap_id in self._items:
                del self._items[snap_id]
                return True
            return False

    def list_all(self) -> list:
        with self._lock:
            return [
                {"id": v["id"], "width": v["width"], "height": v["height"],
                 "created_at": v["created_at"], "thumb_b64": v["jpeg_b64"]}
                for v in self._items.values()
            ]


# ── ConditionManager ──────────────────────────────────────────
@dataclasses.dataclass
class MatchCondition:
    condition_id: str
    name: str
    snapshot_id: str
    roi: list          # [x, y, w, h] normalized 0–1
    threshold: float = 0.92
    min_hits: int = 3
    cooldown_ms: int = 3000
    fps_cap: int = 3
    enabled: bool = False


@dataclasses.dataclass
class ConditionRuntime:
    consecutive_hits: int = 0
    last_fired_at: float = 0.0
    cooldown_until: float = 0.0
    prev_matched: bool = False
    last_score: float = 0.0


class ConditionManager:
    """Manages visual match conditions; persists to conditions.json."""

    def __init__(self):
        self._lock = threading.Lock()
        self._conditions: dict = {}  # cid -> MatchCondition
        self._runtime: dict = {}     # cid -> ConditionRuntime
        self._load()

    def _persist_path(self) -> str:
        return os.path.join(_data_dir(), "conditions.json")

    def _load(self):
        path = self._persist_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    item["enabled"] = False  # always start disarmed
                    c = MatchCondition(**item)
                    self._conditions[c.condition_id] = c
                    self._runtime[c.condition_id] = ConditionRuntime()
        except Exception as exc:
            logger.warning(f"Failed to load conditions.json: {exc}")

    def _save(self):
        try:
            with open(self._persist_path(), "w", encoding="utf-8") as f:
                json.dump([dataclasses.asdict(c) for c in self._conditions.values()], f, indent=2)
        except Exception as exc:
            logger.warning(f"Failed to save conditions.json: {exc}")

    def add(self, name: str, snapshot_id: str, roi: list,
            threshold: float, min_hits: int, cooldown_ms: int) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-") or "cond"
        cid = f"{slug}-{uuid.uuid4().hex[:4]}"
        cond = MatchCondition(
            condition_id=cid, name=name, snapshot_id=snapshot_id, roi=roi,
            threshold=threshold, min_hits=min_hits, cooldown_ms=cooldown_ms,
        )
        with self._lock:
            self._conditions[cid] = cond
            self._runtime[cid] = ConditionRuntime()
            self._save()
        return cid

    def get(self, cid: str) -> Optional[MatchCondition]:
        with self._lock:
            return self._conditions.get(cid)

    def list_all(self) -> list:
        with self._lock:
            now = time.time()
            return [
                {
                    "condition_id": cid,
                    "name": cond.name,
                    "snapshot_id": cond.snapshot_id,
                    "roi": cond.roi,
                    "threshold": cond.threshold,
                    "min_hits": cond.min_hits,
                    "cooldown_ms": cond.cooldown_ms,
                    "enabled": cond.enabled,
                    "last_score": round(self._runtime.get(cid, ConditionRuntime()).last_score, 3),
                    "consecutive_hits": self._runtime.get(cid, ConditionRuntime()).consecutive_hits,
                    "in_cooldown": now < self._runtime.get(cid, ConditionRuntime()).cooldown_until,
                }
                for cid, cond in self._conditions.items()
            ]

    def arm(self, cid: str):
        with self._lock:
            if cid in self._conditions:
                self._conditions[cid].enabled = True
                self._runtime[cid] = ConditionRuntime()

    def disarm(self, cid: str):
        with self._lock:
            if cid in self._conditions:
                self._conditions[cid].enabled = False

    def update(self, cid: str, **kwargs) -> bool:
        with self._lock:
            cond = self._conditions.get(cid)
            if not cond:
                return False
            for k, v in kwargs.items():
                if v is not None and hasattr(cond, k):
                    setattr(cond, k, v)
            self._save()
            return True

    def delete(self, cid: str) -> bool:
        with self._lock:
            if cid in self._conditions:
                del self._conditions[cid]
                self._runtime.pop(cid, None)
                self._save()
                return True
            return False

    def get_armed(self) -> list:
        with self._lock:
            return [
                (cond, self._runtime.get(cid, ConditionRuntime()))
                for cid, cond in self._conditions.items()
                if cond.enabled
            ]

    def update_runtime(self, cid: str, rt: ConditionRuntime):
        with self._lock:
            if cid in self._runtime:
                self._runtime[cid] = rt


frame_buffer     = FrameBuffer(capture_fps=15)
snapshot_pool    = SnapshotPool()
condition_manager = ConditionManager()


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
            logger.info(f"[KBM] EVT received: {evt}")
            kbm_ws.broadcast_sync({"type": "event", "evt": evt, "raw": line})
            try:
                if kbm_ws._loop and kbm_ws._loop.is_running():
                    asyncio.run_coroutine_threadsafe(_emit_event_and_release(evt), kbm_ws._loop)
            except Exception:
                pass
        else:
            logger.info(f"[KBM] ← {line}")
    def on_send(cmd: str):
        logger.info(f"[KBM] → {cmd}")
        kbm_ws.broadcast_sync({"type": "serial", "line": f"→ {cmd}"})
    def on_disconnect():
        logger.info("[KBM] disconnected")
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


async def _auto_attach_servos(req: "MixedScriptRequest"):
    """Auto-attach servos needed by req if SRV is connected and not yet attached."""
    if not srv_serial.is_connected:
        return
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
            logger.warning(f"Auto ATTACH 失敗 sid={sid}")


async def _release_pending_for_event(evt: str, source: str = "event"):
    released = await pending_evt_pool.drain(evt)
    if not released:
        return 0
    released_ids = []
    released_cnt = 0
    for item in released:
        await _auto_attach_servos(item["req"])
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


def _probe_kbm_port(port: str) -> bool:
    """
    Probe for Nano_KB bridge by detecting its startup banner.
    The Nano_KB prints "[Nano_KB] Bridge ready. 38400 baud." on reset,
    which is triggered when the serial port is opened (DTR). This avoids
    requiring a PING round-trip through the SoftwareSerial bridge to Pro Micro.
    Falls back to PING round-trip if banner is not seen (e.g., Nano already
    running and banner was already emitted before this probe).
    """
    ser = None
    try:
        ser = serial.Serial(port, baudrate=38400, timeout=0.5)
        # Read for up to 2.5 s; Arduino resets on DTR and emits its banner within ~1 s.
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if "[Nano_KB]" in line or "Bridge ready" in line:
                return True
        # Banner not seen; try PING round-trip through bridge as fallback.
        ser.reset_input_buffer()
        ser.write(b"PING\n")
        ser.flush()
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip().upper()
            if line.startswith("OK") or line.startswith("ERR"):
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
    all_ports = list(serial.tools.list_ports.comports())
    ports = [p.device for p in all_ports]
    logger.info(
        "Auto connect: found %d port(s): %s",
        len(all_ports),
        ", ".join(f"{p.device}({p.description})" for p in all_ports) if all_ports else "none",
    )
    if not ports:
        logger.info("Auto connect: no serial ports found")
        return

    loop = asyncio.get_running_loop()
    srv_port = None
    kbm_port = None

    # Probe SRV first (115200, supports PING -> OK PONG / READY family).
    for port in ports:
        logger.info(f"Auto probe SRV: {port}")
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
            logger.info(f"Auto probe SRV matched: {port}")
            srv_port = port
            break
        else:
            logger.info(f"Auto probe SRV no match: {port}")

    # Probe KBM on remaining ports (38400, detect Nano_KB startup banner).
    for port in ports:
        if port == srv_port:
            continue
        logger.info(f"Auto probe KBM: {port}")
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(
                    executor,
                    lambda p=port: _probe_kbm_port(p),
                ),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Auto probe KBM timeout: {port}")
            ok = False
        if ok:
            logger.info(f"Auto probe KBM matched: {port}")
            kbm_port = port
            break
        else:
            logger.info(f"Auto probe KBM no match: {port}")

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


# ── Visual Monitor ────────────────────────────────────────────
async def _eval_condition(cond: MatchCondition, rt: ConditionRuntime, frame: Any, fw: int, fh: int):
    """Run template match for one condition against current frame; fire EVT on edge trigger."""
    snap = snapshot_pool.get(cond.snapshot_id)
    if snap is None:
        return
    try:
        rx, ry, rw, rh = cond.roi
        x1 = max(0, int(rx * fw));       y1 = max(0, int(ry * fh))
        x2 = min(fw, int((rx + rw) * fw)); y2 = min(fh, int((ry + rh) * fh))
        if x2 <= x1 or y2 <= y1:
            return
        curr_roi = frame[y1:y2, x1:x2]

        tmpl_bytes = base64.b64decode(snap["jpeg_b64"])
        tmpl_full = cv2.imdecode(np.frombuffer(tmpl_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if tmpl_full is None:
            return
        sh, sw = tmpl_full.shape[:2]
        tx1 = max(0, int(rx * sw));        ty1 = max(0, int(ry * sh))
        tx2 = min(sw, int((rx + rw) * sw)); ty2 = min(sh, int((ry + rh) * sh))
        if tx2 <= tx1 or ty2 <= ty1:
            return
        snap_roi = tmpl_full[ty1:ty2, tx1:tx2]

        # Resize snapshot ROI to match current frame ROI dimensions
        if snap_roi.shape[0] != curr_roi.shape[0] or snap_roi.shape[1] != curr_roi.shape[1]:
            snap_roi = cv2.resize(snap_roi, (curr_roi.shape[1], curr_roi.shape[0]))

        result = cv2.matchTemplate(curr_roi, snap_roi, cv2.TM_CCOEFF_NORMED)
        score = float(result[0][0])
        rt.last_score = score
    except Exception as exc:
        logger.debug(f"Match error [{cond.condition_id}]: {exc}")
        condition_manager.update_runtime(cond.condition_id, rt)
        return

    now = time.time()
    matched = score >= cond.threshold

    # Still in cooldown: suppress counting
    if now < rt.cooldown_until:
        rt.consecutive_hits = 0
        rt.prev_matched = False
        condition_manager.update_runtime(cond.condition_id, rt)
        return

    rt.consecutive_hits = (rt.consecutive_hits + 1) if matched else 0
    if not matched:
        rt.prev_matched = False

    # Edge trigger: fire only on the frame where min_hits first reached
    if matched and rt.consecutive_hits >= cond.min_hits and not rt.prev_matched:
        rt.prev_matched = True
        rt.last_fired_at = now
        rt.cooldown_until = now + cond.cooldown_ms / 1000.0
        condition_manager.update_runtime(cond.condition_id, rt)
        evt = f"IMG.MATCH.{cond.condition_id}"
        logger.info(f"[IMG] MATCH fired: evt={evt}  score={score:.3f}  condition='{cond.name}'")
        await _emit_event_and_release(evt)
        msg = {"type": "visual_fire", "condition_id": cond.condition_id, "evt": evt, "score": round(score, 3)}
        await kbm_ws.broadcast(msg)
        await srv_ws.broadcast(msg)
    else:
        condition_manager.update_runtime(cond.condition_id, rt)


async def _visual_monitor_task():
    """Background task: evaluate all armed conditions at ~3 fps."""
    logger.info("Visual monitor started.")
    while True:
        try:
            armed = condition_manager.get_armed()
            if armed:
                frame = frame_buffer.read()
                if frame is not None:
                    fh, fw = frame.shape[:2]
                    for cond, rt in armed:
                        await _eval_condition(cond, rt, frame, fw, fh)
        except Exception as exc:
            logger.exception(f"Visual monitor error: {exc}")
        await asyncio.sleep(1.0 / 3)  # ~3 fps polling


def _is_service_ready() -> bool:
    cam_ready = bool(camera and camera.isOpened())
    return cam_ready and srv_serial.is_connected and kb_serial.is_connected


async def _service_ready_monitor_task():
    """Log a one-shot readiness line when camera + SRV + KBM are all ready."""
    ready_logged = False
    while True:
        try:
            ready = _is_service_ready()
            if ready and not ready_logged:
                logger.info("service ready for script input")
                ready_logged = True
            elif not ready and ready_logged:
                ready_logged = False
        except Exception as exc:
            logger.exception(f"Service-ready monitor error: {exc}")
        await asyncio.sleep(0.5)


# ── Lifespan ─────────────────────────────────────────────────
_bg_tasks: list[asyncio.Task] = []

@asynccontextmanager
async def lifespan(app: FastAPI):
    global camera
    loop = asyncio.get_running_loop()
    srv_ws.set_loop(loop)
    kbm_ws.set_loop(loop)
    event_bus.set_loop(loop)
    # Auto-open webcam at startup so visual condition matching works without
    # requiring the web page to be opened first.
    try:
        cam = await loop.run_in_executor(executor, _open_camera, CAMERA_INDEX)
        if cam.isOpened():
            camera = cam
            frame_buffer.start(camera)
            logger.info(f"Camera auto-started: index={CAMERA_INDEX}, Camera ready")
        else:
            cam.release()
            logger.warning(f"Camera auto-start failed: index={CAMERA_INDEX} not opened")
    except Exception as _cam_exc:
        logger.warning(f"Camera auto-start error: {_cam_exc}")
    # Run serial auto-connect in background so startup is never blocked by COM probing.
    _bg_tasks.append(asyncio.create_task(_auto_connect_serial_devices()))
    _bg_tasks.append(asyncio.create_task(_visual_monitor_task()))
    _bg_tasks.append(asyncio.create_task(_service_ready_monitor_task()))
    yield
    # Cancel all background tasks and wait for them to finish.
    for t in _bg_tasks:
        if not t.done():
            t.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
    _bg_tasks.clear()
    frame_buffer.stop()
    kb_serial.disconnect()
    srv_serial.disconnect()
    if camera:
        camera.release()
        camera = None
    executor.shutdown(wait=True, cancel_futures=True)
    logger.info("Shutdown complete.")

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="RKS — Remote Keyboard System", lifespan=lifespan)
app.include_router(srv_router, prefix="/srv")
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
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
        logger.info(f"[EVT] step={idx} satisfied: {matched}")
        await kbm_ws.broadcast({
            "type": "script_event",
            "step": idx,
            "total": total,
            "evt": matched,
        })
        return True
    except TimeoutError as exc:
        logger.warning(f"[EVT] step={idx} TIMEOUT waiting for: {step.evt}")
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
                    logger.info(f"[STEP {i+1}/{total}] EVT wait: {step.evt}")
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
                    logger.info(
                        f"[STEP {i+1}/{total}] SRV sid={params['servo_id']} angle={params['angle']} "
                        f"speed={params['speed']} duration={params['duration_ms']}ms"
                    )
                    ok = await loop.run_in_executor(
                        executor, lambda p=params: srv_serial.send_command(p)
                    )
                    # 等 Servo 完成
                    hold = (step.duration_ms or 300)
                    angle = (step.angle or 90)
                    wait_s = hold / 1000 + angle * 0.01 + 0.25
                    await asyncio.sleep(wait_s)
                    if not ok:
                        logger.warning(f"[STEP {i+1}/{total}] SRV failed")
                        await kbm_ws.broadcast({
                            "type": "script_error",
                            "step": i,
                            "message": f"SRV step {i} failed",
                        })

                elif step.type == "kbd":
                    cmd = _resolve_kbd_cmd(step)
                    logger.info(f"[STEP {i+1}/{total}] KBD {cmd}")
                    result = await loop.run_in_executor(
                        executor, kb_serial.send, cmd
                    )
                    if result != "OK":
                        logger.warning(f"[STEP {i+1}/{total}] KBD ACK={result} cmd={cmd}")
                        await kbm_ws.broadcast({
                            "type": "script_error",
                            "step": i,
                            "message": f"KBD step {i} ACK: {result} cmd: {cmd}",
                        })

                elif step.type == "mse":
                    cmd = _resolve_mse_cmd(step)
                    logger.info(f"[STEP {i+1}/{total}] MSE {cmd}")
                    result = await loop.run_in_executor(
                        executor, kb_serial.send, cmd
                    )
                    if result != "OK":
                        logger.warning(f"[STEP {i+1}/{total}] MSE ACK={result} cmd={cmd}")
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
async def script_run(request: Request):
    """
    執行混合腳本（SRV + KBD + MSE 步驟）。

    curl 範例：
      curl -X POST http://127.0.0.1:8000/script/run \\
           -H "Content-Type: application/json" \\
           --data-binary "@exported_script.json"
    """
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON body 必須是 object")

    img_import_summary = {
        "snapshots": 0,
        "conditions": 0,
        "condition_map": {},
    }

    if isinstance(payload.get("img_bundle"), dict):
        bundle_req = ScriptBundleImportRequest.model_validate(payload)
        bundle_result = await script_import_bundle(bundle_req)
        img_import_summary = bundle_result["img_import"]
        payload = {
            "loop": bundle_result["loop"],
            "servos": bundle_result["servos"],
            "attach_cmds": bundle_result["attach_cmds"],
            "steps": bundle_result["steps"],
        }

    req = MixedScriptRequest.model_validate(payload)
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

    # Auto-attach servos before any pending/queue logic so it runs regardless
    # of whether the script goes pending or is queued immediately.
    await _auto_attach_servos(req)

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
                "img_import": img_import_summary,
            }
        req = PendingScriptPool._consume_gate_step(req, gate_evt)

    queued = await _activate_script(req)
    await _notify_pool_snapshot()

    return {
        "ok":    True,
        "steps": len(req.steps),
        "loop":  req.loop,
        "pending": False,
        "queued": queued,
        "img_import": img_import_summary,
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


@app.post("/script/import_bundle")
async def script_import_bundle(req: "ScriptBundleImportRequest"):
    global camera
    logger.info(
        "[IMPORT] Bundle import started: %d snapshot(s), %d condition(s), %d step(s)",
        len(req.img_bundle.snapshots) if req.img_bundle else 0,
        len(req.img_bundle.conditions) if req.img_bundle else 0,
        len(req.steps),
    )
    # Ensure camera is running whenever an IMG bundle is loaded (e.g. via curl with no UI).
    if req.img_bundle and req.img_bundle.conditions and (not camera or not camera.isOpened()):
        try:
            loop_ = asyncio.get_event_loop()
            cam = await loop_.run_in_executor(executor, _open_camera, CAMERA_INDEX)
            if cam.isOpened():
                camera = cam
                frame_buffer.start(camera)
                logger.info(f"Camera auto-started for IMG bundle import: index={CAMERA_INDEX}")
            else:
                cam.release()
                logger.warning("Camera auto-start for IMG bundle import failed: not opened")
        except Exception as _exc:
            logger.warning(f"Camera auto-start for IMG bundle import error: {_exc}")

    snapshot_map: dict[str, str] = {}
    condition_map: dict[str, str] = {}

    for snap in req.img_bundle.snapshots:
        try:
            meta = snapshot_pool.add_b64(snap.jpeg_b64)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid snapshot {snap.snapshot_id}: {exc}") from exc
        snapshot_map[snap.snapshot_id] = meta["id"]

    for cond in req.img_bundle.conditions:
        if len(cond.roi) != 4:
            raise HTTPException(422, f"Invalid roi for condition {cond.condition_id}")
        new_snapshot_id = snapshot_map.get(cond.snapshot_id)
        if not new_snapshot_id:
            raise HTTPException(422, f"Missing snapshot payload for condition {cond.condition_id}")
        new_cid = condition_manager.add(
            name=cond.name,
            snapshot_id=new_snapshot_id,
            roi=cond.roi,
            threshold=cond.threshold,
            min_hits=cond.min_hits,
            cooldown_ms=cond.cooldown_ms,
        )
        condition_manager.arm(new_cid)
        condition_map[cond.condition_id] = new_cid

    remapped_steps: list[dict[str, Any]] = []
    for step in req.steps:
        data = step.model_dump()
        evt_name = (data.get("evt") or "").strip()
        if evt_name.startswith("IMG.MATCH."):
            old_cid = evt_name[len("IMG.MATCH."):]
            new_cid = condition_map.get(old_cid)
            if new_cid:
                data["evt"] = f"IMG.MATCH.{new_cid}"
        remapped_steps.append(data)

    img_import = {
        "snapshots": len(snapshot_map),
        "conditions": len(condition_map),
        "condition_map": condition_map,
    }
    logger.info(
        "[IMPORT] Bundle import done: snapshots=%d conditions=%d map=%s",
        img_import["snapshots"], img_import["conditions"], condition_map,
    )

    if snapshot_map or condition_map:
        await srv_ws.broadcast({
            "type": "visual_sync",
            "reason": "bundle_import",
            **img_import,
        })

    return {
        "ok": True,
        "loop": req.loop,
        "servos": req.servos,
        "attach_cmds": req.attach_cmds,
        "steps": remapped_steps,
        "img_import": img_import,
    }

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

async def _generate_frames():
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
    while frame_buffer._running:
        frame = frame_buffer.read()
        if frame is None:
            await asyncio.sleep(0.05)
            continue
        ok2, buf = cv2.imencode(".jpg", frame, encode_params)
        if not ok2:
            await asyncio.sleep(0.01)
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + buf.tobytes()
            + b"\r\n"
        )
        await asyncio.sleep(1.0 / 30)  # cap at ~30 fps

@app.get("/stream")
async def video_stream():
    if frame_buffer.read() is None and (not camera or not camera.isOpened()):
        raise HTTPException(503, "Camera not available")
    return StreamingResponse(
        _generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ── Camera Toggle API ─────────────────────────────────────────

class CameraStartRequest(BaseModel):
    index: Optional[int] = None


def _open_camera(idx: int) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(idx)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    return cap


@app.get("/camera/devices")
async def camera_devices():
    """Probe indices 0-3 and return those that open successfully."""
    def _probe():
        found = []
        # Silence OpenCV's C-level logger during probing so OBSensor/depth-camera
        # backends don't spam "Camera index out of range" errors to the console.
        try:
            saved_level = cv2.getLogLevel()
            cv2.setLogLevel(0)
        except Exception:
            saved_level = None
        try:
            for i in range(4):
                cap = cv2.VideoCapture(i)
                if cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()
                    found.append({"index": i, "label": f"Camera {i}  ({w}\u00d7{h})"})
                else:
                    cap.release()
        finally:
            if saved_level is not None:
                try:
                    cv2.setLogLevel(saved_level)
                except Exception:
                    pass
        return found
    loop = asyncio.get_running_loop()
    devices = await loop.run_in_executor(executor, _probe)
    return {"devices": devices}


@app.post("/camera/start")
async def camera_start(req: Optional[CameraStartRequest] = None):
    global camera
    idx = (req.index if req and req.index is not None else None)
    if idx is None:
        idx = CAMERA_INDEX
    if camera and camera.isOpened():
        return {"ok": True, "already": True}
    loop = asyncio.get_running_loop()
    camera = await loop.run_in_executor(executor, _open_camera, idx)
    aw = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
    logger.info(f"Camera opened index={idx}: {aw}x{ah}, Camera ready")
    frame_buffer.start(camera)
    return {"ok": True, "width": aw, "height": ah, "index": idx}

@app.post("/camera/stop")
async def camera_stop():
    global camera
    frame_buffer.stop()
    if camera:
        camera.release()
        camera = None
    logger.info("Camera closed.")
    return {"ok": True}

@app.get("/camera/status")
async def camera_status():
    opened = bool(camera and camera.isOpened())
    return {"opened": opened}


# ── Visual Trigger API ────────────────────────────────────────

class CreateConditionRequest(BaseModel):
    name: str
    snapshot_id: str
    roi: list          # [x, y, w, h] normalized 0–1
    threshold: float = Field(default=0.92, ge=0.5, le=1.0)
    min_hits: int = Field(default=3, ge=1, le=30)
    cooldown_ms: int = Field(default=3000, ge=100, le=60000)


class UpdateConditionRequest(BaseModel):
    name: Optional[str] = None
    snapshot_id: Optional[str] = None
    roi: Optional[list] = None
    threshold: Optional[float] = Field(default=None, ge=0.5, le=1.0)
    min_hits: Optional[int] = Field(default=None, ge=1, le=30)
    cooldown_ms: Optional[int] = Field(default=None, ge=100, le=60000)


class ScriptBundleSnapshot(BaseModel):
    snapshot_id: str
    jpeg_b64: str


class ScriptBundleCondition(BaseModel):
    condition_id: str
    name: str
    snapshot_id: str
    roi: list
    threshold: float = Field(default=0.92, ge=0.5, le=1.0)
    min_hits: int = Field(default=3, ge=1, le=30)
    cooldown_ms: int = Field(default=3000, ge=100, le=60000)


class ScriptImgBundle(BaseModel):
    conditions: list[ScriptBundleCondition] = Field(default_factory=list)
    snapshots: list[ScriptBundleSnapshot] = Field(default_factory=list)


class ScriptBundleImportRequest(BaseModel):
    loop: bool = False
    servos: dict[str, int] = Field(default_factory=dict)
    attach_cmds: list[str] = Field(default_factory=list)
    steps: list[MixedStep] = Field(default_factory=list, max_length=200)
    img_bundle: ScriptImgBundle


@app.post("/visual/snapshot")
async def visual_snapshot():
    """Capture current camera frame and save to snapshot pool."""
    frame = frame_buffer.read()
    if frame is None:
        raise HTTPException(503, "Camera not available or no frame yet")
    meta = snapshot_pool.add(frame)
    return {"ok": True, "snapshot": meta}


@app.get("/visual/snapshots")
async def visual_list_snapshots():
    return {"snapshots": snapshot_pool.list_all()}


@app.delete("/visual/snapshots/{snap_id}")
async def visual_delete_snapshot(snap_id: str):
    if not snapshot_pool.delete(snap_id):
        raise HTTPException(404, "Snapshot not found")
    return {"ok": True}


@app.post("/visual/conditions")
async def visual_create_condition(req: CreateConditionRequest):
    if not snapshot_pool.get(req.snapshot_id):
        raise HTTPException(404, "Snapshot not found")
    if len(req.roi) != 4:
        raise HTTPException(422, "roi must be [x, y, w, h]")
    cid = condition_manager.add(
        name=req.name, snapshot_id=req.snapshot_id, roi=req.roi,
        threshold=req.threshold, min_hits=req.min_hits, cooldown_ms=req.cooldown_ms,
    )
    return {"ok": True, "condition_id": cid, "evt": f"IMG.MATCH.{cid}"}


@app.get("/visual/conditions")
async def visual_list_conditions():
    return {"conditions": condition_manager.list_all()}


@app.put("/visual/conditions/{cid}")
async def visual_update_condition(cid: str, req: UpdateConditionRequest):
    if not condition_manager.get(cid):
        raise HTTPException(404, "Condition not found")
    if req.snapshot_id is not None and not snapshot_pool.get(req.snapshot_id):
        raise HTTPException(404, "Snapshot not found")
    if req.roi is not None and len(req.roi) != 4:
        raise HTTPException(422, "roi must be [x, y, w, h]")
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    condition_manager.update(cid, **updates)
    return {"ok": True}


@app.delete("/visual/conditions/{cid}")
async def visual_delete_condition(cid: str):
    if not condition_manager.delete(cid):
        raise HTTPException(404, "Condition not found")
    return {"ok": True}


@app.post("/visual/conditions/{cid}/arm")
async def visual_arm_condition(cid: str):
    if not condition_manager.get(cid):
        raise HTTPException(404, "Condition not found")
    condition_manager.arm(cid)
    return {"ok": True, "armed": True}


@app.post("/visual/conditions/{cid}/disarm")
async def visual_disarm_condition(cid: str):
    if not condition_manager.get(cid):
        raise HTTPException(404, "Condition not found")
    condition_manager.disarm(cid)
    return {"ok": True, "armed": False}


@app.post("/visual/conditions/{cid}/test")
async def visual_test_condition(cid: str):
    """Run a single match against the current frame and return the score."""
    cond = condition_manager.get(cid)
    if not cond:
        raise HTTPException(404, "Condition not found")
    frame = frame_buffer.read()
    if frame is None:
        raise HTTPException(503, "Camera not available")
    snap = snapshot_pool.get(cond.snapshot_id)
    if snap is None:
        raise HTTPException(404, "Snapshot for this condition no longer in pool")
    try:
        fh, fw = frame.shape[:2]
        rx, ry, rw, rh = cond.roi
        x1 = max(0, int(rx * fw));        y1 = max(0, int(ry * fh))
        x2 = min(fw, int((rx + rw) * fw)); y2 = min(fh, int((ry + rh) * fh))
        curr_roi = frame[y1:y2, x1:x2]
        tmpl_full = cv2.imdecode(
            np.frombuffer(base64.b64decode(snap["jpeg_b64"]), dtype=np.uint8),
            cv2.IMREAD_COLOR
        )
        sh, sw = tmpl_full.shape[:2]
        tx1 = max(0, int(rx * sw));        ty1 = max(0, int(ry * sh))
        tx2 = min(sw, int((rx + rw) * sw)); ty2 = min(sh, int((ry + rh) * sh))
        snap_roi = tmpl_full[ty1:ty2, tx1:tx2]
        if snap_roi.shape[0] != curr_roi.shape[0] or snap_roi.shape[1] != curr_roi.shape[1]:
            snap_roi = cv2.resize(snap_roi, (curr_roi.shape[1], curr_roi.shape[0]))
        result = cv2.matchTemplate(curr_roi, snap_roi, cv2.TM_CCOEFF_NORMED)
        score = float(result[0][0])
    except Exception as exc:
        raise HTTPException(500, f"Match failed: {exc}")
    return {"score": round(score, 4), "matched": score >= cond.threshold, "threshold": cond.threshold}


if __name__ == "__main__":
    bind_port = _find_available_port(HOST, PORT)
    if bind_port != PORT:
        logger.warning(f"Port {PORT} is already in use. Fallback to port {bind_port}.")
    _log_access_urls(HOST, bind_port)
    uvicorn.run(app, host=HOST, port=bind_port, reload=False, log_level="info")

