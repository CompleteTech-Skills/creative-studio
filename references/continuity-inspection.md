# Native asset inspection

Issue #114 adds inspection and compatibility orchestration over the existing
`rig_import`, `rig_gltf`, `rig_models`, `rig_geometry`, skeleton, skinning,
attachment and retargeting owners. It introduces no parser, renderer, acquisition
service, identity registry or review authority. Existing production-v1 documents
and fixed pet contracts remain unchanged.

## Exact inputs and operation

```python
from continuity_inspection import inspect_asset, compare_assets, classify_rigs

receipt = inspect_asset(
    "project", source_reference, dependencies=exact_dependency_references,
    landmarks={"top": {"mesh_id": "mesh-0-0", "vertex_index": 7},
               "bottom": {"mesh_id": "mesh-0-0", "vertex_index": 1}},
    distances={"authored_height": ["bottom", "top"]})
```

Every source/dependency is the existing continuity exact
`{id, version, path, sha256, kind}` reference. The caller supplies the complete
file closure, including external buffers/textures and native rig files. Missing
files are errors, rather than permissions to search elsewhere. Native `rig.json`
uses `kind: hatch-3d-rig` and its actual ID/version. GLB/glTF uses a stable caller
asset ID/version with a native kind such as `mesh`; that label grants no
approval. Inputs are never rewritten or promoted.

Run the same operation with a JSON request containing `source`, optional
`dependencies`, `landmarks`, `distances`, restrictive `limits`, and `variants`:

```sh
python scripts/continuity_inspection.py --root project --request inspect-request.json > inspection-receipt.json
```

Unknown request fields reject. Successful JSON contains an `inspection` and its
canonical SHA-256. Native failures retain their structured diagnostic in CLI
output. An inspection receipt binds source closure, software source hashes,
coordinate conventions, measured data and unavailable fields. Reopen/reinspect
the exact closure to reproduce observations; a receipt is not an input cache.

## Measurements and comparisons

The canonical parser supplies actual scene-node parentage and local/world
transforms for supported glTF scenes. Native rig inspection supplies its actual
skeleton hierarchy/rest transforms, controllers, facial controls and sockets.
Original imported scene hierarchy is explicitly unavailable from a native rig
alone; inspect its retained original GLB/glTF separately when that is required.

Geometry analysis observes real bounds, dimensions, vertex/triangle/component
counts, surface area, degeneracy, manifold boundaries and normal/UV/tangent
presence. Native owners validate finite transforms, skin joint references,
normalized weights, inverse binds, constraint dependencies, IK chains and socket
bone bindings. Receipts retain portable array fingerprints and shapes, material
slots and complete portable shader inputs, exact encoded texture hashes,
samplers, UV bindings, morph arrays/defaults and measured source hierarchy.
Texture slots identify their portable glTF color-space semantics. Arbitrary
renderer-specific shader node graphs, displacement and subsurface behavior are
unavailable rather than inferred from labels.

Named landmarks reference actual mesh vertices. Distance measurements use those
observed coordinates and report meters and the ratio to measured overall
height. Naming a vertex “eye” does not establish anatomical correctness; the
receipt preserves that uncertainty. Overall bounds are geometry measurements,
independent of a camera. Variant references preserve exact evidence without
inferring that a state, costume or scale change has been approved.

`compare_assets(root, left_request, right_request)` executes both inspections.
It reports byte identity, triangle-surface identity, attribute/morph identity,
height-normalized geometry identity and material/texture/assignment identity
separately. `source_byte_identity` concerns only the main file;
`byte_identity` covers its pinned dependency closure as well, so an unchanged
glTF document cannot hide a changed external texture. The triangle fingerprint quantizes coordinates to 1e-9 meters and
sorts triangle vertices/triangles, ignoring winding and serialization; original
indices and attributes retain separate fingerprints. It remains topology
sensitive. Equal counts, names or fingerprints do not prove semantic identity.
Every comparison retains `semantic_equivalence: unverified`,
`needs_visual_review: true`, and `visual_approval: false`.

## Rig classification

```python
result = classify_rigs("project", source_request, target_request,
    required_bones=["hand"], required_sockets=["grip"],
    required_morphs=["blink"], profile=exact_profile_ref, review=exact_review_ref)
```

The classifications are `exact`, `retargetable`, `partial`, `incompatible`, and
`unverified`. They concern structural/pose transfer, never artistic approval.

- `exact` requires equal measured skeleton, skin/bind data, controllers,
  sockets, geometry and morph/attribute definitions, and native owner-validated
  approved status on both rigs. Equal unreviewed structures remain unverified.
- `retargetable` requires an exact native retarget profile, an existing retained
  approved continuity review covering the profile and both exact rig sources,
  real bone/rest/root-scale calibration, and native retarget pose/deformation
  execution. It preserves the decision's fixture classification and rejects a
  synthetic-test reviewer. Retained caller-supplied decisions are not signed
  attestations; actual review authority remains with the native review workflow.
- `partial` identifies differing structures with some supported correspondence
  but an absent approved mapping, or an explicitly incomplete validated map.
- Missing required bones/morphs/sockets, or incompatible mapped sockets, yield
  precise incompatible diagnostics. Missing actual skeleton data is unverified.

Retarget calibration does not prove that a future animation preserves contact,
root motion, facial intent or timing. Those claims require the native animation
and continuity validators. Tests label simulated retained approval decisions as
fixture gate tests; they are not actual visual reviews or production approvals.

## Capability and resource ownership

`NativeInspectionOwner(root, source, evidence_folder, dependencies=...)` is an
additive trusted host adapter to the existing `Admission` and `Policy` APIs.
Construction performs and retains an actual source-bound probe. Requests must
bind every source reference. It executes inspection again on admission and
writes a new immutable receipt into a caller-owned existing evidence directory.
Native rig locks remain the existing rig owner's locks. GLB/glTF processing uses
a private read-only source snapshot, not a second job lifecycle.

Before parsing, the existing continuity descriptor reader rejects symlinks,
nonregular files, path aliases, changed handles, invalid hashes and oversized
closures. It copies only pinned bytes into a private directory; native parsers
cannot follow original dependencies outside that directory. Original source
hashes are rechecked even when parsing fails. Caller limits only narrow native
caps. Current inspector caps are 256 files, 8 MiB/file, 32 MiB total encoded data,
100,000 vertices/triangles, 256 bones, 32 morph targets and 256 MiB conservative
data admission. Aggregate instantiated glTF accessor/geometry accounting runs
before native import; canonical native loader limits remain authoritative.

Capability reservations additionally check native whole-process lifetime peak,
reserve 32 MiB inspection workspace, narrow native loading limits to remaining
memory, and account snapshot plus emitted receipt disk bytes. These are admission
and result checks, not OS allocation preemption or a filesystem sandbox against
hostile ancestor renames. Untrusted executable Blender content never runs.
Blender-native inspection requires a separately validated data-only export or a
trusted native adapter; this API does not invent one. No network provider, GPU,
encoder, renderer or review service is implicitly invoked.

## Acceptance traceability

`tests/test_rig_continuity_inspection.py` uses actual persisted procedural GLB,
glTF and native rigs. It verifies hierarchy/transforms, UV/normal/tangent data,
material slots and exact textures, morphs, actual skin/bind data, vertex landmark
distances, format-independent shape comparison, changed geometry/textures with
unchanged descriptive labels, required socket/bone rejection, retained mapping
decision binding, native retarget deformation, corrupt GLB and invalid skin
references, missing external textures, unsafe paths/symlinks, restrictive resource
limits, CLI receipts and actual capability admission. All fixture decisions and
geometry remain explicitly nonproduction. Final execution evidence and any
remaining acceptance gaps are recorded in the coordinator's revision-bound
issue114 receipt; source code or a schema alone does not establish acceptance.
