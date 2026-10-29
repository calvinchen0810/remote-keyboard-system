import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import ocr_digits


class OcrTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_digits_preserve_zeroes_and_reject_extra_content(self):
        self.assertEqual(ocr_digits.parse_digits(" 0037\n"), "0037")
        self.assertEqual(ocr_digits.parse_digits("8462"), "8462")
        for text in ["8462 + ENTER", "846", "84622", "84O2", "84 62", "８４６２", ""]:
            with self.subTest(text=text):
                self.assertIsNone(ocr_digits.parse_digits(text))

    def test_roi_must_be_finite_positive_and_inside_frame(self):
        for roi in [[], [0, 0, 1], [0, 0, 0, 1], [-0.1, 0, 1, 1], [0.9, 0, 0.2, 1], [0, 0, float("nan"), 1]]:
            with self.subTest(roi=roi), self.assertRaises(ValueError):
                ocr_digits.OcrConfig(roi=roi)
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        crop = ocr_digits.crop_roi(image, [0.1, 0.2, 0.3, 0.4])
        self.assertEqual(crop.shape, (40, 60, 3))

    def test_variables_are_literal_digit_strings_not_expressions(self):
        self.assertEqual(ocr_digits.expand_variables("PIN ${digits}", {"digits": "0037"}), "PIN 0037")
        self.assertEqual(ocr_digits.expand_variables("ordinary", {}), "ordinary")
        for text in ["${missing}", "${digits + 1}", "${}", "${digits"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                ocr_digits.expand_variables(text, {"digits": "0037"})

    def test_tesseract_wrapper_prepares_image_and_limits_digits(self):
        frame = np.full((40, 120, 3), (220, 170, 0), dtype=np.uint8)
        with patch.object(ocr_digits, "ensure_available", return_value="tesseract"), \
             patch("pytesseract.image_to_string", return_value="0037\n") as recognize:
            self.assertEqual(ocr_digits.recognize_digits(frame, [0, 0, 1, 1]), "0037")
        prepared = recognize.call_args.args[0]
        self.assertEqual(prepared.ndim, 2)
        self.assertGreater(prepared.shape[0], frame.shape[0])
        self.assertIn("tessedit_char_whitelist=0123456789", recognize.call_args.kwargs["config"])
        self.assertEqual(recognize.call_args.kwargs["timeout"], 1.5)

    def test_tesseract_timeout_is_not_a_valid_digit_result(self):
        frame = np.zeros((20, 80, 3), dtype=np.uint8)
        with patch.object(ocr_digits, "ensure_available", return_value="tesseract"), \
             patch("pytesseract.image_to_string", side_effect=RuntimeError("timeout")):
            self.assertIsNone(ocr_digits.recognize_digits(frame, [0, 0, 1, 1]))

    async def test_only_distinct_post_start_frames_count_and_mismatch_resets(self):
        class Buffer:
            generation = 10

            def __init__(self):
                self.samples = iter([10, 11, 11, 12, 13, 14, 15])

            def read_sample(self):
                generation = next(self.samples)
                return np.full((8, 8, 3), generation, dtype=np.uint8), generation

        recognized = []

        def recognize(frame, roi, timeout):
            generation = int(frame[0, 0, 0])
            recognized.append(generation)
            return {11: "8462", 12: None, 13: "8462", 14: "0037", 15: "0037"}[generation]

        config = ocr_digits.OcrConfig(roi=[0, 0, 1, 1], timeout_ms=1000)
        with patch.object(ocr_digits, "recognize_digits", side_effect=recognize):
            result = await ocr_digits.read_stable_digits(config, Buffer())
        self.assertEqual(result, "0037")
        self.assertEqual(recognized, [11, 12, 13, 14, 15])

    async def test_stalled_camera_times_out_without_recognition(self):
        class Buffer:
            generation = 1

            def read_sample(self):
                return np.zeros((8, 8, 3), dtype=np.uint8), 1

        config = ocr_digits.OcrConfig(roi=[0, 0, 1, 1], timeout_ms=100)
        with patch.object(ocr_digits, "recognize_digits") as recognize:
            with self.assertRaises(TimeoutError):
                await ocr_digits.read_stable_digits(config, Buffer())
        recognize.assert_not_called()

    async def test_cancellation_does_not_return_a_result(self):
        class Buffer:
            generation = 0

            def read_sample(self):
                return None, 0

        task = asyncio.create_task(ocr_digits.read_stable_digits(ocr_digits.OcrConfig(roi=[0, 0, 1, 1]), Buffer()))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()