# Scene3D generated environments

`scene3d_environment.plan(recipe)` builds deterministic, bounded real triangle
meshes before publishing anything. `create_environment(destination, recipe)`
creates a first project, and `apply_recipe(source, destination, scene_path,
recipe, policy='preserve')` publishes an immutable validated snapshot using the
existing Scene3D snapshot service. Destinations must be new directories.

A recipe contains exactly `id`, `version` (currently integer 1), `seed`
(unsigned 32-bit integer), `generator`, `parameters`, and `budget` (maximum
emitted nodes). The same recipe produces the same geometry and node IDs.
`primitive` accepts `kind` and `dimensions`; the available kinds are box, plane,
cylinder, cone, sphere, capsule, torus, stairs, ramp, pipe, beam, platform, door,
window, polygon (simple convex or concave XZ polygon), extrude (simple XZ polygon), and curve (capped straight spans).
`apply_recipes(source, destination, scene_path, recipes, offsets=None, parents=None)`
prepares up to sixteen distinct named recipes and publishes one snapshot. A
failure anywhere publishes nothing. Optional maps select recipe IDs: offsets are
local three-vectors and parents are exact node IDs (including earlier batch
outputs). Placements are recorded beside each persisted recipe. One batch
increments the scene revision once. `create_environments(destination, recipes,
offsets=None, parents=None)` constructs a first project through this batch API.

Meshes use meters, right-handed Y-up coordinates and counterclockwise exterior
triangles. OBJ resources contain actual triangles, explicit flat normals and
per-triangle UVs; they are not textured material demonstrations.

`rooms`, `workshop`, `fantasy`, and `science-fiction` accept room count,
width/depth/height, wall thickness, door width/height and a windows boolean.
Adjacent rooms share an actual partition with an opening. Window and doorway
voids contain no wall triangles. Fantasy adds editable tower meshes;
science-fiction adds editable structural beams. These basic variations do not
claim the complete themed asset/material catalogs requested by issue #61.

`terrain` accepts width/depth, resolution, elevation, optional exact heights,
maximum slope in degrees, smoothing iterations, paths (points/width/elevation),
and bounded seeded tree scatter. Heights use the actual emitted triangular
surface for queries. A supplied slope limit is clamped against both triangles
of every cell, and checked again before publication after path/height edits.
Tree placement checks complete crown footprint clearance at the boundary and
exclusions and uses a grid index for minimum-distance tests. Spacing defaults
to 1.3 meters and cannot be less than the crown diameter. Impossible counts
fail before publication. Heightfields cannot represent caves or overhangs.

Generated assets and recipes have exact file hashes. Generated nodes retain
fingerprints and stable IDs. Regeneration preserves changed nodes, manual
children where valid, and deleted-node tombstones, and returns explicit
conflicts. `policy='reject'` refuses any changed/deleted generated node.
Other generators' recipe records and authored nodes are retained. A changed
parenting dependency that makes regeneration invalid fails atomically under
the existing graph/snapshot validation.

`load_meshes(root, scene_path)` returns verified generated geometry keyed by
node ID as `{vertices, triangles}` in local coordinates. It accepts only the
bounded triangular OBJ grammar owned here. Imported GLB/rig geometry continues
to use the import/evaluation adapter.

Run focused regression coverage with:

```bash
PYTHONPATH=scripts:tests python -m unittest test_rig_scene3d_environment test_rig_scene3d_math -v
```

`demonstrate_scene3d_environment.py DESTINATION --blender /path/to/blender`
constructs a workshop, exports its real geometry to GLB, independently reimports
that GLB, and renders an inspection PNG. The inspection image explicitly hides
ceilings; the exported GLB retains them. In shared coordinated runs this command
must hold the native validation flock for its entire execution. Saved receipts
report actual runtime versions and exact hashes.

Remaining issue #61 acceptance: full architectural/themed catalogs, material
assignment through the lookdev owner, masks/blending/LOD policies, polygon holes and richer layout policies still need implementation
and review. Neither these APIs nor the example claim whole-issue completion.
