# Changelog

## 2026-11-05 - Pro Micro HID and Quick Click

### Added

- Add the alternative `arduino/pro_micro_hid/pro_micro_hid.ino` sketch for
  JSON keyboard/mouse actions over USB Serial while preserving the Nano_KB
  `Serial1` protocol. Document the required ArduinoJson 7.x library and limits.
- Add KBD `REPEAT` mode to Script Editor. It sends short key taps at a configured
  interval until the selected camera condition matches or the maximum duration
  expires. Stop and script cleanup send `RELEASEALL`.
- Allow `DOWN:<key>` / `UP:<key>` to hold and release mapped non-modifier keys,
  including ESC, in both Pro Micro sketches.

### Fixed

- Remap the QUICK step's camera condition ID when importing an image bundle, so
  the step references the newly imported condition instead of its stale source ID.

### Tests

- Verification: 87 Script Editor tests, 16 script runtime tests, 48 control-flow
  tests, 7 camera enumeration tests and 5 Pro Micro protocol tests pass.
- Python syntax checks and `git diff --check` pass. The Pro Micro sketches were
  not compiled with an Arduino toolchain or tested on hardware in this update.

## 2026-11-03 - Script controls and execution status refinements

### Changed

- Wait one second before every IF / ELSE, DO-WHILE and WHILE visual condition
  evaluation, including the initial WHILE check and the final iteration-limit
  check. The wait supports pause/stop and is shown as a runtime delay; the
  separate five-second fresh-frame timeout starts afterward.
- Place the + IF / ELSE, + DO-WHILE and + WHILE buttons on a separate row below
  RUN, STOP and PAUSE.
- Show DO above the DO-WHILE body and its WHILE condition, repeat direction and
  iteration limit below the body. Pre-test WHILE retains its controls above
  the body.
- Add TAB and F4 to the second KBD COMBO slot. Allow an explicitly empty third
  key for ALT+TAB and ALT+F4 without adding C or a trailing plus. Preserve the
  legacy C default for an omitted key and reject completely empty combinations.
- Widen Edit's step DELAY inputs and their columns so 10000 is fully visible,
  including actions inside control blocks.
- Widen Execution Status's TYPE column to show complete IF / ELSE, DO-WHILE
  and WHILE labels.
- Group each runtime control header, branches/body, child actions and child
  details within a shared background and border. Leave a 10px gap before the
  next step while preserving active-row highlights.
- Show the active script's elapsed time as green HH:MM:SS immediately left of
  ARCH, with no text prefix. Update every second regardless of the selected
  editor view or preview job, and hide the timer when no active script remains.
  Elapsed time includes execution, waiting and pauses.

### Tests

- Add regression coverage for condition-wait ordering and cancellation,
  optional COMBO keys and import/export, flow toolbar/footer layout,
  five-digit delays, runtime grouping and the global elapsed timer.
- Verification: 84 editor tests, 48 control-flow tests and 9 runtime tests pass.
  The clear-command suite passes 63 of 64 tests; its local dash fixture sequence
  test also fails with the previously committed backend because the imported
  condition list differs from the executed EVT sequence. This pre-existing
  fixture-dependent failure is not changed in this update.

Earlier flow-control and ROI viewer changes were committed before this entry.
