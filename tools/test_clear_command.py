import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import main
from script_runtime import ScriptRuntime


class ClearCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_auto_attach = main._auto_attach_servos
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_patch = patch.object(main, "_data_dir", return_value=self.temp_dir.name)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.snapshots = main.SnapshotPool()
        self.conditions = main.ConditionManager()
        self.events = main.EventBus()
        self.pending = main.PendingScriptPool()
        self.queue = asyncio.Queue()
        self.srv_broadcast = AsyncMock()
        self.kbm_broadcast = AsyncMock()
        replacements = {
            "snapshot_pool": self.snapshots,
            "condition_manager": self.conditions,
            "event_bus": self.events,
            "pending_evt_pool": self.pending,
            "_script_queue": self.queue,
            "_script_runner_task": None,
            "_script_control_lock": asyncio.Lock(),
            "_script_pause": None,
            "_script_resume_event": None,
            "_script_pause_generation": 0,
        }
        self.state_patch = patch.multiple(main, **replacements)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        wait_patch = patch.object(main, "_script_wait", None, create=True)
        wait_patch.start()
        self.addCleanup(wait_patch.stop)
        runtime_patch = patch.object(main, "script_runtime", ScriptRuntime(), create=True)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        for owner, attribute, replacement in [
            (main.srv_ws, "broadcast", self.srv_broadcast),
            (main.srv_ws, "broadcast_sync", Mock()),
            (main.kbm_ws, "broadcast", self.kbm_broadcast),
            (main.srv_serial, "send", Mock()),
            (main, "_auto_attach_servos", AsyncMock()),
        ]:
            patcher = patch.object(owner, attribute, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def post(self, payload, headers=None):
        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        return await main.script_run(Request({"type": "http", "method": "POST", "path": "/script/run",
            "headers": headers or []}, receive))

    async def api_post(self, path, payload, method="POST"):
        messages = []

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        async def send(message):
            messages.append(message)

        await main.app({"type": "http", "http_version": "1.1", "method": method,
                        "scheme": "http", "path": path, "query_string": b"",
                        "headers": [(b"content-type", b"application/json")],
                        "server": ("test", 80), "client": ("test", 1234)}, receive, send)
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return status, json.loads(body)

    def clear_payload(self):
        return {"command": "clear", "targets": ["snapshots", "conditions", "scripts"]}

    async def seed(self):
        self.snapshots.add(main.np.zeros((8, 8, 3), dtype=main.np.uint8))
        snapshot_id = self.snapshots.list_all()[0]["id"]
        self.conditions.add("test", snapshot_id, [0, 0, 1, 1], 0.92, 3, 3000)
        request = main.MixedScriptRequest(steps=[main.MixedStep(type="evt", evt="WAIT", delay_ms=0)])
        await self.pending.hold(request)
        await self.queue.put(request)
        await self.events.emit("OLD")

    async def test_clear_cancels_runner_and_clears_every_pool_durably(self):
        await self.seed()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def running_script():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(running_script())
        main._script_runner_task = task
        self.addAsyncCleanup(self.cancel_task, task)
        await started.wait()
        response = await self.post(self.clear_payload())
        self.assertTrue(response["ok"])
        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.done())
        self.assertEqual(self.snapshots.list_all(), [])
        self.assertEqual(self.conditions.list_all(), [])
        self.assertEqual(self.conditions._runtime, {})
        self.assertEqual(json.loads((Path(self.temp_dir.name) / "conditions.json").read_text()), [])
        self.assertEqual((await main.script_status())["queued"], 0)
        self.assertFalse((await main.script_status())["running"])
        self.assertEqual((await self.pending.snapshot())["total"], 0)
        self.assertIsNone(await self.events.consume_if_available("OLD"))
        self.assertEqual(response["cleared"]["snapshots"], 1)
        self.assertEqual(response["cleared"]["conditions"], 1)
        self.assertEqual(response["cleared"]["queued_scripts"], 1)
        self.assertEqual(response["cleared"]["pending_scripts"], 1)
        main.srv_serial.send.assert_not_called()
        reset = next(call.args[0] for call in self.srv_broadcast.call_args_list
                     if call.args[0].get("type") == "workspace_cleared")
        self.assertEqual(set(reset["targets"]), {"snapshots", "conditions", "scripts"})
        await asyncio.wait_for(self.queue.join(), timeout=0.2)

    async def cancel_task(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_clear_is_idempotent(self):
        first = await self.post(self.clear_payload())
        second = await self.post(self.clear_payload())
        self.assertTrue(first["ok"] and second["ok"])
        self.assertTrue(all(value == 0 for value in second["cleared"].values()))

    async def test_stop_and_clear_discard_retained_failure_without_active_job(self):
        def retain_failure(name):
            job_id = main.script_runtime.enqueue(
                name=name, steps=[{"type": "kbd"}], loop=False, condition_ids=[])
            main.script_runtime.activate(job_id)
            main.script_runtime.note_error("script failed")
            main.script_runtime.finish(job_id, failed=True)
            self.assertIsNone(main.script_runtime.snapshot()["active"])
            self.assertEqual(main.script_runtime.snapshot()["last_failure"]["job_id"], job_id)

        retain_failure("stop")
        await main.script_stop()
        self.assertIsNone(main.script_runtime.snapshot()["last_failure"])

        retain_failure("clear")
        await self.post(self.clear_payload())
        self.assertIsNone(main.script_runtime.snapshot()["last_failure"])

    async def test_invalid_commands_do_not_mutate_state(self):
        await self.seed()
        invalid = [
            {"command": "unknown", "targets": ["scripts"]},
            {"command": "clear"},
            {"command": "clear", "targets": []},
            {"command": "clear", "targets": ["scripts"]},
            {"command": "clear", "targets": "all"},
            {**self.clear_payload(), "targets": ["snapshots", "conditions", "scripts", "other"]},
            {**self.clear_payload(), "steps": []},
            {**self.clear_payload(), "img_bundle": {}},
            {**self.clear_payload(), "loop": True},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPException) as raised:
                    await self.post(payload)
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(len(self.snapshots.list_all()), 1)
                self.assertEqual(len(self.conditions.list_all()), 1)
                self.assertEqual(self.queue.qsize(), 1)

    async def test_condition_arm_and_disarm_routes_are_removed(self):
        condition_id = self.conditions.add("unlock", "snapshot", [0, 0, 1, 1], 0.92, 3, 3000)
        for action in ("arm", "disarm"):
            with self.subTest(action=action):
                status, _ = await self.api_post(f"/visual/conditions/{condition_id}/{action}", {})
                self.assertEqual(status, 404)
        self.assertEqual(self.conditions.get_armed(), [])

    async def test_persistence_failure_is_not_reported_as_success(self):
        await self.seed()
        with patch("builtins.open", side_effect=OSError("disk unavailable")):
            with self.assertRaises(HTTPException) as raised:
                await self.post(self.clear_payload())
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(len(self.conditions.list_all()), 1)
        self.assertEqual(len(self.snapshots.list_all()), 1)

    async def test_normal_script_requests_enter_fifo_queue_even_with_evt_gate(self):
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            response = await self.post({"steps": [{"type": "evt", "evt": "HELLO", "delay_ms": 0}]})
        self.assertTrue(response["ok"])
        self.assertFalse(response["pending"])
        self.assertEqual(self.queue.qsize(), 1)
        self.assertEqual((await self.pending.snapshot())["total"], 0)
        self.kbm_broadcast.assert_any_await({"type": "script_submitted", "job_id": response["job_id"]})

    async def test_editor_run_is_queued_without_broadcasting_tab_switch_event(self):
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            response = await self.post({"steps": [{"type": "evt", "evt": "HELLO", "delay_ms": 0}]},
                                       headers=[(b"x-rks-source", b"script-editor")])
        self.assertTrue(response["ok"])
        self.assertEqual(self.queue.qsize(), 1)
        events = [call.args[0] for call in self.kbm_broadcast.await_args_list]
        self.assertFalse(any(event.get("type") == "script_submitted" for event in events))

    async def test_evt_wait_does_not_timeout_or_auto_pause(self):
        with patch.object(main, "DEFAULT_EVT_TIMEOUT_MS", 10, create=True):
            task = asyncio.create_task(self.events.wait_for("FOREVER"))
            self.addAsyncCleanup(self.cancel_task, task)
            marker = asyncio.Event()
            asyncio.get_running_loop().call_later(0.06, marker.set)
            await asyncio.wait_for(marker.wait(), timeout=1)
            self.assertFalse(task.done(), "EVT waits must not use the former automatic timeout")
            await self.events.emit("FOREVER")
            self.assertEqual(await asyncio.wait_for(task, timeout=1), "FOREVER")

    async def test_manual_pause_blocks_current_wait_and_preserves_fifo_until_resume(self):
        waiting, resumed = asyncio.Event(), asyncio.Event()

        async def broadcast(message):
            if message["type"] == "script_wait":
                waiting.set()
            if message["type"] == "script_resumed":
                resumed.set()

        self.kbm_broadcast.side_effect = broadcast
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            await self.post({"steps": [{"type": "evt", "evt": "WAIT"}, {"type": "kbd", "cmd_type": "KEY", "key": "ENTER"}]})
            await self.post({"steps": [{"type": "kbd", "cmd_type": "KEY", "key": "F10"}]})
        with patch.object(main.kb_serial, "send", return_value="OK") as send:
            await main._ensure_script_runner()
            self.addAsyncCleanup(self.cancel_task, main._script_runner_task)
            await asyncio.wait_for(waiting.wait(), timeout=1)
            status = await main.script_status()
            self.assertIsNone(status["runtime"]["active"]["deadline"])
            code, body = await self.api_post("/script/pause", {})
            self.assertEqual(code, 200)
            self.assertEqual(body["pause"]["reason"], "manual")
            self.assertTrue((await main.script_status())["paused"])
            self.assertEqual(self.queue.qsize(), 1)
            await self.events.emit("WAIT")
            send.assert_not_called()
            code, _ = await self.api_post("/script/resume", {"pause_id": body["pause"]["pause_id"]})
            self.assertEqual(code, 200)
            await asyncio.wait_for(resumed.wait(), timeout=1)
            self.assertIsNone(await self.events.consume_if_available("WAIT"))
            send.assert_not_called()
            runner = main._script_runner_task
            await self.events.emit("WAIT")
            await asyncio.wait_for(runner, timeout=2)
        self.assertEqual([call.args[0] for call in send.call_args_list], ["KEY:ENTER", "KEY:F10"])

    async def test_manual_pause_at_activation_stays_disarmed(self):
        await self.enqueue_image_scripts(1)
        paused = asyncio.Event()

        async def broadcast(message):
            active = message.get("runtime", {}).get("active")
            if message["type"] == "script_runtime" and active and active["phase"] == "attaching" and not paused.is_set():
                paused.set()
                code, _ = await self.api_post("/script/pause", {})
                self.assertEqual(code, 200)

        self.kbm_broadcast.side_effect = broadcast
        runner = asyncio.create_task(main._script_runner())
        main._script_runner_task = runner
        self.addAsyncCleanup(self.cancel_task, runner)
        await asyncio.wait_for(paused.wait(), timeout=1)
        self.assertTrue((await main.script_status())["paused"])
        self.assertEqual(self.conditions.get_armed(), [])
        await main.script_stop()

    async def test_pause_during_matched_event_notification_requires_a_fresh_match(self):
        waiting, notifying, release_notification = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def broadcast(message):
            if message["type"] == "script_wait":
                waiting.set()
            if message["type"] == "script_event" and not notifying.is_set():
                notifying.set()
                await release_notification.wait()

        self.kbm_broadcast.side_effect = broadcast
        await self.queue.put(main.MixedScriptRequest(steps=[main.MixedStep(type="evt", evt="WAIT"),
            main.MixedStep(type="kbd", cmd_type="KEY", key="ENTER")]))
        with patch.object(main.kb_serial, "send", return_value="OK") as send:
            runner = asyncio.create_task(main._script_runner())
            main._script_runner_task = runner
            self.addAsyncCleanup(self.cancel_task, runner)
            await asyncio.wait_for(waiting.wait(), timeout=1)
            await self.events.emit("WAIT")
            await asyncio.wait_for(notifying.wait(), timeout=1)
            code, body = await self.api_post("/script/pause", {})
            self.assertEqual(code, 200)
            self.assertEqual(body["pause"]["evt"], "WAIT")
            release_notification.set()
            code, _ = await self.api_post("/script/resume", {"pause_id":body["pause"]["pause_id"]})
            self.assertEqual(code, 200)
            marker = asyncio.Event()
            asyncio.get_running_loop().call_later(0.06, marker.set)
            await asyncio.wait_for(marker.wait(), timeout=1)
            send.assert_not_called()
            await self.events.emit("WAIT")
            await asyncio.wait_for(runner, timeout=1)
        self.assertEqual([call.args[0] for call in send.call_args_list], ["KEY:ENTER"])

    async def test_pause_between_auto_attach_commands_blocks_later_attachment(self):
        entered = asyncio.Event()
        release = main.threading.Event()
        loop = asyncio.get_running_loop()
        calls = []

        def attach(servo_id, pin):
            calls.append(servo_id)
            if servo_id == 1:
                loop.call_soon_threadsafe(entered.set)
                release.wait(timeout=2)
            return True

        fake_servo = Mock(is_connected=True, attached={}, attach_servo_and_wait=attach)
        request = main.MixedScriptRequest(servos={"1":9,"2":10}, steps=[
            main.MixedStep(type="srv",servo_id=1),main.MixedStep(type="srv",servo_id=2)])
        await self.queue.put(request)
        with patch.object(main,"srv_serial",fake_servo), \
             patch.object(main,"_auto_attach_servos",self.original_auto_attach), \
             patch.object(main,"_run_mixed_script",new_callable=AsyncMock):
            runner = asyncio.create_task(main._script_runner())
            main._script_runner_task = runner
            self.addAsyncCleanup(self.cancel_task,runner)
            try:
                await asyncio.wait_for(entered.wait(),timeout=1)
                code,body = await self.api_post('/script/pause',{})
                self.assertEqual(code,200)
                release.set()
                marker=asyncio.Event();loop.call_later(0.08,marker.set)
                await asyncio.wait_for(marker.wait(),timeout=1)
                self.assertEqual(calls,[1])
                code,_ = await self.api_post('/script/resume',{'pause_id':body['pause']['pause_id']})
                self.assertEqual(code,200)
                await asyncio.wait_for(runner,timeout=1)
            finally:
                release.set()
        self.assertEqual(calls,[1,2])

    async def test_failed_finishing_job_cannot_enter_an_orphaned_manual_pause(self):
        await self.queue.put(main.MixedScriptRequest(steps=[main.MixedStep(type="evt", evt="WAIT")]))
        codes = []

        async def broadcast(message):
            active = message.get("runtime", {}).get("active")
            if message["type"] == "script_runtime" and active and active["error"] and not codes:
                codes.append(-1)
                code, _ = await self.api_post("/script/pause", {})
                codes[0] = code

        self.kbm_broadcast.side_effect = broadcast
        with patch.object(main, "_auto_attach_servos", side_effect=RuntimeError("attach failed")):
            with self.assertLogs(main.logger, level="ERROR"):
                runner = asyncio.create_task(main._script_runner())
                main._script_runner_task = runner
                self.addAsyncCleanup(self.cancel_task, runner)
                await asyncio.wait_for(runner, timeout=1)
        self.assertEqual(codes, [409])
        self.assertFalse((await main.script_status())["paused"])

    async def test_runtime_jobs_have_names_ids_and_full_readonly_type_details(self):
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            first = await self.post({"name": "Script A", "job_id": "forged", "steps": [
                {"type": "kbd", "cmd_type": "TYPE", "text": '<visible "TYPE">'}]})
            second = await self.post({"name": "Script B", "steps": [{"type": "evt", "evt": "WAIT"}]})
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first["job_id"], "forged")
        status = await main.script_status()
        self.assertEqual([job["name"] for job in status["runtime"]["queued"]], ["Script A", "Script B"])
        self.assertNotIn("steps", status["runtime"]["queued"][0])
        code, detail = await self.api_post("/script/jobs/" + first["job_id"], {}, method="GET")
        self.assertEqual(code, 200)
        self.assertEqual(detail["steps"][0]["params"]["text"], '<visible "TYPE">')
        self.assertFalse(detail["active"])
        self.assertEqual(self.queue.qsize(), 2)

    async def test_reused_request_is_copied_for_each_runtime_job(self):
        request = main.MixedScriptRequest(steps=[main.MixedStep(type="kbd", cmd_type="TYPE", text="first")])
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            await main._activate_script(request)
            first_id = request.job_id
            request.steps[0].text = "second"
            await main._activate_script(request)
            second_id = request.job_id
        first, second = self.queue.get_nowait(), self.queue.get_nowait()
        self.queue.task_done()
        self.queue.task_done()
        self.assertIsNot(first, second)
        self.assertEqual((first.job_id, second.job_id), (first_id, second_id))
        self.assertEqual((first.steps[0].text, second.steps[0].text), ("first", "second"))
        main.script_runtime.finish(first.job_id)
        self.assertIsNotNone(main.script_runtime.detail(second_id))

    async def test_runtime_progress_and_wait_detail_are_restorable_without_replaying(self):
        waiting = asyncio.Event()

        async def broadcast(message):
            if message["type"] == "script_wait":
                waiting.set()

        self.kbm_broadcast.side_effect = broadcast
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            first = await self.post({"name": "Current", "steps": [
                {"type": "kbd", "cmd_type": "KEY", "key": "TAB"},
                {"type": "evt", "evt": "WAIT"},
                {"type": "kbd", "cmd_type": "TYPE", "text": "unmasked"}]})
            await self.post({"name": "Next", "steps": [{"type": "kbd", "cmd_type": "KEY", "key": "F10"}]})
        with patch.object(main.kb_serial, "send", return_value="OK") as send:
            await main._ensure_script_runner()
            self.addAsyncCleanup(self.cancel_task, main._script_runner_task)
            await asyncio.wait_for(waiting.wait(), timeout=2)
            status = await main.script_status()
            self.assertEqual(status["runtime"]["active"]["job_id"], first["job_id"])
            self.assertEqual(status["runtime"]["active"]["phase"], "waiting")
            code, detail = await self.api_post("/script/jobs/" + first["job_id"], {}, method="GET")
            self.assertEqual(code, 200)
            self.assertEqual([step["state"] for step in detail["steps"]], ["completed", "active", "pending"])
            self.assertIsNone(detail["remaining_ms"])
            self.assertEqual(detail["steps"][2]["params"]["text"], "unmasked")
            self.assertEqual([call.args[0] for call in send.call_args_list], ["KEY:TAB"])
            await main.script_stop()
        self.assertIsNone((await main.script_status())["runtime"]["active"])
        self.assertEqual((await main.script_status())["runtime"]["queued"], [])
        code, _ = await self.api_post("/script/jobs/" + first["job_id"], {}, method="GET")
        self.assertEqual(code, 404)

    async def test_queued_condition_preview_is_readonly_and_not_active_job_runtime(self):
        await self.seed()
        await main._drain_script_queue()
        condition_id = self.conditions.list_all()[0]["condition_id"]
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            first = await self.post({"steps": [{"type": "evt", "evt": "IMG.MATCH." + condition_id}]})
            second = await self.post({"steps": [{"type": "evt", "evt": "IMG.MATCH." + condition_id}]})
        main.script_runtime.activate(first["job_id"])
        self.conditions.arm(condition_id)
        self.conditions._runtime[condition_id].consecutive_hits = 7
        before = main.script_runtime.snapshot()
        with patch.object(self.conditions, "activate_only", wraps=self.conditions.activate_only) as activate:
            code, detail = await self.api_post("/script/jobs/" + second["job_id"], {}, method="GET")
        self.assertEqual(code, 200)
        self.assertFalse(detail["conditions"][0]["enabled"])
        self.assertEqual(detail["conditions"][0]["consecutive_hits"], 0)
        self.assertIsNone(detail["conditions"][0]["last_score"])
        self.assertEqual(before, main.script_runtime.snapshot())
        activate.assert_not_called()

    async def test_bundle_name_is_preserved_in_runtime_and_import_response(self):
        payload = {**self.image_bundle(), "name": "Named bundle"}
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock), \
             patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            response = await self.post(payload)
            status, imported = await self.api_post("/script/import_bundle", payload)
        self.assertEqual(status, 200)
        self.assertEqual(imported["name"], "Named bundle")
        self.assertEqual(main.script_runtime.detail(response["job_id"])["name"], "Named bundle")

    async def test_runtime_reports_ocr_and_image_move_results_and_phases(self):
        phases = []
        waiting = asyncio.Event()

        async def broadcast(message):
            if message["type"] == "script_runtime" and message["runtime"]["active"]:
                phases.append(message["runtime"]["active"]["phase"])
            if message["type"] == "script_wait":
                waiting.set()

        self.kbm_broadcast.side_effect = broadcast
        result = main.ImageMatch(box=[0, 0, 20, 16], center=[9.5, 7.5], hid=[100, 200],
                                 score=1, frame_width=200, frame_height=100)
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            response = await self.post({"steps": [self.ocr_step().model_dump(),
                self.image_move_step().model_dump(), {"type": "evt", "evt": "WAIT"}]})
        with patch.object(main, "read_stable_digits", new_callable=AsyncMock, return_value="0037"), \
             patch.object(main, "locate_image", new_callable=AsyncMock, return_value=result), \
             patch.object(main.kb_serial, "send", return_value="OK"):
            await main._ensure_script_runner()
            self.addAsyncCleanup(self.cancel_task, main._script_runner_task)
            await asyncio.wait_for(waiting.wait(), timeout=2)
            detail = main.script_runtime.detail(response["job_id"])
            self.assertIn("recognizing", phases)
            self.assertIn("locating", phases)
            self.assertEqual(detail["steps"][0]["result"]["value"], "0037")
            self.assertEqual(detail["steps"][1]["result"]["hid"], [100, 200])
            self.assertEqual([step["state"] for step in detail["steps"]], ["completed", "completed", "active"])
            await main.script_stop()

    async def test_fifo_status_reports_current_wait_and_excludes_active_job_from_queue_count(self):
        waiting = asyncio.Event()

        async def broadcast(message):
            if message["type"] == "script_wait":
                waiting.set()

        self.kbm_broadcast.side_effect = broadcast
        await self.queue.put(main.MixedScriptRequest(steps=[main.MixedStep(type="evt", evt="IMG.MATCH.current", delay_ms=0)]))
        await self.queue.put(main.MixedScriptRequest(steps=[main.MixedStep(type="kbd", cmd_type="KEY", key="ENTER")]))
        runner = asyncio.create_task(main._script_runner())
        main._script_runner_task = runner
        self.addAsyncCleanup(self.cancel_task, runner)
        await asyncio.wait_for(waiting.wait(), timeout=1)
        status = await main.script_status()
        self.assertTrue(status["running"])
        self.assertFalse(status["paused"])
        self.assertEqual(status["waiting"], {"evt": "IMG.MATCH.current", "step": 0, "total": 1})
        self.assertEqual(status["queued"], 1)
        await main.script_stop()
        stopped = await main.script_status()
        self.assertIsNone(stopped["waiting"])
        self.assertEqual(stopped["queued"], 0)

    async def test_manual_pause_during_delay_does_not_replay_completed_actions(self):
        reached_delay = asyncio.Event()
        async def broadcast(message):
            if message["type"] == "script_progress" and message["step"] == 1:
                reached_delay.set()
        self.kbm_broadcast.side_effect = broadcast
        first = main.MixedScriptRequest(steps=[
            main.MixedStep(type="kbd", cmd_type="KEY", key="TAB"),
            main.MixedStep(type="kbd", cmd_type="KEY", key="ENTER", delay_ms=100)])
        second = main.MixedScriptRequest(steps=[main.MixedStep(type="kbd", cmd_type="KEY", key="F10")])
        await self.queue.put(first)
        await self.queue.put(second)
        with patch.object(main.kb_serial, "send", return_value="OK") as send:
            runner = asyncio.create_task(main._script_runner())
            main._script_runner_task = runner
            self.addAsyncCleanup(self.cancel_task, runner)
            await asyncio.wait_for(reached_delay.wait(), timeout=1)
            code, body = await self.api_post("/script/pause", {})
            self.assertEqual(code, 200)
            status = await main.script_status()
            self.assertTrue(status["paused"])
            self.assertIsNone(status["pause"]["evt"])
            self.assertEqual(status["pause"]["step"], 1)
            self.assertEqual(status["queued"], 1)
            marker = asyncio.Event()
            asyncio.get_running_loop().call_later(0.15, marker.set)
            await asyncio.wait_for(marker.wait(), timeout=1)
            self.assertEqual([call.args[0] for call in send.call_args_list], ["KEY:TAB"])
            self.assertFalse(runner.done())
            with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
                await self.post({"steps": [{"type": "kbd", "cmd_type": "KEY", "key": "ESC"}]})
            self.assertEqual(self.queue.qsize(), 2)
            status_code, response = await self.api_post("/script/resume", {"pause_id": main._script_pause["pause_id"]})
            self.assertEqual(status_code, 200)
            await asyncio.wait_for(runner, timeout=2)
        self.assertEqual([call.args[0] for call in send.call_args_list], ["KEY:TAB", "KEY:ENTER", "KEY:F10", "KEY:ESC"])
        self.assertFalse((await main.script_status())["paused"])

    async def test_manual_pause_disarms_and_stop_or_clear_discards_remaining_jobs(self):
        for action in ["stop", "clear"]:
            with self.subTest(action=action):
                paused = asyncio.Event()

                async def broadcast(message):
                    if message["type"] == "script_wait":
                        paused.set()

                self.kbm_broadcast.side_effect = broadcast
                await self.enqueue_image_scripts(2)
                with patch.object(main.kb_serial, "send", return_value="OK") as send:
                    runner = asyncio.create_task(main._script_runner())
                    main._script_runner_task = runner
                    self.addAsyncCleanup(self.cancel_task, runner)
                    await asyncio.wait_for(paused.wait(), timeout=1)
                    code, _ = await self.api_post("/script/pause", {})
                    self.assertEqual(code, 200)
                    self.assertEqual(self.conditions.get_armed(), [])
                    self.assertEqual(self.queue.qsize(), 1)
                    if action == "clear":
                        await self.post(self.clear_payload())
                    else:
                        await main.script_stop()
                self.assertTrue(runner.done())
                self.assertTrue(self.queue.empty())
                self.assertFalse((await main.script_status())["paused"])
                send.assert_not_called()
                await asyncio.wait_for(self.queue.join(), timeout=1)

    async def test_retry_without_a_paused_script_is_rejected(self):
        status, body = await self.api_post("/script/resume", {})
        self.assertEqual(status, 409)

    async def test_stale_retry_cannot_resume_a_different_pause(self):
        job_id = main.script_runtime.enqueue(name=None, steps=[{"type":"evt","evt":"WAIT"}], loop=False, condition_ids=[])
        main.script_runtime.activate(job_id)
        main._script_pause = {"pause_id": "new-pause", "evt": "WAIT", "step": 0, "total":1,
            "job_id":job_id, "condition_ids":[], "previous_phase":"waiting", "previous_deadline":None}
        main._script_resume_event = asyncio.Event()
        with patch.object(main, "_script_runner_task", Mock(done=Mock(return_value=False))):
            for payload in [{}, {"pause_id": "old-pause"}]:
                status, _ = await self.api_post("/script/resume", payload)
                self.assertEqual(status, 409)
                self.assertFalse(main._script_resume_event.is_set())
            status, _ = await self.api_post("/script/resume", {"pause_id": "new-pause"})
            self.assertEqual(status, 200)
            status, _ = await self.api_post("/script/resume", {"pause_id": "new-pause"})
            self.assertEqual(status, 409)

    async def test_img_resume_rearms_only_current_condition_with_fresh_runtime(self):
        responses = await self.enqueue_image_scripts(2)
        first_id, second_id = [response["img_import"]["condition_map"]["same-condition"] for response in responses]
        paused, resumed, next_job = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def broadcast(message):
            if message["type"] == "script_wait" and message["evt"] == "IMG.MATCH." + first_id:
                paused.set()
            elif message["type"] == "script_wait":
                next_job.set()
            elif message["type"] == "script_resumed":
                resumed.set()

        self.kbm_broadcast.side_effect = broadcast
        with patch.object(main.kb_serial, "send", return_value="OK"):
            runner = asyncio.create_task(main._script_runner())
            main._script_runner_task = runner
            self.addAsyncCleanup(self.cancel_task, runner)
            await asyncio.wait_for(paused.wait(), timeout=1)
            runtime = self.conditions._runtime[first_id]
            runtime.consecutive_hits = 8
            runtime.prev_matched = True
            runtime.cooldown_until = main.time.time() + 9999
            code, _ = await self.api_post("/script/pause", {})
            self.assertEqual(code, 200)
            self.assertFalse(self.conditions.get(second_id).enabled)
            status, _ = await self.api_post("/script/resume", {"pause_id": main._script_pause["pause_id"]})
            self.assertEqual(status, 200)
            await asyncio.wait_for(resumed.wait(), timeout=1)
            condition, runtime = self.conditions.get_armed()[0]
            self.assertEqual(condition.condition_id, first_id)
            self.assertEqual(runtime.consecutive_hits, 0)
            self.assertEqual(runtime.cooldown_until, 0)
            self.assertFalse(runtime.prev_matched)
            self.assertFalse(next_job.is_set())
            frame = main.np.zeros((8, 8, 3), dtype=main.np.uint8)
            for generation in [1, 2]:
                await main._eval_condition(condition, runtime, frame, 8, 8,
                    frame_generation=main.frame_buffer.generation + generation)
            await asyncio.wait_for(next_job.wait(), timeout=1)
            self.assertEqual([item.condition_id for item, _ in self.conditions.get_armed()], [second_id])
            await main.script_stop()

    def ocr_step(self, **overrides):
        return main.MixedStep(type="ocr", roi=[0, 0, 1, 1], variable="digits", **overrides)

    def image_move_step(self):
        template = main.np.random.default_rng(42).integers(0, 256, (16, 20, 3), dtype=main.np.uint8)
        _, encoded = main.cv2.imencode(".png", template)
        return main.MixedStep(type="mse", action="IMAGE_MOVE", image_move={
            "template_b64": main.base64.b64encode(encoded).decode(),
            "source_width": 200, "source_height": 100})

    async def test_image_move_only_moves_and_reports_center(self):
        step = self.image_move_step()
        result = main.ImageMatch(box=[50, 30, 20, 16], center=[59.5, 37.5], hid=[9797, 12412],
                                 score=1, frame_width=200, frame_height=100)
        with patch.object(main, "locate_image", new_callable=AsyncMock, return_value=result), \
             patch.object(main.kb_serial, "send", return_value="OK") as send:
            await main._run_mixed_script(main.MixedScriptRequest(steps=[step]))
        self.assertEqual([call.args[0] for call in send.call_args_list],
                         ["MOUSE:ABS 9797 12412"])
        self.assertTrue(all("allow_send" in call.kwargs for call in send.call_args_list))

    async def test_image_move_can_be_followed_by_independent_click_or_doubleclick(self):
        result = main.ImageMatch(box=[50, 30, 20, 16], center=[59.5, 37.5], hid=[9797, 12412],
                                 score=1, frame_width=200, frame_height=100)
        for action in ["CLICK", "DBLCLICK"]:
            for button in ["L", "R"]:
                with self.subTest(action=action, button=button):
                    request = main.MixedScriptRequest(steps=[self.image_move_step(),
                        main.MixedStep(type="mse", action=action, btn=button)])
                    with patch.object(main, "locate_image", new_callable=AsyncMock, return_value=result), \
                         patch.object(main.kb_serial, "send", return_value="OK") as send:
                        await main._run_mixed_script(request)
                    self.assertEqual([call.args[0] for call in send.call_args_list],
                        ["MOUSE:ABS 9797 12412", f"MOUSE:{action} {button}"])
                    self.assertTrue(all("allow_send" in call.kwargs for call in send.call_args_list))

    async def test_removed_image_click_action_is_rejected(self):
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            status, _ = await self.api_post("/script/run", {"steps": [{"type": "mse", "action": "IMAGE_CLICK"}]})
        self.assertEqual(status, 422)
        self.assertTrue(self.queue.empty())

    async def test_image_move_move_failure_prevents_click(self):
        step = self.image_move_step()
        result = main.ImageMatch(box=[0, 0, 20, 16], center=[9.5, 7.5], hid=[100, 200],
                                 score=1, frame_width=200, frame_height=100)
        with patch.object(main, "locate_image", new_callable=AsyncMock, return_value=result), \
             patch.object(main.kb_serial, "send", return_value="ERR") as send:
            with self.assertRaises(RuntimeError):
                await main._run_mixed_script(main.MixedScriptRequest(steps=[step]))
        self.assertEqual([call.args[0] for call in send.call_args_list], ["MOUSE:ABS 100 200"])

    async def test_stop_and_clear_cancel_image_search_and_drain_fifo(self):
        for action in ["stop", "clear"]:
            with self.subTest(action=action):
                started = asyncio.Event()

                async def search(*args, **kwargs):
                    started.set()
                    await asyncio.Event().wait()

                await self.queue.put(main.MixedScriptRequest(steps=[self.image_move_step()]))
                await self.queue.put(main.MixedScriptRequest(steps=[main.MixedStep(type="kbd", cmd_type="KEY", key="ENTER")]))
                with patch.object(main, "locate_image", side_effect=search), \
                     patch.object(main.kb_serial, "send", return_value="OK") as send:
                    runner = asyncio.create_task(main._script_runner())
                    main._script_runner_task = runner
                    self.addAsyncCleanup(self.cancel_task, runner)
                    await asyncio.wait_for(started.wait(), timeout=2)
                    if action == "clear":
                        await self.post(self.clear_payload())
                    else:
                        await main.script_stop()
                self.assertTrue(runner.done())
                self.assertTrue(self.queue.empty())
                send.assert_not_called()

    async def test_keyboard_cancel_guard_prevents_a_serial_write(self):
        serial_port = Mock(is_open=True)
        with patch.object(main.kb_serial, "_serial", serial_port):
            result = main.kb_serial.send("MOUSE:CLICK L", allow_send=lambda: False)
        self.assertEqual(result, "CANCELLED")
        serial_port.write.assert_not_called()

    async def test_cancelled_independent_clicks_waiting_for_serial_lock_never_write(self):
        loop = asyncio.get_running_loop()
        original_send = main.kb_serial.send
        for action in ["CLICK", "DBLCLICK"]:
            with self.subTest(action=action):
                entered, completed = asyncio.Event(), asyncio.Event()
                serial_port = Mock(is_open=True)

                def blocked_send(*args, **kwargs):
                    loop.call_soon_threadsafe(entered.set)
                    try:
                        return original_send(*args, **kwargs)
                    finally:
                        loop.call_soon_threadsafe(completed.set)

                with patch.object(main.kb_serial, "_serial", serial_port), \
                     patch.object(main.kb_serial, "send", side_effect=blocked_send):
                    main.kb_serial._lock.acquire()
                    request = main.MixedScriptRequest(steps=[main.MixedStep(type="mse", action=action, btn="R")])
                    task = asyncio.create_task(main._run_mixed_script(request))
                    self.addAsyncCleanup(self.cancel_task, task)
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        main.kb_serial._lock.release()
                    await asyncio.wait_for(completed.wait(), timeout=2)
                serial_port.write.assert_not_called()

    async def test_image_move_configuration_survives_bundle_import(self):
        payload = self.image_bundle()
        step = self.image_move_step()
        payload["steps"].append(step.model_dump())
        with patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            status, body = await self.api_post("/script/import_bundle", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["steps"][-1]["image_move"], step.image_move.model_dump())

    async def test_image_move_cancelled_while_waiting_for_serial_lock_never_writes(self):
        entered, completed = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        serial_port = Mock(is_open=True)
        result = main.ImageMatch(box=[0, 0, 20, 16], center=[9.5, 7.5], hid=[100, 200],
                                 score=1, frame_width=200, frame_height=100)
        original_send = main.kb_serial.send

        def blocked_send(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            try:
                return original_send(*args, **kwargs)
            finally:
                loop.call_soon_threadsafe(completed.set)

        with patch.object(main.kb_serial, "_serial", serial_port), \
             patch.object(main.kb_serial, "send", side_effect=blocked_send), \
             patch.object(main, "locate_image", new_callable=AsyncMock, return_value=result):
            main.kb_serial._lock.acquire()
            task = asyncio.create_task(main._run_image_move(self.image_move_step().image_move))
            self.addAsyncCleanup(self.cancel_task, task)
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                main.kb_serial._lock.release()
            await asyncio.wait_for(completed.wait(), timeout=2)
        serial_port.write.assert_not_called()

    async def test_image_move_template_and_preview_api_do_not_send_serial(self):
        frame = main.np.random.default_rng(17).integers(0, 256, (100, 200, 3), dtype=main.np.uint8)
        snapshot = self.snapshots.add(frame)
        with patch.object(main.kb_serial, "send") as send:
            status, body = await self.api_post("/visual/image-move/template",
                {"snapshot_id": snapshot["id"], "roi": [0.1, 0.2, 0.2, 0.2]})
            self.assertEqual(status, 200)
            result = main.ImageMatch(box=[20, 20, 40, 20], center=[39.5, 29.5], hid=[100, 200],
                                     score=1, frame_width=200, frame_height=100, preview_b64="preview")
            with patch.object(main, "locate_image", new_callable=AsyncMock, return_value=result):
                status, located = await self.api_post("/visual/image-move/test", body["image_move"])
            self.assertEqual(status, 200)
            self.assertEqual(located["match"]["center"], [39.5, 29.5])
        send.assert_not_called()

    async def test_image_move_rejects_missing_settings_and_raw_command_override(self):
        invalid = [{"type": "mse", "action": "IMAGE_MOVE"},
                   {**self.image_move_step().model_dump(), "cmd": "MOUSE:CLICK L"}]
        for step in invalid:
            with self.subTest(step=step["action"]):
                status, _ = await self.api_post("/script/run", {"steps": [step]})
                self.assertEqual(status, 422)
        self.assertTrue(self.queue.empty())

    async def test_ocr_step_types_leading_zero_result_and_enter(self):
        request = main.MixedScriptRequest(steps=[self.ocr_step(),
            main.MixedStep(type="kbd", cmd_type="TYPE", text="${digits}"),
            main.MixedStep(type="kbd", cmd_type="KEY", key="ENTER")])
        with patch.object(main, "read_stable_digits", new_callable=AsyncMock, return_value="0037", create=True), \
             patch.object(main.kb_serial, "send", return_value="OK") as send:
            await main._run_mixed_script(request)
        self.assertEqual([call.args[0] for call in send.call_args_list], ["TYPE:0037", "KEY:ENTER"])

    async def test_ocr_variables_do_not_leak_to_next_script(self):
        first = main.MixedScriptRequest(steps=[self.ocr_step()])
        second = main.MixedScriptRequest(steps=[main.MixedStep(type="kbd", cmd_type="TYPE", text="${digits}")])
        with patch.object(main, "read_stable_digits", new_callable=AsyncMock, return_value="8462", create=True), \
             patch.object(main.kb_serial, "send", return_value="OK") as send:
            await main._run_mixed_script(first)
            with self.assertRaisesRegex(ValueError, "Undefined"):
                await main._run_mixed_script(second)
        send.assert_not_called()

    async def test_ocr_failure_in_next_iteration_never_types_previous_value(self):
        request = main.MixedScriptRequest(loop=True, steps=[self.ocr_step(),
            main.MixedStep(type="kbd", cmd_type="TYPE", text="${digits}")])
        with patch.object(main, "read_stable_digits", new_callable=AsyncMock, side_effect=["8462", TimeoutError("OCR timeout")], create=True), \
             patch.object(main.kb_serial, "send", return_value="OK") as send:
            with self.assertRaises(TimeoutError):
                await main._run_mixed_script(request)
        self.assertEqual([call.args[0] for call in send.call_args_list], ["TYPE:8462"])

    async def test_cancelling_ocr_never_sends_keyboard(self):
        started = asyncio.Event()

        async def recognize(*args):
            started.set()
            await asyncio.Event().wait()

        request = main.MixedScriptRequest(steps=[self.ocr_step(),
            main.MixedStep(type="kbd", cmd_type="TYPE", text="${digits}")])
        with patch.object(main, "read_stable_digits", side_effect=recognize, create=True), \
             patch.object(main.kb_serial, "send", return_value="OK") as send:
            task = asyncio.create_task(main._run_mixed_script(request))
            self.addAsyncCleanup(self.cancel_task, task)
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        send.assert_not_called()

    def test_ocr_requires_valid_roi_and_variable_name(self):
        for payload in [
            {"type": "ocr"}, {"type": "ocr", "roi": [0, 0, 0, 1]},
            {"type": "ocr", "roi": [0, 0, 1, 1], "variable": "bad-name"},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                main.MixedStep(**payload)

    async def test_ocr_preview_returns_value_or_explicit_engine_unavailable(self):
        config = {"roi": [0, 0, 1, 1], "variable": "digits"}
        with patch.object(main, "ensure_available", return_value="tesseract"), \
             patch.object(main, "read_stable_digits", new_callable=AsyncMock, return_value="0037"):
            status, body = await self.api_post("/visual/ocr/test", config)
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], "0037")
        with patch.object(main, "ensure_available", side_effect=main.OcrUnavailable("Tesseract missing")):
            status, body = await self.api_post("/visual/ocr/test", config)
        self.assertEqual(status, 503)
        self.assertIn("Tesseract", body["detail"])

    async def test_import_bundle_preserves_ocr_settings(self):
        payload = self.image_bundle()
        payload["steps"].append({"type": "ocr", "roi": [0.1, 0.2, 0.3, 0.4], "variable": "digits"})
        with patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            status, body = await self.api_post("/script/import_bundle", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["steps"][1]["type"], "ocr")
        self.assertEqual(body["steps"][1]["roi"], [0.1, 0.2, 0.3, 0.4])

    async def test_invalid_ocr_script_is_an_http_validation_error(self):
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            status, body = await self.api_post("/script/run", {"steps": [{"type": "ocr", "roi": [0, 0, 0, 1]}]})
        self.assertEqual(status, 422)
        self.assertTrue(self.queue.empty())

    def image_bundle(self):
        frame = main.np.zeros((8, 8, 3), dtype=main.np.uint8)
        ok, jpeg = main.cv2.imencode(".jpg", frame)
        self.assertTrue(ok)
        return {
            "steps": [{"type": "evt", "evt": "IMG.MATCH.same-condition", "delay_ms": 0}],
            "img_bundle": {
                "snapshots": [{"snapshot_id": "same-snapshot", "jpeg_b64": main.base64.b64encode(jpeg.tobytes()).decode()}],
                "conditions": [{"condition_id": "same-condition", "name": "same-image",
                                "snapshot_id": "same-snapshot", "roi": [0, 0, 1, 1], "min_hits": 2}],
            },
        }

    async def enqueue_image_scripts(self, count):
        responses = []
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock), \
             patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            for _ in range(count):
                responses.append(await self.post(self.image_bundle()))
        return responses

    async def test_identical_image_bundles_are_queued_with_independent_disarmed_conditions(self):
        responses = await self.enqueue_image_scripts(3)
        self.assertEqual(self.queue.qsize(), 3)
        self.assertTrue(all(not response["pending"] for response in responses))
        ids = [response["img_import"]["condition_map"]["same-condition"] for response in responses]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(len({condition["snapshot_id"] for condition in self.conditions.list_all()}), 3)
        self.assertEqual(self.conditions.get_armed(), [])
        for condition_id in ids:
            request = self.queue.get_nowait()
            self.assertEqual(request.condition_ids, [condition_id])
            self.assertEqual(request.steps[0].evt, "IMG.MATCH." + condition_id)
            self.queue.task_done()

    async def test_runner_enables_only_current_conditions_and_same_image_matches_afresh(self):
        responses = await self.enqueue_image_scripts(3)
        ids = [response["img_import"]["condition_map"]["same-condition"] for response in responses]
        started = []
        frame = main.np.zeros((8, 8, 3), dtype=main.np.uint8)
        for condition_id in ids:
            self.conditions._runtime[condition_id] = main.ConditionRuntime(
                consecutive_hits=9, cooldown_until=main.time.time() + 9999, prev_matched=True)
            await self.events.emit("IMG.MATCH." + condition_id)

        async def run(request):
            armed = self.conditions.get_armed()
            self.assertEqual([condition.condition_id for condition, _ in armed], [ids[len(started)]])
            condition, runtime = armed[0]
            for other_id in ids:
                if other_id != condition.condition_id:
                    self.conditions.arm(other_id)
            self.assertEqual([item.condition_id for item, _ in self.conditions.get_armed()], [condition.condition_id])
            started.append(condition.condition_id)
            self.assertEqual(runtime.consecutive_hits, 0)
            self.assertEqual(runtime.cooldown_until, 0)
            self.assertFalse(runtime.prev_matched)
            self.assertIsNone(await self.events.consume_if_available(request.steps[0].evt))
            await main._eval_condition(condition, runtime, frame, 8, 8)
            self.assertIsNone(await self.events.consume_if_available(request.steps[0].evt))
            await main._eval_condition(condition, runtime, frame, 8, 8)
            self.assertEqual(await self.events.consume_if_available(request.steps[0].evt), request.steps[0].evt)

        with patch.object(main, "_run_mixed_script", side_effect=run):
            await asyncio.wait_for(main._script_runner(), timeout=2)
        self.assertEqual(started, ids)
        self.assertEqual(self.conditions.get_armed(), [])
        await asyncio.wait_for(self.queue.join(), timeout=0.2)

    async def test_two_scripts_referencing_same_condition_reset_hits_and_cooldown(self):
        await self.seed()
        await main._drain_script_queue()
        condition_id = self.conditions.list_all()[0]["condition_id"]
        self.conditions.update(condition_id, min_hits=2)
        payload = {"steps": [{"type": "evt", "evt": "IMG.MATCH." + condition_id, "delay_ms": 0}]}
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            await self.post(payload)
            await self.post(payload)
        frames_seen = []
        previous_runtime = None
        frame = main.np.zeros((8, 8, 3), dtype=main.np.uint8)

        async def run(request):
            nonlocal previous_runtime
            condition, runtime = self.conditions.get_armed()[0]
            self.assertIsNot(runtime, previous_runtime)
            previous_runtime = runtime
            self.assertEqual(runtime.consecutive_hits, 0)
            self.assertEqual(runtime.cooldown_until, 0)
            self.assertIsNone(await self.events.consume_if_available(payload["steps"][0]["evt"]))
            await main._eval_condition(condition, runtime, frame, 8, 8)
            self.assertIsNone(await self.events.consume_if_available(payload["steps"][0]["evt"]))
            await main._eval_condition(condition, runtime, frame, 8, 8)
            self.assertEqual(await self.events.consume_if_available(payload["steps"][0]["evt"]), payload["steps"][0]["evt"])
            frames_seen.append(runtime.consecutive_hits)

        with patch.object(main, "_run_mixed_script", side_effect=run):
            await asyncio.wait_for(main._script_runner(), timeout=2)
        self.assertEqual(len(frames_seen), 2)
        self.assertEqual(self.conditions.get_armed(), [])

    async def test_cancelling_image_script_disarms_its_conditions(self):
        await self.enqueue_image_scripts(2)
        started = asyncio.Event()

        async def run(request):
            self.assertEqual(len(self.conditions.get_armed()), 1)
            started.set()
            await asyncio.Event().wait()

        with patch.object(main, "_run_mixed_script", side_effect=run):
            runner = asyncio.create_task(main._script_runner())
            main._script_runner_task = runner
            self.addAsyncCleanup(self.cancel_task, runner)
            await asyncio.wait_for(started.wait(), timeout=2)
            await main.script_stop()
        self.assertEqual(self.conditions.get_armed(), [])
        self.assertEqual(self.queue.qsize(), 0)

    async def test_real_runner_executes_identical_image_scripts_in_order(self):
        labels = ["A", "B", "C"]
        responses = []
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock), \
             patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            for label in labels:
                payload = self.image_bundle()
                payload["steps"].append({"type": "kbd", "delay_ms": 0, "cmd_type": "TYPE", "text": label})
                responses.append(await self.post(payload))
        ids = [response["img_import"]["condition_map"]["same-condition"] for response in responses]
        waiting = []
        frame = main.np.zeros((8, 8, 3), dtype=main.np.uint8)

        async def broadcast(message):
            if message["type"] != "script_wait":
                return
            condition, runtime = self.conditions.get_armed()[0]
            self.assertEqual(condition.condition_id, ids[len(waiting)])
            waiting.append(message["evt"])
            self.assertEqual(runtime.consecutive_hits, 0)
            await main._eval_condition(condition, runtime, frame, 8, 8)
            self.assertIsNone(await self.events.consume_if_available(message["evt"]))
            await main._eval_condition(condition, runtime, frame, 8, 8)

        self.kbm_broadcast.side_effect = broadcast
        with patch.object(main.kb_serial, "send", return_value="OK") as send:
            await asyncio.wait_for(main._script_runner(), timeout=3)
        self.assertEqual([call.args[0] for call in send.call_args_list], ["TYPE:A", "TYPE:B", "TYPE:C"])
        self.assertEqual(waiting, ["IMG.MATCH." + condition_id for condition_id in ids])
        self.assertEqual(self.conditions.get_armed(), [])
        self.assertEqual(self.queue.qsize(), 0)

    async def test_dash_sequence_imports_and_matches_each_original_snapshot_in_fifo(self):
        folder = Path(__file__).resolve().parents[1] / "server" / "dist" / "Messi" / "dash"
        if not folder.is_dir():
            self.skipTest("Local dash reproduction fixtures are not available")
        scripts = [folder / name for name in ["00_clear_command.json", "01_pwd-boot-menu.json",
            "02_f12.json", "03_toast.json", "04_toast-cont.json", "05_enter-po.json"]]
        if not all(script.is_file() for script in scripts):
            self.skipTest("Original six dash reproduction fixtures are not available")
        expected_events = []
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock), \
             patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            for script in scripts:
                payload = json.loads(script.read_text(encoding="utf-8-sig"))
                response = await self.post(payload)
                expected_events.extend("IMG.MATCH." + cid for cid in response.get("img_import", {}).get("condition_map", {}).values())
        matched_events = []

        async def run(request):
            active_ids = [condition.condition_id for condition, _ in self.conditions.get_armed()]
            self.assertEqual(set(active_ids), set(request.condition_ids))
            for step in request.steps:
                if step.type != "evt":
                    continue
                condition_id = step.evt.removeprefix("IMG.MATCH.")
                condition = self.conditions.get(condition_id)
                self.assertIsNotNone(condition)
                runtime = self.conditions._runtime[condition_id]
                snapshot = self.snapshots.get(condition.snapshot_id)
                frame = main.cv2.imdecode(main.np.frombuffer(main.base64.b64decode(snapshot["jpeg_b64"]), dtype=main.np.uint8), main.cv2.IMREAD_COLOR)
                for generation in range(1, condition.min_hits + 1):
                    await main._eval_condition(condition, runtime, frame, frame.shape[1], frame.shape[0],
                                               frame_generation=main.frame_buffer.generation + generation)
                self.assertEqual(await self.events.consume_if_available(step.evt), step.evt)
                matched_events.append(step.evt)

        with patch.object(main, "_run_mixed_script", side_effect=run):
            await asyncio.wait_for(main._script_runner(), timeout=5)
        self.assertEqual(matched_events, expected_events)
        self.assertEqual(len(matched_events), 4)
        self.assertEqual(self.conditions.get_armed(), [])

    async def test_unlock_jobs_disarm_previous_condition_and_run_next_match(self):
        folder = Path(__file__).resolve().parents[1] / "server" / "dist" / "Messi" / "unlock"
        files = [folder / name for name in ["04_unlock_dialog.json", "05_unlock_finish.json"]]
        if not all(path.is_file() for path in files):
            self.skipTest("Local unlock reproduction fixtures are unavailable")
        expected = []
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock), \
             patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            for path in files:
                response = await self.post(json.loads(path.read_text(encoding="utf-8-sig")))
                expected.extend(response["img_import"]["condition_map"].values())
        seen = []

        async def broadcast(message):
            if message["type"] != "script_wait":
                return
            condition_id = message["evt"].removeprefix("IMG.MATCH.")
            self.assertEqual(condition_id, expected[len(seen)])
            self.assertEqual([condition.condition_id for condition, _ in self.conditions.get_armed()], [condition_id])
            for previous in seen:
                self.assertFalse(self.conditions.get(previous).enabled)
            condition, runtime = self.conditions.get_armed()[0]
            snapshot = self.snapshots.get(condition.snapshot_id)
            frame = main.cv2.imdecode(main.np.frombuffer(main.base64.b64decode(snapshot["jpeg_b64"]), dtype=main.np.uint8), main.cv2.IMREAD_COLOR)
            for offset in range(1, condition.min_hits + 1):
                await main._eval_condition(condition, runtime, frame, frame.shape[1], frame.shape[0],
                                          frame_generation=main.frame_buffer.generation + offset)
            seen.append(condition_id)

        self.kbm_broadcast.side_effect = broadcast
        with patch.object(main.kb_serial, "send", return_value="OK") as send, \
             patch.object(main, "_script_sleep", new_callable=AsyncMock):
            await asyncio.wait_for(main._script_runner(), timeout=5)
        self.assertEqual(seen, expected)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.conditions.get_armed(), [])
        self.assertTrue(self.queue.empty())

    async def test_active_img_gate_is_not_blocked_by_next_bundle_import(self):
        payload = self.image_bundle()
        payload["steps"].append({"type": "kbd", "delay_ms": 0, "cmd_type": "KEY", "key": "DOWN"})
        waiting = asyncio.Event()
        importing = asyncio.Event()
        resume_import = asyncio.Event()
        keyboard_sent = asyncio.Event()
        tasks = []

        async def broadcast_srv(message):
            if message.get("reason") == "bundle_import":
                importing.set()
                await resume_import.wait()

        async def broadcast_kbm(message):
            if message["type"] == "script_wait":
                waiting.set()

        loop = asyncio.get_running_loop()

        def keyboard_send(command, *, allow_send=None):
            if allow_send is not None and not allow_send():
                return "CANCELLED"
            if command == "KEY:DOWN":
                loop.call_soon_threadsafe(keyboard_sent.set)
            return "OK"

        with patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
                await self.post(payload)
            self.kbm_broadcast.side_effect = broadcast_kbm
            self.srv_broadcast.side_effect = broadcast_srv
            with patch.object(main.kb_serial, "send", side_effect=keyboard_send):
                runner = asyncio.create_task(main._script_runner())
                main._script_runner_task = runner
                tasks.append(runner)
                try:
                    await asyncio.wait_for(waiting.wait(), timeout=2)
                    condition, runtime = self.conditions.get_armed()[0]
                    submission = asyncio.create_task(self.post(self.image_bundle()))
                    tasks.append(submission)
                    await asyncio.wait_for(importing.wait(), timeout=2)
                    self.assertTrue(main._script_control_lock.locked())
                    frame = main.np.zeros((8, 8, 3), dtype=main.np.uint8)

                    async def match():
                        for generation in [1, 2]:
                            await main._eval_condition(condition, runtime, frame, 8, 8,
                                frame_generation=main.frame_buffer.generation + generation)

                    evaluation = asyncio.create_task(match())
                    tasks.append(evaluation)
                    await asyncio.wait_for(keyboard_sent.wait(), timeout=0.5)
                    self.assertFalse(resume_import.is_set())
                    await asyncio.wait_for(evaluation, timeout=0.5)
                finally:
                    resume_import.set()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def test_visual_event_waiting_for_event_bus_is_rejected_after_runtime_reset(self):
        await self.seed()
        condition_id = self.conditions.list_all()[0]["condition_id"]
        self.conditions.activate_only([condition_id])
        condition, old_runtime = self.conditions.get_armed()[0]
        started = asyncio.Event()

        async def emit_old_result():
            started.set()
            return await main._emit_event_and_release("IMG.MATCH." + condition_id, condition, old_runtime)

        async with self.events._lock:
            emission = asyncio.create_task(emit_old_result())
            self.addAsyncCleanup(self.cancel_task, emission)
            await started.wait()
            self.conditions.activate_only([condition_id])
        self.assertFalse(await asyncio.wait_for(emission, timeout=2))
        self.assertIsNone(await self.events.consume_if_available("IMG.MATCH." + condition_id))
        await main._eval_condition(condition, old_runtime, main.np.zeros((8, 8, 3), dtype=main.np.uint8), 8, 8)
        self.assertEqual(self.conditions.get_armed()[0][1].consecutive_hits, 0)

    async def test_visual_boundary_cleanup_preserves_nonvisual_events(self):
        for event in ["HELLO", "EVT:READY", "IMG.MATCH.old", "EVT:IMG.MATCH.old"]:
            await self.events.emit(event)
        await self.events.clear_visual()
        self.assertEqual(await self.events.consume_if_available("HELLO"), "HELLO")
        self.assertEqual(await self.events.consume_if_available("READY"), "EVT:READY")
        self.assertIsNone(await self.events.consume_if_available("IMG.MATCH.old"))

    async def test_same_image_requires_distinct_frames_captured_after_activation(self):
        await self.seed()
        condition_id = self.conditions.list_all()[0]["condition_id"]
        self.conditions.update(condition_id, min_hits=2)
        self.conditions.activate_only([condition_id], frame_generation=10)
        condition, runtime = self.conditions.get_armed()[0]
        frame = main.np.zeros((8, 8, 3), dtype=main.np.uint8)
        event = "IMG.MATCH." + condition_id
        await main._eval_condition(condition, runtime, frame, 8, 8, frame_generation=10)
        self.assertEqual(runtime.consecutive_hits, 0)
        await main._eval_condition(condition, runtime, frame, 8, 8, frame_generation=11)
        self.assertEqual(runtime.consecutive_hits, 1)
        await main._eval_condition(condition, runtime, frame, 8, 8, frame_generation=11)
        self.assertEqual(runtime.consecutive_hits, 1)
        self.assertIsNone(await self.events.consume_if_available(event))
        await main._eval_condition(condition, runtime, frame, 8, 8, frame_generation=12)
        self.assertEqual(await self.events.consume_if_available(event), event)
        self.conditions.activate_only([condition_id], frame_generation=12)
        condition, runtime = self.conditions.get_armed()[0]
        for generation in [11, 12, 12]:
            await main._eval_condition(condition, runtime, frame, 8, 8, frame_generation=generation)
        self.assertEqual(runtime.consecutive_hits, 0)
        for generation in [13, 14]:
            await main._eval_condition(condition, runtime, frame, 8, 8, frame_generation=generation)
        self.assertEqual(await self.events.consume_if_available(event), event)

    async def test_frame_buffer_records_capture_generations(self):
        buffer = main.FrameBuffer()
        frame = main.np.zeros((8, 8, 3), dtype=main.np.uint8)
        buffer._cap = Mock(isOpened=Mock(return_value=True), read=Mock(return_value=(True, frame)))
        for generation in [1, 2]:
            buffer._running = True
            with patch.object(main.time, "sleep", side_effect=lambda _: setattr(buffer, "_running", False)):
                buffer._reader_loop()
            image, current = buffer.read_sample()
            self.assertEqual(current, generation)
            self.assertEqual(buffer.generation, generation)
            self.assertIsNot(image, frame)

    async def test_prefixed_visual_events_keep_condition_ownership_and_bundle_remapping(self):
        await self.seed()
        await main._drain_script_queue()
        condition_id = self.conditions.list_all()[0]["condition_id"]
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock), \
             patch.object(main, "camera", Mock(isOpened=Mock(return_value=True))):
            await self.post({"steps": [{"type": "evt", "evt": "EVT:IMG.MATCH." + condition_id, "delay_ms": 0}]})
            request = self.queue.get_nowait()
            self.queue.task_done()
            self.assertEqual(request.condition_ids, [condition_id])
            self.assertEqual(request.steps[0].evt, "IMG.MATCH." + condition_id)
            bundle = self.image_bundle()
            bundle["steps"][0]["evt"] = "EVT:IMG.MATCH.same-condition"
            response = await self.post(bundle)
            request = self.queue.get_nowait()
            self.queue.task_done()
            imported_id = response["img_import"]["condition_map"]["same-condition"]
            self.assertEqual(request.steps[0].evt, "IMG.MATCH." + imported_id)

    async def test_invalid_servo_map_is_rejected_before_queueing(self):
        with patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            with self.assertRaises(HTTPException) as raised:
                await self.post({"servos": {"invalid": 9}, "steps": [{"type": "evt", "evt": "HELLO", "delay_ms": 0}]})
        self.assertEqual(raised.exception.status_code, 422)
        self.assertTrue(self.queue.empty())

    async def test_runner_initialization_failure_does_not_strand_next_job(self):
        for name in ["A", "B"]:
            await self.queue.put(main.MixedScriptRequest(steps=[main.MixedStep(type="evt", evt=name, delay_ms=0)]))
        with patch.object(main, "_auto_attach_servos", side_effect=[RuntimeError("attach failed"), None]), \
             patch.object(main, "_run_mixed_script", new_callable=AsyncMock) as run:
            with self.assertLogs(main.logger, level="ERROR"):
                await asyncio.wait_for(main._script_runner(), timeout=2)
        self.assertEqual(run.await_count, 1)
        self.assertEqual(run.call_args.args[0].steps[0].evt, "B")
        self.assertTrue(self.queue.empty())
        self.assertEqual(self.conditions.get_armed(), [])
        await asyncio.wait_for(self.queue.join(), timeout=0.2)

    async def test_real_runner_does_not_start_queued_script_after_cancellation(self):
        request = main.MixedScriptRequest(steps=[main.MixedStep(type="evt", evt="WAIT", delay_ms=0)])
        await self.queue.put(request)
        await self.queue.put(request)
        waiting = asyncio.Event()

        async def wait_for_event(*args, **kwargs):
            waiting.set()
            await asyncio.Event().wait()

        with patch.object(self.events, "wait_for", side_effect=wait_for_event) as wait:
            runner = asyncio.create_task(main._script_runner())
            main._script_runner_task = runner
            self.addAsyncCleanup(self.cancel_task, runner)
            await asyncio.wait_for(waiting.wait(), timeout=2)
            response = await asyncio.wait_for(self.post(self.clear_payload()), timeout=2)
            self.assertEqual(response["cleared"]["running_scripts"], 1)
            self.assertEqual(response["cleared"]["queued_scripts"], 1)
            self.assertEqual(wait.await_count, 1)
            self.assertTrue(runner.done())
            await asyncio.wait_for(self.queue.join(), timeout=0.2)

    async def test_clear_serializes_with_inflight_event_release(self):
        await self.seed()
        attaching = asyncio.Event()
        resume_attach = asyncio.Event()
        clear_started = asyncio.Event()
        original_activate = main._activate_script

        async def attach(request):
            attaching.set()
            await resume_attach.wait()
            return await original_activate(request)

        async def clear():
            clear_started.set()
            return await self.post(self.clear_payload())

        with patch.object(main, "_activate_script", side_effect=attach), \
             patch.object(main, "_ensure_script_runner", new_callable=AsyncMock):
            release = asyncio.create_task(main._release_pending_for_event("WAIT"))
            self.addAsyncCleanup(self.cancel_task, release)
            await asyncio.wait_for(attaching.wait(), timeout=2)
            clearing = asyncio.create_task(clear())
            self.addAsyncCleanup(self.cancel_task, clearing)
            await clear_started.wait()
            resume_attach.set()
            released, response = await asyncio.wait_for(asyncio.gather(release, clearing), timeout=2)
        self.assertEqual(released, 1)
        self.assertTrue(response["ok"])
        self.assertEqual(self.queue.qsize(), 0)
        self.assertEqual((await self.pending.snapshot())["total"], 0)

    async def test_http_endpoint_dispatches_clear_and_rejects_malformed_json(self):
        async def request(body):
            messages = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                messages.append(message)

            await main.app({
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/script/run", "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "server": ("test", 80), "client": ("test", 1234),
            }, receive, send)
            status = next(message["status"] for message in messages if message["type"] == "http.response.start")
            body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
            return status, json.loads(body)

        status, body = await request(json.dumps(self.clear_payload()).encode())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        for payload in [b"{", b"[]", b'{"command":"oops"}']:
            with self.subTest(payload=payload):
                status, body = await request(payload)
                self.assertEqual(status, 400)

    async def test_failed_atomic_replace_keeps_original_conditions_file(self):
        await self.seed()
        path = Path(self.temp_dir.name) / "conditions.json"
        before = path.read_bytes()
        with patch.object(main.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(HTTPException) as raised:
                await self.post(self.clear_payload())
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + ".tmp").exists())
        self.assertEqual(len(self.conditions.list_all()), 1)


if __name__ == "__main__":
    unittest.main()