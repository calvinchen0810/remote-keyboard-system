import asyncio
import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import image_move


class ImageMoveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.template = np.random.default_rng(42).integers(0, 256, (16, 20, 3), dtype=np.uint8)
        ok, data = cv2.imencode(".png", self.template)
        self.assertTrue(ok)
        self.config = image_move.ImageMoveConfig(template_b64=base64.b64encode(data).decode(),
            source_width=200, source_height=100)
        self.frame = np.zeros((100, 200, 3), dtype=np.uint8)
        self.frame[30:46, 50:70] = self.template

    def test_unique_target_returns_center_and_absolute_coordinates(self):
        result = image_move.match_image(self.config, self.frame)
        self.assertEqual(result.box, [50, 30, 20, 16])
        self.assertEqual(result.center, [59.5, 37.5])
        self.assertEqual(result.hid, [round(59.5 / 199 * 32767), round(37.5 / 99 * 32767)])
        self.assertGreater(result.score, 0.99)

    def test_duplicate_targets_are_rejected(self):
        self.frame[60:76, 120:140] = self.template
        with self.assertRaises(image_move.AmbiguousMatch):
            image_move.match_image(self.config, self.frame)

    def test_search_roi_can_disambiguate(self):
        self.frame[60:76, 120:140] = self.template
        self.config.search_roi = [0, 0, 0.5, 1]
        self.assertEqual(image_move.match_image(self.config, self.frame).box, [50, 30, 20, 16])

    def test_missing_target_returns_none(self):
        self.assertIsNone(image_move.match_image(self.config, np.zeros_like(self.frame)))

    def test_aspect_change_is_rejected_and_proportional_scaling_works(self):
        with self.assertRaises(ValueError):
            image_move.match_image(self.config, np.zeros((100, 210, 3), dtype=np.uint8))
        frame = cv2.resize(self.frame, (400, 200), interpolation=cv2.INTER_LINEAR)
        result = image_move.match_image(self.config, frame)
        self.assertLess(abs(result.center[0] - 119.5), 2)
        self.assertLess(abs(result.center[1] - 75.5), 2)

    def test_constant_or_invalid_template_is_rejected(self):
        _, data = cv2.imencode(".png", np.zeros((10, 10, 3), dtype=np.uint8))
        for template in ["bad-base64", base64.b64encode(data).decode()]:
            with self.subTest(template=template[:12]), self.assertRaises(ValueError):
                image_move.ImageMoveConfig(template_b64=template, source_width=200, source_height=100)

    def test_search_roi_and_timing_validation(self):
        for fields in [{"search_roi": [0, 0, 0, 1]}, {"search_roi": [0.9, 0, 0.2, 1]},
                       {"timeout_ms": 0}, {"threshold": 1.1}, {"stable_hits": 0}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                image_move.ImageMoveConfig(**{**self.config.model_dump(), **fields})

    async def test_only_distinct_new_generations_satisfy_stability(self):
        frame = self.frame

        class Buffer:
            generation = 10
            samples = iter([10, 11, 11, 12])

            def read_sample(self):
                return frame, next(self.samples)

        with patch.object(image_move, "match_image", wraps=image_move.match_image) as match:
            result = await image_move.locate_image(self.config, Buffer())
        self.assertEqual(match.call_count, 2)
        self.assertEqual(result.box, [50, 30, 20, 16])

    async def test_stalled_capture_times_out(self):
        class Buffer:
            generation = 1

            def read_sample(self):
                return None, 1

        self.config.timeout_ms = 100
        with self.assertRaises(TimeoutError):
            await image_move.locate_image(self.config, Buffer())

    async def test_cancel_does_not_produce_match_result(self):
        class Buffer:
            generation = 0

            def read_sample(self):
                return None, 0

        task = asyncio.create_task(image_move.locate_image(self.config, Buffer()))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()