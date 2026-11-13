# Camera3D motion and tracks

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

`camera3d_paths.CameraPath` validates and freezes existing `camera3d-path`
documents. Linear, Bezier and Catmull-Rom paths have bounded sampled arc-length
lookup, optional reversal, curvature and numerical kinematics. Arc length is an
approximation: inspect `length_refinement_delta_m` and increase
`samples_per_segment` (8–256) if needed. Parent-space sampling requires a rigid
parent transform; world-space paths reject one to prevent double conversion.
Tangent and target orientation use the shared quaternion camera math.

`animation3d_registry.TargetRegistry` resolves the camera through the shared
Animation3D camera owner adapter, backed by an exact-byte Camera3D registry.
This requires the #67 native owner closure, a dependency beyond the foundation. `sample_camera_tracks` uses Animation3D track validation,
clock and native interpolation; it does not invent another track schema.
Translation, quaternion rotation, focal length, orthographic size, focus distance
and aperture tracks are supported by that native registry. Horizontal FOV and
exposure are declared channels but lack persisted native registry properties;
the owner adapter rejects them until the camera/render contract supplies them.
FOV needs an explicit effective horizontal sensor width. Competing focal-length
and FOV tracks, duplicate channels, foreign targets and invalid evaluated optics
are rejected. Exposure is returned separately as `controls.exposure_stops` for
the renderer. Transform outputs and base TRS use native camera-parent-local
space. World-space translation/quaternion tracks require an explicit rigid
`parent_world` (identity for an unparented camera) and convert through its inverse.
Explicit `parent` track space is rejected pending separate semantics; omitted
or `local` space uses native parent-relative TRS. Callers must use a matching base transform and optics from their
resolved camera graph; tracks replace only authored channels. Preset/resource
resolution and reusable binding overrides remain owned by Camera3D sequencing.

`camera3d_animation.smooth_samples` bakes a centered triangular window over
position, rotation and focus. Rotation is deterministic ordered shortest-arc
SLERP; other optical channels remain authored. Boundaries truncate the window
and renormalize. `limit_motion` bakes speed/acceleration/jerk limits from rest and
reports actual measurements and remaining goal error. An infeasible step raises
instead of silently exceeding a limit. Neither function promises endpoint arrival
nor smooths unbounded streaming playback. Both create dependency hashes and
are independent of later seek order.

## Verification

Run the numerical and actual native gates separately:

```sh
PYTHONPATH=scripts:tests python -m unittest test_rig_camera3d_motion -v
blender --background --threads 1 --python-exit-code 1 \
  --python scripts/verify_camera3d_motion.py -- --output /tmp/camera-motion
```

The latter creates three actual 160×120 Cycles PNGs, compares Blender camera
projections to analytical projections, and rerenders frame 24 after seeking
backward to demand identical decoded pixels (PNG timing metadata may differ). It emits a JSON report with Blender
version, coordinates and exact PNG hashes. `HATCH_CAMERA_MOTION_BLENDER` enables
the subprocess gate within the unittest suite. Blender needs the optional 3D
Python dependencies. Shared hosts must acquire the coordination native lock for
the entire command lifetime.

Requirement traceability for #77: procedural movements/dolly zoom are covered by
`CameraMotionTests`; typed channels and native source integrity by
`CameraTrackTests`; spline/arc-length/curvature and space admission by
`CameraPathTests`; smoothing/limits/shake random seeking by `CameraMotionTests`;
actual renderer seeking/projections by `CameraNativeMotionTests`. Look-at,
framing/collision, export scheduling and editor controls are separate packages.
