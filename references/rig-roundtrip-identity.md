# Native round-trip surface identity

`rig_interop.compare_imported(source_rig, imported_model)` is a bounded diagnostic
through the native decoder. `independent` remains false. It does not replace
Khronos validation, independent Blender/Three.js evaluation or visual identity
review.

Successful comparison now covers the exact named mesh-part inventory and region,
oriented triangle multiplicity, vertex corner attributes, named morph delta
fields and defaults, loose vertices, and the material actually assigned to each
part. A static/rest deformation sample always runs, including rigs with no clips.
Named clips are compared at the requested fractions (default .17, .53 and .87).
Missing/extra clips and structural/material mismatches raise explicit `RigError`
diagnostics instead of returning an unqualified successful identity result.

Vertex indices, vertex splits along referenced face corners, triangle ordering,
cyclic triangle rotations and resource-table ordering are storage choices.
They may change while the same oriented surface and bindings remain. Reversed
winding, a changed diagonal, changed corner UVs/normals/colors/morph deltas,
changed loose geometry, and changed assigned materials are not accepted as
reindexing. Geometrically degenerate faces have no measurable winding: their
multiplicity is retained and their count is explicit in the receipt.

| Dimension | Comparison rule |
| --- | --- |
| Rest positions | Absolute component tolerance 0.00002 meters |
| Corner attributes and morph deltas | Absolute component tolerance 0.00002, in each native field's units |
| Default morph weights and material numeric values | Absolute tolerance 0.000001 |
| Sampled deformed positions | Maximum matched vertex Euclidean distance 0.01 meters |
| Mesh IDs, regions, material names and other metadata | Exact native values |
| Triangle multiplicity and orientation | One-to-one capacity matching; cyclic rotations only; opposite nondegenerate face normals reject |
| Bound texture appearance | Identical dimensions and exact decoded RGBA pixels |

Core glTF material defaults are expanded before comparison, including PBR
factors, alpha policy, texture coordinate set, normal scale and occlusion strength.
Texture, image and sampler indices are resolved through their actual resources.
A material slot reorder is accepted only when each mesh still resolves to the
same material. Encoded PNG/JPEG bytes may change when dimensions and decoded RGBA
pixels remain identical; both original encoded hashes and decoded pixel hashes
remain in the receipt. Lossy recompression, resizing, changed UVs, changed sampling
parameters, and unexplained extension/metadata changes are not silently tolerated.
The image comparison uses glTF's decoded channel interpretation; it does not
supply external color-management or artistic approval.

The expected native interchange profile is GLB/glTF with compatible rig parts and
animations. A conversion that renames parts, drops rig/morph data or changes a
material outside these declared rules needs an explicit adapter and separate
review. OBJ is not a skeletal animation container and is not certified by this
function. Arbitrary Blender authoring constraints are not claimed to survive glTF.

The shared `rig_surface_compare.match_rows` uses neighboring spatial cells only
to locate candidates. It compares actual float records, never rounded-bin equality,
and uses capacity/augmenting paths to preserve duplicate multiplicity. There is a
numeric exact-match path and no dense all-pairs matrix. `memory_bytes` defaults to
the rig's native memory budget (2 GiB when unspecified), and `maximum_work` defaults
to 5,000,000 charged candidate/graph operations across the whole diagnostic.
Pre-allocation record estimates, unique-record overhead, graph edges and decoded
textures have explicit guards. Exceeding a guard raises
`RIG_ROUNDTRIP_COMPARE_LIMIT`; it does not certify a partial comparison. An asset
can fit the import ceiling (1M vertices/2M triangles) while exceeding this stricter
comparison working set or ambiguity budget. Increase an explicit comparison budget
only when the host can support it, or report the affected identity gate unverified.

The receipt names its verified dimensions and tolerances. Unobserved animation
times/events, complete authoring topology, bone metadata and subjective anatomical
or artistic identity remain outside this diagnostic. Surface matching does not
establish those dimensions by implication.

Regression commands (optional independent validators enabled explicitly):

```bash
PYTHONPATH=scripts:tests HATCH_RIG_INTEROP_TEST=1 \
  python -m unittest test_rig_surface_compare test_rig_roundtrip_identity -v
python scripts/test_rig_gate.py interoperability
```
