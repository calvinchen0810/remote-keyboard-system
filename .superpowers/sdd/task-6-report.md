# Task 6 — Execution Status and API documentation

## RED

Added editor/runtime tests first for block summaries and child progress, latest-failure selection and read-only state, reset removal, and the architecture `control-flow` feature marker.

Command:

```powershell
node --test tools\test_script_editor.cjs
```

Result: **60 tests, 58 passed, 2 failed** as expected. The architecture assertion reported missing `data-feature="control-flow"`; the runtime-view assertion failed because IF/ELSE progress and retained-failure selection were not rendered.

## GREEN

Implemented read-only IF/ELSE and do-while runtime rows from the existing detail contract (`params`, `control`, and per-slot `children`); added child progress/status/result/error display and escaped server strings. Runtime selection now includes `runtime.last_failure`; selecting it disables PAUSE/RESUME, while STOP remains the FIFO-level action that clears retained failure. Successful jobs remain absent. Updated README and architecture feature documentation.

Commands and results:

```powershell
node --test tools\test_script_editor.cjs
# tests 60
# pass 60
# fail 0

C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_*.py -v
# Ran 48 tests
# OK

C:\GitHub\remote-keyboard-system\server\env\Scripts\python.exe -m unittest discover -s tools -p test_clear_command.py -v
# Ran 64 tests
# OK

git diff --check
# no errors
```

## Self-review

- Runtime fields match `ScriptRuntime.snapshot()` (`active`, `queued`, `last_failure`) and `detail()` (`steps`, with each item carrying `params`, `control`, and child status/result/error arrays); no plan-sample-only fields were assumed.
- Only the selected IF branch can display active child progress. Both submitted branches remain visible and immutable. The progress strip reports the active nested action.
- Failed-job detail and child errors are escaped. Selecting a retained failure does not activate it or enable pause/resume; global STOP remains available to clear retained failure. Reset/clear removes it from the view.
- The runtime/backend regression suite already covers latest-failure-only retention, successful-job removal, STOP/clear removal, and runtime child contract; it passed unchanged.
- README and architecture docs cover schema 6, condition/frame behavior and timeout, do-while defaults/limits/failure, action-count limit, non-nesting, global Loop compatibility, bundle remapping, and failure retention semantics.
- No plan or ledger files were edited.
