# Native editor dependency verification

`editor3d_closure.verify_editor_bundle(project, scenes, nodes, assets,
animations, root)` verifies an already parsed editor binding bundle against the
actual files under one explicit root. It calls the original structural bundle
validator, requires every native binding to appear in `dependency_manifest`, and
checks the native resources through their existing owners.

Scene3D, Camera3D, and legacy production bindings require their native project or
registry in `project.source_projects`. Its parent directory defines that owner's
portable path scope. Model3D retains its established convention that payload
paths are relative to the explicitly supplied root. Rig and LookDev resources
use their manifest directory. A Rig reference uses native kind `Rig`, matching
the rig validator's public kind name. Unsupported owners fail admission.

The verifier captures a bounded temporary snapshot using the existing no-follow
Model3D snapshot implementation: at most 4096 files, 64 MiB per file, 256 MiB
overall, and 32 dependency levels. Every file must match its exact SHA-256 and
byte count when declared. Portable path aliases, links, cycles and conflicting
hashes fail. Native contract relationships determine which files are JSON
  manifests; a filename suffix never converts an opaque payload into a document.
The snapshot is discarded after validation. The original files are
read again before success, so a concurrent source change invalidates the result.
The caller must still own its project write boundary; this read-only verifier
does not lock another process's live project or publish any files.

Native checks are delegated:

- Scene3D: complete native project dependency lock and static document semantics.
  Editor node IDs, parent bindings and supported node kinds must match the exact
  native scene. Other native owners may supply assets but cannot claim verified
  editor-node bindings until a dedicated semantic adapter is implemented. This does not claim every Scene3D asset's geometry was decoded.
- Camera3D: native registry byte closure and cross-document semantics. Every
  editor camera reference must occur in that registry.
- LookDev: the existing immutable registry verifies dependency identities,
  typed material references and exact texture bytes.
- Model3D: full native validation decodes actual geometry and checks payloads.
- Rig: native loading reopens geometry, weights and clips, followed by existing
  numerical rig validation. Original identity and review evidence remain intact.
- Legacy production: the existing project loader validates original assets,
  clips, scenes and continuity. A bound shot must equal the native embedded shot.

Success returns `status: verified-native-closure`, the exact editor project
hash, the captured file inventory, and individual native reports.
`geometry_inspected_owners` identifies the owners for which real decoding ran.
`review_status` remains `unreviewed`, and `rendered_output_verified` remains
false. This result is neither a rendered film nor visual approval, a saved edit,
a server execution receipt, or a provider completion claim.

Run the real-file regression checks with:

```bash
PYTHONPATH=scripts python -m unittest discover -s tests -p test_rig_editor3d_closure.py -v
```

The tests exercise actual retained native projects, mesh payloads and texture
bytes, original legacy production construction, immutable source rechecks and
negative cases. Persistence, native scene commands and interactive authoring are
tracked separately in #100 and #101.
