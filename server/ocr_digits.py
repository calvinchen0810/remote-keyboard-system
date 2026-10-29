import asyncio
import importlib.util
import math
import os
import re
import shutil
from pathlib import Path
from typing import Optional

import cv2
from pydantic import BaseModel, Field, model_validator


class OcrUnavailable(RuntimeError):
    pass


class OcrConfig(BaseModel):
    roi: list[float] = Field(min_length=4, max_length=4)
    variable: str = Field(default="digits", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
    timeout_ms: int = Field(default=5000, ge=100, le=60000)
    stable_hits: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def validate_roi(self):
        left, top, width, height = self.roi
        if (not all(math.isfinite(value) for value in self.roi)
                or left < 0 or top < 0 or width <= 0 or height <= 0
                or left + width > 1.00000001 or top + height > 1.00000001):
            raise ValueError("ROI must be a positive rectangle inside the normalized image")
        return self


def crop_roi(frame, roi):
    OcrConfig(roi=roi)
    height, width = frame.shape[:2]
    left, top, region_width, region_height = roi
    start_x, start_y = int(round(left * width)), int(round(top * height))
    end_x = min(width, int(round((left + region_width) * width)))
    end_y = min(height, int(round((top + region_height) * height)))
    if end_x <= start_x or end_y <= start_y:
        raise ValueError("ROI is smaller than one pixel")
    return frame[start_y:end_y, start_x:end_x].copy()


def parse_digits(text: str) -> Optional[str]:
    value = text.strip()
    return value if re.fullmatch(r"[0-9]{4}", value) else None


def expand_variables(text: str, variables: dict[str, str]) -> str:
    def replace(match):
        name = match.group(1)
        if name not in variables:
            raise ValueError(f"Undefined OCR variable: {name}")
        value = variables[name]
        if parse_digits(value) != value:
            raise ValueError(f"Invalid OCR variable: {name}")
        return value

    expanded = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,31})\}", replace, text)
    if "${" in expanded:
        raise ValueError("Invalid variable placeholder")
    return expanded


def engine_path() -> Optional[str]:
    configured = os.getenv("TESSERACT_CMD")
    if configured:
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)
    found = shutil.which("tesseract")
    if found:
        return found
    for folder in [os.getenv("ProgramFiles", "C:/Program Files"), os.getenv("ProgramFiles(x86)", "C:/Program Files (x86)")]:
        candidate = Path(folder) / "Tesseract-OCR" / "tesseract.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def ensure_available() -> str:
    if importlib.util.find_spec("pytesseract") is None:
        raise OcrUnavailable("OCR requires pytesseract; install server/requirements.txt")
    executable = engine_path()
    if not executable:
        raise OcrUnavailable("Tesseract not found. Install Tesseract and set TESSERACT_CMD to tesseract.exe")
    return executable


def recognize_digits(frame, roi, timeout: float = 1.5) -> Optional[str]:
    executable = ensure_available()
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = executable
    crop = crop_roi(frame, roi)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    scale = max(2.0, min(6.0, 80.0 / gray.shape[0]))
    enlarged = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(enlarged, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if float(binary.mean()) < 127:
        binary = cv2.bitwise_not(binary)
    prepared = cv2.copyMakeBorder(binary, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    try:
        text = pytesseract.image_to_string(
            prepared, lang="eng", config="--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789",
            timeout=max(0.05, timeout),
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailable("Tesseract executable is unavailable") from exc
    except RuntimeError:
        return None
    return parse_digits(text)


async def read_stable_digits(config: OcrConfig, buffer) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.timeout_ms / 1000
    generation = buffer.generation
    previous = None
    hits = 0
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"OCR timed out for {config.variable}; no stable four-digit result")
        frame, current_generation = buffer.read_sample()
        if frame is None or current_generation <= generation:
            await asyncio.sleep(min(0.03, remaining))
            continue
        generation = current_generation
        try:
            value = await asyncio.wait_for(
                asyncio.to_thread(recognize_digits, frame, config.roi, min(1.5, remaining)), remaining,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"OCR timed out for {config.variable}") from exc
        value = parse_digits(value) if isinstance(value, str) else None
        hits = hits + 1 if value is not None and value == previous else (1 if value else 0)
        previous = value
        if value is not None and hits >= config.stable_hits:
            return value