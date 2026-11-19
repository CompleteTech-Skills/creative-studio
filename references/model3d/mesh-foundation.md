# Mesh foundation

This foundation uses `rig_models.Mesh` / `Geometry` and the existing native importer. Canonical coordinates remain right-handed, +Y up, +Z forward, meters. Source acquisition, registry publication and Blender normalization remain owned by the asset-library pipeline; a decoded triangle mesh is not a substitute for its original hierarchy, skins or animation.

## Explicit coordinate conversion

`model3d_coordinates.conversion_matrix(source_basis=..., target_basis=..., unit_scale=..., pivot=..., translation=...)` constructs an affine transform with `target = basis * (source - pivot) * unit_scale + translation`. Pivot is expressed in source coordinates and translation in target coordinates. Supported basis labels are `canonical`, `right-handed-z-up` and `left-handed-y-up`. Unit scale is explicit, finite and within `[1e-6, 1e6]`; no inferred physical scale, grounding or orientation is silently applied.

`transform_geometry(geometry, matrix, limits=None)` returns a copy. Semantic IDs, names, regions, materials and UVs remain attached. Normals use inverse-transpose transforms, tangents use the forward linear transform, direction morphs share their base normalization, and reflections reverse triangle winding and tangent handedness. Singular, nonfinite and numerically ill-conditioned transforms fail. This API operates on unskinned `Geometry`; it does not claim to convert skeletons, animation channels, constraints or source object hierarchies. Use graph-aware canonical acquisition for those inputs.

`placement` measures bounds and dimensional constraints, uses an overridable support-plane pivot for characters, center for props, authored origin for architecture, and requires a joint/hinge/attachment anchor for components. Grounding is explicit. `transform_frames` converts generic named root/object/attachment affine anchors and preserves names. It is not a typed camera or lens implementation. Actual camera records, optical-local -Z (distinct from actor/object +Z), rigid-pose/unit validation and lens semantics belong to the authoritative `camera3d_math/contracts` owner (#75). For unit rigid world poses the world-only C@M conversion agrees with its `canonical_camera_transform`; arbitrary affine scale is not a valid typed camera pose. An in-process cross-owner known-point/ray oracle verified center/right projections and optical direction against the accepted Camera75 helper; no renderer or Blender result is implied. Convert either the common root or individual world frames once, never both. Camera affine scale remains explicit rather than being mislabeled a rigid rotation.

The coordinate regression suite covers independent expected pivot coordinates, basis/unit inverse roundtrips, reflection winding, morph direction evaluation and invalid boundaries. Automated geometry checks do not establish visual or human production approval.

## Delivery scope

This change implements the #44 mesh, import and coordinate foundation. Independent review, hosted checks and merged-main verification are separate gates. General reconstruction, provider generation, topology optimization, robust self-intersection solving, and full Blender authoring belong to the remaining #45–#50 work; this foundation does not claim parent #10 completion.

## Local workflows

Use the primary `python scripts/model3d.py mesh ...` dispatcher or the equivalent `python scripts/model3d_mesh.py ...` entry point. Run `python scripts/model3d.py mesh capabilities` for the implemented and unavailable operations. `primitive --kind box --parameters '{"width":2}' --destination output/new-box --format glb` creates genuine indexed geometry in a new directory. Formats are `glb`, `gltf`, and the strict diffuse `obj` profile. `import --file model.glb --destination output/new-import` captures immutable native source evidence before exporting an unrigged standalone geometry copy. The CLI rejects skins/animation rather than exporting them through a geometry-only interface.

The primitive library provides box, plane, cylinder, cone, sphere and torus with bounded subdivisions. `EditableMesh` retains immutable source polygon loops and positions independently of render triangulation. Flat shading may split render vertices; report topology using retained source loops when assessing authored connectivity. `transform_editable` converts both representations together. Exports can include hash-bound `editable.json`; the original source polygon loops are not claimed to survive inside GLB itself.

`model3d_topology` exposes measured reports, face components, explicit cleanup, winding orientation, flat/smooth shading, linear subdivision, and explicit convex supporting-plane hole caps. Cleanup is opt-in and does not merge attribute seams or remove disconnected semantic parts. General hole filling/remeshing and robust self-intersection solving are unavailable. Operations return copies; cases whose attributes/morphs cannot be preserved fail explicitly.

`model3d_mesh_import.import_mesh` returns a context-managed `MeshImport` owning its private source snapshot. Geometry, weights, skeleton, morphs and clips use the existing native importer. Keep the context alive while using its source paths. The import CLI preserves exact originals under `source/` and writes `source.json` alongside the derived mesh. The export hash inventory covers both.

`import_canonical(library, ref)` reads exact versions through `AssetLibrary.resolve/read_file` and retains the original provenance. It supports #138 runtime geometry and the native #139 GLB/glTF producer's `normalized.glb` payload. Automatic selection of that payload requires native producer provenance. For #139, the adapter checks the inventory and conversion report, reproduces the producer's packing at the reported source scale, and checks the registered GLB with the pinned offline Khronos validator. The returned mesh uses the units already baked into the GLB. A manually registered GLB with the same filename can still be selected explicitly at unit scale. The app-authored packed Blender triangle fixture is supported through its retained two-stage conversion chain: the adapter binds the original `.blend`, intermediate GLB and normalized GLB, validates both GLBs, and decodes only the normalized geometry without starting Blender. This fixture path does not implement general Blender import. Auxiliary Blender tool records on ordinary registered geometry retain their existing entry-selection behavior.

The app-authored Blender fixture reader also checks the decoded world-space mesh against the producer's three meter coordinates and indexed triangle winding. Materials, image/texture/sampler resources, skin, animation and morph channels are outside that fixed static profile and are refused even when their base triangle matches. Both successful worker stages, the package worker digest, exact safe command policy, and consistent resource/time budgets must be retained. Claimed budgets obey the producer's 300-second and 2-GiB maxima and its file cap of at most one sixty-sixth of resident memory. These checks establish consistency of the claimed conversion; they do not cryptographically authenticate its author or reopen an arbitrary Blender file. Independent forged-envelope regressions use real native packing and offline Khronos validation; their explicitly synthetic process receipts are separate from the authentic Blender producer/reopen test. A same-byte evidence alias alone does not turn a legacy runtime asset into a native conversion claim; actual native claims retain their checks through aliases.

Every export reimports the encoded artifact before publication. Nondegenerate faces must remain nondegenerate with the same orientation; face corner coordinate error must remain within 1e-3 of the original face edge span, and mesh/vertex/triangle partitioning must be preserved. Unrepresentable float32 GLB/glTF output fails before publication.

OBJ preserves semantic object/material names, UV0, normals, explicit diffuse color/opacity and raw texture bytes. Unsupported PBR, morphs, tangent/color/secondary-UV/sampler losses are rejected. Referenced materials require explicit metallic=0/roughness=.65; implicit glTF PBR defaults are not reinterpreted. Unreferenced material slots are omitted with an explicit mapping in the export manifest. Native IDs/regions/source-node indices are recorded in the manifest because OBJ cannot carry their native semantics. Geometry-only GLB/glTF exports do not imply preservation of source scene hierarchy or rig channels.

## Requirement-to-test traceability

| Foundation requirement | Evidence suite |
|---|---|
| Six real primitives, indexed triangulation, original polygon loops, flat/smooth normals, analytic volumes/winding and allocation boundaries | test_model3d_primitives.py |
| Duplicate/degenerate/isolation/edge/component measurements, explicit seam-safe cleanup/orientation/subdivision/hole cap | test_model3d_topology.py |
| Basis/unit/pivot roundtrips, reflection, normal/tangent morph math and converted real three-format outputs | test_model3d_coordinates.py |
| Genuine OBJ/MTL/GLB/glTF output, material/UV/normal/source-byte preservation, unsupported loss rejection and destination races | test_model3d_interchange.py |
| Native immutable snapshots, invalid inputs/resources and retained real rig/skin/morph/clip identity | test_model3d_mesh_import.py |
| Operational commands, editable-source hash manifest, actual import/export and structured negative cases | test_model3d_mesh_cli.py |
| Independent in-process pygltflib accessor decoding of six transformed primitives in both GLB/glTF, counts/positions/indices/normals/materials | test_model3d_independent.py |

These are engineering fixtures and numerical checks, not aesthetic approval. Optional backend execution, current shared tangent/material/codec/matrix/resource-limit integration, current-head CI, and fresh independent review remain separate release gates.

## Strict asset document bridge

`model3d_mesh_asset.build_asset(root, geometry_record, asset_id=..., version=..., category=..., sources=..., provenance=..., files=...)` builds and payload-validates the existing Model3DAsset contract from the actual encoded native model. Source, acquisition and license statements must be supplied explicitly; the builder creates no approval or source evidence. Material texture bytes need separately hash-bound file records. Optional canonical evidence retains the exact immutable canonical reference and original namespace-bearing registry manifest, with its runtime geometry inventory hash binding checked.

Parts describe the native world-space mesh view with explicit identity transforms/zero origins. The original GLB remains the authority for authored hierarchy, pivots, skins and animation. The builder accepts canonical evidence for #138 runtime geometry and the native #139 normalized GLB. For #139, it validates the supplied manifest and binds the conversion's payload record to the supplied geometry bytes. A registered fixture keeps its classification in the built asset and in source records whose bytes match the retained originals or normalized payload. Use `import_canonical` to verify the exact registry version and conversion before passing that evidence to the builder. The Blender-produced fixture remains outside this integration. `test_model3d_mesh_asset.py` covers real primitive, retained-rig, textured and registered normalized model documents, plus rejected evidence and budget cases.

For native glTF acquisition evidence, caller-supplied source names must agree with the retained original filename. The logical inventory path remains separate from that name. If identical bytes identify several originals with different names, an exact retained logical path disambiguates the source; otherwise contradictory provenance is refused.

When native normalized geometry also has a legacy runtime inventory entry, the builder selects the evidence bound to the supplied geometry. A same-byte runtime alias cannot suppress native source-name or fixture checks. A different runtime payload cannot replace the verified normalized entry. The legacy runtime-only bridge remains supported.

## Reproduce actual geometry evidence

Run `python scripts/demonstrate_model3d_mesh.py --destination output/new-mesh-demo`
with the optional rig test dependencies installed. The destination must be new.
The command builds six named editable shapes, applies explicit source-basis/unit/
pivot conversion, writes and reimports all eighteen GLB/glTF/OBJ exports, then
imports one GLB through the immutable source pipeline. Its `acceptance.json`
records actual topology and every generated payload hash. This is an engineering
fixture demonstration; it supplies no artistic approval or provider claim.

Source snapshots canonicalize the explicitly selected root once so system path
aliases work on macOS and Windows; descendant symlinks remain rejected. Native
glTF source hierarchies are capped at depth 256 as well as 10,000 total nodes,
returning a structured resource diagnostic before Python recursion limits.
