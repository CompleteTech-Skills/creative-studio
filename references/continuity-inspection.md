# GLB/glTF and saved rig continuity inspection

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

## Saved native rigs

Supply a `hatch-3d-rig` source reference to the saved `rig.json` with its
manifest ID and version. Dependencies are the files the native loader consumes
under that rig directory. Include `identity.json`, the original source and its
dependencies, the geometry and skin bundles, animation clips, validation
records, review receipts, and the evidence referenced by those receipts. Supply
each consumed path once. History and other project files outside that inventory
stay on disk. A rig restored through the native ZIP API uses the same inspection
request and pin rules.

The file pins use the inspector's closure limits during capture and the final
comparison recheck. The native loader also applies the manifest's effective
limits, including its byte cap on the original source asset.
The native preflight checks the consumed inventory and reserves capacity for
geometry analysis and receipt projection before the loader materializes arrays.
The receipt reports the combined estimate in `native_analysis_admission`. The
native loader accounts for image framing and clip limits in its load estimate.
This path reports the saved rest hierarchy, inverse binds, weights, and geometry
of the pinned rig. The source directory and its parent require read access.
The inspector creates its copy in temporary storage and rechecks source pins
before returning. Production artwork approval requires a separate review.

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
each source node and primitive index for GLB/glTF, or at each saved mesh
index for a native rig. A position-only change leaves those assignments
intact. Both material identity fields are `null` when comparing a saved rig
with a GLB/glTF file. Within a format, `material_identity` is `null` if either
input's material coverage is partial. The surface fingerprint uses coordinates
quantized to 1e-9 meters and is independent of winding and triangle order.
Separate index fingerprints preserve connectivity evidence, including
distinctions between shared indices and coincident vertices.
Comparisons report `semantic_equivalence: unverified`,
`needs_visual_review: true`, and `visual_approval: false`.

Native preflight accounts for instantiated geometry, accessor work, and
metadata. Image admission adds aggregate decoded texture capacity using
native image framing before the importer decodes pixels. Animation admission then
adds retained track/container costs and decoded working arrays across all clips,
including a separate times/value/tangent copy for every morph primitive destination.
That subtotal remains in `native_animation_admission`; source keyframe limits
keep their channel-counted meaning while memory admission accounts for instantiated
copies. A final `native_analysis_admission` adds native geometry analysis working
arrays to that complete subtotal through the same `LoadBudget`, before entering
the canonical importer. This covers the importer's analysis and the inspector's
subsequent analysis; these execute sequentially and reuse the reserved capacity.
It does not subtract prior geometry, image, animation or I/O reservations.

Analysis admission uses the selected scene's instantiated primitive counts,
including repeated instances sharing one source accessor. It reproduces the
native analyzer's two sampling strides and largest `array_split` batch size.
The first stride can retain more than 512 vertices per primitive, and the
concatenated backing array remains live even after the final 2048-sample slice.
The native batch can exceed 128 rows: 255 samples use one 255-row batch.

The receipt exposes these conservative numerical reservations:

- For sample count `N` and largest batch `B`, difference and square arrays
  each reserve `B*N*3*8` bytes; the reduction reserves `B*N*8`.
- For pre-slice concatenation count `C`, sample/PCA/reflection/nearest work
  reserves `24*C + 144*N + 24*B` bytes. This includes both centered PCA
  copies, reflected samples, nearest scalars/conversion and batch outputs.
- Topology work reserves `1024*triangles + 64*vertices + 512*mesh_instances
  + 4096` bytes. This covers numerical triangle gathers/cross/norm/volume,
  both edge/unique passes and their sorting/index/mask/count arrays,
  component parent/output arrays, bounds and small PCA/eigensolver work.
  Summing over all instances also covers the last mesh's arrays still live
  during symmetry calculation.

These amounts are added, including arrays whose lifetimes may not overlap;
`analysis_working_bytes` is the full increment and `estimated_memory_bytes`
is the final combined admission. Four 512-vertex instances with one triangle
each reserve 6,291,456 bytes for the difference array alone, the same amount
for its square, and 2,097,152 bytes for reduction. Total additional analysis
capacity is 15,168,512 bytes. The regression uses actual shared-accessor GLB
geometry: a 5 MiB limit rejects before import/accessor decoding/analysis;
one byte below combined admission also rejects, and exact combined admission
executes the real importer and analyzer. Separate real-source cases cover
uneven batches and retained concatenation. The existing small-animation
positive test retains its 12 KiB animation allowance and adds exactly the
new analysis reserve; all original animation assertions and rejection cases
remain, with additional final combined-capacity assertions.

Limits only tighten the inspector's caps. These are conservative numerical
working-set admission estimates, not process RSS, interpreter/allocator overhead,
later evaluation, renderer or visual-acceptance guarantees. The canonical reader applies its declared buffer
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
Khronos Validator, Blender, and Three.js observations remain follow-up work.
Owner capability registration, rig compatibility and facial classification,
renderer-specific outputs, and production artwork review also remain open.
