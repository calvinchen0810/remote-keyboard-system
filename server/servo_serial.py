"""
servo_serial.py
SRV 串口管理 — 移植自 auto-clicker serial_manager.py
電腦2 → Nano_SRV USB Serial (115200)
"""

import serial
import serial.tools.list_ports
import threading
import time
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)

MAX_SERVOS = 6
BAUD       = 115200


class SerialManager:
    def __init__(self):
        self._serial:        Optional[serial.Serial]        = None
        self._lock           = threading.Lock()
        self._on_line:       Optional[Callable[[str], None]] = None
        self._on_send:       Optional[Callable[[str], None]] = None
        self._on_disconnect: Optional[Callable[[], None]]    = None
        self._thread:        Optional[threading.Thread]      = None
        self._running        = False
        self.attached:       dict[int, int]                  = {}   # {sid: pin}
        self._attach_events: dict[int, threading.Event]      = {}

    # ── callbacks ─────────────────────────────────────────────
    def on_line(self, cb: Callable[[str], None]):
        self._on_line = cb

    def on_send(self, cb: Callable[[str], None]):
        self._on_send = cb

    def on_disconnect(self, cb: Callable[[], None]):
        self._on_disconnect = cb

    # ── port scan ──────────────────────────────────────────────
    @staticmethod
    def scan_ports() -> list[dict]:
        ports = []
        for p in serial.tools.list_ports.comports():
            desc   = p.description or ""
            likely = any(k in desc.upper()
                         for k in ["CH340", "CH341", "ARDUINO", "USB SERIAL"])
            ports.append({"port": p.device, "desc": desc, "likely": likely})
        return sorted(ports, key=lambda x: (not x["likely"], x["port"]))

    @staticmethod
    def auto_detect() -> Optional[str]:
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "").upper()
            if any(k in desc for k in ["CH340", "CH341", "ARDUINO"]):
                return p.device
        return None

    # ── connect / disconnect ───────────────────────────────────
    def connect(self, port: str) -> bool:
        self.disconnect()
        try:
            self._serial  = serial.Serial(port, BAUD, timeout=2.0)
            self._running = True
            self._thread  = threading.Thread(
                target=self._read_loop, daemon=True, name="SRVReader"
            )
            self._thread.start()
            time.sleep(2)
            self._serial.reset_input_buffer()
            self.attached = {}
            logger.info(f"SRV connected: {port} @ {BAUD}")
            return True
        except serial.SerialException as e:
            logger.error(f"SRV connect failed: {e}")
            self._serial = None
            return False

    def disconnect(self):
        self._running = False
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self.attached = {}
        logger.info("SRV disconnected")

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def port(self) -> Optional[str]:
        return self._serial.port if self.is_connected else None

    # ── fire-and-forget send ───────────────────────────────────
    def send(self, cmd: str) -> bool:
        if not self.is_connected:
            return False
        try:
            outbound = cmd.strip()
            self._serial.write(f"{outbound}\n".encode())
            self._serial.flush()
            logger.debug(f"SRV >> {outbound}")
            if self._on_send:
                self._on_send(outbound)
            return True
        except serial.SerialException as e:
            logger.error(f"SRV send error: {e}")
            self._serial = None
            return False

    # ── attach / detach ────────────────────────────────────────
    def attach_servo(self, sid: int, pin: int) -> bool:
        ok = self.send(f"ATTACH {sid} {pin}")
        if ok:
            self.attached[sid] = pin
        return ok

    def attach_servo_and_wait(self, sid: int, pin: int, timeout: float = 4.0) -> bool:
        event = threading.Event()
        self._attach_events[sid] = event
        if not self.send(f"ATTACH {sid} {pin}"):
            del self._attach_events[sid]
            return False
        ok = event.wait(timeout)
        self._attach_events.pop(sid, None)
        if ok:
            self.attached[sid] = pin
        return ok

    def detach_servo(self, sid: int) -> bool:
        ok = self.send(f"DETACH {sid}")
        if ok:
            self.attached.pop(sid, None)
        return ok

    def attach_all(self, pin_map: dict[int, int]) -> bool:
        for sid, pin in pin_map.items():
            if not self.attach_servo_and_wait(sid, pin):
                return False
        return True

    def detach_all(self):
        for sid in list(self.attached.keys()):
            self.send(f"DETACH {sid}")
        self.attached = {}

    # ── single step ────────────────────────────────────────────
    def send_command(self, step: dict) -> bool:
        """
        送出單步 BEGIN 1 / STEP / END
        """
        if not self.is_connected:
            return False
        d   = step.get("delay_ms",    0)
        sid = step.get("servo_id",    1)
        ang = step.get("angle",      90)
        spd = step.get("speed",      60)
        dur = step.get("duration_ms",300)
        hom = step.get("home",        1)
        cmds = [
            f"BEGIN 1",
            f"STEP {d} {sid} {ang} {spd} {dur} {hom}",
            f"END",
        ]
        for c in cmds:
            if not self.send(c):
                return False
            time.sleep(0.05)
        return True

    # ── full script ────────────────────────────────────────────
    def send_script(self, steps: list[dict], loop: bool = False) -> bool:
        if not self.is_connected:
            return False
        n = len(steps)
        self.send(f"LOOP {'1' if loop else '0'}")
        time.sleep(0.05)
        self.send(f"BEGIN {n}")
        time.sleep(0.05)
        for s in steps:
            d   = s.get("delay_ms",    0)
            sid = s.get("servo_id",    1)
            ang = s.get("angle",      90)
            spd = s.get("speed",      60)
            dur = s.get("duration_ms",300)
            hom = s.get("home",        1)
            self.send(f"STEP {d} {sid} {ang} {spd} {dur} {hom}")
            time.sleep(0.03)
        self.send("END")
        return True

    # ── background read loop ───────────────────────────────────
    def _read_loop(self):
        while self._running:
            try:
                if not self._serial or not self._serial.is_open:
                    break
                raw = self._serial.readline()
                if raw:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    logger.debug(f"SRV << {line}")
                    # notify attach events
                    if line.startswith("OK ATTACH "):
                        parts = line.split()
                        if len(parts) >= 3:
                            try:
                                sid = int(parts[2])
                                ev  = self._attach_events.get(sid)
                                if ev:
                                    ev.set()
                            except ValueError:
                                pass
                    if self._on_line:
                        self._on_line(line)
            except serial.SerialException as e:
                logger.error(f"SRV read error: {e}")
                break
        if self._on_disconnect:
            self._on_disconnect()
