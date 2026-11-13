# Independent review R5 — PR:newlocal139

- Repository: CompleteTech-Skills/hatch-pet
- Exact HEAD: `dff751b6a44f8c47528f036f7261248ac40789ac`
- Whole candidate base: `209569fa29d8be1b25b9a8cd014c26399e68a71b`
- Reviewer: independent `/root/asset139/review139`; parent-provided actual launch configuration `gpt-6-astra`, high reasoning, no history fork. Runtime identity is not independently introspected.
- Verdict: **CHANGES_REQUIRED**.
- Scope: whole candidate and integrated decoder, carrying forward R1–R4, with actual initial conversion, skinning, provenance, path restoration and texture-admission checks. No source edits or GitHub writes.

## Reconciliation and integrated dependency

All seven original shader/provenance/META/visibility reproduction scripts now reject correctly. The ancestor-symlink test also passes. Evidence: `/tmp/hatch-fleet/review139-evidence/r5-prior-repros.json`. The positive object/channel profile and explicit native-source interpolation losses address the earlier findings.

The previously blocking PNG/JPEG dependency is now integrated from reviewed `10f6c0bb4f303a42e32c06e9fc8cb7d4a3791bbb`. I inspected the complete decoder and its native callsites, requirement pin, workflow and supplied independent review. Integrated source hashes and the copied decoder-review hash match the committed receipt. Independent actual malformed PNG and entropy-damaged JPEG calls through `ImportPipeline.import_asset` return `RIG_TEXTURE_DECODE`, leave source hashes unchanged and leave the library with zero published versions. The old verify-only dependency blocker is therefore resolved for this candidate; hosted CI remains separate.

## Findings

### P1 — Envelope-based armature deformation is accepted but lost

Location: `scripts/asset_import_worker.py:enforce_conversion_profile`, ARMATURE modifier admission; first-conversion checks in `scripts/asset_library_import.py`.

All ARMATURE modifiers are admitted without checking their deformation mode. A genuine cube controlled by an armature using bone envelopes (`use_bone_envelopes=True`, `use_vertex_groups=False`) and an animated bone passes the new object/channel profile. Source evaluation moves the first cube vertex from x=-1 at frame 1 to x=1 at frame 24. The published runtime GLB has no `skins` at all: its cube is unskinned, while unrelated bone translation/rotation/scale channels remain. Publication succeeds as `imported-payload-verified`; no declared loss identifies the missing deformation.

The native observation's `skins=1` is not evidence of a GLB skin: that reader synthesizes an internal binding/profile, while direct GLB inspection shows `skins` absent. Compare the actual exported skin bindings and supported modifier semantics before publication. Admit only verified modifier options or explicitly convert/verify unsupported modes. Also cover preserve-volume, multiple armature modifiers, masks and disabled modifiers; their accepted type alone cannot establish deformation equivalence.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/envelope_repro.py`
- `/tmp/hatch-fleet/review139-evidence/envelope-output.json`
- `/tmp/review139-material-um_xjvvb/create/process.log` records actual evaluated vertices at both frames.
- Actual source `/tmp/review139-material-um_xjvvb/procedural.blend`.

### P1 — Blender derivative silently truncates eight skin influences to four

Location: `scripts/asset_import_worker.py` fixed `bpy.ops.export_scene.gltf` call and derivative comparison in `scripts/asset_library_import.py`.

Blender's exercised default is `export_all_influences=False`, `export_influence_nb=4`. Actual independent source GLB: eight joints, both `JOINTS_0/WEIGHTS_0` and `JOINTS_1/WEIGHTS_1`, each vertex carrying eight weights of 0.125. The canonical runtime correctly retains all eight. The published `native/roundtrip.glb` contains only `JOINTS_0/WEIGHTS_0` with four weights of 0.25. `native_derivative_losses` is empty and import succeeds.

This silently changes derivative deformation despite the module explicitly selecting the eight-influence native profile. Configure actual Blender export to retain all admitted influences and independently compare joint associations/weights, or precisely declare and gate the loss. The number of skins or clips cannot detect this truncation.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/influences_repro.py`
- `/tmp/hatch-fleet/review139-evidence/influences-output.json`
- Source and extracted outputs retained under `/tmp/review139-influences-x5cq1ti_`.

### P2 — Case-folded file/directory prefix collisions publish unrestorable originals

Location: `SourceSnapshot.add` full-path alias check versus `restore_original` prefix collision check.

Source admission compares case-folded complete filenames but does not compare file/directory prefixes. A valid Linux glTF with external buffer `A` and texture `a/texture.png` imports successfully. Calling the same pipeline's `restore_original` on that published reference fails with `Source files collide with directories.` The closure also cannot be represented consistently on case-insensitive filesystems.

Reject case-folded file/directory prefix collisions during source preflight, before any library publication. Cover both discovery orders and directory spelling aliases, and keep source restoration/admission policies consistent.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/alias_repro.py`
- `/tmp/hatch-fleet/review139-evidence/alias-output.txt`
- Real source closure `/tmp/review139-alias-rg_sckdh`.

The parent raised the influence/default and prefix-alias concerns during read-only review; this reviewer independently constructed and executed both reproductions. The envelope deformation case was independently identified and executed by this reviewer.

## Tests and evidence

Used the isolated complete Python 3.12 environment `/tmp/hatch139-py312/bin/python`, including simplejpeg 1.9.0; no packages were installed or shared environments modified. Blender/Khronos locations came from the supplied environment file.

```sh
source /home/complete/.cache/hatch-fleet/environment.sh
PYTHONPATH=scripts:tests /tmp/hatch139-py312/bin/python -m unittest \
  tests.test_rig_asset_import tests.test_rig_texture_integrity tests.test_rig_jpeg -v
```

Result: **45 tests, 44 PASS, one explicitly environment-gated Khronos skip, 45.513 seconds**. All 14 asset tests executed actual configured Blender paths. Log: `/tmp/hatch-fleet/review139-evidence/r5-tests.txt`.

The skipped test was then enabled and independently executed with `HATCH_RIG_INTEROP_TEST=1`: **one PASS in 0.978 seconds**, validating actual PNG/JPEG exports through Khronos. Log: `r5-khronos.txt`. No single zero-skip combined run is claimed.

Additional evidence:
- Seven prior reproduction scripts reject: `r5-prior-repros.json`.
- Actual library corrupt-PNG/recovered-JPEG rejection, unchanged sources and zero publication: `r5-library-corrupt-textures.json`.
- Three new successful-but-incorrect publication reproductions detailed above.
- Integrated pre/post source dictionaries match each other and frozen reviewed Git blobs; copied decoder review hash matches `c5b13f77e2967879b8c85dfa2999163b7c1c05bfb9911600025212f6c15e54b6`.

No full legacy-suite rerun or hosted exact-HEAD CI result is claimed.

## Security, CI, acceptance and recommendation

Fixed scripts, safe descriptor reads, private snapshots, autoexec disabling, process/resource admission and canonical transactional registration remain intact. The strict decoder now fails closed through the library route. Remaining findings concern actual deformation preservation and portable source closure; all reproduce through ordinary supported APIs without executing asset-supplied Python.

The additive Blender and strict-JPEG workflows are present and retain failures through pipefail. Local execution and a separate decoder PASS do not establish hosted CI for this combined HEAD.

Fix the three findings, add real regressions, and request fresh exact-HEAD review and required CI. **Do not merge or close #139 on this verdict.** The former decoder dependency block is resolved, but the candidate itself still requires changes.
