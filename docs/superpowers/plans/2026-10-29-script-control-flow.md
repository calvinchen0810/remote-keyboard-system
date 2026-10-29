# Script Editor Control-Flow Blocks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Scratch-like, non-nested IF/ELSE and do-while blocks to the existing Script Editor, execute them in the FIFO runtime, and preserve structured scripts through import/export and Execution Status.

**Architecture:** Keep the existing step-list editor and add typed control nodes whose children are ordinary `MixedStep` actions. Extend the existing FastAPI execution loop to interpret these nodes, evaluating visual Conditions against a fresh frame, and extend `ScriptRuntime` to publish block progress and retain only the latest failed job. Update the existing inline editor, bundle import/export paths, runtime view, and focused tests without replacing the current architecture.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, asyncio, OpenCV; inline JavaScript in `server\static\index.html`; Node's built-in `node:test`; Python `unittest`.

## Implementation result

All six tasks were implemented and individually reviewed on branch `agents/a`.
The whole-feature review found no significant issues.

- Editor: 60 tests passed.
- Script validation, interpreter, and runtime: 48 tests passed.
- Clear-command regression suite: 64 tests run, 2 skipped, no failures.
- Headless Edge DOM smoke check passed native drag/drop through THEN, ELSE,
  BODY and top level, editable iteration/repeat controls, and unwrap deletion.
- Real hardware execution was intentionally not performed.
- Required previously untracked baseline modules and focused tests were
  preserved in commit `93834ea` without changing the original workspace.

## Global Constraints

- Blocks are not nestable; ordinary existing step types may be placed inside their slots.
- A control block uses one existing visual Condition; do not add AND/OR expressions or OCR predicates.
- IF and each do-while decision use one fresh frame; wait at most 5 seconds for a new frame, then fail explicitly.
- IF immediately selects then on a match and else on a non-match; control decisions ignore `min_hits` and cooldown.
- A do-while body runs at least once; `repeat_when` is `match` or `not_match`.
- New do-while blocks default to `repeat_when: "match"` (confirmed by the user).
- `max_iterations` is an integer from 1 through 10,000, default 10; when another iteration is still required at the limit, fail and stop the job.
- Preserve the existing script-wide `loop` option and its current whole-script behavior.
- Keep the total number of ordinary steps across the complete script at or below 200; control nodes do not count.
- At submit time, an IF must have at least one populated branch; a do-while body must be non-empty.
- New structured scripts and image bundles must round-trip; legacy flat-step JSON remains accepted.
- Preserve FIFO ordering, PAUSE / RESUME / STOP behavior, and the existing action-step semantics.
- Keep only the most recent failed job, including detail; successful jobs are still removed immediately. STOP / workspace clear removes the retained failure.

---

## File map

- `server\main.py` — Pydantic control-node models, request validation, tree traversal for action/condition/servo discovery, image-condition matching, fresh-frame wait, script execution, and bundle import.
- `server\script_runtime.py` — immutable runtime snapshots for structured nodes, branch/iteration state, failed-job retention, and per-child action state.
- `server\static\index.html` — editor model, control-block rendering and drag/drop, import/export and image-bundle traversal, script submission, and read-only Execution Status.
- `tools\test_script_control_flow.py` — new backend validation, matching, interpreter, FIFO, and bundle tests.
- `tools\test_script_runtime.py` — runtime snapshot isolation, nested action state, branch/iteration metadata, and most-recent-failure retention.
- `tools\test_script_editor.cjs` — editor block operations, serialization, bundle round-trips, legacy input, and runtime UI assertions.
- `README.md` — public script JSON and execution semantics.
- `server\static\arch.html` — architecture feature description and the feature-coverage test contract.

The application currently keeps these responsibilities in `main.py` and `index.html`; this plan follows those established entry points and does not introduce a broad refactor.

## Task 1: Add typed control nodes and request validation

**Files:**
- Modify: `server\main.py` (MixedStep and MixedScriptRequest models, script normalization/submit helpers)
- Create: `tools\test_script_control_flow.py`
- Test: `tools\test_script_control_flow.py`
- Regression test: `tools\test_script_runtime.py`

**Interfaces:**
- Produces `IfElseBlock(type="if_else", condition_id, then_steps, else_steps)`.
- Produces `DoWhileBlock(type="do_while", condition_id, repeat_when, max_iterations, steps)`.
- Produces `ScriptNode = Annotated[Union[IfElseBlock, DoWhileBlock, MixedStep], Field(discriminator="type")]`.
- Produces `_iter_action_steps(steps) -> Iterator[MixedStep]`, traversing only ordinary actions at top level and in the two allowed child slots.
- Produces `_collect_condition_ids(steps) -> list[str]`, including control-block Condition IDs and nested `IMG.MATCH.<id>` event steps.
- Produces `_normalize_script_request(req) -> MixedScriptRequest`, retaining legacy event normalization at top level and inside each control slot.

- [ ] **Step 1: Write failing schema and validation tests**

Add these cases to `tools\test_script_control_flow.py`:

```python
import sys
import unittest
from pathlib import Path
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import main


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
        self.assertEqual(request.steps[0].then_steps[0].type, "kbd")
        self.assertEqual(request.steps[1].repeat_when, "not_match")

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
            {"steps": [{"type": "if_else", "then_steps": [action], "else_steps": []}]},
            {"steps": [{"type": "if_else", "condition_id": "door",
                        "then_steps": [action] * 201, "else_steps": []}]},
            {"steps": [action] * 100 + [
                {"type": "if_else", "condition_id": "door",
                 "then_steps": [action] * 101, "else_steps": []}
            ]},
            {"steps": [{"type": "unknown"}]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                main.MixedScriptRequest.model_validate(payload)
```

- [ ] **Step 2: Run the focused tests and verify the new schema is missing**

Run: `server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v`

Expected: FAIL because the control-node models and request validation do not exist.

- [ ] **Step 3: Implement the discriminated script-node models**

Change `MixedStep.type` to `Literal["srv", "kbd", "mse", "evt", "ocr"]`, which preserves its existing allowed values while enabling reliable discrimination. Define `IfElseBlock` and `DoWhileBlock` after it, then use a discriminated `ScriptNode` union for both request models' `steps`. Keep block children typed as `list[MixedStep]`, preserve all old action fields/defaults, constrain `max_iterations` to 1–10,000, and constrain `repeat_when` to `"match"` / `"not_match"`. Import `Annotated`, `Literal`, `Union`, `Iterator`, and `Sequence` from `typing` as needed.

```python
class IfElseBlock(BaseModel):
    type: Literal["if_else"]
    condition_id: str = Field(..., min_length=1)
    then_steps: list[MixedStep]
    else_steps: list[MixedStep]


class DoWhileBlock(BaseModel):
    type: Literal["do_while"]
    condition_id: str = Field(..., min_length=1)
    repeat_when: Literal["match", "not_match"]
    max_iterations: int = Field(default=10, ge=1, le=10_000)
    steps: list[MixedStep]


ScriptNode = Annotated[
    Union[IfElseBlock, DoWhileBlock, MixedStep],
    Field(discriminator="type"),
]
```

Include a test that parses each node to its intended model and rejects an unknown `type`; this prevents ordinary-step parsing from swallowing control-node fields.

- [ ] **Step 4: Add aggregate validation and traversal helpers**

Remove the old top-level `max_length=200` checks from both request fields; validate the total ordinary-action count across all slots instead. Add request validation for non-nested controls, populated IF branches, non-empty do-while bodies, and at most 200 ordinary actions. Add `_iter_action_steps` and `_collect_condition_ids`; include block Conditions and IMG event IDs. Update `_normalize_script_request` to normalize child EVT steps without changing flat payload behavior.

```python
def _iter_action_steps(steps: Sequence[ScriptNode]) -> Iterator[MixedStep]:
    for node in steps:
        if isinstance(node, MixedStep):
            yield node
        elif isinstance(node, IfElseBlock):
            yield from node.then_steps
            yield from node.else_steps
        else:
            yield from node.steps
```

Add a `MixedScriptRequest` `@model_validator(mode="after")` that uses this iterator for the 200-step count and checks the empty-slot rules. Add a counterpart validator to `ScriptBundleImportRequest`.

Update the EventBus gate helpers to handle the widened node union: `_gate_evt` and `_consume_gate_step` may inspect only ordinary top-level EVT nodes, must leave control nodes untouched, and must not consume EVT children inside a control block. `_restore_gate_step` must preserve the same top-level ordering. Add regression coverage for a request containing both a gate EVT and a control node.

The regression test should call `_consume_gate_step` with a top-level `EVT READY` followed by an IF branch containing its own `EVT READY`; assert only the top-level gate is removed and the control node plus its child remain unchanged. Also pass a non-EVT control node through `_gate_evt` to confirm it is skipped safely.

- [ ] **Step 5: Run focused and compatibility tests**

Run:

```powershell
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_runtime.py -v
```

Expected: all new validation cases and existing runtime tests pass; legacy `MixedStep` payloads still parse.

- [ ] **Step 6: Commit the schema and validation task**

```powershell
git add -- server\main.py tools\test_script_control_flow.py
git commit -m "feat: validate structured script control blocks" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Task 2: Share image matching and wait for fresh frames

**Files:**
- Modify: `server\main.py` (visual condition matching, FrameBuffer use, `/visual/conditions/{cid}/test`)
- Modify: `tools\test_script_control_flow.py`
- Test: `tools\test_script_control_flow.py`

**Interfaces:**
- Produces `_match_condition_frame(condition: MatchCondition, frame: Any) -> tuple[float, bool]`.
- Produces `async _evaluate_condition_fresh(condition_id: str, timeout_seconds: float = CONTROL_FRAME_TIMEOUT_SECONDS) -> tuple[float, bool]`.
- `CONTROL_FRAME_TIMEOUT_SECONDS` is exactly `5.0`.

- [ ] **Step 1: Write failing tests for matching and fresh-frame behavior**

Use the existing image-matching inputs (condition ROI, threshold, snapshot JPEG) to assert match and non-match scores. Add an async fake buffer whose `read_sample()` returns frame generations; assert the evaluator ignores the current generation, accepts only a newer one, and raises `TimeoutError` when no new frame arrives. Test missing Condition and missing Snapshot as explicit errors. Inject a short timeout in the stalled-buffer test so the test does not wait five seconds.

```python
import asyncio
import base64
import unittest
from unittest.mock import patch
import cv2
import numpy as np


class FreshConditionFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_condition_evaluation_waits_for_a_new_frame(self):
        frame = np.arange(32 * 32 * 3, dtype=np.uint8).reshape((32, 32, 3))
        ok, encoded = cv2.imencode(".png", frame)
        self.assertTrue(ok)
        condition = main.MatchCondition(
            condition_id="door", name="Door", snapshot_id="snapshot",
            roi=[0.0, 0.0, 1.0, 1.0], threshold=0.9, min_hits=10,
        )

        class SampleBuffer:
            generation = 10

            def __init__(self):
                self.samples = iter([(frame, 10), (frame, 11)])

            def read_sample(self):
                return next(self.samples)

        async def no_pause():
            return None

        with patch.object(main.condition_manager, "get", return_value=condition), \
             patch.object(main.snapshot_pool, "get",
                          return_value={"jpeg_b64": base64.b64encode(encoded).decode()}), \
             patch.object(main, "frame_buffer", SampleBuffer()), \
             patch.object(main, "_await_script_resume", new=no_pause):
            score, matched = await main._evaluate_condition_fresh("door", timeout_seconds=0.05)
        self.assertGreaterEqual(score, 0.9)
        self.assertTrue(matched)

```

Add sibling tests for non-match, missing Condition, missing Snapshot, and a stalled sample buffer. Inject `timeout_seconds=0.01` for the stalled-buffer test. Do not rely on camera hardware.

- [ ] **Step 2: Run the tests and verify they fail for the missing helper**

Run: `server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v`

Expected: FAIL because the shared matcher and fresh-frame evaluator are not implemented.

- [ ] **Step 3: Extract the common single-frame matcher**

Define `CONTROL_FRAME_TIMEOUT_SECONDS = 5.0`. Implement `_match_condition_frame` using the existing ROI crop, snapshot decoding, resize, `cv2.matchTemplate`, and threshold comparison. Use it from both `_evaluate_condition_fresh` and `visual_test_condition` so the comparison behavior does not diverge. The fresh-frame test sets `min_hits=10` and verifies that one matching frame still succeeds, proving that event debounce does not affect a control decision. Missing conditions/snapshots and invalid frames raise clear errors; do not turn errors into a non-match.

```python
def _match_condition_frame(condition: MatchCondition, frame: Any) -> tuple[float, bool]:
    snapshot = snapshot_pool.get(condition.snapshot_id)
    if snapshot is None:
        raise RuntimeError(f"Snapshot for condition {condition.condition_id} is unavailable")
    if frame is None or not hasattr(frame, "shape") or len(frame.shape) < 2:
        raise ValueError("Camera returned an invalid frame")
    fh, fw = frame.shape[:2]
    rx, ry, rw, rh = condition.roi
    x1, y1 = max(0, int(rx * fw)), max(0, int(ry * fh))
    x2, y2 = min(fw, int((rx + rw) * fw)), min(fh, int((ry + rh) * fh))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"Invalid ROI for condition {condition.condition_id}")
    curr_roi = frame[y1:y2, x1:x2]
    template = cv2.imdecode(
        np.frombuffer(base64.b64decode(snapshot["jpeg_b64"]), dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if template is None:
        raise ValueError(f"Snapshot for condition {condition.condition_id} is invalid")
    sh, sw = template.shape[:2]
    tx1, ty1 = max(0, int(rx * sw)), max(0, int(ry * sh))
    tx2, ty2 = min(sw, int((rx + rw) * sw)), min(sh, int((ry + rh) * sh))
    template_roi = template[ty1:ty2, tx1:tx2]
    if template_roi.size == 0:
        raise ValueError(f"Invalid snapshot ROI for condition {condition.condition_id}")
    if template_roi.shape[:2] != curr_roi.shape[:2]:
        template_roi = cv2.resize(template_roi, (curr_roi.shape[1], curr_roi.shape[0]))
    score = float(cv2.matchTemplate(curr_roi, template_roi, cv2.TM_CCOEFF_NORMED)[0][0])
    return score, score >= condition.threshold
```

- [ ] **Step 4: Implement the five-second distinct-frame wait and rerun tests**

Capture the starting generation, repeatedly read `frame_buffer.read_sample()`, and evaluate only a non-null sample with a greater generation. Honor `_await_script_resume()` while waiting and raise `TimeoutError` at five seconds. Run the focused test module and `server\env\Scripts\python.exe -m unittest discover -s tools -p test_image_move.py -v`.

```python
async def _evaluate_condition_fresh(
    condition_id: str,
    timeout_seconds: float = CONTROL_FRAME_TIMEOUT_SECONDS,
) -> tuple[float, bool]:
    condition = condition_manager.get(condition_id)
    if condition is None or snapshot_pool.get(condition.snapshot_id) is None:
        raise RuntimeError(f"Condition {condition_id} is unavailable")
    remaining = timeout_seconds
    initial_generation = frame_buffer.generation
    loop = asyncio.get_running_loop()
    while remaining > 0:
        await _await_script_resume()
        started = loop.time()
        frame, generation = frame_buffer.read_sample()
        if frame is not None and generation > initial_generation:
            if condition_manager.get(condition_id) is not condition:
                raise RuntimeError(f"Condition {condition_id} was removed during evaluation")
            return _match_condition_frame(condition, frame)
        await asyncio.sleep(min(0.01, remaining))
        remaining -= loop.time() - started
    raise TimeoutError(f"No fresh camera frame for condition {condition_id} within 5 seconds")
```

Expected: fresh-generation, timeout, and existing image-move tests pass.

- [ ] **Step 5: Commit the fresh-frame evaluator**

```powershell
git add -- server\main.py tools\test_script_control_flow.py
git commit -m "feat: evaluate control conditions on fresh frames" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Task 3: Execute control nodes and expose structured runtime state

**Files:**
- Modify: `server\main.py` (mixed-script dispatch and FIFO runner failure path)
- Modify: `server\script_runtime.py`
- Modify: `tools\test_script_runtime.py`
- Modify: `tools\test_script_control_flow.py`
- Test: `tools\test_script_runtime.py`
- Test: `tools\test_script_control_flow.py`

**Interfaces:**
- Produces `ScriptRuntime.set_control_state(index, *, branch=None, condition_match=None, iteration=None, iteration_limit=None)`.
- `set_control_state(..., iteration=n)` resets do-while child states when beginning a new body iteration.
- Produces `ScriptRuntime.start_child_step(index, slot, child_index, phase="running", deadline=None)` and `complete_child_step(index, slot, child_index, result=None)`.
- Changes `ScriptRuntime.finish(job_id, *, failed=False)` so only the newest failed job is retained; `clear()` removes it.
- `snapshot()` exposes a lightweight `last_failure` summary; `detail(job_id)` returns active, queued, or retained-failure detail.
- Mixed-script execution dispatches ordinary `MixedStep`, `IfElseBlock`, and `DoWhileBlock` nodes while preserving current outer-loop variables and script-wide loop behavior.

- [ ] **Step 1: Add failing runtime tracker tests**

In `tools\test_script_runtime.py`, assert structured action children are immutable, child status can become active/completed/failed, control branch and iteration metadata are preserved, successful `finish(job_id)` removes a job, failed `finish(job_id, failed=True)` retains the latest failed detail, a later success does not erase it, a newer failure replaces it, and `clear()` removes it.

```python
def test_runtime_retains_only_the_latest_failed_job():
    runtime = ScriptRuntime()
    first = runtime.enqueue(name="A", steps=[{"type": "kbd"}], loop=False, condition_ids=[])
    runtime.activate(first)
    runtime.set_phase("failed")
    runtime.finish(first, failed=True)
    assert runtime.detail(first)["phase"] == "failed"
    assert runtime.snapshot()["last_failure"]["job_id"] == first
```

- [ ] **Step 2: Add failing interpreter tests**

Add `IsolatedAsyncioTestCase` cases that patch `_evaluate_condition_fresh`, action dispatch, WebSocket notifications, and runtime state. Verify every ordinary action type can execute as a child; IF match/non-match actions; do-while runs once before checking; both `repeat_when` modes; normal exit at the limit; `RuntimeError` when the last permitted iteration still requests repetition; and no action after a limit failure. Use a one-action body and a configured maximum of 1 for the limit-failure case. Also verify a paused block does not start its next action until RESUME, STOP cancels a fresh-frame wait, and an active job's block never lets a queued job overtake it.

- [ ] **Step 3: Run runtime and interpreter tests to establish the failures**

Run:

```powershell
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_runtime.py -v
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v
```

Expected: FAIL on missing runtime control-state methods, failure retention, and control-node dispatch.

- [ ] **Step 4: Store control and child-action state in `ScriptRuntime`**

Keep each submitted node immutable. Store per-child status for `then_steps`, `else_steps`, or the do-while body; expose branch, match result, iteration, and iteration limit in detail. Count ordinary child actions for the job summary. Reset child states/results in `begin_iteration()` when the existing script-wide Loop starts another pass. Recursively strip `image_move.template_b64` from runtime params just as for top-level actions.

Use this status shape for every control-node item and preserve child `params` separately from child state:

```python
{
    "params": control_node,
    "state": "active",
    "control": {"branch": "then", "condition_match": True,
                "iteration": 1, "iteration_limit": None},
    "children": {"then_steps": [{"state": "pending", "result": None, "error": None}]},
}
```

`set_control_state`, `start_child_step`, and `complete_child_step` update only the named job item and increment the runtime revision.

- [ ] **Step 5: Retain the newest failed runtime detail**

Add `_last_failure` to `ScriptRuntime`. Keep successful `finish(job_id)` behavior unchanged; failed finish deep-copies one detail before removing it:

```python
def finish(self, job_id: str, *, failed: bool = False) -> None:
    job = self._jobs.get(job_id)
    if job is not None and failed:
        job["phase"] = "failed"
        self._last_failure = copy.deepcopy(job)
    self._jobs.pop(job_id, None)
    if self.active_id == job_id:
        self.active_id = None
    self.revision += 1
```

Return the failure summary from `snapshot()["last_failure"]`; let `detail(job_id)` return that retained detail if it is not an active/queued job. `clear()` clears `_last_failure`.

- [ ] **Step 6: Extract ordinary action execution without changing behavior**

Move the current ordinary-step handling out of the flat `for` body into `_run_action_step(step, *, top_index, variables, slot=None, child_index=None)`. Preserve the same variables dictionary for top-level and child actions within one outer script iteration. Preserve EVT wait, delay, OCR variables, SRV wait/error, KBD result, MSE/IMAGE_MOVE, and runtime updates. Start and complete the top-level or child runtime entry through the appropriate `ScriptRuntime` method.

Use this exact call for ordinary actions already in the current flat loop:

```python
await _run_action_step(step, top_index=i, variables=variables)
```

For a block child, pass its owner and slot:

```python
await _run_action_step(child, top_index=i, variables=variables,
                       slot=branch, child_index=child_index)
```

Move the existing handlers from `_run_mixed_script` verbatim into `_run_action_step`; replace the old `i` references with `top_index`, and route state changes through the top-level or child runtime entry. Do not change commands, delays, OCR variables, hardware waits, or cancellation guards.

- [ ] **Step 7: Dispatch IF/ELSE and do-while nodes**

Call the action helper for top-level ordinary nodes and for selected block children. Use the approved sequence below; `_run_action_step` records the child state and `_evaluate_condition_fresh` enforces the five-second frame deadline:

```python
if isinstance(step, IfElseBlock):
    _, matched = await _evaluate_condition_fresh(step.condition_id)
    branch = "then_steps" if matched else "else_steps"
    script_runtime.set_control_state(i, branch="then" if matched else "else",
                                     condition_match=matched)
    for child_index, child in enumerate(getattr(step, branch)):
        await _run_action_step(child, top_index=i, variables=variables,
                               slot=branch, child_index=child_index)
elif isinstance(step, DoWhileBlock):
    for current in range(1, step.max_iterations + 1):
        script_runtime.set_control_state(i, iteration=current,
                                         iteration_limit=step.max_iterations)
        for child_index, child in enumerate(step.steps):
            await _run_action_step(child, top_index=i, variables=variables,
                                   slot="steps", child_index=child_index)
        _, matched = await _evaluate_condition_fresh(step.condition_id)
        script_runtime.set_control_state(i, condition_match=matched)
        should_repeat = matched if step.repeat_when == "match" else not matched
        if not should_repeat:
            break
        if current == step.max_iterations:
            raise RuntimeError(f"Do-while iteration limit reached ({step.max_iterations})")
```

- [ ] **Step 8: Persist and publish failures from the FIFO runner**

In `_script_runner`, track whether the job raised an exception and pass that flag to the single `finish()` call in `finally`; this retains every failed job but keeps cancellation and explicit STOP as non-failure removal. Notify runtime after recording the failure. Run:

```powershell
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_runtime.py -v
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v
server\env\Scripts\python.exe -m unittest discover -s tools -p test_clear_command.py -v
```

Expected: new control-flow and latest-failure tests pass; STOP / clear still discard active and queued jobs.

- [ ] **Step 9: Commit the runtime task**

```powershell
git add -- server\main.py server\script_runtime.py tools\test_script_runtime.py tools\test_script_control_flow.py
git commit -m "feat: execute script control-flow blocks" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Task 4: Add editor control blocks and drag/drop

**Files:**
- Modify: `server\static\index.html` (script toolbar, step model, renderer, drag/drop, delete controls)
- Modify: `tools\test_script_editor.cjs`
- Test: `tools\test_script_editor.cjs`

**Interfaces:**
- Produces `addIfElseBlock(conditionId = "")` and `addDoWhileBlock(conditionId = "")`; do-while defaults to `repeat_when: "match"` and `max_iterations: 10`.
- Produces `findStepLocation(stepId) -> { list, index, step }`.
- Produces `moveStepToSlot(stepId, blockId, slot)` for `then_steps`, `else_steps`, or do-while `steps`.
- Produces `moveStepToTopLevel(stepId, beforeStepId = null)`.
- Every action step has a unique `_id` regardless of its container; control nodes also have unique `_id`s.

- [ ] **Step 1: Extend the editor VM test harness**

Update `createEditor()` in `tools\test_script_editor.cjs` to provide `steps-tbody`, `empty-hint`, `step-cnt`, and `document.createElement()` mocks that preserve event handlers and child nodes. Keep the existing function-extraction boundaries valid when adding helpers before `collectImgBundle()`.

- [ ] **Step 2: Add failing editor model and movement tests**

In `tools\test_script_editor.cjs`, add tests for default block shapes, moving an action from top-level into then/else/body, moving it between slots, moving it back to top level in a chosen position, and rejecting a control node as a child. Assert that an action exists exactly once after every move.

```js
test('moving an action between block slots preserves one copy and its id', () => {
  const editor = createEditor();
  editor.addKbdStep();
  const actionId = editor.steps[0]._id;
  const branch = editor.addIfElseBlock('door');
  editor.moveStepToSlot(actionId, branch._id, 'then_steps');
  editor.moveStepToSlot(actionId, branch._id, 'else_steps');
  assert.equal(editor.steps[0].then_steps.length, 0);
  assert.equal(editor.steps[0].else_steps[0]._id, actionId);
});
```

- [ ] **Step 3: Run the editor tests and verify the new APIs are missing**

Run: `node --test tools\test_script_editor.cjs`

Expected: the new tests fail because control-node and move helpers do not exist.

- [ ] **Step 4: Add creation, lookup, and move helpers**

Implement the interfaces above with source and destination list lookups. Move the original object rather than cloning it; reject missing IDs, control nodes as children, unknown slots, and moving a block into itself. Keep existing ordinary-step creation and editing functions operating on their located action object rather than assuming every action is top-level.

```js
function findStepLocation(stepId) {
  for (let index = 0; index < steps.length; index++) {
    const node = steps[index];
    if (node._id === stepId) return { list: steps, index, step: node };
    for (const slot of node.type === 'if_else' ? ['then_steps', 'else_steps'] :
      node.type === 'do_while' ? ['steps'] : []) {
      const list = node[slot] || [];
      const childIndex = list.findIndex(item => item._id === stepId);
      if (childIndex >= 0) return { list, index: childIndex, step: list[childIndex] };
    }
  }
  return null;
}
```

- [ ] **Step 5: Route existing step editors through nested lookup**

Update `updStep`, `updStepKbd`, `updStepMse`, `delStep`, `insertKbdVariable`, and `addImageMoveStep` to resolve IDs with `findStepLocation`. Keep OCR variables in the same runtime dictionary. For the variable picker, list earlier OCR variables in the current branch/body plus prior top-level actions; after an IF/ELSE, expose a branch-local variable only when both branches define it. The do-while body runs at least once, so its earlier OCR actions are available later in that body and after the block.

```js
function updStep(id, key, value) {
  const location = findStepLocation(id);
  if (!location || location.step.type === 'if_else' || location.step.type === 'do_while') return;
  const numeric = ['delay_ms', 'servo_id', 'angle', 'speed', 'duration_ms',
    'home', 'x', 'y', 'amount', 'timeout_ms', 'stable_hits'];
  location.step[key] = numeric.includes(key) ? parseInt(value, 10) || 0 : value;
}
```

- [ ] **Step 6: Render blocks in the existing step table**

Add `＋ IF / ELSE` and `＋ DO-WHILE` toolbar controls. Render control headers with Condition selector and (for do-while) repeat direction and numeric max input (`min=1`, `max=10000`, default `10`); render child slots as indented drop areas. Wire native drag/drop to `moveStepToSlot` and `moveStepToTopLevel`, and prevent control nodes from entering child slots. Escape Condition labels and values using the existing `esc` helper.

The drop handler must move the dragged node, not copy it:

```js
dropArea.addEventListener('drop', event => {
  event.preventDefault();
  const stepId = Number(event.dataTransfer.getData('text/plain'));
  moveStepToSlot(stepId, blockId, slot);
});
```

- [ ] **Step 7: Add delete-choice and empty-block submission validation tests**

Test that the delete chooser offers Unwrap, Delete All, and Cancel; Unwrap preserves child order at the block's former top-level position, Delete All removes all descendants, and Cancel changes nothing. Test IF empty-side rules, empty do-while rejection, invalid max values, and valid maximum 10,000.

- [ ] **Step 8: Implement deletion, validation, and rerun editor tests**

Use an in-editor choice dialog with three explicit actions:

```html
<div id="block-delete-dialog" hidden>
  <button onclick="resolveBlockDelete('unwrap')">Unwrap steps</button>
  <button onclick="resolveBlockDelete('delete')">Delete all</button>
  <button onclick="resolveBlockDelete('cancel')">Cancel</button>
</div>
```

Keep the dialog scoped to the selected block ID. Add pre-submit validation:

```js
function validateControlFlowNodes(nodes) {
  for (const node of nodes) {
    if (node.type === 'if_else' && !node.then_steps.length && !node.else_steps.length) {
      throw new Error('IF / ELSE needs at least one step.');
    }
    if (node.type === 'do_while' &&
        (!node.steps.length || !Number.isInteger(node.max_iterations) ||
         node.max_iterations < 1 || node.max_iterations > 10000)) {
      throw new Error('DO-WHILE needs steps and a limit from 1 to 10000.');
    }
  }
}
```

Catch validation errors in `runScript()`, write them with `addLog`, and leave the draft editable. Run `node --test tools\test_script_editor.cjs`.

Expected: all editor tests pass, including existing behavior.

- [ ] **Step 9: Commit the editor interaction task**

```powershell
git add -- server\static\index.html tools\test_script_editor.cjs
git commit -m "feat: add draggable script control blocks" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Task 5: Preserve structured blocks through import, export, and bundles

**Files:**
- Modify: `server\static\index.html` (normalization, payload creation, bundle collection, import)
- Modify: `server\main.py` (bundle request, nested ID remapping, condition IDs, servo discovery)
- Modify: `tools\test_script_editor.cjs`
- Modify: `tools\test_script_control_flow.py`
- Test: `tools\test_script_editor.cjs`
- Test: `tools\test_script_control_flow.py`

**Interfaces:**
- Produces `normalizeImportedSteps(rawSteps)`, assigning `_id` recursively while preserving the node structure.
- Produces `collectImgBundle()` over all ordinary actions and both control branches/body; control `condition_id` references are included alongside nested IMG event IDs.
- Structured JSON export uses `schema_version: 6`; existing schema versions remain accepted.
- Server bundle remapping updates control `condition_id` and nested `IMG.MATCH.<old_id>` events in every slot.

- [ ] **Step 1: Add failing editor round-trip and image-bundle tests**

Build an IF containing an IMG event in its else slot and a do-while referencing a Condition. Assert exported JSON contains the condition and deduplicated snapshot, `schema_version` is 6, import preserves both blocks and their order, and all nested actions receive unique `_id`s. Assert a legacy flat JSON payload still imports unchanged.

- [ ] **Step 2: Add failing backend bundle-remap and recursive-discovery tests**

Assert `_collect_condition_ids` sees control Conditions plus nested IMG event IDs exactly once. Assert `_auto_attach_servos` discovers SRV steps inside all slots. Patch bundle pools and assert `_import_script_bundle` remaps a control `condition_id` and nested IMG event IDs while leaving unrelated IDs unchanged.

- [ ] **Step 3: Run focused tests and verify nested references are currently missed**

Run:

```powershell
node --test tools\test_script_editor.cjs
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v
```

Expected: new tests fail because current collection, normalization, bundle import, and auto-attach only inspect top-level steps.

- [ ] **Step 4: Implement frontend recursive import/export**

Traverse structured child arrays in `normalizeImportedSteps` and `collectImgBundle`; include both control Condition IDs and IMG event Condition IDs. Preserve unique snapshots. Export control-flow scripts as schema version 6. Import bundles using the full structured steps, then normalize all nodes and assign unique editor IDs.

```js
function visitActionSteps(nodes, visit) {
  for (const node of nodes) {
    if (node.type === 'if_else') {
      visitActionSteps(node.then_steps, visit);
      visitActionSteps(node.else_steps, visit);
    } else if (node.type === 'do_while') {
      visitActionSteps(node.steps, visit);
    } else {
      visit(node);
    }
  }
}
```

Use this traversal to assign child `_id`s and collect ordinary-step Conditions. Add each control node's own `condition_id` to the same deduplicated Condition set. Set schema version 6 whenever any top-level node is `if_else` or `do_while`; retain the existing version selection for scripts without control nodes.

- [ ] **Step 5: Implement server recursive bundle and dependency handling**

Use `_iter_action_steps` and `_collect_condition_ids` in `_submit_script`, condition validation, and `_auto_attach_servos`. In `_import_script_bundle`, copy the node structure and remap control Condition IDs and IMG event names through every allowed slot. Validate that all Condition IDs resolve to a Condition with a live Snapshot before enqueueing; keep old flat payloads unchanged.

```python
def _remap_imported_node(node: dict[str, Any], condition_map: dict[str, str]) -> dict[str, Any]:
    result = dict(node)
    if result.get("type") == "if_else":
        result["condition_id"] = condition_map[result["condition_id"]]
        result["then_steps"] = [_remap_imported_node(child, condition_map)
                                for child in result["then_steps"]]
        result["else_steps"] = [_remap_imported_node(child, condition_map)
                                for child in result["else_steps"]]
    elif result.get("type") == "do_while":
        result["condition_id"] = condition_map[result["condition_id"]]
        result["steps"] = [_remap_imported_node(child, condition_map)
                           for child in result["steps"]]
    elif result.get("type") == "evt" and result.get("evt", "").startswith("IMG.MATCH."):
        old_id = result["evt"][len("IMG.MATCH."):]
        result["evt"] = f"IMG.MATCH.{condition_map[old_id]}"
    return result
```

Run this only after checking that every referenced Condition has a mapping; return an explicit 422 for a missing bundle Condition rather than allowing a `KeyError` or leaving the old ID in the script.

- [ ] **Step 6: Rerun editor and backend round-trip tests**

Run:

```powershell
node --test tools\test_script_editor.cjs
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v
```

Expected: new structured and legacy round-trips pass, as do missing-reference and ID-remapping tests.

- [ ] **Step 7: Commit the import/export task**

```powershell
git add -- server\main.py server\static\index.html tools\test_script_editor.cjs tools\test_script_control_flow.py
git commit -m "feat: round-trip control blocks and image conditions" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Task 6: Show block execution, latest failure, and document the API

**Files:**
- Modify: `server\static\index.html` (runtime job selection, nested detail, control status, progress)
- Modify: `server\static\arch.html` (control-flow feature and latest-failure behavior)
- Modify: `README.md` (JSON format, behavior, limits, and frame timeout)
- Modify: `tools\test_script_editor.cjs`
- Test: `tools\test_script_editor.cjs`
- Regression tests: `tools\test_script_runtime.py`, `tools\test_clear_command.py`

**Interfaces:**
- Runtime selector displays active and queued jobs plus `runtime.last_failure` when present.
- Runtime detail presents branch/match result, do-while iteration/maximum, nested action states, and explicit job error.
- Successful jobs remain absent after finish; the latest failure remains until another failure replaces it or STOP / workspace clear removes it.

- [ ] **Step 1: Add failing runtime-view and latest-failure UI tests**

Assert active IF detail shows match result and chosen branch, do-while shows iteration/max, child steps show their state, and the retained failed job is selectable after the runner has no active job. Assert a successful job is not retained and STOP/clear removes the retained failure. Extend the architecture-page feature assertion to require `data-feature="control-flow"`.

- [ ] **Step 2: Run frontend and runtime tests to verify they fail**

Run:

```powershell
node --test tools\test_script_editor.cjs
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_runtime.py -v
```

Expected: failures identify missing runtime tree rendering / latest-failure selection or feature documentation.

- [ ] **Step 3: Implement structured Execution Status rendering**

Update runtime row rendering to distinguish control nodes and ordinary actions. Keep the view read-only, escape all server-supplied strings, render only the active branch's progress as active, show both branch contents as immutable submitted steps, and display do-while iteration/max and condition result. Include the retained failure in the job selector without making it active or enabling control buttons for it. Preserve the existing no-history behavior for successful jobs.

Render the control row from `item.control` and recursively render only the allowed child arrays:

```js
function runtimeControlSummary(item) {
  const control = item.control || {};
  if (item.params.type === 'if_else') {
    if (control.condition_match == null) return 'IF pending';
    return `IF ${control.condition_match ? 'MATCH → THEN' : 'NO MATCH → ELSE'}`;
  }
  if (control.iteration == null) return 'DO-WHILE pending';
  return `DO-WHILE ${control.iteration}/${control.iteration_limit}`;
}
```

Use escaped text when assigning generated HTML; include the active child index in the progress row while leaving runtime controls disabled for a selected retained failure.

- [ ] **Step 4: Update README and architecture documentation**

Document the `if_else` / `do_while` JSON shape, one-frame Condition semantics, five-second frame timeout, iteration-limit failure behavior, 200 ordinary-action maximum, global Loop compatibility, image-bundle remapping, and latest-failure-only retention. Add `data-feature="control-flow"` to `server\static\arch.html`; replace the claim of no failure visibility with the single retained failure exception while explicitly stating successful jobs are not history.

- [ ] **Step 5: Run all focused tests**

Run:

```powershell
node --test tools\test_script_editor.cjs
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_*.py -v
server\env\Scripts\python.exe -m unittest discover -s tools -p test_clear_command.py -v
```

Expected: all editor, runtime, control-flow, and clear-command tests pass.

- [ ] **Step 6: Commit runtime UI and documentation**

```powershell
git add -- server\static\index.html server\static\arch.html README.md tools\test_script_editor.cjs
git commit -m "feat: report control-flow execution status" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Plan self-review against the approved spec

- Editor structure, no nesting, all existing action types, slot drag/drop, delete choices, empty-slot validation, and max-input validation are covered in Task 4.
- Structured JSON, legacy flat requests, the aggregate 200-action limit, condition references, and event normalization are covered in Tasks 1 and 5.
- Top-level EventBus gate handling remains safe with control nodes and does not consume nested EVT children; covered in Task 1.
- Single-frame matching, the five-second timeout, missing-condition/snapshot errors, and ignoring `min_hits` for branch decisions are covered in Task 2.
- At-least-once do-while behavior, both repeat directions, max-iteration failure, and script-wide Loop compatibility are covered in Task 3.
- FIFO ordering, PAUSE / RESUME / STOP checkpoints, cancellation, and auto-attach of nested SRV steps are covered in Tasks 3 and 5.
- Bundle traversal/remapping, runtime branch/iteration detail, latest-failure-only retention, and clearing that failure are covered in Tasks 3, 5, and 6.
- README and architecture documentation plus focused regression commands are included in Task 6 and Final verification.
- No separate independent subsystem requires another plan; all listed surfaces implement and test the same script-node contract.

## Final verification

After all tasks:

```powershell
node --test tools\test_script_editor.cjs
server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_*.py -v
server\env\Scripts\python.exe -m unittest discover -s tools -p test_clear_command.py -v
git diff --check
```

Expected: all focused suites pass with no whitespace errors. Manually open the Script Editor in a browser and verify that an action can be dragged from the top-level list into each slot, between slots, and back out; verify that block controls remain editable and the Execution Status view remains read-only. Do not run a real hardware script for this UI check.
