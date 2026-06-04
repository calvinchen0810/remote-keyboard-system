"""
servo_router.py
Servo 相關 API 路由
掛載到 main app 的 /srv 前綴下
WebSocket: /ws/srv
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel, Field

from servo_serial import SerialManager as ServoSerial, MAX_SERVOS

logger = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────
class StepModel(BaseModel):
    delay_ms:    int = Field(0,   ge=0)
    servo_id:    int = Field(1,   ge=1, le=6)
    angle:       int = Field(90,  ge=0, le=180)
    speed:       int = Field(60,  ge=1, le=100)
    duration_ms: int = Field(300, ge=0)
    home:        int = Field(1,   ge=0, le=1)

class RunRequest(BaseModel):
    steps:       list[StepModel] = Field(default_factory=list, max_length=48)
    loop:        bool            = Field(False)
    servos:      dict[str, int]  = Field(default_factory=dict)
    attach_cmds: list[str]       = Field(default_factory=list, max_length=48)

class ConnectRequest(BaseModel):
    port: Optional[str] = None

class AttachRequest(BaseModel):
    sid: int = Field(..., ge=1, le=6)
    pin: int = Field(..., ge=2, le=13)

class AttachAllRequest(BaseModel):
    servos: dict[str, int]

class DetachRequest(BaseModel):
    sid: int = Field(..., ge=1, le=6)


# ── WebSocket manager ─────────────────────────────────────────
class SrvWSManager:
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
        data["channel"] = "srv"
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


# ── Factory ───────────────────────────────────────────────────
def create_servo_router() -> tuple[APIRouter, ServoSerial, SrvWSManager]:
    router     = APIRouter()
    serial_mgr = ServoSerial()
    ws_mgr     = SrvWSManager()

    def on_serial_line(line: str):
        logger.info(f"[SRV] ← {line}")
        ws_mgr.broadcast_sync({"type": "serial", "line": line})
        if line.startswith("OK RUNNING ") and "/" in line:
            try:
                cur, tot = line.split()[-1].split("/")
                ws_mgr.broadcast_sync({
                    "type": "status", "state": "running",
                    "step": max(0, int(cur) - 1), "total": int(tot),
                    "port": serial_mgr.port,
                })
            except Exception:
                pass
        elif line == "OK DONE":
            ws_mgr.broadcast_sync({"type": "done", "state": "idle"})
        elif line in ("OK STOPPED", "OK READY"):
            ws_mgr.broadcast_sync({"type": "status", "state": "idle",
                                    "port": serial_mgr.port,
                                    "attached": serial_mgr.attached})
        elif line.startswith("OK ATTACH") or line.startswith("OK DETACH"):
            ws_mgr.broadcast_sync({"type": "attached", "attached": serial_mgr.attached})
        elif line.startswith("ERR"):
            ws_mgr.broadcast_sync({"type": "error", "message": line})

    def on_serial_send(cmd: str):
        logger.info(f"[SRV] → {cmd}")
        ws_mgr.broadcast_sync({"type": "serial", "line": f"→ {cmd}"})

    def on_disconnect():
        logger.info("[SRV] disconnected")
        ws_mgr.broadcast_sync({"type": "status", "state": "disconnected",
                                "port": None, "attached": {}})

    serial_mgr.on_line(on_serial_line)
    serial_mgr.on_send(on_serial_send)
    serial_mgr.on_disconnect(on_disconnect)

    # ── WebSocket /ws/srv ─────────────────────────────────────
    @router.websocket("/ws/srv")
    async def ws_endpoint(ws: WebSocket):
        await ws_mgr.connect(ws)
        await ws.send_text(json.dumps(_status(serial_mgr)))
        try:
            while True:
                data = await ws.receive_text()
                msg  = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "channel": "srv"}))
        except WebSocketDisconnect:
            await ws_mgr.disconnect(ws)

    # ── GET /srv/api/ports ────────────────────────────────────
    @router.get("/api/ports")
    async def api_ports():
        return {"ports": ServoSerial.scan_ports()}

    # ── GET /srv/api/status ───────────────────────────────────
    @router.get("/api/status")
    async def api_status():
        return _status(serial_mgr)

    # ── POST /srv/api/connect ─────────────────────────────────
    @router.post("/api/connect")
    async def api_connect(req: ConnectRequest = ConnectRequest()):
        port = req.port or ServoSerial.auto_detect()
        if not port:
            raise HTTPException(400, "找不到 Arduino，請指定 COM port")
        ok = await asyncio.get_event_loop().run_in_executor(
            None, serial_mgr.connect, port
        )
        if not ok:
            raise HTTPException(500, f"無法連線到 {port}")
        ws_mgr.broadcast_sync({"type": "status", "state": "idle",
                                "port": port, "attached": {}})
        serial_mgr.send("STATUS")
        return {"ok": True, "port": port}

    # ── POST /srv/api/disconnect ──────────────────────────────
    @router.post("/api/disconnect")
    async def api_disconnect():
        serial_mgr.disconnect()
        return {"ok": True}

    # ── POST /srv/api/attach ──────────────────────────────────
    @router.post("/api/attach")
    async def api_attach(req: AttachRequest):
        if not serial_mgr.is_connected:
            raise HTTPException(400, "尚未連線")
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: serial_mgr.attach_servo_and_wait(req.sid, req.pin)
        )
        if not ok:
            raise HTTPException(500, "ATTACH 失敗")
        return {"ok": True, "sid": req.sid, "pin": req.pin,
                "attached": serial_mgr.attached}

    # ── POST /srv/api/detach ──────────────────────────────────
    @router.post("/api/detach")
    async def api_detach(req: DetachRequest):
        if not serial_mgr.is_connected:
            raise HTTPException(400, "尚未連線")
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: serial_mgr.detach_servo(req.sid)
        )
        if not ok:
            raise HTTPException(500, "DETACH 失敗")
        return {"ok": True, "sid": req.sid, "attached": serial_mgr.attached}

    # ── POST /srv/api/attach_all ──────────────────────────────
    @router.post("/api/attach_all")
    async def api_attach_all(req: AttachAllRequest):
        if not serial_mgr.is_connected:
            raise HTTPException(400, "尚未連線")
        pin_map = {int(k): v for k, v in req.servos.items()}
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: serial_mgr.attach_all(pin_map)
        )
        if not ok:
            raise HTTPException(500, "ATTACH_ALL 失敗")
        return {"ok": True, "attached": serial_mgr.attached}

    # ── POST /srv/api/detach_all ──────────────────────────────
    @router.post("/api/detach_all")
    async def api_detach_all():
        if not serial_mgr.is_connected:
            raise HTTPException(400, "尚未連線")
        await asyncio.get_event_loop().run_in_executor(
            None, serial_mgr.detach_all
        )
        return {"ok": True, "attached": {}}

    # ── POST /srv/api/run (SRV-only script) ───────────────────
    @router.post("/api/run")
    async def api_run(req: RunRequest):
        if not serial_mgr.is_connected:
            raise HTTPException(400, "尚未連線")
        if not req.steps and not req.attach_cmds:
            raise HTTPException(400, "steps 不能為空")

        # pre-attach
        for raw in req.attach_cmds:
            parts = raw.strip().split()
            if len(parts) != 3 or parts[0].upper() != "ATTACH":
                continue
            try:
                sid, pin = int(parts[1]), int(parts[2])
            except ValueError:
                continue
            if serial_mgr.attached.get(sid) == pin:
                continue
            ok = await asyncio.get_event_loop().run_in_executor(
                None, lambda s=sid, p=pin: serial_mgr.attach_servo_and_wait(s, p)
            )
            if not ok:
                raise HTTPException(500, f"attach_cmd 失敗 sid={sid}")

        steps    = [s.model_dump() for s in req.steps]
        used_sids = sorted({s.get("servo_id", 1) for s in steps})
        req_pin_map = {}
        for k, v in (req.servos or {}).items():
            try:
                req_pin_map[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        effective = dict(serial_mgr.attached)
        effective.update(req_pin_map)

        for sid in used_sids:
            if sid in serial_mgr.attached:
                continue
            pin = effective.get(sid)
            if pin is None:
                continue
            ok = await asyncio.get_event_loop().run_in_executor(
                None, lambda s=sid, p=pin: serial_mgr.attach_servo_and_wait(s, p)
            )
            if not ok:
                raise HTTPException(500, f"自動 ATTACH 失敗 sid={sid}")

        ws_mgr.broadcast_sync({"type": "attached", "attached": serial_mgr.attached})
        if not steps:
            return {"ok": True, "attach_only": True, "attached": serial_mgr.attached}

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: serial_mgr.send_script(steps, req.loop)
        )
        if not ok:
            raise HTTPException(500, "腳本傳送失敗")
        ws_mgr.broadcast_sync({
            "type": "status", "state": "running",
            "step": 0, "total": len(steps), "port": serial_mgr.port,
        })
        return {"ok": True, "steps": len(steps), "loop": req.loop}

    # ── POST /srv/api/stop ────────────────────────────────────
    @router.post("/api/stop")
    async def api_stop():
        if not serial_mgr.is_connected:
            raise HTTPException(400, "尚未連線")
        serial_mgr.send("STOP")
        return {"ok": True}

    # ── POST /srv/api/command (single step) ──────────────────
    @router.post("/api/command")
    async def api_command(req: StepModel):
        if not serial_mgr.is_connected:
            raise HTTPException(400, "尚未連線")
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: serial_mgr.send_command(req.model_dump())
        )
        if not ok:
            raise HTTPException(500, "指令傳送失敗")
        return {"ok": True}

    # ── POST /srv/api/send (raw cmd) ─────────────────────────
    @router.post("/api/send")
    async def api_send(body: dict):
        if not serial_mgr.is_connected:
            raise HTTPException(400, "尚未連線")
        cmd = body.get("cmd", "").strip()
        if not cmd:
            raise HTTPException(400, "cmd 不能為空")
        serial_mgr.send(cmd)
        return {"ok": True}

    return router, serial_mgr, ws_mgr


def _status(mgr: ServoSerial) -> dict:
    return {
        "type":     "status",
        "channel":  "srv",
        "state":    "idle" if mgr.is_connected else "disconnected",
        "port":     mgr.port,
        "attached": mgr.attached,
    }
