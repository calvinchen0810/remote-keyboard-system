import asyncio
import base64
import binascii
from typing import Optional

import cv2
import numpy as np
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from ocr_digits import OcrConfig


class AmbiguousMatch(ValueError):
    pass


class ImageMoveConfig(BaseModel):
    template_b64: str = Field(min_length=1, max_length=2800000)
    source_width: int = Field(ge=16, le=8192)
    source_height: int = Field(ge=16, le=8192)
    search_roi: list[float] = Field(default_factory=lambda: [0, 0, 1, 1])
    threshold: float = Field(default=0.92, ge=0.5, le=1)
    timeout_ms: int = Field(default=5000, ge=100, le=60000)
    stable_hits: int = Field(default=2, ge=1, le=10)
    _template: np.ndarray = PrivateAttr()

    @model_validator(mode="after")
    def validate_image(self):
        OcrConfig(roi=self.search_roi)
        try:
            data = base64.b64decode(self.template_b64, validate=True)
            template = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        except (ValueError, binascii.Error, cv2.error) as exc:
            raise ValueError("Invalid image-move template") from exc
        if template is None or min(template.shape[:2]) < 4:
            raise ValueError("Image-move template must be at least 4 by 4 pixels")
        if template.shape[1] > self.source_width or template.shape[0] > self.source_height:
            raise ValueError("Template is larger than the source frame")
        if float(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY).std()) < 2:
            raise ValueError("Select a template containing distinct image features")
        self._template = template
        return self


class ImageMatch(BaseModel):
    box: list[int]
    center: list[float]
    hid: list[int]
    score: float
    frame_width: int
    frame_height: int
    preview_b64: Optional[str] = None


def match_image(config: ImageMoveConfig, frame) -> Optional[ImageMatch]:
    frame_height, frame_width = frame.shape[:2]
    expected_ratio = config.source_width / config.source_height
    if abs(frame_width / frame_height / expected_ratio - 1) > 0.01:
        raise ValueError("Capture aspect ratio changed; select the template again")
    template_width = max(1, round(config._template.shape[1] * frame_width / config.source_width))
    template_height = max(1, round(config._template.shape[0] * frame_height / config.source_height))
    template = cv2.resize(config._template, (template_width, template_height), interpolation=cv2.INTER_LINEAR)
    left, top, width, height = config.search_roi
    start_x, start_y = round(left * frame_width), round(top * frame_height)
    end_x = min(frame_width, round((left + width) * frame_width))
    end_y = min(frame_height, round((top + height) * frame_height))
    region = frame[start_y:end_y, start_x:end_x]
    if region.shape[0] < template_height or region.shape[1] < template_width:
        raise ValueError("Search region is smaller than the template")
    scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, score, _, location = cv2.minMaxLoc(scores)
    if score < config.threshold:
        return None
    match_x, match_y = location
    other_scores = scores.copy()
    radius_x, radius_y = max(1, template_width // 2), max(1, template_height // 2)
    other_scores[max(0, match_y-radius_y):match_y+radius_y+1,
                 max(0, match_x-radius_x):match_x+radius_x+1] = -1
    if float(other_scores.max()) >= config.threshold:
        raise AmbiguousMatch("Multiple image targets match; narrow the search region or choose a more distinctive template")
    match_x += start_x
    match_y += start_y
    center_x = match_x + (template_width - 1) / 2
    center_y = match_y + (template_height - 1) / 2
    return ImageMatch(box=[match_x, match_y, template_width, template_height],
        center=[center_x, center_y], hid=[round(center_x / (frame_width-1) * 32767),
        round(center_y / (frame_height-1) * 32767)], score=round(float(score), 6),
        frame_width=frame_width, frame_height=frame_height)


def _preview(frame, match: ImageMatch) -> str:
    image = frame.copy()
    left, top, width, height = match.box
    cv2.rectangle(image, (left, top), (left+width-1, top+height-1), (0, 220, 80), 2)
    cv2.drawMarker(image, tuple(round(value) for value in match.center), (0, 220, 80), cv2.MARKER_CROSS, 20, 2)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("Could not encode match preview")
    return base64.b64encode(encoded).decode()


async def locate_image(config: ImageMoveConfig, buffer, preview: bool = False) -> ImageMatch:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.timeout_ms / 1000
    generation = buffer.generation
    previous = None
    hits = 0
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("Image move timed out; no unique stable target")
        frame, current_generation = buffer.read_sample()
        if frame is None or current_generation <= generation:
            await asyncio.sleep(min(0.03, remaining))
            continue
        generation = current_generation
        result = await asyncio.wait_for(asyncio.to_thread(match_image, config, frame), remaining)
        agrees = (result is not None and previous is not None
                  and max(abs(result.center[index]-previous.center[index]) for index in [0, 1]) <= 3)
        hits = hits + 1 if agrees else (1 if result is not None else 0)
        previous = result
        if result is not None and hits >= config.stable_hits:
            if preview:
                result.preview_b64 = await asyncio.to_thread(_preview, frame, result)
            return result