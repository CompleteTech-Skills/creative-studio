# Keyboard access to the saved native scene

The conventional editor now supports one keyboard focus stop for the hierarchical
outliner. Tab enters the current object row; Up/Down and Home/End focus other rows,
Left focuses a parent and Right focuses the first child. Enter or Space selects
that object through the same native selection rules used by the viewport. Hidden
and locked rows remain discoverable, announce their disabled state, and cannot
be selected or transformed. Tab leaves the outliner for ordinary controls.
Numeric inspector fields provide a noncanvas transform alternative; each accepted
edit persists through the existing revision-checked command service.

The Keyboard reference button and `?` open a searchable modal reference listing
only implemented shortcuts. Escape closes it, focus stays inside while it is
open, and closing returns focus to its invoking control. If an in-flight acknowledged save
rebuilds the outliner, focus returns to the same native node or bone row. If that
row was removed, the stable Keyboard reference button receives focus. Status, selection and
errors use conventional live-region semantics; this is not a claim of manual
screen-reader certification. The search uses text nodes and never renders supplied
HTML. Focus outlines remain visible against selected object rows.

Scene shortcuts leave native text inputs, selects, rich-text editing, Alt
combinations and unrelated Ctrl/Cmd browser commands alone. Object movement arrows
apply only while the viewport has focus. Outliner arrows navigate without writing
a transform. Ctrl/Cmd+Shift+Z recognizes the actual uppercase keyboard event.
Commands save automatically after backend acknowledgement; this increment does
not introduce a fake manual Save shortcut or any unsupported editing capability.

## Requirement traceability and limits

| Authoritative requirement | Implemented increment and execution |
| --- | --- |
| #17 §28 keyboard operation and focus order; #106 task 1 | Roving outliner focus, native controls, modal focus containment and return; actual Chromium keyboard tests |
| #17 §29 discoverable searchable shortcut reference; prevent text/browser interference | Searchable dialog, supported key descriptions, modifier/rich-text/widget guards; real keyboard events |
| #17 §30 noncanvas selection/transform, semantic labels, visible focus and announcements | Native outliner selection, numeric transforms, live status/selection, existing error alert; real duplicate/edit/save/undo/redo/reopen sequence |
| #17 §31 native security boundaries; #106 invalid/boundary tests | Existing validated commands unchanged; hidden/locked rows cannot bypass native selectability; literal search input remains text |
| #17 §34 acknowledged persistence; #106 actual affected workflow | Browser keyboard duplicate and numeric edit commit immutable revisions, undo/redo and authenticated reopen preserve native values |
| #17 §37 testing and #106 evidence | `test_rig_editor3d_accessibility.py` is in the schema gate and explicitly enabled in actual browser CI. Retained source-bound local execution is recorded in the coordination receipt. |

This is a bounded part of issue #106. That issue and parent #17 remain open.
Resizable/persistent workspaces, all workspace presets, UI scaling, full responsive
layout acceptance, cross-browser and manual assistive-technology certification,
performance profiling, the complete rig/material/timeline workflow, archive
restore, and real production render/export acceptance require their existing
owners and further integrated verification. Accessible native controls do not
certify the canvas gizmo, bone/curve authoring or every optional extension.
Opening a new browser document requires the authenticated launch URL because the
bearer token deliberately stays in page memory; it is never saved in the project,
a browser storage mechanism or a query string.

Run with installed Chromium and the rig dependencies:

```
HATCH_EDITOR_BROWSER_TEST=1 PYTHONPATH=scripts:tests python -m unittest -v test_rig_editor3d_accessibility
```

Without that explicit flag, the native browser cases report their opt-in skip;
collection is not execution evidence. Local orchestration must also use the shared
browser validation lane and an owned home temporary directory.
