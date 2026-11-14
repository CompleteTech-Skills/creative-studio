# Independent review R4 — PR:newlocal139

- Repository: CompleteTech-Skills/hatch-pet
- Exact HEAD: `e11d5a08a1f0299c3c754e2a6d13bded29bf3460`
- Whole candidate base: `209569fa29d8be1b25b9a8cd014c26399e68a71b`
- Reviewer: independent agent `/root/asset139/review139`; parent-provided actual launch configuration `gpt-6-astra`, high reasoning, no history fork. Exact runtime model identity is not independently exposed to the reviewer.
- Verdict: **CHANGES_REQUIRED**.
- Scope: carried-forward whole candidate review plus all R4 changes, exact source admission, normalization, provenance, first Blender conversion, receipts/tests/CI. No source edits or GitHub writes.

## Prior findings reconciled

All specific R1–R3 reproductions are now rejected correctly: ancestor symlinks, Noise graph, nondefault subsurface input, animated non-node material, marked Blender derivative reimport as non-fixture, and explicit fixture API classification followed by runtime reimport as non-fixture. The fixture marker is added to normalized runtime metadata while exact original source bytes remain retained.

Independent replay of five original external reproduction scripts returned rejection with the expected shader/material/provenance diagnostics. Evidence: `/tmp/hatch-fleet/review139-evidence/r4-prior-repros.json`. Ancestor-symlink rejection also passes in the independently rerun test suite.

## Findings

### P1 — Initial Blender conversion accepts and drops unsupported geometry

Location: `scripts/asset_import_worker.py:25-31,148-156`; `scripts/asset_library_import.py` initial non-glTF conversion branch.

The positive conversion profile still does not bound object types. A trusted `.blend` containing a cube and a real metaball object (`META`, named `RequiredMetaball`, at x=3) passes admission. Blender's GLB export does not provide the metaball's geometry. The import still publishes `imported-payload-verified` with empty normalization and derivative loss lists.

Observed source inspection contains both the cube and `RequiredMetaball`; runtime inspection has one mesh, 12 triangles and bounds [-1,-1,-1] to [1,1,1], i.e. only the cube. A valid remaining mesh prevents the general no-geometry guard from detecting the omitted object. The generic source limitation about unused datablocks/application metadata does not describe loss of visible scene geometry.

Reject unsupported scene object types before export, or perform a specifically supported conversion and verify its actual geometry. Verify initial source-to-GLB capabilities, not only the later GLB-to-Blender derivative.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/metaball_repro.py`
- `/tmp/hatch-fleet/review139-evidence/metaball-output.json`
- Real source: `/tmp/review139-material-gjet6oc9/procedural.blend`

### P1 — Unsupported object animation channels are silently discarded

Location: `scripts/asset_import_worker.py:25-31,148-156`, initial conversion admission and export.

Material animation is now rejected, but ordinary object animation has no corresponding positive channel admission. Actual source: a cube with `hide_render=False` at frame 1 and `hide_render=True` at frame 24. The trusted Blender import succeeds. Source inspection reports action `CubeAction`; the runtime GLB contains no animation, normalized `preserved_animations` is zero, and both loss lists are empty. The visible/hidden behavior is lost.

Use a documented positive set of convertible animation paths and inspect the applicable action/channel owners before export. Reject unsupported channels or precisely declare their loss. Test actual layered Blender 4.5 actions and compare the first conversion's source capabilities with its output; checking output counts only after conversion cannot discover a channel already discarded.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/visibility_animation_repro.py`
- `/tmp/hatch-fleet/review139-evidence/visibility-animation-output.json`
- Real source: `/tmp/review139-material-dtebus6n/procedural.blend`

Both findings violate #139's explicit unsupported-feature/no-silent-loss requirements and parent #128 §§7/9. Original-byte retention does not make the converted runtime output preserve these features.

## Tests and evidence

Independently ran:

```sh
source /home/complete/.cache/hatch-fleet/environment.sh
PYTHONPATH=scripts:tests hatch_rig_python -m unittest tests/test_rig_asset_import.py -v
```

**14 tests PASS in 37.713 seconds, no skips**, using actual Blender 4.5.3 and Khronos. Log: `/tmp/hatch-fleet/review139-evidence/r4-tests.txt`. The two new adversarial cases independently complete actual publication and expose the remaining losses. Five earlier reproduction scripts were replayed and correctly reject.

R4 receipt hashes match frozen Git blobs at the reviewed HEAD. The worktree was clean when verification finished. No full legacy-suite rerun or remote exact-HEAD CI result is claimed.

## Security, CI, acceptance and recommendation

Previously reviewed safe-reader, fixed-worker, no-shell, autoexec, process-budget and canonical publication protections remain applicable. Current findings are source-semantic losses through supported API paths. CI remains additive with real Blender/Khronos tooling and pipefail, but the present test inventory misses unsupported object geometry and animation paths.

The independently owned complete PNG / strict JPEG decoder remains a **blocking dependency**. Verify-only texture checks have not been accepted, and no report here implies that dependency is complete.

Establish a coherent positive initial Blender conversion profile, fix both cases with real regressions, integrate the reviewed texture decoder, and request a new exact-HEAD review plus required CI. **Do not merge or close #139 on this verdict.** Later corrections do not inherit approval.
