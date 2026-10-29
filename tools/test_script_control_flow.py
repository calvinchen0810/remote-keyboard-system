import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import main


class SharedConditionMatcherTests(unittest.TestCase):
    def setUp(self):
        self.frame = main.np.random.default_rng(42).integers(
            0, 256, (32, 32, 3), dtype=main.np.uint8)
        ok, encoded = main.cv2.imencode(".jpg", self.frame)
        self.assertTrue(ok)
        self.snapshot = {"jpeg_b64": base64.b64encode(encoded).decode()}
        self.frame = main.cv2.imdecode(encoded, main.cv2.IMREAD_COLOR)
        self.condition = main.MatchCondition(
            condition_id="door", name="Door", snapshot_id="snapshot",
            roi=[0.0, 0.0, 1.0, 1.0], threshold=0.9, min_hits=10,
        )

    def test_matcher_returns_score_and_match_for_matching_roi(self):
        with patch.object(main.snapshot_pool, "get", return_value=self.snapshot):
            score, matched = main._match_condition_frame(self.condition, self.frame)

        self.assertGreaterEqual(score, 0.99)
        self.assertTrue(matched)

    def test_matcher_returns_non_match_below_threshold(self):
        other_frame = main.np.random.default_rng(43).integers(
            0, 256, (32, 32, 3), dtype=main.np.uint8)
        with patch.object(main.snapshot_pool, "get", return_value=self.snapshot):
            score, matched = main._match_condition_frame(self.condition, other_frame)

        self.assertLess(score, self.condition.threshold)
        self.assertFalse(matched)

    def test_matcher_reports_missing_snapshot_and_invalid_frame(self):
        with patch.object(main.snapshot_pool, "get", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Snapshot.*door.*unavailable"):
                main._match_condition_frame(self.condition, self.frame)

        with patch.object(main.snapshot_pool, "get", return_value=self.snapshot):
            with self.assertRaisesRegex(ValueError, "invalid frame"):
                main._match_condition_frame(self.condition, None)


class FreshConditionFrameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.frame = main.np.random.default_rng(42).integers(
            0, 256, (32, 32, 3), dtype=main.np.uint8)
        ok, encoded = main.cv2.imencode(".jpg", self.frame)
        self.assertTrue(ok)
        self.snapshot = {"jpeg_b64": base64.b64encode(encoded).decode()}
        self.frame = main.cv2.imdecode(encoded, main.cv2.IMREAD_COLOR)
        self.condition = main.MatchCondition(
            condition_id="door", name="Door", snapshot_id="snapshot",
            roi=[0.0, 0.0, 1.0, 1.0], threshold=0.9, min_hits=10,
        )

    async def test_evaluation_waits_for_a_new_frame_and_ignores_min_hits(self):
        class SampleBuffer:
            generation = 10

            def __init__(self):
                self.samples = iter([(self_frame, 10), (self_frame, 11)])

            def read_sample(self):
                try:
                    return next(self.samples)
                except StopIteration:
                    return self_frame, 11

        self_frame = self.frame
        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.snapshot_pool, "get", return_value=self.snapshot), \
             patch.object(main, "frame_buffer", SampleBuffer()):
            score, matched = await main._evaluate_condition_fresh(
                "door", timeout_seconds=0.1)

        self.assertGreaterEqual(score, 0.99)
        self.assertTrue(matched)

    async def test_evaluation_returns_non_match_for_a_new_frame(self):
        other_frame = main.np.random.default_rng(43).integers(
            0, 256, (32, 32, 3), dtype=main.np.uint8)

        class NewFrameBuffer:
            generation = 10

            def read_sample(self):
                return other_frame, 11

        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.snapshot_pool, "get", return_value=self.snapshot), \
             patch.object(main, "frame_buffer", NewFrameBuffer()):
            score, matched = await main._evaluate_condition_fresh(
                "door", timeout_seconds=0.1)

        self.assertLess(score, self.condition.threshold)
        self.assertFalse(matched)

    async def test_evaluation_times_out_when_no_new_frame_arrives(self):
        class StalledBuffer:
            generation = 7

            def read_sample(self):
                return self_frame, 7

        self_frame = self.frame
        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.snapshot_pool, "get", return_value=self.snapshot), \
             patch.object(main, "frame_buffer", StalledBuffer()):
            with self.assertRaisesRegex(TimeoutError, "No fresh camera frame.*door"):
                await main._evaluate_condition_fresh("door", timeout_seconds=0.01)

    async def test_evaluation_reports_missing_condition_or_snapshot(self):
        with patch.object(main.condition_manager, "get", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Condition door is unavailable"):
                await main._evaluate_condition_fresh("door", timeout_seconds=0.01)

        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.snapshot_pool, "get", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Condition door is unavailable"):
                await main._evaluate_condition_fresh("door", timeout_seconds=0.01)

    async def test_evaluation_wait_is_pause_aware_and_propagates_stop_cancellation(self):
        class NewFrameBuffer:
            generation = 2

            def read_sample(self):
                return self_frame, 3

        self_frame = self.frame
        resume = asyncio.Event()
        paused = {"reason": "manual"}
        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.snapshot_pool, "get", return_value=self.snapshot), \
             patch.object(main, "frame_buffer", NewFrameBuffer()), \
             patch.object(main, "_script_pause", paused), \
             patch.object(main, "_script_resume_event", resume):
            task = asyncio.create_task(main._evaluate_condition_fresh(
                "door", timeout_seconds=0.01))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            await asyncio.sleep(0.02)
            resume.set()
            main._script_pause = None
            score, matched = await task
            self.assertGreaterEqual(score, 0.99)
            self.assertTrue(matched)

        class StalledBuffer:
            generation = 4

            def read_sample(self):
                return self_frame, 4

        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.snapshot_pool, "get", return_value=self.snapshot), \
             patch.object(main, "frame_buffer", StalledBuffer()):
            task = asyncio.create_task(main._evaluate_condition_fresh(
                "door", timeout_seconds=1))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class ManualConditionTestEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        frame = main.np.random.default_rng(42).integers(
            0, 256, (32, 32, 3), dtype=main.np.uint8)
        ok, encoded = main.cv2.imencode(".jpg", frame)
        self.assertTrue(ok)
        self.frame = main.cv2.imdecode(encoded, main.cv2.IMREAD_COLOR)
        self.snapshot = {"jpeg_b64": base64.b64encode(encoded).decode()}
        self.condition = main.MatchCondition(
            condition_id="door", name="Door", snapshot_id="snapshot",
            roi=[0.0, 0.0, 1.0, 1.0], threshold=0.9,
        )

    async def test_success_keeps_the_existing_response_shape(self):
        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.snapshot_pool, "get", return_value=self.snapshot), \
             patch.object(main.frame_buffer, "read", return_value=self.frame):
            result = await main.visual_test_condition("door")

        self.assertEqual(set(result), {"score", "matched", "threshold"})
        self.assertEqual(result["score"], 1.0)
        self.assertTrue(result["matched"])
        self.assertEqual(result["threshold"], self.condition.threshold)

    async def test_missing_condition_frame_and_snapshot_keep_existing_http_errors(self):
        with patch.object(main.condition_manager, "get", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await main.visual_test_condition("door")
        self.assertEqual((raised.exception.status_code, raised.exception.detail),
                         (404, "Condition not found"))

        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.frame_buffer, "read", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await main.visual_test_condition("door")
        self.assertEqual((raised.exception.status_code, raised.exception.detail),
                         (503, "Camera not available"))

        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.frame_buffer, "read", return_value=self.frame), \
             patch.object(main.snapshot_pool, "get", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await main.visual_test_condition("door")
        self.assertEqual(
            (raised.exception.status_code, raised.exception.detail),
            (404, "Snapshot for this condition no longer in pool"))

    async def test_matcher_errors_keep_the_existing_http_500_prefix(self):
        with patch.object(main.condition_manager, "get", return_value=self.condition), \
             patch.object(main.frame_buffer, "read", return_value=self.frame), \
             patch.object(main.snapshot_pool, "get", return_value=self.snapshot), \
             patch.object(main, "_match_condition_frame", side_effect=ValueError("broken")):
            with self.assertRaises(HTTPException) as raised:
                await main.visual_test_condition("door")

        self.assertEqual((raised.exception.status_code, raised.exception.detail),
                         (500, "Match failed: broken"))


class ScriptControlFlowValidationTests(unittest.TestCase):
    def test_control_nodes_validate_and_keep_action_children(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [{"type": "kbd", "cmd_type": "KEY", "key": "ENTER"}],
                 "else_steps": [{"type": "evt", "evt": "READY"}]},
                {"type": "do_while", "condition_id": "door", "repeat_when": "not_match",
                 "max_iterations": 10, "steps": [{"type": "mse", "action": "CLICK"}]},
            ]
        })

        self.assertIsInstance(request.steps[0], main.IfElseBlock)
        self.assertIsInstance(request.steps[1], main.DoWhileBlock)
        self.assertEqual(request.steps[0].then_steps[0].type, "kbd")
        self.assertEqual(request.steps[1].repeat_when, "not_match")

    def test_control_nodes_use_defaults_and_reject_unknown_types(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [{"type": "do_while", "condition_id": "door",
                       "repeat_when": "match", "steps": [{"type": "kbd"}]}]
        })
        self.assertEqual(request.steps[0].max_iterations, 10)

        with self.assertRaises(ValidationError):
            main.MixedScriptRequest.model_validate({"steps": [{"type": "unknown"}]})

    def test_nested_empty_invalid_and_over_limit_blocks_are_rejected(self):
        action = {"type": "kbd"}
        invalid_payloads = [
            {"steps": [{"type": "if_else", "condition_id": "door",
                        "then_steps": [{"type": "if_else", "condition_id": "inner",
                                        "then_steps": [action], "else_steps": []}],
                        "else_steps": []}]},
            {"steps": [{"type": "if_else", "condition_id": "door",
                        "then_steps": [], "else_steps": []}]},
            {"steps": [{"type": "do_while", "condition_id": "door",
                        "repeat_when": "match", "max_iterations": 10, "steps": []}]},
            {"steps": [{"type": "do_while", "condition_id": "door",
                        "repeat_when": "sometimes", "max_iterations": 10, "steps": [action]}]},
            {"steps": [{"type": "do_while", "condition_id": "door",
                        "repeat_when": "match", "max_iterations": 10001, "steps": [action]}]},
            {"steps": [{"type": "do_while", "condition_id": "door",
                        "repeat_when": "match", "max_iterations": 0, "steps": [action]}]},
            {"steps": [{"type": "do_while", "condition_id": "door",
                        "repeat_when": "match", "max_iterations": 1.5, "steps": [action]}]},
            {"steps": [{"type": "do_while", "condition_id": "door",
                        "repeat_when": "match", "max_iterations": True, "steps": [action]}]},
            {"steps": [{"type": "if_else", "then_steps": [action], "else_steps": []}]},
            {"steps": [{"type": "if_else", "condition_id": "door",
                        "then_steps": [action] * 201, "else_steps": []}]},
            {"steps": [action] * 100 + [
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [action] * 101, "else_steps": []}
            ]},
            {"steps": [action] * 201},
            {"steps": [{"type": "unknown"}]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                main.MixedScriptRequest.model_validate(payload)

    def test_bundle_request_enforces_aggregate_action_limit(self):
        with self.assertRaises(ValidationError):
            main.ScriptBundleImportRequest.model_validate({
                "steps": [{"type": "if_else", "condition_id": "door",
                           "then_steps": [{"type": "kbd"}] * 101, "else_steps": []},
                          {"type": "do_while", "condition_id": "door",
                           "repeat_when": "match", "steps": [{"type": "kbd"}] * 100}],
                "img_bundle": {},
            })

    def test_action_and_condition_helpers_traverse_control_slots(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [{"type": "evt", "evt": "IMG.MATCH.then"},
                                {"type": "kbd"}],
                 "else_steps": [{"type": "evt", "evt": "READY"}]},
                {"type": "do_while", "condition_id": "loop",
                 "repeat_when": "match", "steps": [{"type": "evt", "evt": "IMG.MATCH.body"}]},
                {"type": "evt", "evt": "IMG.MATCH.top"},
            ]
        })

        actions = list(main._iter_action_steps(request.steps))
        self.assertEqual([step.type for step in actions], ["evt", "kbd", "evt", "evt", "evt"])
        self.assertEqual(main._collect_condition_ids(request.steps),
                         ["door", "then", "loop", "body", "top"])

    def test_normalization_preserves_flat_behavior_and_normalizes_control_children(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [
                {"type": "kbd", "evt": " legacy "},
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [{"type": "kbd", "evt": " inner "}],
                 "else_steps": [{"type": "evt", "evt": " READY "}]},
            ]
        })

        normalized = main._normalize_script_request(request)
        self.assertEqual([step.type for step in normalized.steps], ["evt", "kbd", "if_else"])
        self.assertEqual(normalized.steps[0].evt, "legacy")
        self.assertIsNone(normalized.steps[1].evt)
        block = normalized.steps[2]
        self.assertEqual(block.then_steps[0].type, "evt")
        self.assertEqual(block.then_steps[0].evt, "inner")
        self.assertIsNone(block.then_steps[1].evt)
        self.assertEqual(block.else_steps[0].evt, "READY")

    def test_normalization_rejects_more_than_200_aggregate_actions(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [{"type": "kbd", "evt": "READY"}] * 100 + [
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [{"type": "kbd", "evt": "READY"}],
                 "else_steps": []},
            ]
        })

        with self.assertRaisesRegex(ValueError, "at most 200 action steps"):
            main._normalize_script_request(request)

    def test_normalization_accepts_exactly_200_aggregate_actions(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [{"type": "kbd", "evt": "READY"}] * 100
        })

        normalized = main._normalize_script_request(request)

        self.assertEqual(len(list(main._iter_action_steps(normalized.steps))), 200)


class ScriptControlFlowSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.activate = AsyncMock(return_value=1)
        for owner, attribute, replacement in [
            (main, "_activate_script", self.activate),
            (main, "_notify_pool_snapshot", AsyncMock()),
            (main, "_script_control_lock", asyncio.Lock()),
            (main, "_script_queue", asyncio.Queue()),
            (main, "_script_runner_task", None),
            (main.event_bus, "clear", AsyncMock()),
        ]:
            patcher = patch.object(owner, attribute, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def payload(self, slot, count):
        actions = [{"type": "kbd", "evt": "READY"}] * count
        if slot == "top":
            steps = actions
        elif slot == "body":
            steps = [{"type": "do_while", "condition_id": "door",
                      "repeat_when": "match", "steps": actions}]
        else:
            steps = [{"type": "if_else", "condition_id": "door",
                      "then_steps": actions if slot == "then" else [],
                      "else_steps": actions if slot == "else" else []}]
        return {"steps": steps}

    async def post(self, payload):
        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(),
                    "more_body": False}

        return await main.script_run(Request({
            "type": "http", "method": "POST", "path": "/script/run",
            "headers": [(b"x-rks-source", b"script-editor")],
        }, receive))

    async def test_expanded_scripts_over_200_return_422_before_enqueue(self):
        for slot in ("top", "then", "else", "body"):
            with self.subTest(slot=slot):
                with self.assertRaises(HTTPException) as raised:
                    await self.post(self.payload(slot, 101))
                self.assertEqual(raised.exception.status_code, 422)
                self.assertIn("at most 200 action steps", str(raised.exception.detail))
                self.activate.assert_not_awaited()
                main.event_bus.clear.assert_not_awaited()
                self.assertTrue(main._script_queue.empty())

    async def test_expanded_scripts_with_exactly_200_actions_can_enqueue(self):
        for slot in ("top", "then", "else", "body"):
            with self.subTest(slot=slot):
                self.activate.reset_mock()
                result = await self.post(self.payload(slot, 100))
                self.assertTrue(result["ok"])
                self.activate.assert_awaited_once()
                request = self.activate.await_args.args[0]
                self.assertEqual(len(list(main._iter_action_steps(request.steps))), 200)


if __name__ == "__main__":
    unittest.main()
