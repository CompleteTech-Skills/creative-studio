# Native semantic parts and source-bound appearance

`model3d_semantic` is an additive editing layer over `rig_models.Geometry`,
`Mesh`, native affine transforms and native geometry storage. Coordinates remain
right handed, +Y up, +Z forward and meters. Part matrices are local affine 4×4
matrices. Every mesh has exactly one stable part owner. Initial geometry imports
are explicit world-space mesh views with unknown semantics; the original source
closure is retained. Bounds and a painted 2D mask do not infer anatomy or a 3D
hierarchy. Authored labels remain pending review.

The API returns copies. `group_parts`, `transform_part`, `reparent_part`, `set_visibility`,
`label_part`, `duplicate_part`, `replace_part`, `split_part`, `combine_parts` and
`change_material` preserve the supplied source object and create another integer
revision. Reparenting preserves world geometry by default. Visibility includes
ancestors. Duplicate subtrees get new stable part/mesh IDs and remap internal
attachment targets. External attachment targets remain explicit. Splitting uses
an authored complete face partition; combining compatible siblings creates one
real mesh in the common parent frame. Vertex attributes, morph data and material
assignments are preserved or incompatible combinations fail. Combination retains
the original part records as ancestry rather than inventing a new semantic label.
Transform-only groups provide an explicit character root without inventing mesh
geometry. Grouping preserves selected world placements and rejects parent cycles.

Native meshes keep their own material/UV/image resources. Selected-array native
transforms avoid copying unrelated geometry for every part. Conservative memory,
vertex/triangle, part, metadata and cumulative hierarchy limits apply before
expensive copy steps. Matrices reject booleans, singular/nonfinite values and
unrepresentable inverses. Direct callers may edit native arrays, but a recorded
derivative cannot remain current after its bound inputs change.

`register_derivative` records exact part signatures, an artifact hash and optional
identity/style bindings. Edits invalidate derivatives whose declared dependencies
changed, preserving unrelated component work. A derivative hash does not grant
review approval. `define_pose` and `apply_pose` evaluate real rigid part transforms.
`map_states` chooses existing catalog definitions and existing poses or records
unavailable. Rigid poses are not skins, IK solvers, functioning animation clips,
or 416 completed 3D state animations. Persisted stale poses fail when applied;
state mappings whose represented parts disappear become unavailable.

`model3d_semantic_evidence.identity_contract` validates an original Hatch identity
document through its existing authority and retains its exact bytes. Explicit
3D rules measure required/forbidden part IDs, geometric extent ratios, linear
base color with vertex-color modulation, and the source prohibition on reflected
part transforms. Textured colors are unavailable to that simple color check;
they are not certified from a material factor. Strict rules reject violating
edits. Reports list executed invariants and explicitly leave unmapped source
markings, proportions, texture appearance, rendered silhouette correspondence,
anatomy and artistic approval unmeasured. No absolute perceptual identity score
or automatic complete identity approval is supplied.
Ratio axes are canonical world axes. Each rule records `scope='owned-meshes'`
by default; `scope='subtree'` is an explicit alternative. This separates torso
proportions from changing arm bounds during a pose.

`map_decomposition(asset, verified_run, {'native-part':'component-id'})` uses the
existing decomposition loader and its exact-pixel/hash validation. It captures
the original source, manifest, annotation, identity, component pixels and masks
unchanged, and retains the original pivots, attachments, uncertainty, orientation
and semantic review status. The explicit correspondence is evidence, not proof
that a 2D pivot has a depth coordinate or that a reconstructed hidden part exists.
Native 3D pivots/attachments still require authored geometry. `bind_style` freezes
the existing registry snapshot plus explicit geometry/material/texture/render
adaptations. Selecting a style does not generate geometry or alter it silently.

The optional `from_assembly_ir` adapter calls the existing #45 generator and
imports its actual local meshes, materials, hierarchy, transforms and anchors.
It records canonical validated IR as derived evidence, never as an untouched
original file. The assembly owner must be installed; absence fails explicitly.

`model3d_semantic_surfaces` versions the native surface APIs described in
[surfaces](surfaces.md). `generate_asset_uv` makes real native UVs, `texture_asset`
binds validated actual image resources, and `project_asset` projects calibrated
visible image colors on evaluated world geometry while preserving native local
positions and part hierarchy. Reports bind source/result geometry and exact
image bytes; unseen regions and depth ambiguity remain explicit. These operations
do not create learned textures or certify subjective appearance.

`save_asset` saves a new immutable directory with `semantic.json`, native
content-addressed geometry and exact evidence blobs. `load_asset` admits the
versioned metadata, captures the complete declared closure with bounded no-follow
reads pinned to the selected root, verifies hashes, and parses only the private
snapshot. It rejects malformed nested structures, conflicting paths, unknown
versions, altered blobs and stale dependencies. Saved earlier revisions are never
overwritten. The native file representation remains authoritative; this is not a
new mesh serialization engine or a machine power-loss durability guarantee.
Native bundle directory admission rejects authoritative ZIP64 locators before
opening the ZIP directory, even behind a maximal comment or bounded classic
fields. Ordinary small ZIP64 member headers remain supported.

`export_asset(asset, NEW_DIR, format='glb'|'gltf')` writes actual hierarchical part
nodes and native mesh children, supported PBR/images/UVs/morphs, semantic extras
and an immutable `native/` companion. Hidden parts remain in the companion and
are explicitly omitted from the visible export. The exporter independently
reimports the file and checks mesh inventory, positions, indices, materials,
attributes, original image bytes and noncollapsed/nonreversed geometry before
exclusive publication. Float32-unrepresentable features fail. Original skins and
animation must stay in the native rig owner; mesh-only import refuses to discard
them. Known fixture provenance propagates through CLI replacement and exports.

The standalone CLI leaves the central dispatcher and 2D schemas unchanged:

```bash
python scripts/model3d_semantic_cli.py import --source input.glb --destination output/revision1
python scripts/model3d_semantic_cli.py inspect --source output/revision1
python scripts/model3d_semantic_cli.py edit --source output/revision1 --operation transform --spec move.json --destination output/revision2
python scripts/model3d_semantic_cli.py export --source output/revision2 --destination output/export --format glb
```

Edit specifications use the corresponding API keyword arguments. For example:
`{"part_id":"head","transform":[[1,0,0,0],[0,1,0,1.5],[0,0,1,0],[0,0,0,1]]}`.
Replacement explicitly chooses an existing target material slot while retaining
the original replacement source appearance in evidence. Unknown operations and
expected invalid inputs return JSON diagnostics and nonzero status.

These modules provide executable portions of #47; full acceptance also requires
the #45/#46 integration, advanced Blender vertex/UV/material edits, same-model
multi-angle renders and actual human identity review through the native #49
owner, final combined regressions, fresh whole-head review and normal merge.
No Blender/browser or artistic acceptance is inferred from the tests here.
