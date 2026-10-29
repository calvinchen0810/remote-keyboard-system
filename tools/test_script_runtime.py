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