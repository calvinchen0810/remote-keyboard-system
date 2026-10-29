# Task 4 — Script editor control-flow blocks

## RED

Added editor model, movement, validation, deletion, nested-editor, and submission tests first, then ran:

```text
node --test tools\test_script_editor.cjs
```

The new tests failed as expected before implementation:

```text
not ok 1 - control blocks have stable defaults and preserve an action when moved between slots
error: 'editor.addIfElseBlock is not a function'
```

## GREEN

After implementation, ran the requested command:

```text
node --test tools\test_script_editor.cjs
# tests 57
# pass 57
# fail 0
```

Additional checks:

- Parsed the page's inline JavaScript with Node: `Parsed 1 inline script(s)`.
- `git diff --check` completed without errors.

## Self-review

- Added blank IF / ELSE and DO-WHILE toolbar actions, editable Condition/repeat/max controls, visible indented slots, and a three-choice block-removal dialog.
- Movement uses the original action object and stable ID; drop handlers stop bubbling, allow insertion/reordering within slots, and move actions back to top-level positions. Control blocks are rejected as slot children with an editor log.
- Existing keyboard, mouse, OCR, image-move, and delete editors now resolve nested actions. OCR variable options use earlier top-level actions, current branch/body actions, branch intersection after IF / ELSE, and do-while body definitions after the block.
- Submit validation checks conditions, non-empty required branches/body, repeat direction, iteration bounds, and the 200-action limit. `runScript()` serializes nested children without editor-only IDs and logs invalid drafts without submitting.
- The VM harness retains element children and event handlers; tests exercise the model and drag/drop callbacks, including order and identity.

Task 5 remains responsible for complete structured JSON and image-bundle round-tripping. No broad editor/page restructuring was done. A real-browser layout/interaction smoke test was not run; validation used the requested VM suite and inline-script syntax parsing.
