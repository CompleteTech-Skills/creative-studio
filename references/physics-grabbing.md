# Physical prop grip and release consumers

`physics_grabbing.py` reuses canonical rig attachment sockets, `rig_attachments`
and the Scene3D asset's named sockets. Actor root transforms are explicit;
coordinates remain right-handed +Y up, +Z forward, meters, XYZW quaternions.
Reach admission measures present grip distance and orientation. It does not
promise unobstructed reach, character locomotion, or an IK solution.

`attach_kinematic`, `attachment_pose`, `detach_kinematic` and
`handoff_kinematic` compute authored carry transforms while preserving world
pose. Their results are uncommitted proposals. The underlying rig character ID
is not an actor-instance identity. Persistent, exclusive, exactly-once
ownership must use the shared continuity IdentityRegistry transaction authority
(#118, on the persistent world-state foundation #117). This module creates no
new ownership store or animation scheduler. Character movement remains #173.

`release_state` consumes two increasing, exact rational-time world transforms.
It recovers origin velocity and shortest-arc world angular velocity, then
includes the angular contribution at the center of mass. More than pi radians
between samples is ambiguous and is not inferred. Explicit linear and angular
impulses remain separately declared native force inputs. No velocity is reset
to zero unless the sampled motion actually gives zero. Nonunit scales, malformed
numbers, ambiguous times and excessive speed fail before native execution.

`compile_release` returns a new PhysicsWorld plus source payload snapshot. The
original world, source Scene3D, rig and geometry bytes are preserved. The
SceneGraph owner applies the new pose with its lock and revision checks;
project/scene hashes are rebound. The supplied original collider profile is
retained, and the released body becomes awake and dynamically physics-owned.
Other transform writers, parents, active joints, and pre-existing force/event
programs fail closed in this bounded profile. Use the ordinary PyBulletBackend
to execute, publish, verify and replay this world. Compilation predicts no
dynamic trajectories.

The fixed and spring joint owner currently supports whole-bake constraints,
not evented attachment/removal. Those attachment strategies are explicitly
unsupported here. There is no claim of full #174 completion: persistent
ownership, collision-aware approach, integrated grab/release events and the
two-character end-to-end handoff depend on #173/#118 and the joint authority.

The explicit optional native gate creates an actual weighted humanoid rig,
evaluates its named grip socket, carries an actual mesh prop kinematically,
and releases it through the existing native DIRECT adapter. It compares
measured velocity and separately applied impulse, ballistic fall and actual
floor contacts. The retained solver states are rendered and reopened by the
existing Blender replay owner. These engineering fixtures are not production
art or proof of persistent ownership.

```sh
python -B -m unittest discover -s tests -p test_rig_physics_grabbing.py -v
HATCH_GRABBING_TEST=1 HATCH_PYBULLET_PYTHON=/path/to/isolated/python \
HATCH_BLENDER=/path/to/blender HATCH_GRABBING_EVIDENCE=/new/evidence/root \
python -B -m unittest discover -s tests/optional -p test_physics_grabbing_native.py -v
```

The native gate has its own explicit CI step. Ordinary 2D discovery neither
loads optional 3D dependencies nor discovers the optional native test directory.
