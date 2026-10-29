# Task 5: Structured import/export and bundle references

## TDD RED

### Editor tests
Command: `node --test tools\test_script_editor.cjs`

Observed output:
```text
not ok 13 - structured imports assign unique IDs recursively and preserve legacy flat steps
Expected values to be strictly equal: expected 5, actual 1
not ok 14 - structured export is schema 6 and deduplicates nested condition snapshots
Expected values to be strictly equal: expected 6, actual 1
# tests 59
# pass 57
# fail 2
Exit code: 1
```

### Backend control-flow tests
Command: `server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v`

Observed failing cases after correcting the test fixture's property mock:
```text
FAIL test_auto_attach_finds_servos_inside_all_block_slots: AssertionError: 0 != 2
FAIL test_import_rejects_references_absent_from_bundle: AssertionError: HTTPException not raised
FAIL test_import_remaps_block_conditions_and_nested_image_events: AssertionError: 'old-door' != 'new-door'
FAIL test_submit_rejects_missing_or_unusable_condition_before_enqueue: AssertionError: HTTPException not raised
FAIL test_action_and_condition_helpers_traverse_control_slots: lists differed; actual IDs were ['door', 'then', 'door', 'loop', 'body', 'top']
Ran 36 tests
FAILED (failures=5)
Exit code: 1
```

These failures demonstrated missing recursive editor IDs/export schema, nested servo and IMG collection, recursive reference remapping, unique condition activation, and usable-condition validation.

## TDD GREEN

### Editor tests
Command: `node --test tools\test_script_editor.cjs`
Exit code: 0
Output:
```text
TAP version 13
# Subtest: control blocks have stable defaults and preserve an action when moved between slots
ok 1 - control blocks have stable defaults and preserve an action when moved between slots
  ---
  duration_ms: 15.2218
  ...
# Subtest: script toolbar exposes controls for adding blank flow blocks
ok 2 - script toolbar exposes controls for adding blank flow blocks
  ---
  duration_ms: 0.2995
  ...
# Subtest: actions move between blocks and back to a requested top-level position
ok 3 - actions move between blocks and back to a requested top-level position
  ---
  duration_ms: 5.4153
  ...
# Subtest: control nodes cannot be moved into a slot and invalid moves are reported
ok 4 - control nodes cannot be moved into a slot and invalid moves are reported
  ---
  duration_ms: 4.6378
  ...
# Subtest: block slots accept native drops and action rows drop back to the selected top-level position
ok 5 - block slots accept native drops and action rows drop back to the selected top-level position
  ---
  duration_ms: 6.8001
  ...
# Subtest: block controls render escaped condition labels and editable repeat settings
ok 6 - block controls render escaped condition labels and editable repeat settings
  ---
  duration_ms: 6.7684
  ...
# Subtest: ordinary editors and OCR variables operate within the correct block scope
ok 7 - ordinary editors and OCR variables operate within the correct block scope
  ---
  duration_ms: 8.731
  ...
# Subtest: mouse action editors resolve nested steps by identity
ok 8 - mouse action editors resolve nested steps by identity
  ---
  duration_ms: 4.7164
  ...
# Subtest: block deletion offers unwrap, delete-all, and cancel with order and identity preserved
ok 9 - block deletion offers unwrap, delete-all, and cancel with order and identity preserved
  ---
  duration_ms: 5.7037
  ...
# Subtest: control-flow validation rejects empty blocks and invalid loop limits
ok 10 - control-flow validation rejects empty blocks and invalid loop limits
  ---
  duration_ms: 5.7931
  ...
# Subtest: nested script serialization strips editor IDs but preserves block children and order
ok 11 - nested script serialization strips editor IDs but preserves block children and order
  ---
  duration_ms: 6.3869
  ...
# Subtest: nested imported actions receive unique editor IDs and nested blocks are rejected
ok 12 - nested imported actions receive unique editor IDs and nested blocks are rejected
  ---
  duration_ms: 3.7889
  ...
# Subtest: structured imports assign unique IDs recursively and preserve legacy flat steps
ok 13 - structured imports assign unique IDs recursively and preserve legacy flat steps
  ---
  duration_ms: 3.024
  ...
# Subtest: structured export is schema 6 and deduplicates nested condition snapshots
ok 14 - structured export is schema 6 and deduplicates nested condition snapshots
  ---
  duration_ms: 3.5596
  ...
# Subtest: RUN submits nested block steps in order without leaking editor IDs
ok 15 - RUN submits nested block steps in order without leaking editor IDs
  ---
  duration_ms: 9.148
  ...
# Subtest: RUN reports invalid blocks before making a request and keeps the draft
ok 16 - RUN reports invalid blocks before making a request and keeps the draft
  ---
  duration_ms: 3.9007
  ...
# Subtest: servo quick actions persist a fixed six-field snapshot and execute that snapshot
ok 17 - servo quick actions persist a fixed six-field snapshot and execute that snapshot
  ---
  duration_ms: 3.4643
  ...
# Subtest: servo quick action parameters can be loaded, cancelled, updated, renamed and deleted
ok 18 - servo quick action parameters can be loaded, cancelled, updated, renamed and deleted
  ---
  duration_ms: 5.3761
  ...
# Subtest: servo action update control is blue and labeled UPDATE
ok 19 - servo action update control is blue and labeled UPDATE
  ---
  duration_ms: 1.2268
  ...
# Subtest: accepted script submission switches to Execution Status and follows the active queue
ok 20 - accepted script submission switches to Execution Status and follows the active queue
  ---
  duration_ms: 2.529
  ...
# Subtest: Editor RUN marks its request local and leaves tab switching to external submissions
ok 21 - Editor RUN marks its request local and leaves tab switching to external submissions
  ---
  duration_ms: 0.6556
  ...
# Subtest: switching a new KBD step to KEY stores ENTER in the script payload
ok 22 - switching a new KBD step to KEY stores ENTER in the script payload
  ---
  duration_ms: 3.5637
  ...
# Subtest: switching to KEY preserves a supported special key
ok 23 - switching to KEY preserves a supported special key
  ---
  duration_ms: 3.9235
  ...
# Subtest: a selected special key survives a TYPE round trip
ok 24 - a selected special key survives a TYPE round trip
  ---
  duration_ms: 3.8356
  ...
# Subtest: switching to TYPE does not alter the existing combo key
ok 25 - switching to TYPE does not alter the existing combo key
  ---
  duration_ms: 3.2546
  ...
# Subtest: COMBO offers F10 only in the second key dropdown and preserves its selection
ok 26 - COMBO offers F10 only in the second key dropdown and preserves its selection
  ---
  duration_ms: 3.5171
  ...
# Subtest: SRV ID selectors leave room for the ID and native dropdown arrow
ok 27 - SRV ID selectors leave room for the ID and native dropdown arrow
  ---
  duration_ms: 3.9525
  ...
# Subtest: workspace clear resets editor, visual lists, keyboard lock and progress
ok 28 - workspace clear resets editor, visual lists, keyboard lock and progress
  ---
  duration_ms: 3.6421
  ...
# Subtest: SRV WebSocket clear notification resets the editor
ok 29 - SRV WebSocket clear notification resets the editor
  ---
  duration_ms: 4.0212
  ...
# Subtest: visual responses started before clear cannot restore stale lists
ok 30 - visual responses started before clear cannot restore stale lists
  ---
  duration_ms: 7.1246
  ...
# Subtest: OCR steps retain normalized ROI and settings through JSON round trips
ok 31 - OCR steps retain normalized ROI and settings through JSON round trips
  ---
  duration_ms: 3.9855
  ...
# Subtest: KBD variable selection uses only earlier OCR steps and inserts a literal placeholder
ok 32 - KBD variable selection uses only earlier OCR steps and inserts a literal placeholder
  ---
  duration_ms: 4.3471
  ...
# Subtest: invalid OCR ROI, name and timing cannot be added
ok 33 - invalid OCR ROI, name and timing cannot be added
  ---
  duration_ms: 15.2845
  ...
# Subtest: OCR ROI modal saves new steps and edits nested settings without changing delay
ok 34 - OCR ROI modal saves new steps and edits nested settings without changing delay
  ---
  duration_ms: 4.922
  ...
# Subtest: imported OCR steps materialize defaults for variable selection and export
ok 35 - imported OCR steps materialize defaults for variable selection and export
  ---
  duration_ms: 5.1883
  ...
# Subtest: IMAGE_MOVE steps embed their template and materialize portable defaults
ok 36 - IMAGE_MOVE steps embed their template and materialize portable defaults
  ---
  duration_ms: 4.6707
  ...
# Subtest: saving IMAGE_MOVE into an existing MSE step preserves its ID and delay
ok 37 - saving IMAGE_MOVE into an existing MSE step preserves its ID and delay
  ---
  duration_ms: 3.3243
  ...
# Subtest: IMAGE_MOVE validates search region, threshold and template before adding
ok 38 - IMAGE_MOVE validates search region, threshold and template before adding
  ---
  duration_ms: 14.7654
  ...
# Subtest: a stale template response cannot replace a newly selected image target
ok 39 - a stale template response cannot replace a newly selected image target
  ---
  duration_ms: 4.6273
  ...
# Subtest: image-move save disables both actions and restores them after failure
ok 40 - image-move save disables both actions and restores them after failure
  ---
  duration_ms: 3.9313
  ...
# Subtest: Snap Pool and Conditions share equal-height panels with independent scrolling
ok 41 - Snap Pool and Conditions share equal-height panels with independent scrolling
  ---
  duration_ms: 1.042
  ...
# Subtest: SNAP thumbnail and CON OCR MOV delete actions share one compact row without showing ID
ok 42 - SNAP thumbnail and CON OCR MOV delete actions share one compact row without showing ID
  ---
  duration_ms: 4.3142
  ...
# Subtest: architecture page covers recent script, visual, firmware and editor features
ok 43 - architecture page covers recent script, visual, firmware and editor features
  ---
  duration_ms: 1.219
  ...
# Subtest: FIFO pause displays retry state without unlocking the keyboard
ok 44 - FIFO pause displays retry state without unlocking the keyboard
  ---
  duration_ms: 2.6172
  ...
# Subtest: status refresh restores FIFO pause after reconnect or reload
ok 45 - status refresh restores FIFO pause after reconnect or reload
  ---
  duration_ms: 3.0874
  ...
# Subtest: late retry HTTP response cannot restore pause after WebSocket resumed it
ok 46 - late retry HTTP response cannot restore pause after WebSocket resumed it
  ---
  duration_ms: 2.6223
  ...
# Subtest: stale status cannot restore pause after stop and idle status clears pause locks
ok 47 - stale status cannot restore pause after stop and idle status clears pause locks
  ---
  duration_ms: 33.7086
  ...
# Subtest: FIFO Queue summarizes the active job, phase, current step and queued count
ok 48 - FIFO Queue summarizes the active job, phase, current step and queued count
  ---
  duration_ms: 3.2465
  ...
# Subtest: right column has no duplicate standalone Progress panel
ok 49 - right column has no duplicate standalone Progress panel
  ---
  duration_ms: 0.1584
  ...
# Subtest: Conditions are script-controlled and expose no manual ARM or DISARM actions
ok 50 - Conditions are script-controlled and expose no manual ARM or DISARM actions
  ---
  duration_ms: 3.2257
  ...
# Subtest: Conditions delete button aligns to the right edge like Snap Pool
ok 51 - Conditions delete button aligns to the right edge like Snap Pool
  ---
  duration_ms: 0.3359
  ...
# Subtest: Mouse R click button shares the green style with L
ok 52 - Mouse R click button shares the green style with L
  ---
  duration_ms: 0.3421
  ...
# Subtest: runtime snapshots preserve draft and reject older same-session updates
ok 53 - runtime snapshots preserve draft and reject older same-session updates
  ---
  duration_ms: 4.1378
  ...
# Subtest: runtime detail shows full escaped TYPE and current row without editable controls
ok 54 - runtime detail shows full escaped TYPE and current row without editable controls
  ---
  duration_ms: 4.2132
  ...
# Subtest: selecting another runtime job clears the old job content immediately
ok 55 - selecting another runtime job clears the old job content immediately
  ---
  duration_ms: 3.2582
  ...
# Subtest: late runtime status cannot revive a retired server session
ok 56 - late runtime status cannot revive a retired server session
  ---
  duration_ms: 5.2664
  ...
# Subtest: manual PAUSE uses its endpoint without resubmitting a script and renders no null event
ok 57 - manual PAUSE uses its endpoint without resubmitting a script and renders no null event
  ---
  duration_ms: 3.3172
  ...
# Subtest: execution status uses Edit columns and badges with readonly values and one current detail row
ok 58 - execution status uses Edit columns and badges with readonly values and one current detail row
  ---
  duration_ms: 4.1208
  ...
# Subtest: execution status formats active elapsed time as HH:MM:SS without wrapping hours
ok 59 - execution status formats active elapsed time as HH:MM:SS without wrapping hours
  ---
  duration_ms: 2.8052
  ...
1..59
# tests 59
# suites 0
# pass 59
# fail 0
# cancelled 0
# skipped 0
# todo 0
# duration_ms 557.2343
```

### Control-flow tests
Command: `server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_control_flow.py -v`
Exit code: 0
Output:
```text
test_evaluation_reports_missing_condition_or_snapshot (test_script_control_flow.FreshConditionFrameTests.test_evaluation_reports_missing_condition_or_snapshot) ... ok
test_evaluation_returns_non_match_for_a_new_frame (test_script_control_flow.FreshConditionFrameTests.test_evaluation_returns_non_match_for_a_new_frame) ... ok
test_evaluation_times_out_when_no_new_frame_arrives (test_script_control_flow.FreshConditionFrameTests.test_evaluation_times_out_when_no_new_frame_arrives) ... ok
test_evaluation_wait_is_pause_aware_and_propagates_stop_cancellation (test_script_control_flow.FreshConditionFrameTests.test_evaluation_wait_is_pause_aware_and_propagates_stop_cancellation) ... ok
test_evaluation_waits_for_a_new_frame_and_ignores_min_hits (test_script_control_flow.FreshConditionFrameTests.test_evaluation_waits_for_a_new_frame_and_ignores_min_hits) ... ok
test_matcher_errors_keep_the_existing_http_500_prefix (test_script_control_flow.ManualConditionTestEndpointTests.test_matcher_errors_keep_the_existing_http_500_prefix) ... ok
test_missing_condition_frame_and_snapshot_keep_existing_http_errors (test_script_control_flow.ManualConditionTestEndpointTests.test_missing_condition_frame_and_snapshot_keep_existing_http_errors) ... ok
test_success_keeps_the_existing_response_shape (test_script_control_flow.ManualConditionTestEndpointTests.test_success_keeps_the_existing_response_shape) ... ok
test_active_block_keeps_fifo_runner_ahead_of_queued_job (test_script_control_flow.MixedScriptExecutionTests.test_active_block_keeps_fifo_runner_ahead_of_queued_job) ... 2026-10-29 15:05:55,008 [INFO] main: Script done: 1 steps � 1 iterations
2026-10-29 15:05:55,008 [INFO] main: Script done: 1 steps � 1 iterations
ok
test_all_ordinary_action_types_run_as_children_with_shared_ocr_variables (test_script_control_flow.MixedScriptExecutionTests.test_all_ordinary_action_types_run_as_children_with_shared_ocr_variables) ... 2026-10-29 15:05:55,050 [INFO] main: [STEP 1/1] EVT wait: READY
2026-10-29 15:05:55,050 [INFO] main: [STEP 1/1] KBD TYPE:0042
2026-10-29 15:05:55,050 [INFO] main: [STEP 1/1] SRV sid=1 angle=45 speed=60 duration=300ms
2026-10-29 15:05:55,058 [INFO] main: [STEP 1/1] MSE MOUSE:MOVE 4 8
2026-10-29 15:05:55,058 [INFO] main: Script done: 1 steps � 1 iterations
ok
test_do_while_runs_once_then_repeats_using_each_repeat_mode (test_script_control_flow.MixedScriptExecutionTests.test_do_while_runs_once_then_repeats_using_each_repeat_mode) ... 2026-10-29 15:05:55,092 [INFO] main: Script done: 1 steps � 1 iterations
2026-10-29 15:05:55,092 [INFO] main: Script done: 1 steps � 1 iterations
2026-10-29 15:05:55,092 [INFO] main: Script done: 1 steps � 1 iterations
2026-10-29 15:05:55,096 [INFO] main: Script done: 1 steps � 1 iterations
ok
test_each_do_while_iteration_and_condition_change_is_published (test_script_control_flow.MixedScriptExecutionTests.test_each_do_while_iteration_and_condition_change_is_published) ... 2026-10-29 15:05:55,125 [INFO] main: Script done: 1 steps � 1 iterations
ok
test_failed_child_status_names_slot_and_index_in_parent_error (test_script_control_flow.MixedScriptExecutionTests.test_failed_child_status_names_slot_and_index_in_parent_error) ... 2026-10-29 15:05:55,145 [INFO] main: [STEP 1/1] KBD KEY:A
2026-10-29 15:05:55,145 [WARNING] main: [STEP 1/1] KBD ACK=BUSY cmd=KEY:A
2026-10-29 15:05:55,145 [INFO] main: [STEP 1/1] KBD KEY:B
2026-10-29 15:05:55,145 [INFO] main: Script done: 1 steps � 1 iterations
ok
test_if_executes_only_the_selected_branch_and_records_decision (test_script_control_flow.MixedScriptExecutionTests.test_if_executes_only_the_selected_branch_and_records_decision) ... 2026-10-29 15:05:55,178 [INFO] main: Script done: 1 steps � 1 iterations
2026-10-29 15:05:55,178 [INFO] main: Script done: 1 steps � 1 iterations
ok
test_iteration_limit_failure_stops_before_later_actions (test_script_control_flow.MixedScriptExecutionTests.test_iteration_limit_failure_stops_before_later_actions) ... ok
test_paused_block_does_not_start_its_next_action_before_resume (test_script_control_flow.MixedScriptExecutionTests.test_paused_block_does_not_start_its_next_action_before_resume) ... 2026-10-29 15:05:55,252 [INFO] main: Script done: 1 steps � 1 iterations
ok
test_runner_retains_error_when_failure_precedes_activation (test_script_control_flow.MixedScriptExecutionTests.test_runner_retains_error_when_failure_precedes_activation) ... 2026-10-29 15:05:55,283 [ERROR] main: Queued script failed
Traceback (most recent call last):
  File "C:\GitHub\remote-keyboard-system.worktrees\a\server\main.py", line 1829, in _script_runner
    await event_bus.clear_visual()
  File "C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.11_3.11.2544.0_x64__qbz5n2kfra8p0\Lib\unittest\mock.py", line 2249, in _execute_mock_call
    raise result
RuntimeError: visual reset failed
ok
test_runner_retains_iteration_limit_failure_as_latest_job (test_script_control_flow.MixedScriptExecutionTests.test_runner_retains_iteration_limit_failure_as_latest_job) ... 2026-10-29 15:05:55,308 [ERROR] main: Queued script failed
Traceback (most recent call last):
  File "C:\GitHub\remote-keyboard-system.worktrees\a\server\main.py", line 1841, in _script_runner
    await _run_mixed_script(req)
  File "C:\GitHub\remote-keyboard-system.worktrees\a\server\main.py", line 1796, in _run_mixed_script
    raise RuntimeError(
RuntimeError: Do-while iteration limit reached (1)
ok
test_auto_attach_finds_servos_inside_all_block_slots (test_script_control_flow.ScriptBundleImportTests.test_auto_attach_finds_servos_inside_all_block_slots) ... ok
test_import_rejects_references_absent_from_bundle (test_script_control_flow.ScriptBundleImportTests.test_import_rejects_references_absent_from_bundle) ... 2026-10-29 15:05:55,362 [INFO] main: [IMPORT] Bundle import started: 0 snapshot(s), 0 condition(s), 1 step(s)
ok
test_import_remaps_block_conditions_and_nested_image_events (test_script_control_flow.ScriptBundleImportTests.test_import_remaps_block_conditions_and_nested_image_events) ... 2026-10-29 15:05:55,375 [INFO] main: [IMPORT] Bundle import started: 1 snapshot(s), 2 condition(s), 2 step(s)
2026-10-29 15:05:55,375 [INFO] main: [IMPORT] Bundle import done: snapshots=1 conditions=2 map={'old-door': 'new-door', 'old-image': 'new-image'}
ok
test_expanded_scripts_over_200_return_422_before_enqueue (test_script_control_flow.ScriptControlFlowSubmissionTests.test_expanded_scripts_over_200_return_422_before_enqueue) ... ok
test_expanded_scripts_with_exactly_200_actions_can_enqueue (test_script_control_flow.ScriptControlFlowSubmissionTests.test_expanded_scripts_with_exactly_200_actions_can_enqueue) ... ok
test_submit_collects_and_activates_distinct_control_and_image_conditions (test_script_control_flow.ScriptControlFlowSubmissionTests.test_submit_collects_and_activates_distinct_control_and_image_conditions) ... ok
test_submit_rejects_missing_or_unusable_condition_before_enqueue (test_script_control_flow.ScriptControlFlowSubmissionTests.test_submit_rejects_missing_or_unusable_condition_before_enqueue) ... ok
test_action_and_condition_helpers_traverse_control_slots (test_script_control_flow.ScriptControlFlowValidationTests.test_action_and_condition_helpers_traverse_control_slots) ... ok
test_bundle_request_enforces_aggregate_action_limit (test_script_control_flow.ScriptControlFlowValidationTests.test_bundle_request_enforces_aggregate_action_limit) ... ok
test_control_nodes_use_defaults_and_reject_unknown_types (test_script_control_flow.ScriptControlFlowValidationTests.test_control_nodes_use_defaults_and_reject_unknown_types) ... ok
test_control_nodes_validate_and_keep_action_children (test_script_control_flow.ScriptControlFlowValidationTests.test_control_nodes_validate_and_keep_action_children) ... ok
test_nested_empty_invalid_and_over_limit_blocks_are_rejected (test_script_control_flow.ScriptControlFlowValidationTests.test_nested_empty_invalid_and_over_limit_blocks_are_rejected) ... ok
test_normalization_accepts_exactly_200_aggregate_actions (test_script_control_flow.ScriptControlFlowValidationTests.test_normalization_accepts_exactly_200_aggregate_actions) ... ok
test_normalization_preserves_flat_behavior_and_normalizes_control_children (test_script_control_flow.ScriptControlFlowValidationTests.test_normalization_preserves_flat_behavior_and_normalizes_control_children) ... ok
test_normalization_rejects_more_than_200_aggregate_actions (test_script_control_flow.ScriptControlFlowValidationTests.test_normalization_rejects_more_than_200_aggregate_actions) ... ok
test_matcher_reports_missing_snapshot_and_invalid_frame (test_script_control_flow.SharedConditionMatcherTests.test_matcher_reports_missing_snapshot_and_invalid_frame) ... ok
test_matcher_returns_non_match_below_threshold (test_script_control_flow.SharedConditionMatcherTests.test_matcher_returns_non_match_below_threshold) ... ok
test_matcher_returns_score_and_match_for_matching_roi (test_script_control_flow.SharedConditionMatcherTests.test_matcher_returns_score_and_match_for_matching_roi) ... ok
System.Management.Automation.RemoteException
----------------------------------------------------------------------
Ran 36 tests in 0.904s
System.Management.Automation.RemoteException
OK
```

### Runtime regressions
Command: `server\env\Scripts\python.exe -m unittest discover -s tools -p test_script_runtime.py -v`
Exit code: 0
Output:
```text
test_control_iteration_preserves_unrelated_job_error (test_script_runtime.ScriptRuntimeTests.test_control_iteration_preserves_unrelated_job_error) ... ok
test_control_iteration_resets_child_states_and_failure_is_identified (test_script_runtime.ScriptRuntimeTests.test_control_iteration_resets_child_states_and_failure_is_identified) ... ok
test_control_nodes_keep_immutable_params_and_track_child_and_control_state (test_script_runtime.ScriptRuntimeTests.test_control_nodes_keep_immutable_params_and_track_child_and_control_state) ... ok
test_finish_removes_job_without_history_and_clear_discards_all (test_script_runtime.ScriptRuntimeTests.test_finish_removes_job_without_history_and_clear_discards_all) ... ok
test_gate_consumption_skips_control_nodes_and_preserves_their_children (test_script_runtime.ScriptRuntimeTests.test_gate_consumption_skips_control_nodes_and_preserves_their_children) ... ok
test_jobs_have_unique_ids_and_fifo_order_without_exposing_steps_in_status (test_script_runtime.ScriptRuntimeTests.test_jobs_have_unique_ids_and_fifo_order_without_exposing_steps_in_status) ... ok
test_readonly_detail_freezes_full_text_and_omits_large_template (test_script_runtime.ScriptRuntimeTests.test_readonly_detail_freezes_full_text_and_omits_large_template) ... ok
test_runtime_retains_only_the_latest_failed_job (test_script_runtime.ScriptRuntimeTests.test_runtime_retains_only_the_latest_failed_job) ... ok
test_step_states_pause_results_and_loop_reset (test_script_runtime.ScriptRuntimeTests.test_step_states_pause_results_and_loop_reset) ... ok
System.Management.Automation.RemoteException
----------------------------------------------------------------------
Ran 9 tests in 0.002s
System.Management.Automation.RemoteException
OK
```

## Self-review

- Imported structured blocks and all child actions receive fresh, globally unique editor IDs; serialization recursively strips editor-only IDs.
- Control-block scripts select schema version 6; existing version selection remains intact for flat scripts. Bundle collection walks all control slots, deduplicates Condition IDs and shared snapshots, and logs missing Conditions/Snapshots explicitly.
- Backend discovery/activation validates each referenced Condition and its live Snapshot before enqueue, deduplicates activation IDs, and discovers SRV steps inside every block slot.
- Bundle import validates every referenced Condition before side effects, remaps control references and nested IMG.MATCH events through all allowed slots, preserves unrelated events, and returns 422 for missing references.
- Existing flat-step import/export behavior and action ordering remain covered by editor/control-flow/runtime tests. Control blocks remain non-nestable.
- No plan or ledger files were edited. `git diff --check` is run before commit.
- The supplied `server\env\Scripts\python.exe` path was initially absent; a temporary local virtual environment was created from `server\requirements.txt` to run the requested Python suites and removed after verification.
