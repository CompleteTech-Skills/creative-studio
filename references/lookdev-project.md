# Exact LookDev project CLI and native delivery

`scripts/lookdev.py` is an adjacent CLI to `production.py`. It loads the shared
LookDev Registry and retains source provenance and bytes. It does not load Python
or Blender code from a project, crawl directories, fetch URLs, or infer versions.

A project contains `lookdev-project.json`:

```json
{"schema_version":1,"resources":["materials/ceramic.json","textures/albedo.json"],"payloads":["textures/albedo.png"]}
```

Every listed resource must be a valid canonical LookDev resource. All dependency
references must match its exact ID, version, kind, path and SHA-256; every texture
payload must match its declared size and hash. No unlisted file is incorporated.
The project inventory hash binds the exact index, source documents and payloads.
Resource paths are relative to the project root, including dependencies in nested
directories. Duplicate, case-aliased, symlink, FIFO, device and traversing paths
are rejected. Input resources are limited to 1,024 documents, 2 MiB per document
and 32 MiB combined; payloads are limited to 1,024 and 64 MiB combined. These are
import bounds, not a guarantee that an arbitrary backend can render the resources.

```sh
python scripts/lookdev.py --project PROJECT materials list
python scripts/lookdev.py --project PROJECT materials inspect --id ceramic --version 1.0.0
python scripts/lookdev.py --project PROJECT textures validate
python scripts/lookdev.py --project PROJECT lighting list
python scripts/lookdev.py --project PROJECT lighting inspect --id key --version 1.0.0
python scripts/lookdev.py --project PROJECT export-native --destination NEW_DIRECTORY
```

All commands print JSON. Invalid inputs return exit status 2. Omitting a version
is allowed only when exactly one version of the selected resource exists.
Inspection resolves material inheritance through the canonical Registry and
returns defensive copies. Validation reports source closure separately from
rendering and aesthetic approval.

Native export publishes the original captured bytes into a new directory,
reopens the output through the same canonical validator, compares inventories,
and atomically publishes without replacing an existing destination. Source edits
after capture cannot alter the export. `export-manifest.json` records exact hashes
and a native-format loss report. Both index and export manifest names are reserved.
Export parent must already exist and destination must be outside the source
project. Failed staging is preserved for diagnosis. This package preserves all
native definitions, including unsupported features; it does not certify backend
compatibility. GLB, Blender output, texture conversion and rendered approval
require their actual corresponding adapters and tests.

## Verification and remaining integration

`tests/test_lookdev_project.py` executes the real CLI, imports actual PNG bytes,
checks source mutation after capture and reopening an exported project, and
rejects malformed closures, ambiguous versions, path aliases, symlinks and FIFOs.
It runs beside the complete existing LookDev contract tests.

This is a verified portion of #89. The issue remains open: actual viewport
material/light controls, shared renderer budgets/cache/resume integration, GLB
and Blender delivery, backend comparisons and evidence-bound visual review are
separate mandatory requirements. Passing source validation/native export does
not establish these behaviors.

## Scene and renderer source closure API

`load_closure(root, exact_references)` captures a bounded transitive native
closure without requiring `lookdev-project.json`. `Project.capture_closure(ref)`
selects the same subset from already captured project bytes. Both return a
`CapturedClosure` exposing:

- `registry`: canonical LookDev Registry with resolved dependencies.
- `references`: canonicalized requested root references.
- `all_references`: every canonical resource reference in the closure.
- `sources`: root-relative path to immutable captured source/payload bytes.
- `inventory`: exact source sizes and SHA-256 hashes.
- `sha256`: canonical inventory hash.
- `canonical_reference(ref)`: canonical reference for an admitted source pin.

An existing Scene3D material root reference may use `kind: "material"`; the
loader checks its exact ID, version, path and original-byte hash, requires an
actual native material base/instance/variant, and returns that actual native
kind. Transitive LookDev references always use native kinds. No other kind alias
is inferred. This canonicalization is an explicit adapter boundary and does not
change any source bytes or hashes.

Scene snapshots should include `closure.sources` and their complete inventory,
including all texture payloads and inherited material documents. Passing only a
material's root JSON file omits dependencies and cannot establish portable
rendering. Geometry primitive/material-slot projection remains owned by Scene3D;
this API resolves resources and does not invent material assignments. Provenance
and attribution remain verbatim in the retained source documents.
