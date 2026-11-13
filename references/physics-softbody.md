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
is divided by the vertex count to set native vertex mass. Normalized damping
maps to Blender's edge damping range 0–50. Air friction is explicitly zero.
Pins use a source-bound goal vertex group; numerical pin residuals must be
inspected. Native fixed minimum/maximum steps per output interval are
`iterations * substeps`, with adaptive step selection disabled. Requests whose
rational fixed interval disagrees with these configured limits are rejected.
Native substep state is not exported or asserted independently.

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
no-replace promotion, and every cache inventories all required native bytes.

A static verification report establishes exact-byte/source consistency and
checks recorded native observations. It is not a signed provenance service or
a fresh solver execution. `probe` without `--smoke` reports discovery separately
from actual solver readiness. Unsupported fields, missing bytes, changed source
hashes, malformed topology and exceeded resource limits fail before publication.
