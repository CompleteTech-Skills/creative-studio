# Native collision proxies and queries

The additive `collision_profile: "explicit-proxies-v1"` PhysicsWorld profile
separates render geometry from collision proxies. The current implementation
uses the optional, isolated PyBullet 3.2.7 DIRECT worker. The legacy Blender and
DIRECT sphere/box profile remains unchanged when this property is absent.

The explicit profile accepts spheres, boxes, capsules and cylinders with explicit
X/Y/Z axes, convex hulls, compound trees and static triangle meshes. Compound
children carry their own local transforms. A compound of verified convex pieces
is an explicit convex decomposition; there is no automatic decomposition claim.

Hull inputs must be closed, outward, consistently oriented convex surfaces. The
validator rejects flat hulls, degenerate/duplicate triangles, unused vertices and
concave hull faces. Triangle meshes may be open but are rejected on dynamic
bodies. A proxy mesh is bounded to 4,096 vertices and 8,192 triangles; compounds
have at most 64 leaves and eight levels, with at most 128 leaves per world. Existing world and process budgets also
apply. Visual OBJ assets are independently decoded and bound to the native
Scene3D inventory. Their geometry need not match a separately declared proxy.
All original source bytes remain in the verified cache.

`group` and `mask` use unsigned 32-bit values mapped to native signed masks
without changing bits. Both objects must permit the interaction. Optional
`collision_exclusions` is a list of distinct existing body-ID pairs; reversed
or duplicate pairs fail admission. Native pair exclusions override masks.
Sensors, shape sweeps and continuous collision guarantees are unsupported and
fail rather than being implemented as invisible collision-response changes.
The existing `continuous_collision_detection` setting remains unsupported.

The worker reports native shape types, dimensions and child frames. Verification
compares these observations to the requested proxy and the native inertial frame.
Native mesh filenames are not treated as source evidence: Bullet may report
`unknown_file`. Source hashes, deterministic owned proxy files, and the complete
worker request bind that geometry instead. Native broad/narrow-phase response,
contact normals and distances come from Bullet. Extended contact records also
contain lateral forces/directions and `normal_impulse`, calculated as the observed
normal force times one effective solver step; it is not an accumulated frame
impulse. Contacts retain the actual upper solver bracket time.

## Run the playground

With the repository's optional rig dependencies and an isolated PyBullet worker:

```bash
python scripts/physics_collision_demo.py output/collision-playground \
  --python /path/to/pybullet-environment/bin/python
```

The demonstration executes a real ramp, bouncing sphere, sliding box, rolling
cylinder, two colliding dynamic spheres, compound body, convex body, high-speed
projectile and excluded pair. It saves the actual immutable cache, observations,
rendered sampled frames and a diagnostic GIF. The excluded body must pass through
the floor; the output retains high-speed trajectories without pretending they
establish CCD. Rendered previews use actual collision geometry and solved cached
poses. They are engineering diagnostics, not production-art approval.

## Query completed states

```python
from physics_collision_query import raycast_cache

result = raycast_cache(
    "output/collision-playground/cache", 24,
    [{"start": [0, 8, 0], "end": [0, -2, 0], "mask": 1}],
    interpreter="/path/to/pybullet-environment/bin/python",
)
```

The API first verifies and captures the full source/cache closure, reconstructs
actual native collision shapes at the selected exact cached output pose in a
private isolated worker, then executes native closest-hit ray queries. It does
not advance or resume physics. Results identify the body, hit fraction, position
and normal, or an explicit miss. Rays are limited to 1,024 per request, endpoints
within 10,000 meters, and a nonzero segment. Unknown frames or unverified caches
fail. Bullet3.2.7 misses zero-margin convex hulls in native ray tests, so rays whose masks include any convex-hull body (including compound children) fail with an explicit capability gap. Assign distinct collision groups and exclude hull groups to query supported shapes; previews still show all collision geometry. The playground places its hull in group2 and queries group1. `preview=True` returns a decoded PNG rendered from that same native proxy
state. The returned receipt binds the source cache, world, worker and actual
backend identity. No arbitrary input file or uploaded code is executed.

## Verification boundaries

`test_rig_physics_collisions.py` contains geometric, schema and malformed-input
checks plus an opt-in actual solver gate (`HATCH_PHYSICS_BACKEND_TEST=1`). The
physics workflow executes the complete gate and retains its artifacts. The
native cases compare shape landing/contact behavior, filtering and excluded
pairs, friction, restitution and dynamic collisions. They exercise real cache
verification and native ray/preview behavior. There is no claim of a complete
physics engine, liquid/cloth coupling, arbitrary CCD or Blender support for this
new explicit profile. Issue #171 acceptance must be assessed against executed
current-head evidence, including the actual playground outputs.

Native cache queries reconstruct zero-margin boxes as exact twelve-triangle closed
surfaces for the query process only. Bullet3.2.7 can falsely miss rays against its
zero-margin box support shape; adding an undisclosed skin is not used. The solver
keeps its original box. Query receipts explicitly name
`exact-box-triangle-surface-v1;native-other-proxies-v1` and bind the original world
and owned worker hash. Box vertices use the native in-memory double-precision API; OBJ import is not
used for query boxes because its float32 quantization can create false boundary
hits. Compound leaves receive separate native query bodies mapped back to their
original source body. Native slab-oracle tests cover648 forward/reverse/inside face/near-edge/near-corner
rays over .001–1000m boxes, including non-binary-friendly999.123456789m dimensions, and rotated/offset poses with a1e-8m position tolerance.
This is a ray-query representation, not evidence of new solver geometry or
continuation. Zero-margin convex-hull and cylinder queries remain explicitly unsupported;
the playground assigns these to group2 and explicitly queries group1. Raw native
characterization confirms sphere and capsule radius mappings survive zero extra
margin; cylinder cap rays falsely miss and are not advertised.
