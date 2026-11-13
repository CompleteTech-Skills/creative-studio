# Native property inspectors

The editor mounts `inspectors.js` into the native-inspectors panel. Camera projection,
clipping planes and field of view, point/directional/spot light color and intensity,
and world background/exposure submit real Scene3D commands with the currently
acknowledged revision and hash. A failed save remains an error; it does not report
success. Locked nodes are read-only. Controls have associated labels and keyboard
operable save buttons. Successful saves reload the authoritative snapshot.

The Python adapter validates complete candidate native documents before returning
commands. The existing repository supplies atomic persistence, undo and recovery.
The trusted material route publishes an immutable LookDev material version and a
native per-node slot override inside one repository transaction. Original bytes
are retained; material edits can be undone and redone. Only existing native slots
are accepted. Surface and texture structures are checked by LookDev itself.

## Current integration limits

Issue #103 remains open. The native camera contract in this integration supports
projection, clipping and field of view, but does not expose persistent Camera3D
optics, target or path adapters. The native rig adapter for persistent bone posing,
constraints, mirroring and morph editing is also not connected. These controls are
explicitly unavailable rather than simulated.

Material PBR editing has a tested persistence adapter, but remains disabled in the
browser until the importer supplies a canonical slot-to-primitive mapping consumed
by both preview and rendering. Current imported Scene3D assets contain no material
slot references; assigning an arbitrary material to all meshes would change the
meaning of multi-material models. The material HTTP route is a backend integration
interface, not evidence of complete browser PBR editing or rendered appearance.
Environment resource import and area light dimensions are not implemented here.
No complete issue acceptance or production-render agreement is claimed.

## Validation

Run `PYTHONPATH=scripts python -m unittest discover -s tests -p
'test_rig_editor3d_inspectors.py' -v`.
Use `HATCH_EDITOR_BROWSER_TEST=1 HATCH_BROWSER_EXECUTABLE=/path/to/chromium` to
include real browser save/reload and rejection checks. In the shared development
environment, acquire the documented native-validation flock before browser tests.
The inspector tests also run in the `operations-archive` release gate.
The native tests include exact source preservation, material version publication,
reopening, stale-revision rejection, undo/redo, lock enforcement and nonfinite /
invalid values. They use engineering fixtures, not production artwork.
