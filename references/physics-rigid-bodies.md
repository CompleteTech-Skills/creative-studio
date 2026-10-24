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
checks. Initial linear velocity is the center-of-mass velocity in canonical world coordinates.
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
