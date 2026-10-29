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
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from collections import deque
from typing import Optional, Any, Callable, Annotated, Iterator, Literal, Sequence, Union

import base64
import dataclasses
import re
import threading
import uuid

import numpy as np
import cv2
from cv2_enumerate_cameras import enumerate_cameras
import serial
import serial.tools.list_ports
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, model_validator

from keyboard_serial import KeyboardSerial
from ocr_digits import OcrConfig, OcrUnavailable, crop_roi, ensure_available, expand_variables, read_stable_digits
from image_move import AmbiguousMatch, ImageMoveConfig, ImageMatch, locate_image
from script_runtime import ScriptRuntime
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
current_camera_index: Optional[int] = None

# 混合腳本執行 task / queue 控制
_script_queue: asyncio.Queue["MixedScriptRequest"] = asyncio.Queue()
_script_runner_task: Optional[asyncio.Task] = None
_script_control_lock = asyncio.Lock()
_script_pause: Optional[dict] = None
_script_resume_event: Optional[asyncio.Event] = None
_script_pause_generation = 0
_script_wait: Optional[dict] = None
script_runtime = ScriptRuntime()


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
        self._generation = 0
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
                    self._generation += 1
            time.sleep(interval)

    def read(self) -> Optional[Any]:
        """Return a copy of the latest BGR frame, or None if camera not yet ready."""
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def read_sample(self) -> tuple[Optional[Any], int]:
        with self._lock:
            return (None if self._frame is None else self._frame.copy(), self._generation)


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

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items.clear()
            return count

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
    last_frame_generation: int = -1
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
        self._script_conditions: Optional[set[str]] = None
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
            if self._script_conditions is not None and cid not in self._script_conditions:
                return
            if cid in self._conditions:
                self._conditions[cid].enabled = True
                self._runtime[cid] = ConditionRuntime(last_frame_generation=frame_buffer.generation)

    def activate_only(self, condition_ids: Optional[list[str]], frame_generation: Optional[int] = None):
        with self._lock:
            self._script_conditions = None if condition_ids is None else set(condition_ids)
            for cid, condition in self._conditions.items():
                condition.enabled = self._script_conditions is not None and cid in self._script_conditions
                if condition.enabled:
                    generation = frame_buffer.generation if frame_generation is None else frame_generation
                    self._runtime[cid] = ConditionRuntime(last_frame_generation=generation)

    def is_current(self, condition: MatchCondition, runtime: ConditionRuntime) -> bool:
        with self._lock:
            cid = condition.condition_id
            return (
                self._conditions.get(cid) is condition
                and condition.enabled
                and self._runtime.get(cid) is runtime
                and (self._script_conditions is None or cid in self._script_conditions)
            )

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

    def clear(self) -> int:
        with self._lock:
            path = self._persist_path()
            temp_path = path + ".tmp"
            try:
                with open(temp_path, "w", encoding="utf-8") as output:
                    json.dump([], output)
                os.replace(temp_path, path)
            except OSError:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise
            count = len(self._conditions)
            self._conditions.clear()
            self._runtime.clear()
            self._script_conditions = None
            return count

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
CONTROL_FRAME_TIMEOUT_SECONDS = 5.0


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

    async def emit(self, event_line: str, guard: Optional[Callable[[], bool]] = None) -> bool:
        async with self._lock:
            if guard is not None and not guard():
                return False
            self._events.append(event_line)
            return True

    async def clear(self):
        async with self._lock:
            self._events.clear()

    async def clear_visual(self):
        async with self._lock:
            self._events = deque(event for event in self._events
                                 if not self._normalize(event).startswith("IMG.MATCH."))

    async def discard(self, expected: str):
        async with self._lock:
            self._events = deque(event for event in self._events if not self._match(expected, event))

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

    async def wait_for(self, expected: str, timeout_ms: Optional[int] = None, *, wait_gate=None, allow_consume=None) -> str:
        deadline = None if timeout_ms is None else asyncio.get_running_loop().time() + timeout_ms / 1000
        while True:
            if wait_gate is not None:
                await wait_gate()
            async with self._lock:
                for idx, actual in enumerate(self._events):
                    if (allow_consume is None or allow_consume()) and self._match(expected, actual):
                        match = self._events[idx]
                        del self._events[idx]
                        return match
            remain = None if deadline is None else deadline - asyncio.get_running_loop().time()
            if remain is not None and remain <= 0:
                raise TimeoutError(f"EVT timeout waiting for {expected}")
            await asyncio.sleep(0.05 if remain is None else min(0.05, remain))


event_bus = EventBus()


class PendingScriptPool:
    def __init__(self):
        self._buckets: dict[str, deque[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._seq = 0

    @staticmethod
    def _gate_evt(req: "MixedScriptRequest") -> Optional[str]:
        for step in req.steps:
            if isinstance(step, MixedStep) and step.type == "evt":
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
        new_steps: list[ScriptNode] = []
        consumed = False
        for step in req.steps:
            cur_evt = (step.evt or "").strip() if isinstance(step, MixedStep) else ""
            if not consumed and isinstance(step, MixedStep) and step.type == "evt" and cur_evt == gate_evt:
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

async def _notify_runtime():
    await kbm_ws.broadcast({"type": "script_runtime", "runtime": script_runtime.snapshot()})


def _register_runtime_job(req: "MixedScriptRequest"):
    req.job_id = script_runtime.enqueue(name=req.name,
        steps=[step.model_dump(exclude_none=True) for step in req.steps],
        loop=req.loop, condition_ids=req.condition_ids)


async def _activate_script(req: "MixedScriptRequest") -> int:
    _register_runtime_job(req)
    queued_request = req.model_copy(deep=True)
    await _ensure_script_runner()
    await _script_queue.put(queued_request)
    await _notify_runtime()
    return _script_queue.qsize()


async def _auto_attach_servos(req: "MixedScriptRequest"):
    """Auto-attach servos needed by req if SRV is connected and not yet attached."""
    if not srv_serial.is_connected:
        return
    loop_ = asyncio.get_event_loop()
    used_sids = {step.servo_id for step in _iter_action_steps(req.steps)
                 if step.type == "srv" and step.servo_id}
    pin_map   = {int(k): v for k, v in (req.servos or {}).items()}
    for sid in sorted(used_sids):
        await _await_script_resume()
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
    async with _script_control_lock:
        return await _release_pending_scripts(evt, source)


async def _release_pending_scripts(evt: str, source: str):
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


async def _emit_event_and_release(
    evt: str, condition: Optional[MatchCondition] = None, runtime: Optional[ConditionRuntime] = None
) -> bool:
    guard = None
    if condition is not None:
        guard = lambda: runtime is not None and condition_manager.is_current(condition, runtime)
    if not await event_bus.emit(evt, guard=guard):
        return False
    if await pending_evt_pool.peek(evt) is None:
        return True
    async with _script_control_lock:
        if guard is not None and not guard():
            return True
        await _release_pending_scripts(evt, source="event")
    return True


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
def _match_condition_frame(condition: MatchCondition, frame: Any) -> tuple[float, bool]:
    snapshot = snapshot_pool.get(condition.snapshot_id)
    if snapshot is None:
        raise RuntimeError(
            f"Snapshot for condition {condition.condition_id} is unavailable")
    if frame is None or not hasattr(frame, "shape") or len(frame.shape) < 2:
        raise ValueError("Camera returned an invalid frame")

    fh, fw = frame.shape[:2]
    rx, ry, rw, rh = condition.roi
    x1, y1 = max(0, int(rx * fw)), max(0, int(ry * fh))
    x2, y2 = min(fw, int((rx + rw) * fw)), min(fh, int((ry + rh) * fh))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"Invalid ROI for condition {condition.condition_id}")
    curr_roi = frame[y1:y2, x1:x2]

    try:
        template = cv2.imdecode(
            np.frombuffer(base64.b64decode(snapshot["jpeg_b64"]), dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
    except Exception as exc:
        raise ValueError(
            f"Snapshot for condition {condition.condition_id} is invalid") from exc
    if template is None:
        raise ValueError(f"Snapshot for condition {condition.condition_id} is invalid")

    sh, sw = template.shape[:2]
    tx1, ty1 = max(0, int(rx * sw)), max(0, int(ry * sh))
    tx2, ty2 = min(sw, int((rx + rw) * sw)), min(sh, int((ry + rh) * sh))
    if tx2 <= tx1 or ty2 <= ty1:
        raise ValueError(f"Invalid snapshot ROI for condition {condition.condition_id}")
    template_roi = template[ty1:ty2, tx1:tx2]
    if template_roi.shape[:2] != curr_roi.shape[:2]:
        template_roi = cv2.resize(template_roi, (curr_roi.shape[1], curr_roi.shape[0]))

    score = float(cv2.matchTemplate(
        curr_roi, template_roi, cv2.TM_CCOEFF_NORMED)[0][0])
    return score, score >= condition.threshold


async def _eval_condition(
    cond: MatchCondition, rt: ConditionRuntime, frame: Any, fw: int, fh: int,
    frame_generation: Optional[int] = None,
):
    """Run template match for one condition against current frame; fire EVT on edge trigger."""
    if not condition_manager.is_current(cond, rt):
        return
    if frame_generation is not None:
        if frame_generation <= rt.last_frame_generation:
            return
        rt.last_frame_generation = frame_generation
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
        if not await _emit_event_and_release(evt, cond, rt):
            return
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
                frame, generation = frame_buffer.read_sample()
                if frame is not None:
                    fh, fw = frame.shape[:2]
                    for cond, rt in armed:
                        await _eval_condition(cond, rt, frame, fw, fh, frame_generation=generation)
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
    global camera, current_camera_index
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
            current_camera_index = CAMERA_INDEX
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
    current_camera_index = None
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
    type:        Literal["srv", "kbd", "mse", "evt", "ocr"]
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
    roi:         Optional[list[float]] = None
    variable:    str = Field(default="digits", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
    timeout_ms:  int = Field(default=5000, ge=100, le=60000)
    stable_hits: int = Field(default=2, ge=1, le=10)
    image_move: Optional[ImageMoveConfig] = None

    @model_validator(mode="after")
    def validate_ocr(self):
        if self.type == "ocr":
            OcrConfig.model_validate(self.model_dump())
        if self.type == "mse":
            action = (self.action or "MOVE").upper()
            if action not in {"MOVE", "MOVE_TO", "CLICK", "DBLCLICK", "SCROLL", "DOWN", "UP", "IMAGE_MOVE"}:
                raise ValueError("Unsupported mouse action; use IMAGE_MOVE followed by CLICK or DBLCLICK")
            if action == "IMAGE_MOVE" and (self.image_move is None or self.cmd):
                raise ValueError("IMAGE_MOVE requires image_move settings and cannot use a raw cmd")
        return self


class IfElseBlock(BaseModel):
    type: Literal["if_else"]
    condition_id: str = Field(..., min_length=1)
    then_steps: list[MixedStep]
    else_steps: list[MixedStep]


class DoWhileBlock(BaseModel):
    type: Literal["do_while"]
    condition_id: str = Field(..., min_length=1)
    repeat_when: Literal["match", "not_match"]
    max_iterations: int = Field(default=10, ge=1, le=10_000, strict=True)
    steps: list[MixedStep]


ScriptNode = Annotated[
    Union[IfElseBlock, DoWhileBlock, MixedStep],
    Field(discriminator="type"),
]


def _iter_action_steps(steps: Sequence[ScriptNode]) -> Iterator[MixedStep]:
    for node in steps:
        if isinstance(node, MixedStep):
            yield node
        elif isinstance(node, IfElseBlock):
            yield from node.then_steps
            yield from node.else_steps
        else:
            yield from node.steps


def _validate_control_steps(steps: Sequence[ScriptNode]) -> None:
    if sum(1 for _ in _iter_action_steps(steps)) > 200:
        raise ValueError("A script may contain at most 200 action steps")
    for node in steps:
        if isinstance(node, IfElseBlock) and not (node.then_steps or node.else_steps):
            raise ValueError("An IF block must have at least one populated branch")
        if isinstance(node, DoWhileBlock) and not node.steps:
            raise ValueError("A do-while block must have a non-empty body")


def _collect_condition_ids(steps: Sequence[ScriptNode]) -> list[str]:
    condition_ids: list[str] = []
    for node in steps:
        if isinstance(node, IfElseBlock):
            condition_ids.append(node.condition_id)
            node_steps = [*node.then_steps, *node.else_steps]
        elif isinstance(node, DoWhileBlock):
            condition_ids.append(node.condition_id)
            node_steps = node.steps
        else:
            node_steps = [node]
        for step in node_steps:
            evt = EventBus._normalize((step.evt or "").strip())
            if evt.startswith("IMG.MATCH."):
                condition_ids.append(evt[len("IMG.MATCH."):])
    return list(dict.fromkeys(condition_ids))


class MixedScriptRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    job_id: Optional[str] = Field(default=None, exclude=True)
    steps:       list[ScriptNode] = Field(default_factory=list)
    loop:        bool            = False
    condition_ids: list[str] = Field(default_factory=list, exclude=True)
    # auto-attach: 執行前自動 attach 需要的 servo
    servos:      dict[str, int]  = Field(default_factory=dict)
    attach_cmds: list[str]       = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_control_steps(self):
        _validate_control_steps(self.steps)
        return self


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
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    loop: bool = False
    servos: dict[str, int] = Field(default_factory=dict)
    attach_cmds: list[str] = Field(default_factory=list)
    steps: list[ScriptNode] = Field(default_factory=list)
    img_bundle: ScriptImgBundle

    @model_validator(mode="after")
    def validate_control_steps(self):
        _validate_control_steps(self.steps)
        return self


# ═══════════════════════════════════════════════════════════════
#  Helper: 把步驟結構化欄位轉成協議字串
# ═══════════════════════════════════════════════════════════════
def _resolve_kbd_cmd(step: MixedStep, variables: Optional[dict[str, str]] = None) -> str:
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
        return "TYPE:" + expand_variables(step.text or "", variables or {})

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
    def normalize_action(step: MixedStep) -> list[MixedStep]:
        evt_name = EventBus._normalize(step.evt or "")
        if step.type != "evt" and evt_name:
            normalized = [MixedStep(type="evt", delay_ms=0, evt=evt_name)]
            step = step.model_copy(update={"evt": None})
        elif step.type == "evt":
            step = step.model_copy(update={"evt": evt_name})
            normalized = []
        else:
            normalized = []
        normalized.append(step)
        return normalized

    normalized_steps: list[ScriptNode] = []
    for node in req.steps:
        if isinstance(node, MixedStep):
            normalized_steps.extend(normalize_action(node))
        elif isinstance(node, IfElseBlock):
            normalized_steps.append(node.model_copy(update={
                "then_steps": [action for step in node.then_steps for action in normalize_action(step)],
                "else_steps": [action for step in node.else_steps for action in normalize_action(step)],
            }))
        else:
            normalized_steps.append(node.model_copy(update={
                "steps": [action for step in node.steps for action in normalize_action(step)],
            }))
    _validate_control_steps(normalized_steps)
    return req.model_copy(update={"steps": normalized_steps})


# ═══════════════════════════════════════════════════════════════
#  Mixed Script 後端執行引擎
# ═══════════════════════════════════════════════════════════════
async def _await_script_resume():
    while _script_pause is not None:
        resume = _script_resume_event
        if resume is None:
            await asyncio.sleep(0)
        else:
            await resume.wait()


async def _evaluate_condition_fresh(
    condition_id: str,
    timeout_seconds: float = CONTROL_FRAME_TIMEOUT_SECONDS,
) -> tuple[float, bool]:
    condition = condition_manager.get(condition_id)
    if condition is None or snapshot_pool.get(condition.snapshot_id) is None:
        raise RuntimeError(f"Condition {condition_id} is unavailable")

    remaining = timeout_seconds
    initial_generation = frame_buffer.generation
    loop = asyncio.get_running_loop()
    while remaining > 0:
        await _await_script_resume()
        started = loop.time()
        frame, generation = frame_buffer.read_sample()
        if frame is not None and generation > initial_generation:
            if condition_manager.get(condition_id) is not condition:
                raise RuntimeError(
                    f"Condition {condition_id} was removed during evaluation")
            return _match_condition_frame(condition, frame)
        await asyncio.sleep(min(0.01, remaining))
        remaining -= loop.time() - started

    raise TimeoutError(
        f"No fresh camera frame for condition {condition_id} within "
        f"{timeout_seconds:g} seconds")


async def _script_sleep(seconds: float):
    remaining = seconds
    loop = asyncio.get_running_loop()
    while remaining > 0:
        await _await_script_resume()
        started = loop.time()
        await asyncio.sleep(min(0.05, remaining))
        remaining -= loop.time() - started
    await _await_script_resume()


async def _wait_for_step_event(step: MixedStep, idx: int, total: int) -> bool:
    global _script_wait
    if not step.evt:
        return True
    _script_wait = {"evt": step.evt, "step": idx, "total": total}
    try:
        while True:
            await _await_script_resume()
            script_runtime.set_phase("waiting")
            await _notify_runtime()
            await kbm_ws.broadcast({"type": "script_wait", "step": idx, "total": total, "evt": step.evt})
            srv_ws.broadcast_sync({"type": "status", "state": "waiting", "step": idx, "total": total})
            consumed_generation = _script_pause_generation

            def allow_event():
                nonlocal consumed_generation
                consumed_generation = _script_pause_generation
                return _script_pause is None

            matched = await event_bus.wait_for(step.evt, wait_gate=_await_script_resume, allow_consume=allow_event)
            await _await_script_resume()
            logger.info(f"[EVT] step={idx} satisfied: {matched}")
            script_runtime.set_phase("running")
            await _notify_runtime()
            await kbm_ws.broadcast({"type": "script_event", "step": idx, "total": total, "evt": matched})
            await _await_script_resume()
            if consumed_generation != _script_pause_generation:
                continue
            _script_wait = None
            return True
    finally:
        _script_wait = None


async def _send_kbm_command(command: str):
    cancelled = threading.Event()
    loop = asyncio.get_running_loop()
    try:
        while True:
            await _await_script_resume()
            status = await loop.run_in_executor(executor,
                lambda: kb_serial.send(command, allow_send=lambda: not cancelled.is_set() and _script_pause is None))
            if status == "CANCELLED" and not cancelled.is_set():
                continue
            await _await_script_resume()
            return status
    finally:
        cancelled.set()


async def _send_mouse_command(command: str):
    status = await _send_kbm_command(command)
    if status != "OK":
        raise RuntimeError(f"MSE {command.split()[0]} failed: {status}; no further commands sent")


async def _run_image_move(config: ImageMoveConfig) -> ImageMatch:
    result = await locate_image(config, frame_buffer)
    await _send_mouse_command(f"MOUSE:ABS {result.hid[0]} {result.hid[1]}")
    return result


async def _run_action_step(step: MixedStep, *, top_index, variables, slot=None, child_index=None):
    runtime = script_runtime
    active = runtime.detail(runtime.active_id) if runtime.active_id else None
    total = len(active["steps"]) if active is not None else top_index + 1
    phase = "delay" if step.delay_ms > 0 and step.type != "evt" else "running"
    if slot is None:
        runtime.start_step(top_index, phase)
    else:
        runtime.start_child_step(top_index, slot, child_index, phase)
    await _notify_runtime()
    await kbm_ws.broadcast({
        "type": "script_progress",
        "step": top_index,
        "total": total,
        "step_type": step.type,
        "iteration": active["iteration"] if active is not None else 0,
    })
    srv_ws.broadcast_sync({
        "type": "status",
        "state": "running",
        "step": top_index,
        "total": total,
    })

    def note_action_error(message):
        if slot is None:
            runtime.note_error(message)
        else:
            runtime.complete_child_step(
                top_index, slot, child_index,
                error=f"{slot}[{child_index}]: {message}")

    step_result = None
    try:
        await _await_script_resume()
        if step.type == "evt":
            logger.info(f"[STEP {top_index+1}/{total}] EVT wait: {step.evt}")
            ok_evt = await _wait_for_step_event(step, top_index, total)
            await _await_script_resume()
            if not ok_evt:
                return

        if step.delay_ms > 0:
            await _script_sleep(step.delay_ms / 1000)

        if step.type == "evt":
            await _await_script_resume()
            if slot is None:
                runtime.complete_step(top_index)
            else:
                runtime.complete_child_step(top_index, slot, child_index)
            await _notify_runtime()
            return

        if step.type == "ocr":
            runtime.set_phase("recognizing", deadline=time.time() + step.timeout_ms / 1000)
            await _notify_runtime()
            await _await_script_resume()
            variables.pop(step.variable, None)
            config = OcrConfig.model_validate(step.model_dump())
            variables[step.variable] = await read_stable_digits(config, frame_buffer)
            await _await_script_resume()
            step_result = {"variable": step.variable, "value": variables[step.variable]}
            await kbm_ws.broadcast({"type": "ocr_result", "step": top_index,
                                    "variable": step.variable, "value": variables[step.variable]})

        elif step.type == "srv":
            runtime.set_phase("running")
            await _notify_runtime()
            await _await_script_resume()
            params = {
                "delay_ms": 0,
                "servo_id": step.servo_id or 1,
                "angle": step.angle if step.angle is not None else 90,
                "speed": step.speed or 60,
                "duration_ms": step.duration_ms if step.duration_ms is not None else 300,
                "home": step.home if step.home is not None else 1,
            }
            logger.info(
                f"[STEP {top_index+1}/{total}] SRV sid={params['servo_id']} angle={params['angle']} "
                f"speed={params['speed']} duration={params['duration_ms']}ms"
            )
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(
                executor, lambda p=params: srv_serial.send_command(p)
            )
            hold = step.duration_ms or 300
            angle = step.angle or 90
            wait_s = hold / 1000 + angle * 0.01 + 0.25
            await _script_sleep(wait_s)
            if not ok:
                message = f"SRV step {top_index} failed"
                note_action_error(message)
                logger.warning(f"[STEP {top_index+1}/{total}] SRV failed")
                await kbm_ws.broadcast({
                    "type": "script_error",
                    "step": top_index,
                    "message": message,
                })

        elif step.type == "kbd":
            runtime.set_phase("running")
            await _notify_runtime()
            cmd = _resolve_kbd_cmd(step, variables)
            logger.info(f"[STEP {top_index+1}/{total}] KBD {cmd}")
            result = await _send_kbm_command(cmd)
            if result != "OK":
                message = f"KBD ACK: {result}"
                note_action_error(message)
                logger.warning(f"[STEP {top_index+1}/{total}] KBD ACK={result} cmd={cmd}")
                await kbm_ws.broadcast({
                    "type": "script_error",
                    "step": top_index,
                    "message": f"KBD step {top_index} ACK: {result} cmd: {cmd}",
                })

        elif step.type == "mse":
            if (step.action or "").upper() == "IMAGE_MOVE":
                runtime.set_phase(
                    "locating", deadline=time.time() + step.image_move.timeout_ms / 1000)
                await _notify_runtime()
                await _await_script_resume()
                result = await _run_image_move(step.image_move)
                step_result = result.model_dump(exclude={"preview_b64"})
                if slot is None:
                    runtime.complete_step(top_index, step_result)
                else:
                    runtime.complete_child_step(
                        top_index, slot, child_index, result=step_result)
                await _notify_runtime()
                await kbm_ws.broadcast({"type": "image_move_result", "step": top_index,
                                        "match": step_result})
                return
            else:
                cmd = _resolve_mse_cmd(step)
                runtime.set_phase("running")
                await _notify_runtime()
                logger.info(f"[STEP {top_index+1}/{total}] MSE {cmd}")
                await _send_mouse_command(cmd)

        await _await_script_resume()
        if slot is None:
            runtime.complete_step(top_index, step_result)
        else:
            runtime.complete_child_step(top_index, slot, child_index, result=step_result)
        await _notify_runtime()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if slot is not None:
            message = f"{slot}[{child_index}] failed: {exc}"
            runtime.complete_child_step(top_index, slot, child_index, error=message)
            await _notify_runtime()
            raise RuntimeError(message) from exc
        raise


async def _run_mixed_script(req: MixedScriptRequest):
    """後端逐步執行混合腳本，透過 WS 推播進度"""
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
            await _await_script_resume()
            script_runtime.begin_iteration(iteration)
            variables: dict[str, str] = {}
            for i, step in enumerate(req.steps):
                await _await_script_resume()
                if isinstance(step, MixedStep):
                    await _run_action_step(step, top_index=i, variables=variables)
                    continue

                script_runtime.start_step(i, "running")
                await _notify_runtime()
                await kbm_ws.broadcast({
                    "type":      "script_progress",
                    "step":      i,
                    "total":     total,
                    "step_type": step.type,
                    "iteration": iteration,
                })
                srv_ws.broadcast_sync({
                    "type":  "status",
                    "state": "running",
                    "step":  i,
                    "total": total,
                })
                await _await_script_resume()

                if isinstance(step, IfElseBlock):
                    script_runtime.set_phase("evaluating")
                    await _notify_runtime()
                    await _await_script_resume()
                    _, matched = await _evaluate_condition_fresh(step.condition_id)
                    branch = "then_steps" if matched else "else_steps"
                    script_runtime.set_control_state(
                        i, branch=branch[:-6], condition_match=matched)
                    await _notify_runtime()
                    await _await_script_resume()
                    for child_index, child in enumerate(getattr(step, branch)):
                        await _await_script_resume()
                        await _run_action_step(
                            child, top_index=i, variables=variables,
                            slot=branch, child_index=child_index)
                    script_runtime.complete_step(i)
                    await _notify_runtime()
                    continue

                if isinstance(step, DoWhileBlock):
                    for current in range(1, step.max_iterations + 1):
                        await _await_script_resume()
                        script_runtime.set_control_state(
                            i, iteration=current, iteration_limit=step.max_iterations)
                        await _notify_runtime()
                        for child_index, child in enumerate(step.steps):
                            await _await_script_resume()
                            await _run_action_step(
                                child, top_index=i, variables=variables,
                                slot="steps", child_index=child_index)
                        await _await_script_resume()
                        script_runtime.set_phase("evaluating")
                        await _notify_runtime()
                        await _await_script_resume()
                        _, matched = await _evaluate_condition_fresh(step.condition_id)
                        script_runtime.set_control_state(i, condition_match=matched)
                        await _notify_runtime()
                        await _await_script_resume()
                        should_repeat = (
                            matched if step.repeat_when == "match" else not matched)
                        if not should_repeat:
                            break
                        if current == step.max_iterations:
                            raise RuntimeError(
                                f"Do-while iteration limit reached ({step.max_iterations})")
                    script_runtime.complete_step(i)
                    await _notify_runtime()
                    continue

            iteration += 1
            if not req.loop:
                break

        await _await_script_resume()
        script_runtime.set_phase("finishing")
        await kbm_ws.broadcast({"type": "script_done", "iterations": iteration})
        srv_ws.broadcast_sync({"type": "done", "state": "idle"})
        logger.info(f"Script done: {total} steps × {iteration} iterations")

    except asyncio.CancelledError:
        await kbm_ws.broadcast({"type": "script_stopped"})
        srv_ws.broadcast_sync({"type": "status", "state": "idle"})
        logger.info("Script cancelled")
        raise


async def _script_runner():
    global _script_runner_task
    try:
        while True:
            await _await_script_resume()
            req = await _script_queue.get()
            failed = False
            try:
                if not script_runtime.contains(req.job_id):
                    _register_runtime_job(req)
                await event_bus.clear_visual()
                await _await_script_resume()
                script_runtime.activate(req.job_id)
                condition_manager.activate_only(req.condition_ids)
                await _notify_runtime()
                await _await_script_resume()
                await srv_ws.broadcast({"type": "visual_sync", "reason": "script_activation",
                                        "snapshots": len(snapshot_pool.list_all()),
                                        "conditions": len(condition_manager.list_all())})
                await _await_script_resume()
                await _auto_attach_servos(req)
                await _await_script_resume()
                await _run_mixed_script(req)
            except Exception as exc:
                failed = True
                await _await_script_resume()
                script_runtime.note_error(str(exc), job_id=req.job_id)
                script_runtime.set_phase("finishing")
                await _notify_runtime()
                logger.exception("Queued script failed")
                await kbm_ws.broadcast({"type": "script_error", "message": str(exc), "fatal": True})
                srv_ws.broadcast_sync({"type": "status", "state": "idle"})
            finally:
                script_runtime.finish(req.job_id, failed=failed)
                await _notify_runtime()
                condition_manager.activate_only(None)
                await event_bus.clear_visual()
                _script_queue.task_done()
                await srv_ws.broadcast({"type": "visual_sync", "reason": "script_finished",
                                        "snapshots": len(snapshot_pool.list_all()),
                                        "conditions": len(condition_manager.list_all())})
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
            req = _script_queue.get_nowait()
            script_runtime.finish(req.job_id)
            _script_queue.task_done()
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
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON body 必須是 object")

    async with _script_control_lock:
        if "command" in payload:
            return await _clear_workspace(payload)
        try:
            result = await _submit_script(payload)
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json(include_url=False))) from exc
        if request.headers.get("x-rks-source") != "script-editor":
            await kbm_ws.broadcast({"type": "script_submitted", "job_id": result["job_id"]})
        return result


async def _clear_workspace(payload: dict):
    targets = payload.get("targets")
    if (
        payload.get("command") != "clear"
        or set(payload) != {"command", "targets"}
        or not isinstance(targets, list)
        or len(targets) != 3
        or not all(isinstance(target, str) for target in targets)
        or set(targets) != {"snapshots", "conditions", "scripts"}
    ):
        raise HTTPException(400, 'Use {"command":"clear","targets":["snapshots","conditions","scripts"]} without script fields')

    pending = await pending_evt_pool.snapshot()
    cleared = {
        "running_scripts": int(bool(_script_runner_task and not _script_runner_task.done())),
        "queued_scripts": _script_queue.qsize(),
        "pending_scripts": pending["total"],
    }
    await _stop_scripts(stop_servos=False)
    try:
        cleared["conditions"] = condition_manager.clear()
    except OSError as exc:
        logger.exception("Failed to persist cleared conditions")
        raise HTTPException(500, "Scripts stopped, but conditions could not be cleared; snapshots retained. Retry after fixing storage.") from exc
    cleared["snapshots"] = snapshot_pool.clear()
    await srv_ws.broadcast({"type": "workspace_cleared", "targets": targets, "cleared": cleared})
    return {"ok": True, "command": "clear", "cleared": cleared}


async def _submit_script(payload: dict):
    img_import_summary = {
        "snapshots": 0,
        "conditions": 0,
        "condition_map": {},
    }

    if isinstance(payload.get("img_bundle"), dict):
        bundle_req = ScriptBundleImportRequest.model_validate(payload)
        bundle_result = await _import_script_bundle(bundle_req)
        img_import_summary = bundle_result["img_import"]
        payload = {
            "name": bundle_result.get("name"),
            "loop": bundle_result["loop"],
            "servos": bundle_result["servos"],
            "attach_cmds": bundle_result["attach_cmds"],
            "steps": bundle_result["steps"],
        }

    req = MixedScriptRequest.model_validate(payload)
    try:
        req = _normalize_script_request(req)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        servo_ids = [int(servo_id) for servo_id in req.servos]
    except ValueError as exc:
        raise HTTPException(422, "Servo mapping IDs must be integers from 1 to 6") from exc
    if any(servo_id < 1 or servo_id > 6 for servo_id in servo_ids):
        raise HTTPException(422, "Servo mapping IDs must be integers from 1 to 6")

    if not req.steps:
        raise HTTPException(400, "steps 不能為空")
    action_steps = list(_iter_action_steps(req.steps))
    if any(step.type == "evt" and not (step.evt or "").strip() for step in action_steps):
        raise HTTPException(400, "EVT step 需要 evt 名稱")

    referenced_condition_ids = _collect_condition_ids(req.steps)
    for condition_id in referenced_condition_ids:
        condition = condition_manager.get(condition_id)
        if condition is None:
            raise HTTPException(422, f"Condition {condition_id} is unavailable")
        if snapshot_pool.get(condition.snapshot_id) is None:
            raise HTTPException(
                422,
                f"Snapshot {condition.snapshot_id} for Condition {condition_id} is unavailable",
            )

    req.condition_ids = list(dict.fromkeys([
        *img_import_summary["condition_map"].values(),
        *referenced_condition_ids,
    ]))

    if (_script_runner_task is None or _script_runner_task.done()) and _script_queue.empty():
        await event_bus.clear()

    queued = await _activate_script(req)
    await _notify_pool_snapshot()

    return {
        "ok":    True,
        "job_id": req.job_id,
        "steps": len(req.steps),
        "loop":  req.loop,
        "pending": False,
        "queued": queued,
        "img_import": img_import_summary,
    }

@app.post("/script/stop")
async def script_stop():
    async with _script_control_lock:
        return await _stop_scripts()


async def _stop_scripts(stop_servos: bool = True):
    global _script_runner_task, _script_pause, _script_resume_event, _script_wait
    runner = _script_runner_task
    if runner and not runner.done():
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
    _script_runner_task = None
    _script_pause = None
    _script_resume_event = None
    _script_wait = None
    await _drain_script_queue()
    script_runtime.clear()
    await _notify_runtime()
    await event_bus.clear()
    await pending_evt_pool.clear()
    await _notify_pool_snapshot()
    # 也停 Servo
    if stop_servos and srv_serial.is_connected:
        srv_serial.send("STOP")
    return {"ok": True}


@app.post("/script/import_bundle")
async def script_import_bundle(req: "ScriptBundleImportRequest"):
    async with _script_control_lock:
        return await _import_script_bundle(req)


async def _import_script_bundle(req: "ScriptBundleImportRequest"):
    global camera, current_camera_index
    logger.info(
        "[IMPORT] Bundle import started: %d snapshot(s), %d condition(s), %d step(s)",
        len(req.img_bundle.snapshots) if req.img_bundle else 0,
        len(req.img_bundle.conditions) if req.img_bundle else 0,
        len(req.steps),
    )
    bundle_condition_ids = {condition.condition_id for condition in req.img_bundle.conditions}
    missing_condition_ids = [
        condition_id for condition_id in _collect_condition_ids(req.steps)
        if condition_id not in bundle_condition_ids
    ]
    if missing_condition_ids:
        missing = ", ".join(missing_condition_ids)
        raise HTTPException(422, f"Missing bundle Condition payload for referenced Condition(s): {missing}")

    # Ensure camera is running whenever an IMG bundle is loaded (e.g. via curl with no UI).
    if req.img_bundle and req.img_bundle.conditions and (not camera or not camera.isOpened()):
        try:
            loop_ = asyncio.get_event_loop()
            cam = await loop_.run_in_executor(executor, _open_camera, CAMERA_INDEX)
            if cam.isOpened():
                camera = cam
                current_camera_index = CAMERA_INDEX
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
        condition_map[cond.condition_id] = new_cid

    remapped_steps = [
        _remap_imported_node(step.model_dump(), condition_map)
        for step in req.steps
    ]

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
        "name": req.name,
        "loop": req.loop,
        "servos": req.servos,
        "attach_cmds": req.attach_cmds,
        "steps": remapped_steps,
        "img_import": img_import,
    }


def _remap_imported_node(node: dict[str, Any], condition_map: dict[str, str]) -> dict[str, Any]:
    result = dict(node)
    node_type = result.get("type")
    if node_type == "if_else":
        condition_id = result.get("condition_id")
        if condition_id not in condition_map:
            raise HTTPException(422, f"Missing bundle Condition payload for Condition {condition_id}")
        result["condition_id"] = condition_map[condition_id]
        result["then_steps"] = [
            _remap_imported_node(child, condition_map) for child in result.get("then_steps", [])
        ]
        result["else_steps"] = [
            _remap_imported_node(child, condition_map) for child in result.get("else_steps", [])
        ]
    elif node_type == "do_while":
        condition_id = result.get("condition_id")
        if condition_id not in condition_map:
            raise HTTPException(422, f"Missing bundle Condition payload for Condition {condition_id}")
        result["condition_id"] = condition_map[condition_id]
        result["steps"] = [
            _remap_imported_node(child, condition_map) for child in result.get("steps", [])
        ]
    else:
        event_name = EventBus._normalize(result.get("evt") or "")
        if event_name.startswith("IMG.MATCH."):
            old_condition_id = event_name[len("IMG.MATCH."):]
            if old_condition_id not in condition_map:
                raise HTTPException(
                    422,
                    f"Missing bundle Condition payload for Condition {old_condition_id}",
                )
            result["evt"] = f"IMG.MATCH.{condition_map[old_condition_id]}"
    return result

@app.get("/script/status")
async def script_status():
    running = bool(_script_runner_task and not _script_runner_task.done())
    pending = await pending_evt_pool.snapshot()
    pause = None if _script_pause is None else {**_script_pause,
        "resuming": bool(_script_resume_event and _script_resume_event.is_set())}
    return {"running": running, "queued": _script_queue.qsize(), "pending": pending,
            "paused": pause is not None, "pause": pause,
            "waiting": dict(_script_wait) if _script_wait is not None else None,
            "runtime": script_runtime.snapshot()}


@app.get("/script/jobs/{job_id}")
async def script_job_detail(job_id: str):
    detail = script_runtime.detail(job_id)
    if detail is None:
        raise HTTPException(404, "Script job is no longer active or queued")
    conditions = {condition["condition_id"]: condition for condition in condition_manager.list_all()}
    detail["conditions"] = []
    for condition_id in detail["condition_ids"]:
        condition = conditions.get(condition_id)
        if condition is None:
            condition = {"condition_id": condition_id, "name": "Missing condition", "snapshot_id": None,
                         "roi": [], "enabled": False, "last_score": None, "consecutive_hits": 0, "min_hits": 0}
        else:
            condition = dict(condition)
        if not detail["active"]:
            condition.update(enabled=False, last_score=None, consecutive_hits=0, in_cooldown=False)
        detail["conditions"].append(condition)
    detail["remaining_ms"] = max(0, round((detail["deadline"] - time.time()) * 1000)) if detail["deadline"] is not None else None
    detail["pause"] = dict(_script_pause) if detail["active"] and _script_pause else None
    return detail


@app.post("/script/pause")
async def script_pause():
    global _script_pause, _script_resume_event, _script_pause_generation
    async with _script_control_lock:
        active = script_runtime.snapshot()["active"]
        if (_script_runner_task is None or _script_runner_task.done() or active is None
                or _script_pause is not None or active["phase"] == "finishing"):
            raise HTTPException(409, "No running script is available for pause")
        conditions = [condition.condition_id for condition, _ in condition_manager.get_armed()]
        _script_pause_generation += 1
        _script_resume_event = asyncio.Event()
        _script_pause = {"pause_id": uuid.uuid4().hex, "reason": "manual", "message": "Manually paused",
            "since": time.time(), "job_id": active["job_id"], "step": active["current_step"],
            "total": active["total"], "evt": (_script_wait or {}).get("evt"),
            "previous_phase": active["phase"], "previous_deadline": active["deadline"], "condition_ids": conditions}
        condition_manager.activate_only([])
        script_runtime.set_phase("paused")
        await _notify_runtime()
        await kbm_ws.broadcast({"type": "script_paused", **_script_pause})
        await srv_ws.broadcast({"type": "visual_sync", "reason": "script_paused",
                                "snapshots": len(snapshot_pool.list_all()), "conditions": len(condition_manager.list_all())})
        return {"ok": True, "pause": dict(_script_pause)}


@app.post("/script/resume")
async def script_resume(request: Request):
    global _script_pause, _script_resume_event
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Resume requires a JSON object containing pause_id") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Resume requires a JSON object containing pause_id")
    async with _script_control_lock:
        if (_script_pause is None or _script_resume_event is None or _script_resume_event.is_set()
                or _script_runner_task is None or _script_runner_task.done()
                or payload.get("pause_id") != _script_pause["pause_id"]):
            raise HTTPException(409, "Pause is no longer current; refresh status before resuming")
        pause = _script_pause
        resume = _script_resume_event
        if pause.get("job_id") != script_runtime.active_id:
            raise HTTPException(409, "Paused job is no longer current")
        if pause.get("evt"):
            await event_bus.discard(pause["evt"])
        condition_manager.activate_only(pause["condition_ids"])
        _script_pause = None
        _script_resume_event = None
        script_runtime.set_phase(pause["previous_phase"], deadline=pause["previous_deadline"])
        resume.set()
        await _notify_runtime()
        await kbm_ws.broadcast({"type": "script_resumed", "evt": pause["evt"], "step": pause["step"], "total": pause["total"]})
        await srv_ws.broadcast({"type": "visual_sync", "reason": "script_resumed",
                                "snapshots": len(snapshot_pool.list_all()), "conditions": len(condition_manager.list_all())})
        return {"ok": True, "resuming": True, "pause_id": pause["pause_id"]}


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
            "index":  current_camera_index,
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
    width: Optional[int] = None
    height: Optional[int] = None


def _open_camera(idx: int, width: Optional[int] = None, height: Optional[int] = None) -> cv2.VideoCapture:
    # On Windows, prefer DirectShow first for more stable webcam switching.
    if os.name == "nt":
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
    else:
        cap = cv2.VideoCapture(idx)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  width or CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height or CAMERA_HEIGHT)
    return cap


def _windows_camera_friendly_names(backend: int = cv2.CAP_DSHOW) -> dict[int, str]:
    """Return camera names keyed by the selected OpenCV backend's device indices."""
    if os.name != "nt":
        return {}
    try:
        return {info.index: info.name for info in enumerate_cameras(backend)}
    except Exception as exc:
        logger.warning(f"Camera name enumeration failed for backend {backend}: {exc}")
        return {}


@app.get("/camera/devices")
async def camera_devices():
    """Return openable cameras with names matched by backend and device index."""
    def _probe():
        found = []
        friendly_names = {
            backend: _windows_camera_friendly_names(backend)
            for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF)
        }
        indices = set(range(10))
        for names in friendly_names.values():
            indices.update(names)
        if current_camera_index is not None:
            indices.add(current_camera_index)
        # Silence OpenCV's C-level logger during probing so OBSensor/depth-camera
        # backends don't spam "Camera index out of range" errors to the console.
        try:
            saved_level = cv2.getLogLevel()
            cv2.setLogLevel(0)
        except Exception:
            saved_level = None
        try:
            for index in sorted(indices):
                active = bool(camera and camera.isOpened() and current_camera_index == index)
                cap = camera if active else _open_camera(index)
                try:
                    if cap is not None and cap.isOpened():
                        backend = int(cap.get(cv2.CAP_PROP_BACKEND))
                        name = friendly_names.get(backend, {}).get(index)
                        found.append({
                            "index": index,
                            "label": name or f"Camera {index}",
                            "available": True,
                        })
                finally:
                    if cap is not None and not active:
                        cap.release()
        finally:
            if saved_level is not None:
                try:
                    cv2.setLogLevel(saved_level)
                except Exception:
                    pass
        found.sort(key=lambda d: int(d.get("index", 0)))
        return found
    loop = asyncio.get_running_loop()
    devices = await loop.run_in_executor(executor, _probe)
    return {"devices": devices}


@app.post("/camera/start")
async def camera_start(req: Optional[CameraStartRequest] = None):
    global camera, current_camera_index
    idx = (req.index if req and req.index is not None else None)
    w = (req.width if req and req.width is not None else None)
    h = (req.height if req and req.height is not None else None)
    if idx is None:
        idx = CAMERA_INDEX

    if camera and camera.isOpened() and current_camera_index == idx:
        return {"ok": True, "already": True, "index": idx}

    previous_idx = current_camera_index
    if camera and camera.isOpened():
        frame_buffer.stop()
        camera.release()
        camera = None
        current_camera_index = None

    loop = asyncio.get_running_loop()
    new_camera = await loop.run_in_executor(executor, _open_camera, idx, w, h)
    if new_camera is None or not new_camera.isOpened():
        if new_camera is not None:
            new_camera.release()
        # Best-effort rollback to previously opened camera.
        if previous_idx is not None and previous_idx != idx:
            fallback_camera = await loop.run_in_executor(executor, _open_camera, previous_idx, w, h)
            if fallback_camera.isOpened():
                camera = fallback_camera
                current_camera_index = previous_idx
                frame_buffer.start(camera)
                logger.warning(f"Camera switch failed: requested index={idx}, restored index={previous_idx}")
            else:
                fallback_camera.release()
        raise HTTPException(503, f"Camera index {idx} not available")

    camera = new_camera
    current_camera_index = idx
    aw = int(new_camera.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(new_camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
    logger.info(f"Camera opened index={idx}: {aw}x{ah}, Camera ready")
    frame_buffer.start(new_camera)
    return {"ok": True, "width": aw, "height": ah, "index": idx}

@app.post("/camera/stop")
async def camera_stop():
    global camera, current_camera_index
    frame_buffer.stop()
    if camera:
        camera.release()
        camera = None
    current_camera_index = None
    logger.info("Camera closed.")
    return {"ok": True}

@app.get("/camera/status")
async def camera_status():
    opened = bool(camera and camera.isOpened())
    return {"opened": opened, "index": current_camera_index}


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

@app.post("/visual/ocr/test")
async def visual_ocr_test(req: OcrConfig):
    try:
        ensure_available()
        value = await read_stable_digits(req, frame_buffer)
    except OcrUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(408, str(exc)) from exc
    return {"ok": True, "variable": req.variable, "value": value}

class ImageTemplateRequest(BaseModel):
    snapshot_id: str
    roi: list[float]

    @model_validator(mode="after")
    def validate_roi(self):
        OcrConfig(roi=self.roi)
        return self


@app.post("/visual/image-move/template")
async def image_move_template(req: ImageTemplateRequest):
    snapshot = snapshot_pool.get(req.snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "Snapshot not found")
    frame = cv2.imdecode(np.frombuffer(base64.b64decode(snapshot["jpeg_b64"]), dtype=np.uint8), cv2.IMREAD_COLOR)
    try:
        template = crop_roi(frame, req.roi)
        ok, encoded = cv2.imencode(".png", template)
        if not ok:
            raise ValueError("Cannot encode template")
        config = ImageMoveConfig(template_b64=base64.b64encode(encoded).decode(),
                                  source_width=frame.shape[1], source_height=frame.shape[0])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "image_move": config.model_dump()}


@app.post("/visual/image-move/test")
async def image_move_test(req: ImageMoveConfig):
    try:
        result = await locate_image(req, frame_buffer, preview=True)
    except AmbiguousMatch as exc:
        raise HTTPException(409, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(408, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "match": result.model_dump()}


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
        score, matched = _match_condition_frame(cond, frame)
    except Exception as exc:
        raise HTTPException(500, f"Match failed: {exc}")
    return {"score": round(score, 4), "matched": matched, "threshold": cond.threshold}


if __name__ == "__main__":
    bind_port = _find_available_port(HOST, PORT)
    if bind_port != PORT:
        logger.warning(f"Port {PORT} is already in use. Fallback to port {bind_port}.")
    _log_access_urls(HOST, bind_port)
    uvicorn.run(app, host=HOST, port=bind_port, reload=False, log_level="info")
