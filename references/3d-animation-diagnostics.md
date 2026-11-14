# Canonical curve and continuity diagnostics

`animation3d_diagnostics` inspects values produced by the existing typed
`sample_track` authority. It adds no curve, pose, skeleton, skinning or playback
implementation. The caller supplies its authoritative target registry. One
binding is admitted and detached for the call; the source track is copied before
validation. Results carry the canonical track hash and admitted source reference.

```python
from animation3d_diagnostics import diagnose_clip, diagnose_track
report = diagnose_clip(saved_clip, registry,
    policy={"maximum_velocity": 10, "maximum_acceleration": 100})
track_report = diagnose_track(saved_track, registry, fps={"numerator": 24,
    "denominator": 1}, duration_frames=24,
    intentional_cuts=[{"frame": 12}])
```

Reports measure short-interval translation/rotation/scale changes, velocity and
acceleration, sampled spline overshoot, native property-domain errors, quaternion
hemisphere signs, repeated-loop value/velocity seams and short visibility pulses.
Redundant Boolean keys cannot hide a pulse. Stepped intervals are explicit cuts
for derivative diagnostics. Caller-supplied cuts acknowledge the boundary they
name; they do not excuse high velocities elsewhere along a continuous curve.
Visibility pulses need an explicit cut acknowledgment. `hold` and `ping_pong`
clip modes do not wrap the terminal value to the first value and receive no
repeat-loop seam warning.

Time subdivision uses exact rational positions, rounded once to the canonical
million-ticks-per-frame grid. Rates and derivatives use the declared rational
fps. Quaternion velocities use shortest-path rotations in one common frame.
Thresholds are caller policy: distances use meters, angles radians, scale is
dimensionless, and derivative thresholds use seconds. Thresholds must be finite,
nonnegative and bounded; Boolean thresholds and oversized integers reject.

The default grid has 16 subdivisions per key segment. Limits are 512 keys,
4,096 samples per track, 128 tracks and 16,384 predicted samples per clip.
Excess work rejects before evaluation. `source_valid` means the source passed
its closed contract and target admission. `sample_complete` additionally tells
whether every requested sample respected its native property domain. A
property-domain failure remains an explicit finding and does not connect the
surrounding valid samples into an invented derivative interval. The track API
can report unsupported extrapolation as an invalid-source diagnostic; whole
clip validation remains strict.

This is a **sampled diagnostic**, not a proof that no extrema exist between
samples. Reports explicitly state their coverage and always return
`approved: false`. They confer neither artistic approval nor a production-ready
motion classification. Repeat with a denser bounded grid or use the native
owner's specific validation where a continuous bound is required.

## Euler conversion checks

`diagnose_euler_conversion(quaternions, converted_xyz)` checks actual
caller-provided XYZ Euler conversion output against the canonical
`animation3d_math.euler_xyz` reconstruction. XYZ angles are radians in the
existing `Rz @ Ry @ Rx` convention. Findings distinguish reconstruction error,
a conversion singularity and a large Euler-coordinate branch jump accompanying
small physical rotation. This does not introduce Euler animation tracks or
promise to remove gimbal lock. Conversion input dimensions and numerical ranges
are validated before NumPy conversion.

## Configurable easing proof for issue 67

The existing `ease` interpolation is a Hermite time warp. For segment fraction
`u`, the start slope is `1 - left.ease_out` and the end slope is
`1 - right.ease_in`. Thus `ease_out=1, ease_in=0` eases in;
`ease_out=0, ease_in=1` eases out; both one produce smoothstep; both zero produce
linear time. Intermediate controls continuously change the end slopes.

The independent regression uses the literal Hermite basis at `u=1/4`:

```
f = 5/32 + (9/64)*(1-ease_out) - (3/64)*(1-ease_in)
value = start + (end-start)*f
```

It exercises nondefault asymmetric and fractional controls, independent of
realtime playback or any editor UI. Quaternion channels continue to use their
native SLERP/SQUAD modes instead of scalar component easing.

## Executed validation and traceability

`tests/test_animation3d_diagnostics.py` executes:

| Requirement | Test evidence |
| --- | --- |
| Position jumps, rates, acceleration, repeatability | Literal 240 m/s oracle; repeated complete reports and unchanged input |
| Intentional discontinuities | Stepped intervals and explicit cut boundaries produce no infinite-derivative finding |
| Rotation/scale discontinuity and sign handling | Actual normalized shortest rotation and sign-equivalent keys |
| All supported curve families | Canonical step/linear/ease/Bezier/Hermite/Catmull/TCB/SLERP/SQUAD sampled values |
| Overshoot and native domain failure | Actual Hermite translation overshoot and invalid scale samples |
| Loop and visibility continuity | Repeated-loop value/velocity seams; hold exclusion; redundant-key visibility pulse |
| Invalid declarations and resource limits | Unsupported extrapolation, unknown/malformed policy, bounded sample work |
| Euler artifacts | Canonical reconstruction, actual wrong conversion, singularity and equivalent branch crossing |
| Configurable easing | Exact independent rational Hermite-basis oracle, including asymmetric/fractional controls |
| Genuine skeletal motion | Persisted native rig; curve velocity finding and per-vertex one-meter displacement from actual FK/skinning |

The native fixture test preserves every original source hash and never substitutes
metadata changes for measured geometry. No Blender render or human visual
approval is claimed. CLI/editor exposure and whole-candidate review remain with
the Animation68 integration owner.
