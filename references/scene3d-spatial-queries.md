# Canonical spatial queries for actual geometry consumers

This is a narrow transport of the existing Scene62 query authority, with original
commit, author and unchanged module blobs in `scene3d-spatial-query-provenance.json`.
It allows Director38 to use the canonical implementation without importing the
unreleased placement/environment/temporal aggregate. It does not close Scene62.

Construct `SpatialIndex(graph, meshes)` using actual node-local vertex arrays and
integer triangle topology under matching SceneGraph node IDs. The index applies
evaluated base-pose world transforms exactly once, retains geometry identity and
refreshes after graph revisions. Consumers of evaluated animation must supply a
matching evaluated graph and geometry; authored base-pose state alone cannot prove
animated collision freedom. Bounds-only members are omitted from triangle queries;
verify that every required collider has actual geometry before claiming coverage.

`mesh_overlap(node, solid=False)` detects real triangle intersections. Enabling
`solid=True` additionally requires oriented closed two-manifold edges and detects
containment for disconnected components. Touching counts as collision. Bounds are
only broad phase. To bound cumulative work across repeated calls, create one
`scene3d_spatial._QueryBudget()` and pass it as `_budget` to each call. This existing
owner-supported integration interface accounts for possible triangle pairs and
containment work and rejects at two million operations. It never silently switches
to a bounds-only result when topology or budgets are unsupported.

Positive `clearance` belongs only to conservative AABB `overlap` queries. Exact
mesh overlap does not promise positive triangle clearance. Sphere sweeps are
provided by the unchanged canonical companion module; nested/cavity/interior-
overlapping closed components reject explicitly in that separate operation.

Run the actual query regressions with the optional rig test environment:

```
PYTHONPATH=scripts:tests python -m unittest test_rig_scene3d_spatial_queries test_rig_scene3d_sweep -v
```

Both modules are explicitly registered in the rig schema/math gate. Ordinary 2D
compatibility excludes their `test_rig_` names and needs no NumPy installation.
The final consuming candidate still requires a new independent whole-head review,
current hosted checks and actual native geometry validation. Historical source
approval is supporting evidence, not approval of a new integration head.
