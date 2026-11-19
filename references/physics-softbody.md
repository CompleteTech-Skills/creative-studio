# Native soft-body deformation

`python scripts/physics_softbody.py --blender /path/to/blender probe --smoke`
executes the Blender 4.5.3 soft-body solver against real imported Scene3D/OBJ
geometry. `demo NEW_DIRECTORY` preserves source bytes, per-vertex samples,
native disk caches, an editable Blender file and diagnostic renders. `verify`
checks the complete inventory and source closure; `inspect CACHE FRAME` returns
one stored evaluated mesh sample. It does not re-run a simulation.

The bounded adapter supports one closed connected triangulated soft body and
static canonical box/sphere collider meshes. The common physics world contract
owns component mass, edge stiffness, damping, gravity, pins and collider IDs.
Stiffness is admitted from 0.01 to 0.99 and maps to native pull/push. Total mass
is divided by the vertex count to set native vertex mass. Each native vertex must have at least 0.01 kg; lower requests fail before execution because Blender internally clamps the force denominator below that boundary. Normalized damping
maps to Blender's edge damping range 0–50. Air friction is explicitly zero. Numerical stability is checked from actual evaluated output; admission alone does not promise a stable trajectory for every parameter combination.
Pins use a source-bound goal vertex group; numerical pin residuals must be
inspected. Blender 4.5.3 soft-body timing is frame-based and ignores render FPS. Its gravity force scale is 0.001. The adapter sets native speed to `sqrt(1000) / fps` to map solver evolution to physical seconds; actual 12/24-fps free-fall and equal-time tests check the mapping against SI gravity with a 1 mm position tolerance. Native speed is read back during simulation and reopen, and is bound into the private worker job. The derivation follows [the pinned Blender implementation](https://github.com/blender/blender/blob/v4.5.3/source/blender/blenkernel/intern/softbody.cc); it is qualified by actual solver tests, including the minimum supported vertex mass. Native fixed minimum/maximum steps per output interval are
`iterations * substeps`, with adaptive step selection disabled. Requests whose
rational fixed interval disagrees with these configured limits are rejected.
Native substep state is not exported or asserted independently. The retained fixture uses 1,000 native steps per 24 fps output interval (1/24,000 second), with a 100,000 step budget. Output is bounded to 128 frames and a Blender FPS numerator at most 32,767.

Outputs retain evaluated vertex positions in the canonical right-handed Y-up
meter coordinate system. The adapter uses the existing conversion authority's
proper Y-up/Z-up basis. Original source and native state remain distinct.
Three rendered diagnostic frames show actual evaluated geometry, rather than
a rigid scale animation. They are engineering evidence, not artistic approval.

This is a surface spring model. Volume preservation, pressure, incompressibility,
self-collision, moving colliders and two-way rigid-body coupling are unsupported.
The unchanged general rigid-body adapter does not advertise those capabilities.
A native reopened-file reverse seek under changed gravity checks that the saved
bake supplies the retained evaluated vertices. Restarting the solver from an
arbitrary intermediate state is not implemented. Published files use exclusive
no-replace promotion, and every cache inventories all required native bytes. Simulation and reopen use separate immutable trusted jobs. Disk caching is enabled after saving the native project so Blender retains the setting.

A static verification report establishes exact-byte/source consistency and
checks recorded native observations, source geometry, native jobs, Blend containers and decoded diagnostic PNGs. Cache inspection and metrics use the exact verified byte snapshot. Retained-world cache, temporary-copy and per-file limits are checked before native payloads are retained; the manifest counts toward those limits. The world and manifest bootstrap reads also have fixed document limits. Worker bytes are bound to this installed adapter version. It is not a signed provenance service or
a fresh solver execution. `probe` without `--smoke` reports discovery separately
from actual solver readiness. The adapter exposes soft-body capability probes, resource estimates, cache validation and evaluated-mesh inspection; transform-only animation export is unsupported.

Unsupported fields, missing bytes, changed source
hashes, malformed topology and exceeded resource limits fail before publication.

Run the explicit solver gate with `HATCH_SOFTBODY_TEST=1 HATCH_BLENDER=/path/to/blender python -B -m unittest discover -s tests -p test_rig_physics_softbody.py -v`. Ordinary 2D discovery does not execute this gate. The native workflow runs it with the existing checksum-verified Blender and isolated host dependencies. Diagnostic artwork remains an engineering fixture, and the broader stage/parent acceptance remains open.

Historical pre-clock-correction evidence below used native speed 1. It remains exact geometry/cache evidence, but its frame labels did not establish physical seconds. New clock-correct evidence must be checked against its own worker and source revision. The historical engineering fixture uses stiffness 0.2 and damping 0.2.
At 1,000 native steps per frame, its 100-frame measured final center is about
0.491 m above the floor and maximum final-ten-frame vertex step is 0.014 m.
The matched stiffness-0.7 control deforms less but continues bouncing at frame 99;
it is not labeled settled. Reducing damping from 0.2 to 0.05 at stiffness 0.7
increases the measured tail vertex step (about 0.118 to 0.163 m). These are
pinned-environment comparisons, not universal material calibrations. An earlier goal-pin control was invalid: zero native default goal weight disabled every pin while global goal friction slowed freefall. That failed evidence is retained, not accepted as pin proof. The corrected mapper uses default weight1, explicit selected vertex weights and zero goal friction; selected and unselected vertex motion must be distinguished by real native tests. Pins remain approximate solver goals rather than hard positional locks.

The default 100-frame soft control retains about 0.0132 m of maximum per-vertex motion between its final output frames. This is a decaying bounded impact response; physical rest requires a separately declared velocity and duration tolerance. The stiffness and lower-damping controls remain in motion at frame 99.
