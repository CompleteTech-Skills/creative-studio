# GLB/glTF continuity inspection

This slice of issue #114 uses the canonical rig importer's glTF reader,
preflight, and model importer. It provides read-only engineering evidence for
GLB/glTF and uses the existing continuity reference format
`{id, version, path, sha256, kind}`. IDs and versions are caller-supplied
pointers. They do not grant visual approval or semantic identity.

## Exact inputs and operation

Supply the source and every native local buffer or image dependency as
explicit, project-relative pins. The inspector rejects missing pins, unused
pins, changed bytes, and duplicate or case-alias paths. Embedded data uses
the source pin. The inspector reads pins through the existing native payload
reader, including active registered directory capabilities. The model reader
uses a private copy of the pinned bytes. Original and variant pins are
rechecked against the admitted directory identity before a receipt returns.
Comparisons share that directory identity across both inputs and their final
closure checks.

Run an inspection with a JSON request containing `source` and optional
`dependencies`, `landmarks`, `distances`, `limits`, and `variants`.
For comparison, supply an object with `left` and `right` inspection requests.
Python callers can use `inspect_asset(root, source, ...)` and
`compare_assets(root, left, right)` from `scripts/continuity_inspection.py`.

```sh
python scripts/continuity_inspection.py --root project --request inspect-request.json
```

Successful inspections contain `inspection` and its canonical `sha256`.
Comparisons retain both inspection receipts. On rejection, the CLI returns a
nonzero exit and reports diagnostic codes and bounded resource counts when
available. Public failures omit authored names, paths, and native message text.

Named landmarks bind the receipt's `mesh_id` and a `vertex_index`. Distances bind two landmark
names. The receipt reports distance in meters and as a fraction of observed
height. Landmark names are authored. Optional variant references are separately
verified evidence pointers. They confer no approval.

## What the receipts measure

Receipts retain selected scene hierarchy and transforms, geometry measurements,
array fingerprints, imported skin joints and bind data, material inputs,
texture hashes, sampler and UV bindings, morphs, and source extension and
animation-channel inventories. Skin records carry the canonical importer's
method. `imported` identifies a source skin carried into the normalized model.
`rigid` identifies a binding synthesized by the importer for an unskinned
instance in a scene with a skeleton. The inverse bind matrices use the
normalized importer rest space. Software hashes identify selected on-disk
implementations, including the native JPEG verifier.

Public observations use stable node, primitive, and bone indices. They retain
typed core shader fields, validated extension texture bindings, sampler settings, and
texture byte hashes. Authored resource names and arbitrary metadata stay in
the pinned source with other extension parameters. Morph labels, bone roles and unsupported extension labels
are represented by fingerprints.

Texture slot color spaces describe portable glTF semantics. Their UV binding
uses the texture transform coordinate set when specified, otherwise the base
coordinate set or glTF default of zero. Optional extensions outside supported coverage yield partial
evidence. Recognized material extensions with semantics beyond the projected
fields also yield partial material coverage. The native reader rejects
unsupported required extensions.

The comparison reports main-file byte identity and full closure byte identity.
It measures triangle surfaces, attributes and morphs, height-normalized
surfaces, and materials and textures. `observed_material_identity` compares the
projected material fields, texture bytes, sampler settings, and the slot at
each source node and primitive index. A position-only change leaves those
assignments intact. `material_identity` uses that result within the supported
profile and is `null` if either input's material coverage is partial. The surface fingerprint uses coordinates
quantized to 1e-9 meters and is independent of winding and triangle order.
Separate index fingerprints preserve connectivity evidence, including
distinctions between shared indices and coincident vertices.
Comparisons report `semantic_equivalence: unverified`,
`needs_visual_review: true`, and `visual_approval: false`.

Native preflight accounts for instantiated geometry, accessor work, and
metadata. Image admission adds aggregate decoded texture capacity using
native image framing before the importer decodes pixels. Limits only tighten
the inspector's caps. These are conservative loader estimates, with no process
RSS or renderer guarantee. The canonical reader applies its declared buffer
limits. The inspection estimate does not separately meter cumulative work
from repeated buffer URI references.

## Local evidence and remaining acceptance

```sh
python -m unittest discover -s tests -p 'test_rig_continuity*.py' -v
python tests/continuity_inspection_probe.py new-evidence-directory
```

The probe writes portable source and receipt hashes for procedural fixtures
with actual skinning, positions, morphs, and PNG texture bytes. It includes
geometry and texture changes, partial material-extension comparison, exact
external closure acceptance and missing pin rejection, and aggregate image
capacity acceptance and rejection. The shared rig interoperability gate
registers the inspection suite. Hosted probe evidence and independent
Khronos Validator, Blender, and Three.js observations remain follow-up work. Native `rig.json`, owner capability registration, rig compatibility and
facial classification, renderer-specific outputs, and production artwork
review are outside this slice.

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
through the shared `rig_gltf.preflight_gltf` authority before native import. It
reuses native `LoadBudget`, restrictive effective limits and the canonical JSON
expansion factor. The caller supplies its measured captured-closure byte count;
the helper never reopens source paths or alters descriptor APIs. Conservative
memory accounting can narrow the encoded-byte caps further.
That shared helper covers geometry only. Before the importer decodes any image,
the inspector separately reuses native `image_dimensions` and `LoadBudget` to
combine admitted geometry/metadata with every image's four RGBA working copies.
The native PNG/JPEG decoder still validates complete containers and actual pixels;
an image header is capacity evidence, not integrity or visual approval.

Capability reservations additionally check native whole-process lifetime peak,
reserve 32 MiB inspection workspace, narrow native loading limits to remaining
memory, and account snapshot plus emitted receipt disk bytes. These are admission
and result checks, not OS allocation preemption or a filesystem sandbox against
hostile ancestor renames. Untrusted executable Blender content never runs.
Blender-native inspection requires a separately validated data-only export or a
trusted native adapter; this API does not invent one. No network provider, GPU,
encoder, renderer or review service is implicitly invoked.
