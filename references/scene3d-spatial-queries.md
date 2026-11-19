# Canonical spatial queries for actual geometry consumers

The canonical Scene62 query implementation came from the archived Director38
integration. The four module and test blobs restored at `d47211823` matched
that archive. Subsequent review corrections changed all four files.
`scene3d-spatial-query-provenance.json` records the lineage and provides the
archived and current Git blob IDs and SHA-256 digests for each path. Both
identities describe Git file content, including its line endings.

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
only broad phase. Every triangle pair surviving float separating-axis rejection is
confirmed on the exact integer lattice of the input floats. This ports the source
and regression from contributor commit `f2dd69f6e58317015bbebb202b08473eedf737c7`
(Claude coordinator / Claude Opus 5.5), without replacing the prior box and vertex
repairs or their historical evidence. To bound cumulative work across repeated calls, create one
`scene3d_spatial._QueryBudget()` and pass it as `_budget` to each call. This existing
owner-supported integration interface accounts for possible triangle pairs and
containment work and rejects at two million operations. It never silently switches
to a bounds-only result when topology or budgets are unsupported.

Positive `clearance` belongs only to conservative AABB `overlap` queries. Exact
mesh overlap does not promise positive triangle clearance. Sphere sweeps reject
nested, cavity or interior-overlapping closed components.

`nearest` projects onto the triangle face and checks its edges, including on
thin triangles. Sphere sweeps validate contact in the triangle's coordinate
frame before returning world-space points. The sweep accepts endpoints and
geometry coordinates with absolute values up to 10¹² metres and positive radii
up to 10¹² metres. Face-plane, edge-cylinder and vertex-sphere sweep coefficients
are formed from the actual endpoint and transformed triangle floats using rational
arithmetic, before subtracting or projecting away small gaps. Discriminant and
initial residual signs are exact; irrational roots use a bounded 128-bit square
root and stable root-product recovery. Candidate times are checked against the
segment interval before any float conversion, including subnormal motion whose
unbounded contact time would exceed the float range. Internal rational times retain
ordering when public float fractions coincide. Returned points, normals and
fractions remain floating approximations; this does not promise symbolic contacts.
Initial sphere membership always compares the canonical exact nearest squared
distance against the squared radius, using the original world-coordinate floats.
A radius-only ULP band cannot bound the error introduced by a distant triangle
anchor or an approximate nearest-point projection. Zero-time contacts therefore
reuse the exact nearest point and displacement before converting output fields to
floats. Contact directions are scaled before normalization, so a subnormal
displacement does not lose its unit normal to a rounded length. Closed-mesh initial
candidates also sort and prune by exact squared distance. This adds exact nearest-point work for every initially tested triangle;
no mesh-scale performance guarantee is made.
The same triangle/component query budgets and geometry owners remain authoritative.
Grid and scale snapping raise a spatial error if the snapped
result exceeds the finite number range.

Run the actual query regressions with the optional rig test environment:

```
PYTHONPATH=scripts:tests python -B -m unittest test_rig_scene3d_spatial_queries test_rig_scene3d_sweep test_rig_scene3d_evaluate -v
```

The spatial query and sphere sweep suites are registered in the rig schema/math gate. Ordinary 2D
compatibility excludes their `test_rig_` names and needs no NumPy installation.
The final consuming candidate still requires a new independent whole-head review,
current hosted checks and actual native geometry validation. Historical source
approval is supporting evidence, not approval of a new integration head.

The additive oblique face/edge regressions compare first entry with a separate
100-digit decimal root oracle over exact binary-input coefficients. They include
the review's unit-scale and large-face reproductions, finite-edge tangency with an
adjacent-radius miss, and subnormal-motion misses. Existing corner, oriented-box,
long-sweep, geometry-domain and budget assertions remain in the suites. This is
synthetic geometric regression coverage, not full #62 or #12 acceptance, renderer
validation, or a production collision-freedom claim.

The initial-contact regressions independently establish both signs of stationary
clearance through real synthetic OBJ imports: a separated sphere must miss and an
overlapping sphere must return fraction zero. Closed tetrahedron variants exercise
the corresponding initial-distance ordering and pruning. They retain the same
engineering-fixture and whole-head review limits described above.
