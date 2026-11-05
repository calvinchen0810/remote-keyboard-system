import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import main


class CameraDevicesTests(unittest.TestCase):
    def test_enumeration_preserves_indices_and_all_device_names(self):
        cameras = [
            SimpleNamespace(index=0, name="OBS Virtual Camera"),
            SimpleNamespace(index=1, name="USB Camera"),
            SimpleNamespace(index=2, name="USB Camera"),
            SimpleNamespace(index=4, name="IR Camera"),
        ]
        with (
            patch.object(main.os, "name", "nt"),
            patch.object(main, "enumerate_cameras", return_value=cameras) as enumerate_mock,
        ):
            names = main._windows_camera_friendly_names(main.cv2.CAP_DSHOW)
        enumerate_mock.assert_called_once_with(main.cv2.CAP_DSHOW)
        self.assertEqual(names, {
            0: "OBS Virtual Camera", 1: "USB Camera", 2: "USB Camera", 4: "IR Camera",
        })

    def test_enumeration_failure_returns_no_unverified_names(self):
        with (
            patch.object(main.os, "name", "nt"),
            patch.object(main, "enumerate_cameras", side_effect=RuntimeError("unavailable")),
            self.assertLogs(main.logger, level="WARNING"),
        ):
            self.assertEqual(main._windows_camera_friendly_names(), {})

    def scan(self, devices, names, active=None):
        captures = {}
        for index, backend in devices.items():
            capture = Mock()
            capture.isOpened.return_value = True
            capture.get.side_effect = lambda prop, backend=backend: (
                backend if prop == main.cv2.CAP_PROP_BACKEND else 640
            )
            captures[index] = capture

        def open_camera(index):
            if index in captures:
                return captures[index]
            capture = Mock()
            capture.isOpened.return_value = False
            return capture

        def friendly_names(backend=main.cv2.CAP_DSHOW):
            return names.get(backend, {})

        with (
            patch.object(main, "_open_camera", side_effect=open_camera),
            patch.object(main, "_windows_camera_friendly_names", side_effect=friendly_names),
            patch.object(main, "camera", captures.get(active)),
            patch.object(main, "current_camera_index", active),
        ):
            result = asyncio.run(main.camera_devices())
        return result["devices"], captures

    def test_unavailable_index_does_not_shift_names(self):
        devices, _ = self.scan(
            {1: main.cv2.CAP_DSHOW},
            {main.cv2.CAP_DSHOW: {0: "Built-in Camera", 1: "USB Camera"}},
        )
        self.assertEqual(devices, [{"index": 1, "label": "USB Camera", "available": True}])

    def test_fallback_backend_uses_its_own_device_order(self):
        devices, _ = self.scan(
            {0: main.cv2.CAP_MSMF},
            {
                main.cv2.CAP_DSHOW: {0: "Built-in Camera"},
                main.cv2.CAP_MSMF: {0: "USB Camera"},
            },
        )
        self.assertEqual(devices[0]["label"], "USB Camera")

    def test_missing_name_uses_index_instead_of_guessing(self):
        devices, _ = self.scan(
            {1: main.cv2.CAP_DSHOW},
            {main.cv2.CAP_DSHOW: {0: "Built-in Camera"}},
        )
        self.assertEqual(devices[0]["label"], "Camera 1")

    def test_identical_device_names_keep_both_indices(self):
        devices, _ = self.scan(
            {0: main.cv2.CAP_DSHOW, 1: main.cv2.CAP_DSHOW},
            {main.cv2.CAP_DSHOW: {0: "USB Camera", 1: "USB Camera"}},
        )
        self.assertEqual([device["index"] for device in devices], [0, 1])
        self.assertEqual([device["label"] for device in devices], ["USB Camera", "USB Camera"])

    def test_active_camera_is_not_reopened_or_released(self):
        devices, captures = self.scan(
            {0: main.cv2.CAP_DSHOW},
            {main.cv2.CAP_DSHOW: {0: "USB Camera"}},
            active=0,
        )
        self.assertEqual(devices[0]["label"], "USB Camera")
        captures[0].release.assert_not_called()


if __name__ == "__main__":
    unittest.main()