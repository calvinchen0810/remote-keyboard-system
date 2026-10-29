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

## Review correction — stale do-while job error (2026-10-29)

- Confirmed `complete_child_step(error=...)` sets child, parent, and job errors; non-fatal keyboard/servo failures can continue into another do-while body iteration. Previously, `set_control_state(iteration=...)` reset only the parent/children, leaving the job summary inconsistent.
- The iteration reset now reconciles the job error only when it matches the parent error being cleared. It falls back to the last remaining top-level step error, or `None` when none remains. A different job error remains untouched; unrelated step errors survive even when their message equals the cleared child error.
- Extended the existing iteration-reset regression to assert parent state/error and job detail/status error. Added preservation coverage for unrelated errors recorded before/after the child failure, with different and identical messages. No runner behavior or public runtime schema changed.

### RED — original stale-error regression

From `C:\GitHub\remote-keyboard-system.worktrees\a`:

```text
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_runtime.py -v

FAIL: test_control_iteration_resets_child_states_and_failure_is_identified
  self.assertIsNone(detail["error"])
AssertionError: 'KBD ACK: ERROR' is not None
Ran 9 tests in 0.013s
FAILED (failures=1)
Exit code: 1
```

### Additional RED — unrelated errors must not be erased

A preliminary match-and-clear implementation fixed the stale error but failed the expanded unrelated-error regression:

```text
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_runtime.py -v

FAIL: test_control_iteration_preserves_unrelated_job_error
  (error='unrelated failure', unrelated_first=True)
AssertionError: None != 'unrelated failure'
FAIL: test_control_iteration_preserves_unrelated_job_error
  (error='child failure', unrelated_first=True)
AssertionError: None != 'child failure'
FAIL: test_control_iteration_preserves_unrelated_job_error
  (error='child failure', unrelated_first=False)
AssertionError: None != 'child failure'
Ran 9 tests in 0.016s
FAILED (failures=3)
Exit code: 1
```

### GREEN — runtime and control-flow suites

After adding the remaining-step-error fallback:

```text
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_runtime.py -v

test_control_iteration_preserves_unrelated_job_error ... ok
test_control_iteration_resets_child_states_and_failure_is_identified ... ok
Ran 9 tests in 0.010s
OK
Exit code: 0

& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_control_flow.py -v

Ran 31 tests in 2.622s
OK
Exit code: 0
```

The control-flow suite emitted expected logs for mocked keyboard BUSY, visual reset failure, and iteration-limit failure. Self-review confirmed that ordinary step error recording, global iteration reset, child reset, and retained failure behavior remain unchanged; `git diff --check` passed.
