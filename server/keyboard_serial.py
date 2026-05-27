"""
keyboard_serial.py
KBM（Keyboard + Mouse）串口管理
電腦2 → Nano_KB USB Serial (38400) → SoftwareSerial → Pro Micro Serial1
"""

import serial
import serial.tools.list_ports
import threading
import time
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)

BAUD = 38400   # Nano_KB SoftwareSerial 穩定上限


class KeyboardSerial:
    def __init__(self):
        self._serial:        Optional[serial.Serial] = None
        self._lock           = threading.Lock()
        self._on_line:       Optional[Callable[[str], None]] = None
        self._on_disconnect: Optional[Callable[[], None]]   = None
        self._thread:        Optional[threading.Thread]     = None
        self._running        = False

    # ── callbacks ─────────────────────────────────────────────
    def on_line(self, cb: Callable[[str], None]):
        self._on_line = cb

    def on_disconnect(self, cb: Callable[[], None]):
        self._on_disconnect = cb

    # ── port scan ──────────────────────────────────────────────
    @staticmethod
    def scan_ports() -> list[dict]:
        ports = []
        for p in serial.tools.list_ports.comports():
            desc   = p.description or ""
            likely = any(k in desc.upper()
                         for k in ["CH340", "CH341", "ARDUINO", "LEONARDO", "USB SERIAL"])
            ports.append({"port": p.device, "desc": desc, "likely": likely})
        return sorted(ports, key=lambda x: (not x["likely"], x["port"]))

    # ── connect / disconnect ───────────────────────────────────
    def connect(self, port: str) -> bool:
        self.disconnect()
        try:
            self._serial  = serial.Serial(port, BAUD, timeout=1.0)
            self._running = True
            self._thread  = threading.Thread(
                target=self._read_loop, daemon=True, name="KBMReader"
            )
            self._thread.start()
            time.sleep(2)           # 等 Arduino reset
            self._serial.reset_input_buffer()
            logger.info(f"KBM connected: {port} @ {BAUD}")
            return True
        except serial.SerialException as e:
            logger.error(f"KBM connect failed: {e}")
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
        logger.info("KBM disconnected")

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def port(self) -> Optional[str]:
        return self._serial.port if self.is_connected else None

    # ── send & wait ACK ───────────────────────────────────────
    def send(self, cmd: str, timeout: float = 1.0) -> str:
        """
        送出指令，阻塞等待 ACK。
        回傳：'OK' | 'ERR' | 'TIMEOUT' | 'DISCONNECTED'
        """
        if not self.is_connected:
            return "DISCONNECTED"
        with self._lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write(f"{cmd.strip()}\n".encode())
                self._serial.flush()
                logger.debug(f"KBM >> {cmd}")
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if self._serial.in_waiting:
                        raw = self._serial.readline()
                        ack = raw.decode("utf-8", errors="ignore").strip()
                        logger.debug(f"KBM << {ack}")
                        if ack in ("OK", "ERR"):
                            return ack
                        if ack.startswith("OK"):   # OK PONG など
                            return "OK"
                return "TIMEOUT"
            except serial.SerialException as e:
                logger.error(f"KBM send error: {e}")
                self._serial = None
                return "ERR"

    # ── background read loop (for log broadcast) ──────────────
    def _read_loop(self):
        while self._running:
            try:
                if not self._serial or not self._serial.is_open:
                    break
                raw = self._serial.readline()
                if raw:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line and self._on_line:
                        self._on_line(line)
            except serial.SerialException as e:
                logger.error(f"KBM read error: {e}")
                break
        if self._on_disconnect:
            self._on_disconnect()
