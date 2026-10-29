import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import main
from script_runtime import ScriptRuntime


class ScriptRuntimeTests(unittest.TestCase):
    def test_jobs_have_unique_ids_and_fifo_order_without_exposing_steps_in_status(self):
        runtime = ScriptRuntime()
        first = runtime.enqueue(name="A", steps=[{"type": "evt", "evt": "HELLO"}], loop=False, condition_ids=[])
        second = runtime.enqueue(name=None, steps=[{"type": "kbd", "text": "plain text"}], loop=False, condition_ids=[])
        self.assertNotEqual(first, second)
        self.assertEqual([job["job_id"] for job in runtime.snapshot()["queued"]], [first, second])
        self.assertNotIn("steps", runtime.snapshot()["queued"][0])
        self.assertIsNone(runtime.snapshot()["queued"][0].get("started_at"))
        runtime.activate(first)
        self.assertEqual(runtime.snapshot()["active"]["job_id"], first)
        self.assertGreaterEqual(runtime.snapshot()["active"]["started_at"],
                    runtime.snapshot()["active"]["submitted_at"])
        self.assertEqual([job["job_id"] for job in runtime.snapshot()["queued"]], [second])

    def test_readonly_detail_freezes_full_text_and_omits_large_template(self):
        runtime = ScriptRuntime()
        steps = [{"type": "kbd", "cmd_type": "TYPE", "text": '<full "text">'},
                 {"type": "mse", "action": "IMAGE_MOVE", "image_move": {"template_b64": "large", "threshold": 0.92}}]
        job_id = runtime.enqueue(name="Example", steps=steps, loop=False, condition_ids=["condition1"])
        steps[0]["text"] = "mutated"
        detail = runtime.detail(job_id)
        self.assertEqual(detail["steps"][0]["params"]["text"], '<full "text">')
        self.assertNotIn("template_b64", detail["steps"][1]["params"]["image_move"])
        detail["steps"][0]["params"]["text"] = "changed by caller"
        self.assertEqual(runtime.detail(job_id)["steps"][0]["params"]["text"], '<full "text">')

    def test_step_states_pause_results_and_loop_reset(self):
        runtime = ScriptRuntime()
        job_id = runtime.enqueue(name=None, steps=[{"type": "ocr"}, {"type": "evt"}], loop=True, condition_ids=[])
        runtime.activate(job_id)
        runtime.begin_iteration(0)
        runtime.start_step(0, "recognizing", deadline=123.0)
        runtime.complete_step(0, result={"variable": "digits", "value": "0037"})
        runtime.start_step(1, "waiting", deadline=456.0)
        runtime.set_phase("paused")
        detail = runtime.detail(job_id)
        self.assertEqual(detail["phase"], "paused")
        self.assertIsNone(detail["deadline"])
        self.assertEqual(detail["steps"][0]["state"], "completed")
        self.assertEqual(detail["steps"][0]["result"]["value"], "0037")
        self.assertEqual(detail["steps"][1]["state"], "active")
        runtime.begin_iteration(1)
        self.assertTrue(all(step["state"] == "pending" for step in runtime.detail(job_id)["steps"]))
        self.assertTrue(all(step["result"] is None for step in runtime.detail(job_id)["steps"]))

    def test_finish_removes_job_without_history_and_clear_discards_all(self):
        runtime = ScriptRuntime()
        first = runtime.enqueue(name="A", steps=[], loop=False, condition_ids=[])
        second = runtime.enqueue(name="B", steps=[], loop=False, condition_ids=[])
        runtime.activate(first)
        revision = runtime.snapshot()["revision"]
        runtime.finish(first)
        self.assertIsNone(runtime.detail(first))
        self.assertIsNone(runtime.snapshot()["active"])
        self.assertGreater(runtime.snapshot()["revision"], revision)
        runtime.clear()
        self.assertIsNone(runtime.detail(second))
        self.assertEqual(runtime.snapshot()["queued"], [])

    def test_control_nodes_keep_immutable_params_and_track_child_and_control_state(self):
        runtime = ScriptRuntime()
        submitted = {
            "type": "if_else",
            "condition_id": "door",
            "then_steps": [{"type": "mse", "action": "IMAGE_MOVE",
                            "image_move": {"template_b64": "large", "threshold": 0.9}}],
            "else_steps": [{"type": "kbd", "text": "else"}],
        }
        job_id = runtime.enqueue(name="A", steps=[submitted], loop=False, condition_ids=["door"])
        submitted["then_steps"][0]["type"] = "mutated"
        runtime.activate(job_id)
        runtime.set_control_state(0, branch="then", condition_match=True)
        runtime.start_child_step(0, "then_steps", 0)
        self.assertEqual(
            runtime.detail(job_id)["steps"][0]["children"]["then_steps"][0]["state"],
            "active")
        runtime.complete_child_step(0, "then_steps", 0, result={"sent": True})

        item = runtime.detail(job_id)["steps"][0]
        self.assertEqual(item["params"]["then_steps"][0]["type"], "mse")
        self.assertNotIn("template_b64", item["params"]["then_steps"][0]["image_move"])
        self.assertEqual(item["control"]["branch"], "then")
        self.assertIs(item["control"]["condition_match"], True)
        self.assertEqual(item["children"]["then_steps"][0]["state"], "completed")
        self.assertEqual(item["children"]["then_steps"][0]["result"], {"sent": True})
        self.assertEqual(runtime.snapshot()["active"]["total"], 2)

    def test_control_iteration_resets_child_states_and_failure_is_identified(self):
        runtime = ScriptRuntime()
        job_id = runtime.enqueue(name="A", steps=[{
            "type": "do_while", "condition_id": "door", "repeat_when": "match",
            "max_iterations": 2, "steps": [{"type": "kbd"}],
        }], loop=False, condition_ids=["door"])
        runtime.activate(job_id)
        runtime.set_control_state(0, iteration=1, iteration_limit=2)
        runtime.start_child_step(0, "steps", 0)
        runtime.complete_child_step(0, "steps", 0, error="KBD ACK: ERROR")
        failed_item = runtime.detail(job_id)["steps"][0]
        self.assertEqual(failed_item["children"]["steps"][0]["state"], "failed")
        self.assertEqual(failed_item["children"]["steps"][0]["error"], "KBD ACK: ERROR")
        self.assertEqual(failed_item["error"], "KBD ACK: ERROR")

        runtime.set_control_state(0, iteration=2, iteration_limit=2)
        item = runtime.detail(job_id)["steps"][0]
        self.assertEqual(item["control"]["iteration"], 2)
        self.assertEqual(item["control"]["iteration_limit"], 2)
        self.assertEqual(item["children"]["steps"][0]["state"], "pending")
        self.assertIsNone(item["children"]["steps"][0]["error"])

    def test_runtime_retains_only_the_latest_failed_job(self):
        runtime = ScriptRuntime()
        first = runtime.enqueue(name="A", steps=[{"type": "kbd"}], loop=False, condition_ids=[])
        runtime.activate(first)
        runtime.set_phase("failed")
        runtime.note_error("first failure")
        runtime.finish(first, failed=True)
        self.assertEqual(runtime.detail(first)["phase"], "failed")
        self.assertEqual(runtime.snapshot()["last_failure"]["job_id"], first)

        success = runtime.enqueue(name="success", steps=[], loop=False, condition_ids=[])
        runtime.activate(success)
        runtime.finish(success)
        self.assertEqual(runtime.snapshot()["last_failure"]["job_id"], first)

        second = runtime.enqueue(name="B", steps=[], loop=False, condition_ids=[])
        runtime.activate(second)
        runtime.note_error("second failure")
        runtime.finish(second, failed=True)
        self.assertIsNone(runtime.detail(first))
        self.assertEqual(runtime.detail(second)["error"], "second failure")

        runtime.clear()
        self.assertIsNone(runtime.detail(second))
        self.assertIsNone(runtime.snapshot()["last_failure"])

    def test_gate_consumption_skips_control_nodes_and_preserves_their_children(self):
        request = main.MixedScriptRequest.model_validate({
            "steps": [
                {"type": "evt", "evt": "READY"},
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [{"type": "evt", "evt": "READY"}],
                 "else_steps": [{"type": "kbd"}]},
            ]
        })

        self.assertEqual(main.PendingScriptPool._gate_evt(request), "READY")
        consumed = main.PendingScriptPool._consume_gate_step(request, "READY")
        self.assertEqual([step.type for step in consumed.steps], ["if_else"])
        self.assertEqual(consumed.steps[0].then_steps[0].evt, "READY")
        self.assertEqual(main.PendingScriptPool._gate_evt(consumed), None)

        restored = main.PendingScriptPool._restore_gate_step(consumed, "READY")
        self.assertEqual([step.type for step in restored.steps], ["evt", "if_else"])
        self.assertEqual(restored.steps[1].then_steps[0].evt, "READY")


if __name__ == "__main__":
    unittest.main()