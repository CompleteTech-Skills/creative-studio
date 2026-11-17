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
uses a private copy of the pinned bytes. The inspector rechecks the original
pins before returning and rechecks both inputs before returning a comparison.

Run an inspection with a JSON request containing `source` and optional
`dependencies`, `landmarks`, `distances`, `limits`, and `variants`.
For comparison, supply an object with `left` and `right` inspection requests.
Python callers can use `inspect_asset(root, source, ...)` and
`compare_assets(root, left, right)` from `scripts/continuity_inspection.py`.

```sh
python scripts/continuity_inspection.py --root project --request inspect-request.json
```

Successful inspections contain `inspection` and its canonical `sha256`.
Comparisons retain both inspection receipts. The CLI reports structured
diagnostics from the native readers and returns a nonzero exit on rejection.

Named landmarks bind `mesh_id` and `vertex_index`. Distances bind two landmark
names. The receipt reports distance in meters and as a fraction of observed
height. Landmark names are authored. Optional variant references are separately
verified evidence pointers. They confer no approval.

## What the receipts measure

Receipts retain selected scene hierarchy and transforms, geometry measurements,
array fingerprints, imported skin joints and bind data, material inputs,
texture hashes, sampler and UV bindings, morphs, and source extension and
animation-channel inventories. Texture slot color spaces describe portable
glTF semantics. Optional extensions outside supported coverage yield partial
evidence. The native reader rejects unsupported required extensions.

The comparison reports main-file byte identity and full closure byte identity.
It measures triangle surfaces, attributes and morphs, height-normalized
surfaces, and materials and textures. Triangle surfaces are quantized to
1e-9 meters, with vertices and triangles ordered independently of winding and
serialization. Original index and attribute fingerprints remain available.
Comparisons report `semantic_equivalence: unverified`,
`needs_visual_review: true`, and `visual_approval: false`.

Native preflight accounts for instantiated geometry, accessor work, and
metadata. Image admission adds aggregate decoded texture capacity using
native image framing before the importer decodes pixels. Limits only tighten
the inspector's caps. These are conservative loader estimates, with no process
RSS or renderer guarantee.

## Local evidence and remaining acceptance

```sh
python -m unittest discover -s tests -p 'test_rig_continuity*.py' -v
python tests/continuity_inspection_probe.py new-evidence-directory
```

The probe writes portable source and receipt hashes for procedural fixtures
with actual skinning, positions, morphs, and PNG texture bytes. It includes
geometry and texture changes, exact external closure acceptance and missing
pin rejection, and aggregate image capacity acceptance and rejection. These
observations are local to the canonical Python readers. Registering the new
suite or probe with the shared rig interoperability gate and obtaining
independent Khronos Validator, Blender, and Three.js evidence remain follow-up
work. Native `rig.json`, owner capability registration, rig compatibility and
facial classification, renderer-specific outputs, and production artwork
review are outside this slice.
