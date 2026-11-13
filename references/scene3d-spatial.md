# Scene3D spatial queries and placement

`scene3d_spatial.SpatialIndex(graph, meshes)` evaluates exact SceneGraph base-pose
matrices in canonical Y-up world metres. `meshes` maps persistent node IDs to
`{vertices: Nx3, triangles: Mx3}` in node-local coordinates. Inputs are copied and
bounded to one million total vertices and triangles. `scene3d_environment.load_meshes`
loads the generated OBJ geometry only after validating the complete saved project
and exact content hashes. It rejects unsupported geometry owners.

The index refreshes world geometry when the graph revision changes and samples
editor/render visibility for every query. It never treats an authored bounds box
as a triangle mesh. Its API provides:

- `raycast(origin, direction, max_distance=..., exclude=..., visibility=..., first=True)`:
  exact double-sided triangle hits, ordered by distance, node ID and triangle index,
  with world point, winding normal, barycentrics and graph revision. Direction is
  normalized, so distance remains metres.
- `nearest(point, ...)`: exact triangle nearest point, including degenerate edges.
- `ground(point, min_up_dot=.5, ...)`: downward hits restricted to upward authored
  winding and the selected slope limit. It does not turn ceilings into support.
- `bounds(node)`, `obb(node)`: own geometry's world AABB and transformed local box
  (center and three half axes); shear is retained.
- `overlap(bounds, clearance=...)`, `frustum(six_inward_world_planes, ...)`:
  conservative AABB candidates. `oriented_overlap(node, ...)` uses separating axes
  on transformed local boxes. Neither operation claims exact concave-mesh collision.

`scene3d_constraints` authors changes through the existing graph transaction and
revision checks. `snap_transform` requires explicit local/world space and supports
translation grids, positive scale grids, and rotation about a declared axis.
`attach`/`detach` target existing named socket nodes, retaining exact world pose or
rejecting unrepresentable shear. `set_constraint` reuses graph look-at,
copy-transform and socket validation, cycle detection and evaluation.
`sample_path` uses bounded arc-length linear interpolation; `follow_path` authors
one explicit world sample. Animation persistence/time evaluation remains owned by
the animation subsystem. `snap_surface` uses actual upward triangles and projects
the previous heading onto the tangent plane. `snap_bounds` aligns declared AABB
faces with an explicit axis, side and nonnegative gap.

`place` checks at most 1,000 candidate world positions against conservative AABBs
and moves an entire selected subtree only after finding a valid position. Driven
descendants must first have their constraints/attachments resolved, because a
rigid translation cannot predict externally driven child motion. Failure
leaves its authored document and revision unchanged. Touching bounds count as
collision; use an explicit clearance when separation is required.

Validation:

```sh
PYTHONPATH=scripts:tests python -m unittest test_rig_scene3d_spatial test_rig_scene3d_environment test_rig_scene3d_math -v
python scripts/demonstrate_scene3d_spatial.py --destination /tmp/scene3d-spatial-rooms
```

The demonstration saves and independently reopens two connected rooms, loads exact
hashed OBJ triangles, verifies that a ray traverses the doorway while another hits
the lintel, and finds the floor by a downward ray. Its adjacent JSON receipt retains
exact geometry/scene hashes and hit coordinates. It establishes persisted geometry
queries, not Blender rendering or animated/skinned deformation. Those workflows
must supply evaluated mesh vertices from their owning subsystem; bounds-only
assets are intentionally omitted from exact triangle queries. Arbitrary rotation
snapping, concave solid intersection and automatic rig-bone-name conversion are
not implemented. Authored socket nodes can represent named rig anchors explicitly.
