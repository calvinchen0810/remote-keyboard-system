# Task 2 Report: Shared Condition Matching and Fresh Frames

## Result

Implemented Task 2 without changing the existing visual-monitor debounce, cooldown, edge-trigger, or event-emission path.

- Added `CONTROL_FRAME_TIMEOUT_SECONDS = 5.0`.
- Added `_match_condition_frame(condition, frame)` for single-frame ROI/template matching, threshold evaluation, and explicit missing-snapshot/invalid-input errors.
- Added `_evaluate_condition_fresh(condition_id, timeout_seconds=...)`. It captures the current frame generation, ignores stale samples, waits for a newer non-null sample, honors pause time without consuming the frame-wait timeout, checks that the condition still exists, and propagates task cancellation for STOP.
- Routed `/visual/conditions/{cid}/test` through the shared matcher while keeping its response fields and existing 404/503/500 error behavior.
- Added control-flow tests for match/non-match, fresh-generation gating, debounce independence (`min_hits=10`), missing condition/snapshot, stalled-frame timeout, pause/resume, cancellation, and manual endpoint compatibility.

## TDD Evidence

### RED

Command:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_control_flow.py -v
```

Result before implementation: exit code 1; `Ran 17 tests`; 7 new tests errored with `AttributeError` because `_match_condition_frame` and `_evaluate_condition_fresh` did not exist. This confirmed the requested behavior was absent; the existing Task 1 tests continued to pass.

### GREEN

Command:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_control_flow.py -v
```

Final result after implementation and added regressions:

```text
Ran 21 tests in 0.586s
OK
```

Focused image-move suite:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_image_move.py -v
```

```text
Ran 10 tests in 0.579s
OK
```

Visual monitor and script-runtime regression suite:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_clear_command.py -q
```

```text
Ran 63 tests in 4.969s
exit=0
```

Syntax validation:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m py_compile server\main.py tools\test_script_control_flow.py
```

Result: exit code 0.

## Self-Review

- The fresh evaluator only accepts `generation > initial_generation`, and its tests prove the initial sample is ignored.
- `min_hits` and cooldown are not consulted by the control-flow matcher; the one-frame test uses `min_hits=10` and succeeds.
- Pause waiting is outside the elapsed-frame-wait accounting; a pause longer than the injected timeout does not consume that timeout. Cancellation is not swallowed.
- The visual monitor's `_eval_condition` and `_visual_monitor_task` were left unchanged, preserving debounce, cooldown, edge-trigger, and event semantics. Existing `test_clear_command.py` regression coverage passes.
- The manual test route still checks condition, current frame, and snapshot in its original order, returns the same `{score, matched, threshold}` shape, and retains its prior HTTP error status/detail behavior. Its match implementation is now shared with control-flow evaluation.
- The implementation follows the brief's interfaces and five-second timeout. No camera hardware is needed by the tests.
- `git diff --check` completed without errors.
