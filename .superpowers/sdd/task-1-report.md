# Task 1 Report: Script Control-Flow Models and Validation

## Status

DONE. Implemented the Task 1 schema, validation, traversal, normalization, and EventBus gate-safety changes. No plan or progress-ledger files were modified.

## RED

Command:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_control_flow.py -v
```

Result before implementation: **FAILED (6 tests run)**. Four new control-flow tests errored because the existing flat `MixedStep` schema rejected `if_else` and `do_while`; two rejection tests passed under the old flat schema. This confirmed that valid control-node payloads were not yet supported.

## Changes

- Changed `MixedStep.type` to the required `Literal` action values and added the discriminated `ScriptNode` union with `IfElseBlock` and `DoWhileBlock`.
- Added strict `max_iterations` validation (integer only, excluding booleans and fractional values), the default of 10, and the specified range and `repeat_when` constraints.
- Replaced top-level-only step limits on script and bundle-import requests with aggregate validation across ordinary actions at top level and in control slots. Validation rejects nested controls, empty IF branches, empty do-while bodies, and scripts with more than 200 ordinary actions.
- Added `_iter_action_steps` and `_collect_condition_ids`; condition collection includes block conditions and `IMG.MATCH.<id>` events in action slots.
- Extended `_normalize_script_request` to preserve existing flat legacy EVT conversion and apply it within both IF branches and do-while bodies.
- Made EventBus gate discovery and consumption inspect only ordinary top-level EVT nodes. Added regression coverage showing a branch EVT remains untouched and restore preserves top-level ordering.
- Added focused validation and traversal tests in `tools/test_script_control_flow.py`.

## GREEN / Verification

Commands:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_control_flow.py -v
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_runtime.py -v
git diff --check
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m py_compile server\main.py tools\test_script_control_flow.py tools\test_script_runtime.py
```

Results:

- Control-flow tests: **Ran 6 tests — OK**.
- Runtime compatibility tests: **Ran 5 tests — OK**.
- `git diff --check`: passed with no output.
- Python compilation: passed with no output.

## Self-review

- Existing action models and fields remain in place; flat payloads continue to use the same event normalization behavior.
- Aggregate limits count ordinary actions in every allowed slot, not the control nodes themselves.
- The control-node child fields are action-only, so nested control nodes cannot validate.
- `max_iterations` uses strict integer validation, including explicit tests for `1.5` and `True`.
- EventBus gate helpers skip control nodes and do not enter their child lists.
- Implementation stayed within Task 1. Control-flow execution and structured bundle/editor round-tripping remain for later tasks.

## Commit

Commit message: `feat: validate structured script control blocks`

Required trailer: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`

## Follow-up Fix: Validate the Normalized Action Count

The request's initial validation counted legacy actions before `_normalize_script_request` inserted their standalone EVT actions. Normalization now reruns aggregate control-step validation on the normalized tree, counting actions across top-level, IF branches, and do-while bodies.

### RED

Command:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' tools\test_script_control_flow.py
```

Output before the fix:

```text
.......F
FAIL: test_normalization_rejects_more_than_200_aggregate_actions
AssertionError: ValueError not raised
Ran 8 tests in 0.008s
FAILED (failures=1)
```

### GREEN

Commands:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' tools\test_script_control_flow.py
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' tools\test_script_runtime.py
```

Output after the fix:

```text
........
Ran 8 tests in 0.006s
OK
.....
Ran 5 tests in 0.002s
OK
```

Regression coverage verifies 101 legacy EVT-bearing actions across top-level and an IF branch are rejected after expansion, and exactly 200 normalized actions are accepted.

## Follow-up Fix: Return HTTP 422 for Rejected Expanded Scripts

Post-normalization validation raises plain `ValueError`, unlike Pydantic's initial
model validation. `/script/run` only translates `ValidationError`, so expanded
scripts exceeding the action limit previously escaped as unhandled server errors.
`_submit_script` now translates normalization's `ValueError` to
`HTTPException(422, str(exc))`, following the existing API validation pattern.
The direct normalization helper's `ValueError` contract remains unchanged.

Added endpoint-handler regression coverage using real `Request` payloads for
top-level actions, both IF child slots, and do-while bodies. In each slot, 101
legacy EVT-bearing actions expand to 202 actions and are rejected with HTTP 422
before activation/enqueue or event clearing. Exactly 100 expand to 200 actions
and reach activation successfully. Hardware execution and notifications are
mocked; parsing, model validation, normalization, submission, and route handling
are exercised.

### RED

Command:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' tools\test_script_control_flow.py
```

Output before the API fix (exit code 1):

```text
EEEE.........
ERROR: test_expanded_scripts_over_200_return_422_before_enqueue (slot='top')
ERROR: test_expanded_scripts_over_200_return_422_before_enqueue (slot='then')
ERROR: test_expanded_scripts_over_200_return_422_before_enqueue (slot='else')
ERROR: test_expanded_scripts_over_200_return_422_before_enqueue (slot='body')
ValueError: A script may contain at most 200 action steps
Ran 10 tests in 0.252s
FAILED (errors=4)
```

All four errors traced through `script_run` → `_submit_script` →
`_normalize_script_request` → `_validate_control_steps`, confirming the reported
unhandled exception. The exact-200 acceptance regression already passed.

### GREEN / Verification

Commands:

```powershell
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_control_flow.py -v
& 'C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe' -m unittest discover -s tools -p test_script_runtime.py -v
git diff --check
```

Output after the fix:

```text
Ran 10 tests in 0.074s
OK
Ran 5 tests in 0.001s
OK
```

Both focused suites passed. `git diff --check` passed with no output. Self-review
confirmed that only normalization errors are translated at this boundary;
existing Pydantic handling and the helper contract are preserved.
