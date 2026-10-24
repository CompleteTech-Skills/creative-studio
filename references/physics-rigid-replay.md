# DIRECT solved-state Blender replay

`physics_rigid_render.render_direct_cache(cache, destination, executable=None)`
accepts a completed, hash-and-contract-verified PyBullet DIRECT cache and a new
output directory. It preserves the complete original cache, including its native
Scene3D project and actual OBJ meshes, under `source-cache/`. It independently
verifies that private snapshot before executing the package-owned Blender worker.
The existing trusted subprocess runner enforces process isolation, resource
bounds and a shared deadline. Blender 4.5.3 build `67807e1800cc` is required.

The worker builds actual Blender meshes from admitted source geometry and keys
the actual cached body-origin translations and rotations. Blender does not
simulate these bodies. The saved scene explicitly identifies this as DIRECT
solved-state replay with Blender rendering only. Only integer cached frames are
verified; interpolation accuracy is not established. Original canonical physics
inputs and solver receipts remain in the retained source cache.

The output includes `scene.blend`, three representative first/middle/last neutral
128×128 CPU Cycles diagnostic renders (four samples), package worker and job,
actual process logs, and `replay.json` with the full output inventory and hashes.
One setup creates the camera, light and neutral materials; subsequent frames use
that same setup, fitted to the complete cached trajectory and transformed actual mesh bounds. These are diagnostic images without artistic approval.

A separate Blender process reopens only the freshly generated private `.blend`,
checks its source identity, actual mesh vertices/topology and absence of a rigid
body world, and compares translations and quaternion rotations at every cached
frame in reversed order. Quaternion sign equivalence is allowed. The retained
blend hash must survive reopen unchanged. Output publication uses the shared
exclusive atomic publication primitive and preserves an existing destination.

Run `tests/test_rig_physics_rigid_replay.py` with the rig dependency interpreter
for admission checks. Setting `HATCH_PHYSICS_BACKEND_TEST=1`, `HATCH_BLENDER` and
`HATCH_PYBULLET_PYTHON` enables the actual DIRECT → render → independent reopen
lifecycle test with initial velocity, shifted COM, a dense inertia tensor, an off-center impulse and torque. Set `HATCH_RIGID_REPLAY_EVIDENCE` to retain its actual complete source cache, blend and rendered images under a new test directory. A skipped native test is not native execution evidence.
