# Native rigid-body controls

This work extends the real #168/#169 adapters; it does not replace their source,
worker ownership, exact clocks, cache lifecycle or optional-dependency boundaries.
The implementation is under development: execution receipts and independent
review, rather than this document, establish a verified capability.

Canonical coordinates remain right-handed Y-up meters, XYZW rotations, kilograms,
seconds, newtons and newton-meters. Source Scene3D documents and OBJ geometry remain
immutable. Kinematic translation comes from an actual hash-bound native Scene3D
animation in the validated project closure; it is explicitly authored motion.
Dynamic motion is obtained from the native solver, never authored fall/bounce keys.

## Controls and mappings

`PhysicsForce.application` distinguishes `center` from `world-point`. The latter
uses the specified canonical world-space point. A center request must supply zero
for its unused point; torque requests also use center/zero. `vector * strength`
is the physical wrench. The existing seed field must be zero for these
nonstochastic controls. Bounds apply to magnitude, target count and total
force/body/solver-step operations. Forces target dynamic bodies only.

The DIRECT adapter calls native `applyExternalForce` or `applyExternalTorque`
before actual fixed steps. A continuous force is integrated over its half-open
frame interval, weighting a boundary step by its exact overlapping duration. A
linear or angular impulse uses a wrench of `J / dt` or `L / dt` for **one complete
native fixed step**; this is a finite-step integrated impulse, not an instantaneous
analytical velocity edit. Impulse starts must align with the native solver clock.
The velocity changes, angular response and integration error require numerical
checks. The collision-free momentum oracle does not imply equivalence to an
ideal instantaneous impulse during contact: contact resolution and damping
operate during the same finite native step. Initial linear velocity is the center-of-mass velocity in canonical world coordinates.
It uses the distinct native initial-state setter once,
before dynamics. It is never used to stand in for a sustained force.

For DIRECT, admitted initial and observed native speed bounds are 50 m/s and 50 rad/s.
An actual pinned DIRECT characterization clamps requested 200 m/s and 200 rad/s
to 100 after one step. The DIRECT adapter rejects excessive initial requests and fails
closed if forces/collisions produce observed speed above its conservative bound;
it never publishes that clamp as physically faithful motion. The shared authored kinematic track-speed cap is 50 m/s; this is distinct from
DIRECT's observed solver-speed guard, which is not implemented for Blender.

DIRECT's principal-axis inertia frame is calculated from the validated symmetric
physical tensor. The native diagonal and inertial frame are read back and must
reconstruct that tensor with scale-relative error, strictly positive dynamic
principal moments, and zero static/kinematic inertia. Numerically near-singular
tensors outside the conservative native minimum-principal-moment bound fail
admission instead of silently becoming angular-locked bodies. Centers of mass are passed to the native inertial frame;
reported poses are converted back to the declared body origin in double
precision. A transformed inertial frame can make the native collision shape a
compound wrapper: its reported margin is zero even though the sphere child
retains its complete radius. The adapter validates the actual child dimensions
and composes the native child/inertial frames back to the declared unchanged
primitive before distinguishing wrapper margin from inherent sphere radius.
Actual narrow-phase rays and shifted-inertia floor contacts characterize this
representation; the margin check is not removed. Damping and mass
are native settings. `physics_rigid.density_mass` provides an explicit immutable
conversion from density and one primitive's measured dimensions to mass and
uniform inertia; no material density silently overrides an explicit body mass.

The Blender adapter builds actual active/passive rigid bodies and configures its
native world. Kinematic bodies use the native animated setting and native
translation keys; the supported profile requires one independent linear/step
translation track, unchanged orientation, and complete time coverage. Gravity,
physical material response and damping retain their native mappings. Sleep can
be enabled, initially deactivated, and configured with Blender's native linear
and angular deactivation thresholds. DIRECT supports native sleep/activation
requests, but rejects explicit sleep thresholds because that API does not expose
them. Its receipt records those unexposed thresholds as `null` rather than
inventing a readback.

Blender central forces use real WIND plane effectors with zero flow/noise and no
distance attenuation. The pinned native effector code divides force by the
integer render-fps numerator (`frs_sec`), so field strength is explicitly multiplied
by that numerator, including rational rates with a separate fps base. Readback
retains this conversion and actual two-mass/two-rate acceleration tests must
verify newtons independently of gravity/contact. Their direction and strength represent a central force,
not an airflow velocity. This bounded profile applies each such force to **all**
dynamic bodies in the world. Unsupported target subsets fail admission. Native
frame-end evaluation is accounted for when keying the half-open force interval.

## Explicit gaps

The backend boundary is explicit:

| Control | Blender Bullet | PyBullet DIRECT |
| --- | --- | --- |
| Static, dynamic and authored kinematic translation | Implemented | Implemented |
| Gravity, native material response, linear/angular damping | Implemented | Implemented |
| Sleep and initial sleeping state | Implemented, including native thresholds | Implemented; explicit thresholds rejected |
| Center force | Global native effectors affecting all dynamic bodies | Native per-body external force |
| Off-center force, torque, linear/angular impulse | Rejected | Native external wrench; impulses integrated for one solver step |
| Explicit initial linear/angular velocity | Rejected | Native initial state, before stepping |
| Custom center of mass and inertia tensor | Rejected; native shape-centered mass properties | Native inertial frame and verified principal moments |

Support in the DIRECT column does not imply support in Blender. The rendered
DIRECT replay is a view of separately solved states, not a second Blender
simulation of those controls. Both backends retain actual state, settings and
source records for the supported operations.

Blender 4.5.3's public rigid-body API does not expose direct off-center wrench,
torque, impulse, arbitrary initial velocity, shifted inertial-frame or custom
inertia setters. These requests fail explicitly on that backend. They are not
approximated by kinematic movement or velocity assignment. DIRECT provides the
listed additional controls through its separate actual solver; this is not proof
that Blender supports those controls. #170 must remain open if its full
acceptance requires those unavailable Blender operations.

The pinned Blender [native rigid-body implementation](https://github.com/blender/blender/blob/v4.5.3/source/blender/blenkernel/intern/rigidbody.cc)
applies effectors as central forces, activates affected bodies, and steps Bullet
with its configured substep. Its [effector implementation](https://github.com/blender/blender/blob/v4.5.3/source/blender/blenkernel/intern/effect.cc)
constructs the WIND force from its direction, strength and falloff. Neither source
establishes native off-center torque support through the Python effectors route.

Compound shapes, additional collision profiles, arbitrary animation bindings,
constraints, cross-category simulation and native checkpoint continuation remain
outside this slice. Saved samples and Blender bakes retain their prior replay
limits. Engineering diagnostic renders are not human or production-art approval.

## Execute and inspect the engineering examples

Use the pinned host/optional-worker installation documented in
[physics-backends.md](physics-backends.md). Set `HATCH_BLENDER` to the actual
supported Blender4.5.3 binary and `HATCH_PYBULLET_PYTHON` to the isolated
CPython3.11 interpreter containing `requirements-physics-pybullet.txt`. The host
uses Python3.12 with `requirements-rig-test.txt`. No backend is imported or
installed by ordinary2D workflows. Native execution is currently POSIX only.

These commands run the real cases and retain original sources, settings,
version/usage observations, cache hashes and actual generated `.blend`/PNG files:

```bash
mkdir -p output/rigid-cases output/replay-cases
HATCH_PHYSICS_BACKEND_TEST=1 HATCH_RIGID_EVIDENCE=output/rigid-cases \
  python3.12 -m unittest discover -s tests -p test_rig_physics_rigid.py -v
HATCH_PHYSICS_BACKEND_TEST=1 HATCH_RIGID_REPLAY_EVIDENCE=output/replay-cases \
  python3.12 -m unittest discover -s tests -p test_rig_physics_rigid_replay.py -v
```

Choose fresh output directories for repeated runs. Without the explicit native
gate, the native cases are **skipped**, establishing no solver capability. Expected
negative cases retain failed-attempt evidence labeled as failed, not reusable
successful caches. The test methods include bounce/rest, a mass-ratio10 stack,
physical force/damping/impulse controls, and source-bound replay of supplemental
DIRECT COM/inertia/velocity/off-center impulse/torque states in Blender.

The simultaneous four-body Falling Objects source is available independently of
the test runner. The original `physics_backends.py demo` remains the earlier
two-body readiness fixture; it is not relabeled as this new demonstration:

```bash
mkdir -p output
PYTHONPATH=scripts python3.12 - <<'PY'
import os
from physics_backends import BlenderBackend
from physics_rigid_example import build_falling_inputs
backend = BlenderBackend(os.environ['HATCH_BLENDER'])
world, sources = build_falling_inputs()  # floor, sphere, cube and heavier cube
prepared = backend.prepare(world, sources)
try:
    solved = backend.simulate(prepared)
    print(backend.bake(solved, 'output/falling-cache'))
    print(backend.export_animation('output/falling-cache', 'output/falling-transforms'))
finally:
    backend.cleanup(prepared)
PY
python3.12 scripts/physics_backends.py inspect output/falling-cache --frame 24
```

`build_stack_inputs()` supplies the separate stable-contact fixture. For a real
DIRECT cache, `physics_rigid_render.render_direct_cache(cache, new_destination,
executable=...)` creates a source-bound editable Blender replay, first/middle/last
neutral diagnostic renders, independent reverse-order reopen verification and
complete hashed inventory. Blender renders those already solved states; it does
not re-simulate DIRECT controls or couple two physics worlds. Only cached integer
poses are verified. This is separate from future production render integration.

## #170 acceptance traceability

The following executable methods are in `tests/test_rig_physics_rigid.py`; the
replay cases are in `tests/test_rig_physics_rigid_replay.py`. Their actual retained
execution records, rather than merely discovery of these methods, establish the
observed outcomes at the recorded source revision.

| Required behavior | Executable verification | Inspectable evidence |
| --- | --- | --- |
| Actual static/dynamic Blender bodies; gravity/mass independence | `test_real_blender_and_direct_freefall_zero_g_and_mass` | original native meshes, configured world, solved heights and saved Blender cache |
| Sphere/cube/heavy-object scene | `test_real_simultaneous_falling_objects_scene` | four actual bodies with masses1/0/1/10, three fall trajectories, rendered cache |
| Kinematic authority distinct from dynamics | `test_real_kinematic_native_clip_and_dynamic_authorities` | hash-bound native Scene3D translation clip plus separate solver-driven falling body |
| COM/custom inertia and initial velocities | `test_real_direct_impulse_torque_initial_velocity_and_custom_inertia` | native principal-frame readback, both tensor cases, original-origin poses and raw cache |
| Central physical forces; correct SI units | `test_real_native_central_force_is_mass_dependent` | zero-gravity/no-contact two-mass controls at24 and30000/1001fps on both backends |
| Off-center and angular response | `test_real_direct_offcenter_force_and_damping` and torque/impulse case | quantitative position/quaternion oracle, native external wrench calls and angular/linear damping controls |
| Finite-duration integrated impulse | `test_real_finite_pulse_timestep_convergence` | three actual timesteps and continuous rectangular-pulse error convergence; not instantaneous-contact equivalence |
| Bounce and resting | `test_real_native_bounce_and_rest` | real post-impact rise and final stable height window with nonzero restitution |
| Stable stacking | `test_real_native_stack_tail_is_stable` | both native backends, mass-ratio10 two-cube stack,24-frame height/drift window |
| Sleeping and force-driven waking | `test_real_sleep_and_force_wake` | unchanged initially sleeping pose, then actual falling after native force activation |
| Preserve transformed collision geometry | `test_real_transformed_child_collision_and_native_speed_failure` | actual child radius/frame observations, wrapper margin, floor contact and fail-closed excessive-speed run |
| Actual Blender view of supplemental DIRECT states | replay lifecycle case | exact original source/cache bytes, editable Blender file, three actual images, all-frame native reopen and integer-float byte-count boundary |
| Strict invalid-input, resource and provenance boundaries | pure rigid/replay tests plus prior backend lifecycle suite | malformed clock/tensor/frame/report/image/path rejection and unchanged source inputs |

These are engineering fixtures. Automated numerical/structural validation,
assistant visual inspection, independent model review and human production-art
approval are distinct. No human or production-art approval is supplied here.
