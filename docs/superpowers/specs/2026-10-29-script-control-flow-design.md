# Script Editor Control-Flow Blocks Design

**Date:** 2026-10-29
**Status:** Approved for spec review; implementation has not started

## Context

The Script Editor currently represents a script as an ordered, flat list of action steps. It supports a script-wide `loop` option and event steps, including image-match events, but not conditional branches or loops scoped to part of a script. The backend accepts and runs the same flat step list. The Execution Status view is read-only and tracks submitted jobs separately from the editable draft.

Users need Scratch-like control flow without replacing the existing step-list workflow. The first release adds `if/else` and `do-while` blocks driven by existing visual Conditions. Blocks are not nestable; ordinary existing step types may be placed inside their slots.

## Goals

- Add `if/else` and `do-while` control-flow blocks to the existing step list.
- Let users add an empty control block and drag existing action steps into and out of its slots.
- Make branch decisions against a fresh camera frame using an existing Condition.
- Preserve the existing script-wide Loop behavior and legacy flat JSON requests.
- Carry structured blocks through editor import/export, image bundles, API execution, and read-only runtime status.
- Retain the most recent failed job so a control-flow failure remains inspectable after the runner releases it.
- Preserve FIFO, pause/resume, stop, and existing action-step behavior.

## Non-goals

- Nested control-flow blocks.
- Combining multiple Conditions with AND/OR.
- OCR-variable or other non-visual control predicates.
- Replacing the Script Editor with a separate Scratch workspace or toolbox.
- Changing existing event-wait semantics or the script-wide Loop behavior.

## Editor interaction

The editor remains a step list. Ordinary steps and control blocks share the top-level order. A control block is rendered as a control row with its contained steps visually indented:

- `if_else` has a then slot and an else slot.
- `do_while` has one body slot.
- Users add an empty block, then drag ordinary steps into its slots. Steps can be dragged between slots or out to the top-level list. Control blocks themselves cannot be placed inside another block.
- Every existing ordinary step type is valid in a block, including SRV, KBD, MSE, OCR, and EVT.
- A do-while block shows a Condition selector, a repeat-direction selector (`matched` or `not matched`), and an integer maximum-iterations input. The maximum defaults to 10 and accepts values from 1 through 10,000.
- An if/else block shows a Condition selector.
- Deleting a control block requires confirmation. The user can either unwrap its ordinary steps into the top-level list or delete the block and its contents.
- Empty blocks may be created while editing. At submission, an if/else block must have at least one non-empty branch; a do-while body must contain at least one ordinary step.

## Structured JSON

Control blocks are tagged nodes in the existing top-level `steps` array. Ordinary step payloads retain their existing formats. Example:

```json
{
  "name": "Open door",
  "loop": false,
  "steps": [
    { "type": "kbd", "cmd_type": "TYPE", "text": "open" },
    {
      "type": "if_else",
      "condition_id": "door-visible-a1b2",
      "then_steps": [
        { "type": "mse", "action": "CLICK", "btn": "L" }
      ],
      "else_steps": [
        { "type": "kbd", "cmd_type": "KEY", "key": "ENTER" }
      ]
    },
    {
      "type": "do_while",
      "condition_id": "door-visible-a1b2",
      "repeat_when": "not_match",
      "max_iterations": 10,
      "steps": [
        { "type": "mse", "action": "CLICK", "btn": "L" },
        { "type": "evt", "evt": "READY" }
      ]
    }
  ]
}
```

The editor preserves block order, branch/body contents, condition references, repeat direction, and iteration limit when exporting and re-importing. Requests containing only legacy ordinary steps remain valid and keep their current behavior. A structured request is executable through `/script/run` as well as through the editor.

The existing 200-step maximum applies to the total number of ordinary action steps across the whole script, including all block slots. Control nodes do not count as ordinary steps. Every control node must be valid and non-nested; each must reference an existing Condition.

## Execution semantics

### Condition evaluation

When execution reaches an if/else block, the server waits for a new camera frame and performs one comparison using the selected Condition's snapshot, ROI, and threshold. A match runs `then_steps`; a non-match runs `else_steps`.

The do-while body runs at least once. After each completed body iteration, the server evaluates the selected Condition against a new frame. `repeat_when` determines whether a match or a non-match starts another iteration. The Condition's `min_hits` and cooldown are not applied to these single-frame control decisions; they continue to apply to the existing event-driven visual-monitor behavior.

The implementation must not substitute a prior `IMG.MATCH` event or cached score for a block decision. A missing camera frame, deleted Condition, or missing Condition snapshot is surfaced as an explicit execution/submission error; it must not silently choose a branch.

### Iteration limit and script-wide loop

The maximum-iterations value is a positive integer in the inclusive range 1–10,000. If the Condition still requests another iteration after the configured number of body executions, the whole job fails, stops, and reports that the do-while iteration limit was reached. It does not continue to steps after that block. If the condition requests exit on the final allowed iteration, the block completes normally.

The existing script-wide `loop` option remains unchanged and may be used alongside local do-while blocks. A do-while limit failure terminates the job, including any enclosing script-wide repetitions.

### FIFO controls

Control checks and body steps execute as part of the active FIFO job. PAUSE / RESUME and STOP remain effective at safe execution boundaries, including before and after fresh-frame evaluations and between body iterations. The new control flow does not allow a queued job to run ahead of the active job.

## Import/export and image bundles

Image-bundle collection and import must traverse the top-level steps and both if/else branches and do-while bodies. Every referenced Condition and its Snapshot must be included in an exported bundle, with duplicate snapshots emitted only once. Bundle import remaps Condition IDs throughout all branches and bodies before the resulting script is loaded into the editor. Missing references are reported explicitly.

## Execution Status

Execution Status remains read-only and separate from the local editing draft. For an active if/else block it shows the selected branch and the single-frame match result. For a do-while block it shows the current iteration and configured maximum. The active ordinary step inside a block uses the existing step progress/result representation.

The runtime retains only the most recent failed job, including its structured detail and explicit error message, after the active runner releases it. A later successful job does not clear this failure. A newer failed job replaces it; STOP / workspace clear removes it. Successful completed jobs are still removed immediately, so this is not general execution history.

## Validation and error handling

The server validates the structured request before enqueueing:

- Recognized block types, required fields, and valid `repeat_when` values.
- A positive `max_iterations` no greater than 10,000.
- No nested control blocks.
- One existing, usable Condition per control block.
- At least one populated if/else branch and a non-empty do-while body.
- At most 200 ordinary steps across the complete script.

Invalid input receives an explicit validation response; it is not silently discarded or interpreted as an ordinary step. Runtime camera, condition, and snapshot failures stop the active job and are visible in its execution status.

## Testing

Focused automated tests cover:

- Adding and deleting blocks, dragging steps into, between, and out of slots, and preserving order.
- Import/export round-trips for structured blocks and image bundles, including Condition/Snapshot ID remapping.
- Legacy flat-script import and `/script/run` compatibility.
- Request validation for unknown/missing conditions, nested blocks, empty slots, invalid iteration values, and the 200-action-step aggregate limit.
- If/else match and non-match behavior using fresh frames.
- Do-while's at-least-once behavior, both repeat directions, normal exit, and failure when the limit is reached while repetition is still required.
- Execution Status branch/iteration details and explicit runtime failures, including retention of only the latest failed job until it is replaced or cleared.
- PAUSE / RESUME / STOP and FIFO ordering while a block is active.

## Acceptance criteria

1. A user can build a non-nested if/else or do-while by adding a block and dragging existing ordinary steps into its slots.
2. The selected Condition is evaluated against a fresh, single frame at each decision point; if/else branches immediately on that result.
3. Do-while runs its body at least once and follows the selected repeat direction, with a default maximum of 10 and allowed range 1–10,000.
4. A do-while limit failure stops the job and is shown as an error; prior FIFO, pause/resume, stop, and script-wide Loop semantics remain intact.
5. Structured scripts and image bundles round-trip through the editor; `/script/run` accepts them, and legacy flat scripts continue to work.
6. Execution Status displays the active branch or do-while iteration without changing the editable draft.
7. Automated tests cover the editor, backend validation and execution, import/export, runtime status, and FIFO controls listed above.
