# Task 3 — Control-Flow Runtime Report

## Implementation

- Added immutable structured runtime params, per-slot child action state, IF/do-while metadata, total action counts, and recursive removal of nested `image_move.template_b64`.
- Added child start/completion/failure tracking and latest-failed-job retention. Successful finishes are removed immediately; later successes do not replace the retained failure; STOP/workspace clear remove it.
- Extracted ordinary mixed-action handling and dispatched IF/ELSE and do-while blocks through the FIFO runner. OCR variables are shared across top-level and child actions within one global iteration, then recreated on the next global iteration.
- Preserved action-specific waits, delays, result payloads, and cancellation behavior. Control evaluations publish runtime changes before/after frame checks; each do-while body iteration publishes its state and checks pause boundaries.
- Do-while limit failures identify the reached limit and stop before subsequent actions. Child action failures identify their slot/index in both child and parent runtime state.

## TDD evidence

Interpreter: the provided worktree does not contain `server\env\Scripts\python.exe`; commands used the requested interpreter at `C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe` against this worktree's `tools` directory.

**RED — runtime tracker tests**, after adding the runtime tests:

```text
C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe -m unittest discover -s C:\GitHub\remote-keyboard-system.worktrees\a\tools -p test_script_runtime.py -v
AttributeError: 'ScriptRuntime' object has no attribute 'set_control_state' (2 tests)
TypeError: ScriptRuntime.finish() got an unexpected keyword argument 'failed'
Ran 8 tests ... FAILED (errors=3)
```

**RED — control interpreter test**, against the flat-step runner:

```text
C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe -m unittest test_script_control_flow.MixedScriptExecutionTests.test_if_executes_only_the_selected_branch_and_records_decision
AttributeError: 'IfElseBlock' object has no attribute 'delay_ms'
```

These failures identified the missing runtime APIs and structured-node dispatch.

**Additional RED/GREEN — runner failure before job activation:**

```text
RED: test_runner_retains_error_when_failure_precedes_activation
AssertionError: None != 'visual reset failed'
GREEN: same test ... ok
```

The runner now records an exception against the specific queued job even when it fails before that job becomes active.

**GREEN — focused runtime, control-flow, and clear/action/event suites:**

```text
C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe -m unittest discover -s C:\GitHub\remote-keyboard-system.worktrees\a\tools -p test_script_runtime.py -v
Ran 8 tests ... OK

C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe -m unittest discover -s C:\GitHub\remote-keyboard-system.worktrees\a\tools -p test_script_control_flow.py -v
Ran 31 tests ... OK

C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe -m unittest discover -s C:\GitHub\remote-keyboard-system.worktrees\a\tools -p test_clear_command.py -v
Ran 64 tests ... OK (skipped=2)
```

The final count run used `unittest` discovery wrappers to suppress application log noise; the test selectors are as shown above. The control-flow suite covers both IF branches, both do-while modes, body-first execution, normal exit at the limit, limit failure/no later action, child execution for all ordinary action types (including image move), OCR variable sharing, pause behavior, FIFO ordering, runtime notifications, and retained runner failures. Clear tests verify STOP and workspace clear discard retained failures without an active job.

## Self-review

- Confirmed control nodes remain non-nested and ordinary actions retain their original dispatch semantics; no schema, editor, import/export, plan, or ledger files were changed.
- Confirmed action failures preserve the exact slot and zero-based child index; runtime detail retains child results/errors with the parent failure.
- Confirmed successful jobs leave the previous latest failure intact, a newer failure replaces it, and clear removes it.
- `git diff --check` reported no whitespace errors.

The passing clear suite emits expected exception logs for mocked persistence failures; it also reports two skipped tests. The dedicated limit-failure test logs its expected runner exception.
