import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

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
                                {"type": "kbd"},
                                {"type": "evt", "evt": "IMG.MATCH.door"}],
                 "else_steps": [{"type": "evt", "evt": "READY"}]},
                {"type": "do_while", "condition_id": "loop",
                 "repeat_when": "match", "steps": [{"type": "evt", "evt": "IMG.MATCH.body"}]},
                {"type": "evt", "evt": "EVT: IMG.MATCH.top"},
            ]
        })

        actions = list(main._iter_action_steps(request.steps))
        self.assertEqual([step.type for step in actions], ["evt", "kbd", "evt", "evt", "evt", "evt"])
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
            (main.condition_manager, "get", Mock(return_value=main.MatchCondition(
                condition_id="door", name="Door", snapshot_id="live-snapshot",
                roi=[0, 0, 1, 1],
            ))),
            (main.snapshot_pool, "get", Mock(return_value={"jpeg_b64": "live"})),
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

    async def test_submit_collects_and_activates_distinct_control_and_image_conditions(self):
        condition = main.MatchCondition(
            condition_id="door", name="Door", snapshot_id="live-snapshot",
            roi=[0, 0, 1, 1],
        )
        with patch.object(main.condition_manager, "get", return_value=condition), \
             patch.object(main.snapshot_pool, "get", return_value={"jpeg_b64": "live"}):
            await self.post({"steps": [
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [{"type": "evt", "evt": "IMG.MATCH.door"}],
                 "else_steps": []},
            ]})

        request = self.activate.await_args.args[0]
        self.assertEqual(request.condition_ids, ["door"])

    async def test_submit_rejects_missing_or_unusable_condition_before_enqueue(self):
        request = {"steps": [{
            "type": "if_else", "condition_id": "missing",
            "then_steps": [{"type": "kbd"}], "else_steps": [],
        }]}
        with patch.object(main.condition_manager, "get", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await self.post(request)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("Condition missing", str(raised.exception.detail))
        self.activate.assert_not_awaited()

        condition = main.MatchCondition(
            condition_id="door", name="Door", snapshot_id="deleted",
            roi=[0, 0, 1, 1],
        )
        request["steps"][0]["condition_id"] = "door"
        with patch.object(main.condition_manager, "get", return_value=condition), \
             patch.object(main.snapshot_pool, "get", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await self.post(request)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("Snapshot", str(raised.exception.detail))
        self.activate.assert_not_awaited()


class ScriptBundleImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_remaps_block_conditions_and_nested_image_events(self):
        request = main.ScriptBundleImportRequest.model_validate({
            "steps": [
                {"type": "if_else", "condition_id": "old-door",
                 "then_steps": [{"type": "kbd"}],
                 "else_steps": [{"type": "evt", "evt": "IMG.MATCH.old-image"},
                                {"type": "evt", "evt": "READY"}]},
                {"type": "do_while", "condition_id": "old-image",
                 "repeat_when": "match", "steps": [{"type": "evt", "evt": "IMG.MATCH.old-door"}]},
            ],
            "img_bundle": {
                "snapshots": [{"snapshot_id": "old-snapshot", "jpeg_b64": "c25hcA=="}],
                "conditions": [
                    {"condition_id": "old-door", "name": "Door", "snapshot_id": "old-snapshot",
                     "roi": [0, 0, 1, 1]},
                    {"condition_id": "old-image", "name": "Image", "snapshot_id": "old-snapshot",
                     "roi": [0, 0, 1, 1]},
                ],
            },
        })
        camera = Mock()
        camera.isOpened.return_value = True
        mapped = iter(["new-door", "new-image"])
        with patch.object(main, "camera", camera), \
             patch.object(main.snapshot_pool, "add_b64", return_value={"id": "new-snapshot"}), \
             patch.object(main.condition_manager, "add", side_effect=lambda **kwargs: next(mapped)), \
             patch.object(main.srv_ws, "broadcast", new_callable=AsyncMock):
            result = await main._import_script_bundle(request)

        self.assertEqual(result["steps"][0]["condition_id"], "new-door")
        self.assertEqual(result["steps"][0]["else_steps"][0]["evt"], "IMG.MATCH.new-image")
        self.assertEqual(result["steps"][0]["else_steps"][1]["evt"], "READY")
        self.assertEqual(result["steps"][1]["condition_id"], "new-image")
        self.assertEqual(result["steps"][1]["steps"][0]["evt"], "IMG.MATCH.new-door")

    async def test_import_rejects_references_absent_from_bundle(self):
        request = main.ScriptBundleImportRequest.model_validate({
            "steps": [{"type": "do_while", "condition_id": "missing",
                       "repeat_when": "match", "steps": [{"type": "kbd"}]}],
            "img_bundle": {"conditions": [], "snapshots": []},
        })
        with self.assertRaises(HTTPException) as raised:
            await main._import_script_bundle(request)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("missing", str(raised.exception.detail))

    async def test_auto_attach_finds_servos_inside_all_block_slots(self):
        request = main.MixedScriptRequest.model_validate({
            "servos": {"2": 18, "3": 19, "4": 20},
            "steps": [
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [{"type": "srv", "servo_id": 2}],
                 "else_steps": [{"type": "srv", "servo_id": 4}]},
                {"type": "do_while", "condition_id": "door",
                 "repeat_when": "match", "steps": [{"type": "srv", "servo_id": 3}]},
            ],
        })
        loop = Mock()
        loop.run_in_executor = AsyncMock(return_value=True)
        with patch.object(type(main.srv_serial), "is_connected",
                          new_callable=PropertyMock, return_value=True), \
             patch.object(main.srv_serial, "attached", {}), \
             patch("asyncio.get_event_loop", return_value=loop), \
             patch.object(main, "_await_script_resume", new_callable=AsyncMock):
            await main._auto_attach_servos(request)

        self.assertEqual(loop.run_in_executor.await_count, 3)


class MixedScriptExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = main.script_runtime.__class__()
        self.patches = [
            patch.object(main, "script_runtime", self.runtime),
            patch.object(main, "_await_script_resume", new_callable=AsyncMock),
            patch.object(main, "_notify_runtime", new_callable=AsyncMock),
            patch.object(main.kbm_ws, "broadcast", new_callable=AsyncMock),
            patch.object(main.srv_ws, "broadcast_sync", Mock()),
            patch.object(main, "_script_sleep", new_callable=AsyncMock),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def execute(self, payload, action_runner=None, evaluations=None):
        request = main.MixedScriptRequest.model_validate(payload)
        main._register_runtime_job(request)
        self.runtime.activate(request.job_id)
        run_action = action_runner or AsyncMock()
        with patch.object(main, "_run_action_step", run_action, create=True), \
             patch.object(main, "_evaluate_condition_fresh",
                          new_callable=AsyncMock,
                          side_effect=evaluations or [(0.95, True)]):
            await main._run_mixed_script(request)
        return request.job_id, run_action

    async def test_if_executes_only_the_selected_branch_and_records_decision(self):
        for matched, branch, expected_text in (
            (True, "then", "then"),
            (False, "else", "else"),
        ):
            with self.subTest(matched=matched):
                run_action = AsyncMock()
                job_id, _ = await self.execute({
                    "steps": [{
                        "type": "if_else", "condition_id": "door",
                        "then_steps": [{"type": "kbd", "text": "then"}],
                        "else_steps": [{"type": "kbd", "text": "else"}],
                    }]
                }, run_action, [(0.95, matched)])

                self.assertEqual(run_action.await_count, 1)
                kwargs = run_action.await_args.kwargs
                self.assertEqual(run_action.await_args.args[0].text, expected_text)
                self.assertEqual(kwargs["slot"], f"{branch}_steps")
                item = self.runtime.detail(job_id)["steps"][0]
                self.assertEqual(item["control"]["branch"], branch)
                self.assertIs(item["control"]["condition_match"], matched)

    async def test_do_while_runs_once_then_repeats_using_each_repeat_mode(self):
        for mode, conditions, max_iterations, expected_iterations in (
            ("match", [True, False], 2, 2),
            ("not_match", [False, True], 2, 2),
            ("match", [False], 2, 1),
            ("match", [False], 1, 1),
        ):
            with self.subTest(mode=mode, conditions=conditions,
                              max_iterations=max_iterations):
                run_action = AsyncMock()
                job_id, _ = await self.execute({
                    "steps": [{
                        "type": "do_while", "condition_id": "door",
                        "repeat_when": mode, "max_iterations": max_iterations,
                        "steps": [{"type": "kbd", "text": "body"}],
                    }]
                }, run_action, [(0.95, match) for match in conditions])

                self.assertEqual(run_action.await_count, expected_iterations)
                self.assertTrue(all(call.kwargs["slot"] == "steps"
                                    for call in run_action.await_args_list))
                item = self.runtime.detail(job_id)["steps"][0]
                self.assertEqual(item["control"]["iteration"], expected_iterations)
                self.assertEqual(item["control"]["iteration_limit"], max_iterations)

    async def test_each_do_while_iteration_and_condition_change_is_published(self):
        run_action = AsyncMock()
        await self.execute({
            "steps": [{
                "type": "do_while", "condition_id": "door",
                "repeat_when": "match", "max_iterations": 2,
                "steps": [{"type": "kbd", "text": "body"}],
            }]
        }, run_action, [(0.95, True), (0.95, False)])

        self.assertEqual(run_action.await_count, 2)
        main._notify_runtime.assert_awaited()
        self.assertEqual(main._notify_runtime.await_count, 8)

    async def test_iteration_limit_failure_stops_before_later_actions(self):
        run_action = AsyncMock()
        request = {
            "steps": [
                {"type": "do_while", "condition_id": "door",
                 "repeat_when": "match", "max_iterations": 1,
                 "steps": [{"type": "kbd", "text": "body"}]},
                {"type": "kbd", "text": "must not run"},
            ]
        }
        with self.assertRaisesRegex(RuntimeError, r"iteration limit reached \(1\)"):
            await self.execute(request, run_action, [(0.95, True)])

        self.assertEqual(run_action.await_count, 1)
        self.assertEqual(run_action.await_args.args[0].text, "body")

    async def test_all_ordinary_action_types_run_as_children_with_shared_ocr_variables(self):
        def make_request():
            template = main.np.random.default_rng(42).integers(
                0, 256, (16, 20, 3), dtype=main.np.uint8)
            _, encoded = main.cv2.imencode(".png", template)
            template_b64 = base64.b64encode(encoded).decode()
            return main.MixedScriptRequest.model_validate({
                "steps": [{
                    "type": "if_else", "condition_id": "door",
                    "then_steps": [
                        {"type": "evt", "evt": "READY", "delay_ms": 5},
                        {"type": "ocr", "roi": [0, 0, 1, 1], "variable": "digits"},
                        {"type": "kbd", "cmd_type": "TYPE", "text": "${digits}"},
                        {"type": "srv", "servo_id": 1, "angle": 45},
                        {"type": "mse", "action": "MOVE", "x": 4, "y": 8},
                        {"type": "mse", "action": "IMAGE_MOVE", "image_move": {
                            "template_b64": template_b64,
                            "source_width": 200, "source_height": 100}},
                    ],
                    "else_steps": [],
                }]
            })

        request = await asyncio.to_thread(make_request)
        main._register_runtime_job(request)
        self.runtime.activate(request.job_id)
        servo_send = Mock(return_value=True)
        image_result = main.ImageMatch(
            box=[50, 30, 20, 16], center=[59.5, 37.5], hid=[9797, 12412],
            score=1, frame_width=200, frame_height=100, preview_b64="preview")
        with patch.object(main, "_evaluate_condition_fresh",
                          new_callable=AsyncMock, return_value=(0.95, True)), \
             patch.object(main, "_wait_for_step_event",
                          new_callable=AsyncMock, return_value=True) as wait_event, \
             patch.object(main, "read_stable_digits",
                          new_callable=AsyncMock, return_value="0042"), \
             patch.object(main, "_run_image_move",
                          new_callable=AsyncMock, return_value=image_result) as image_move, \
             patch.object(main, "_send_kbm_command",
                          new_callable=AsyncMock, return_value="OK") as send_key, \
             patch.object(main, "_send_mouse_command",
                          new_callable=AsyncMock) as send_mouse, \
             patch.object(main.srv_serial, "send_command", servo_send):
            await main._run_mixed_script(request)

        self.assertEqual(wait_event.await_args.args[1:], (0, 1))
        send_key.assert_awaited_once_with("TYPE:0042")
        send_mouse.assert_awaited_once_with("MOUSE:MOVE 4 8")
        servo_send.assert_called_once()
        image_move.assert_awaited_once()
        item = self.runtime.detail(request.job_id)["steps"][0]
        self.assertEqual(
            [child["state"] for child in item["children"]["then_steps"]],
            ["completed"] * 6)
        self.assertEqual(item["children"]["then_steps"][1]["result"]["value"], "0042")
        self.assertNotIn("preview_b64", item["children"]["then_steps"][5]["result"])

    async def test_failed_child_status_names_slot_and_index_in_parent_error(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [{
                "type": "if_else", "condition_id": "door",
                "then_steps": [{"type": "kbd", "cmd": "KEY:A"},
                               {"type": "kbd", "cmd": "KEY:B"}],
                "else_steps": [],
            }]
        })
        main._register_runtime_job(request)
        self.runtime.activate(request.job_id)
        with patch.object(main, "_evaluate_condition_fresh",
                          new_callable=AsyncMock, return_value=(0.95, True)), \
             patch.object(main, "_send_kbm_command",
                          new_callable=AsyncMock, side_effect=["BUSY", "OK"]):
            await main._run_mixed_script(request)

        detail = self.runtime.detail(request.job_id)
        item = detail["steps"][0]
        self.assertEqual(item["children"]["then_steps"][0]["state"], "failed")
        self.assertEqual(item["children"]["then_steps"][0]["error"],
                         "then_steps[0]: KBD ACK: BUSY")
        self.assertEqual(item["error"], "then_steps[0]: KBD ACK: BUSY")
        self.assertEqual(item["children"]["then_steps"][1]["state"], "completed")

    async def test_paused_block_does_not_start_its_next_action_before_resume(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [{
                "type": "if_else", "condition_id": "door",
                "then_steps": [{"type": "kbd", "cmd": "KEY:A"},
                               {"type": "kbd", "cmd": "KEY:B"}],
                "else_steps": [],
            }]
        })
        main._register_runtime_job(request)
        self.runtime.activate(request.job_id)
        first_action_done = asyncio.Event()
        resume = asyncio.Event()
        actions = []

        async def wait_for_resume():
            if first_action_done.is_set():
                await resume.wait()

        async def record_action(step, **kwargs):
            actions.append(step.cmd)
            if len(actions) == 1:
                first_action_done.set()

        self.patches[1].stop()
        pause_patch = patch.object(main, "_await_script_resume", side_effect=wait_for_resume)
        pause_patch.start()
        self.addCleanup(pause_patch.stop)
        with patch.object(main, "_evaluate_condition_fresh",
                          new_callable=AsyncMock, return_value=(0.95, True)), \
             patch.object(main, "_run_action_step", side_effect=record_action, create=True):
            task = asyncio.create_task(main._run_mixed_script(request))
            await asyncio.wait_for(first_action_done.wait(), timeout=1)
            await asyncio.sleep(0)
            self.assertEqual(actions, ["KEY:A"])
            resume.set()
            await asyncio.wait_for(task, timeout=1)

        self.assertEqual(actions, ["KEY:A", "KEY:B"])

    async def test_runner_retains_iteration_limit_failure_as_latest_job(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [
                {"type": "do_while", "condition_id": "door",
                 "repeat_when": "match", "max_iterations": 1,
                 "steps": [{"type": "kbd", "cmd": "KEY:A"}]},
                {"type": "kbd", "cmd": "KEY:B"},
            ]
        })
        main._register_runtime_job(request)
        queue = asyncio.Queue()
        await queue.put(request)
        action = AsyncMock()
        with patch.object(main, "_script_queue", queue), \
             patch.object(main, "_auto_attach_servos", new_callable=AsyncMock), \
             patch.object(main.event_bus, "clear_visual", new_callable=AsyncMock), \
             patch.object(main.condition_manager, "activate_only", Mock()), \
             patch.object(main, "_run_action_step", action, create=True), \
             patch.object(main, "_evaluate_condition_fresh",
                          new_callable=AsyncMock, return_value=(0.95, True)), \
             patch.object(main.srv_ws, "broadcast", new_callable=AsyncMock):
            await main._script_runner()

        failure = self.runtime.detail(request.job_id)
        self.assertEqual(failure["phase"], "failed")
        self.assertIn("iteration limit reached (1)", failure["error"])
        self.assertEqual(self.runtime.snapshot()["last_failure"]["job_id"], request.job_id)
        self.assertEqual(action.await_count, 1)

    async def test_runner_retains_error_when_failure_precedes_activation(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [{"type": "kbd", "cmd": "KEY:A"}]
        })
        main._register_runtime_job(request)
        queue = asyncio.Queue()
        await queue.put(request)
        with patch.object(main, "_script_queue", queue), \
             patch.object(main.event_bus, "clear_visual",
                          new_callable=AsyncMock,
                          side_effect=[RuntimeError("visual reset failed"), None]), \
             patch.object(main.condition_manager, "activate_only", Mock()), \
             patch.object(main.srv_ws, "broadcast", new_callable=AsyncMock):
            await main._script_runner()

        failure = self.runtime.detail(request.job_id)
        self.assertEqual(failure["phase"], "failed")
        self.assertEqual(failure["error"], "visual reset failed")
        self.assertEqual(self.runtime.snapshot()["last_failure"]["job_id"], request.job_id)

    async def test_active_block_keeps_fifo_runner_ahead_of_queued_job(self):
        first = main.MixedScriptRequest.model_validate({
            "steps": [{
                "type": "if_else", "condition_id": "door",
                "then_steps": [{"type": "kbd", "cmd": "KEY:first"}],
                "else_steps": [],
            }]
        })
        second = main.MixedScriptRequest.model_validate({
            "steps": [{"type": "kbd", "cmd": "KEY:second"}]
        })
        main._register_runtime_job(first)
        main._register_runtime_job(second)
        queue = asyncio.Queue()
        await queue.put(first)
        await queue.put(second)
        block_started = asyncio.Event()
        release_block = asyncio.Event()
        actions = []

        async def run_action(step, **kwargs):
            actions.append(step.cmd)
            if step.cmd == "KEY:first":
                block_started.set()
                await release_block.wait()

        with patch.object(main, "_script_queue", queue), \
             patch.object(main, "_auto_attach_servos", new_callable=AsyncMock), \
             patch.object(main.event_bus, "clear_visual", new_callable=AsyncMock), \
             patch.object(main.condition_manager, "activate_only", Mock()), \
             patch.object(main, "_run_action_step", side_effect=run_action, create=True), \
             patch.object(main, "_evaluate_condition_fresh",
                          new_callable=AsyncMock, return_value=(0.95, True)), \
             patch.object(main.srv_ws, "broadcast", new_callable=AsyncMock):
            runner = asyncio.create_task(main._script_runner())
            await asyncio.wait_for(block_started.wait(), timeout=1)
            await asyncio.sleep(0)
            self.assertEqual(actions, ["KEY:first"])
            release_block.set()
            await asyncio.wait_for(runner, timeout=1)

        self.assertEqual(actions, ["KEY:first", "KEY:second"])
        self.assertIsNone(self.runtime.snapshot()["active"])


if __name__ == "__main__":
    unittest.main()
