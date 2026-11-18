# Camera3D motion

`camera3d_motion.sample_motion` evaluates an immutable starting transform and
optics at an exact frame/subframe clock. It returns a detached transform, optics,
terminal marker and SHA-256 dependency key. Dolly, truck, pedestal, pan, tilt,
orbit, crane, follow, dolly zoom, path and seeded analytic shake support arbitrary
seek order. Follow/orbit targets are evaluated world points supplied by the scene
owner; this API does not resolve scene nodes or apply aiming constraints.

Camera-local -Z is forward. Dolly/truck follow camera orientation; pedestal and
pan use world +Y. Distances are meters and angles radians. Dolly zoom preserves
optical-axis subject scale by changing focal length in proportion to subject
depth; it rejects orthographic cameras and crossing/behind-camera targets.
Elliptical orbit computes phase from radius-normalized starting coordinates.
An on-ellipse start is preserved; explicitly changed radii project an off-ellipse
start onto the newly authored ellipse.

Shake is analytic, not a random-number stream: per-axis phases are derived from
SHA-256 of the seed and axis index, so any frame can be evaluated without
earlier frames. The default envelope is zero at the first and terminal frames.

`camera3d_paths.CameraPath` validates and freezes existing `camera3d-path`
documents. Linear, Bezier and Catmull-Rom paths have bounded sampled arc-length
lookup, optional reversal, curvature and numerical kinematics. Arc length is an
approximation: inspect `length_refinement_delta_m` and increase
`samples_per_segment` (8–256) if needed. Parent-space sampling requires a rigid
parent transform; world-space paths reject one to prevent double conversion.
Tangent and target orientation use the shared quaternion camera math. Path
motion requires the path's authored duration and frame rate to match the
sampled clock; implicit retiming is rejected.

`camera3d_animation.smooth_samples` bakes a centered triangular window over
position, rotation and focus. Rotation is deterministic ordered shortest-arc
SLERP; other optical channels remain authored. Boundaries truncate the window
and renormalize. `limit_motion` bakes speed/acceleration/jerk limits from rest and
reports actual measurements and remaining goal error. An infeasible step raises
instead of silently exceeding a limit. Neither function promises endpoint arrival
nor smooths unbounded streaming playback. Both create dependency hashes and
are independent of later seek order.

Typed camera tracks over the shared Animation3D track authority, and per-shot
track overrides, are not part of this module set. They depend on the
Animation3D camera owner adapter and are delivered separately.

## Verification

Run the numerical and actual native gates separately:

```sh
python -m unittest discover -s tests -p test_rig_camera3d_motion.py -v
blender --background --disable-autoexec --threads 1 --python-exit-code 1 \
  --python scripts/verify_camera3d_motion.py -- --output camera-motion-evidence
```

The latter creates three actual 160×120 Cycles movement PNGs (frames 24, 0, 12,
then 24 again), compares Blender camera projections to analytical projections,
and rerenders frame 24 after seeking backward to demand identical decoded pixels
(PNG timing metadata may differ). It writes `report.json` with the Blender
version, source file hashes, projected coordinates and exact PNG hashes.
Setting `HATCH_CAMERA_MOTION_BLENDER` to a Blender executable enables the
subprocess gate within the unittest suite; `HATCH_CAMERA_MOTION_EVIDENCE` keeps
its output directory and `HATCH_CAMERA_TEST_SITE` adds a site directory with
Python dependencies compiled for Blender's embedded Python (Blender 4.5.3 uses
Python 3.11; host Python wheels may be incompatible).

## Requirement traceability (#77)

| #77 task | Implementation | Tests |
| --- | --- | --- |
| 1. Dolly/pan/tilt/truck/pedestal/orbit/crane/follow and dolly zoom | `scripts/camera3d_motion.py` | `CameraMotionTests`: axis movements, pan/tilt, orbit (including elliptical phase), crane/easing, follow, dolly zoom subject size and crossing rejection, parameter admission, out-of-order seek for every motion, real mesh projection; `CameraNativeMotionTests` (Blender) |
| 2. Typed lens/focus/transform tracks, quaternion interpolation, reusable overrides | Not in this module set | Delivered with the camera track adapter |
| 3. Spline path length/curvature and bounded arc-length speed control | `scripts/camera3d_paths.py`, `path` motion | `CameraPathTests`: constant arc-length speed, Bezier speed error, Catmull-Rom interpolation and closure, parent-space admission, bounds, analytic curvature and tangent orientation, detached documents; `test_path_motion_and_hash_invalidation` |
| 4. Analytic/baked/windowed smoothing and seeded shake with out-of-order reproducibility | `scripts/camera3d_animation.py`, `shake` motion | `test_smoothing_*`, `test_baked_motion_limits_are_measured_and_seek_independent`, `test_random_seek_shake_reproducible`, `test_every_motion_is_stateless_under_out_of_order_seek` |

Look-at, framing/collision, export scheduling and editor controls belong to
separate Camera3D work packages.
