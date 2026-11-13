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

Scalar PBR assignment consumes the canonical `material_bindings` mapping from the
Scene3D importer. Imported source surfaces are read-only; assigning a new authored
material replaces only the selected slot. Existing scalar native overrides can
then be edited as immutable versions. New materials make no license assertion
(`LicenseRef-Unspecified`). Native texture and advanced surface controls remain
unavailable until their adapters are connected. Browser primitive targeting uses
normalized GLB parser associations, or the canonical single-surface OBJ mapping.
Full production-render agreement remains a separate acceptance requirement.
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

## Native pose authoring

The native pose inspector resolves one exact imported rig below the selected
node. Bone translation, XYZW quaternion and scale are authored as absolute local
Pose overrides; omitted channels use the skeleton rest transform. Morph controls
retain the mesh and target identity. Reset publishes a neutral Pose. Mirroring
requires explicit approval of the rig symmetry. Every edit uses the selected
saved shot and frame, publishes one immutable revision, and reloads before
requesting native geometry from the canonical scene evaluator. Skeleton rest
assets are preserved. Inherited collection and layer locks disable authoring.

The viewport consumes the evaluator's world-space vertex samples using the exact
imported node and mesh identities. It validates membership, topology, finite
coordinates and the 100,000-vertex budget before applying an update, then
recomputes normals and bounds. Clearing preview restores bind geometry; saved
pose-bound components stay hidden until their exact frame is sampled. Constraint
configuration, rest-skeleton editing and pose/clip blending remain unavailable.
These engineering fixtures do not establish full issue103 acceptance or visual
approval.

Run `PYTHONPATH=scripts:tests python -m unittest test_rig_editor3d_pose_inspector -v`.
Set `HATCH_EDITOR_BROWSER_TEST=1` to run the actual Chromium authoring, every-world-
vertex comparison, malformed-sample atomicity and clear-preview checks. The
Scene3D service workflow runs this browser acceptance explicitly.
