# Native texture CI integration correction review

**Verdict: CHANGES_REQUIRED** — one P2 Model3D diagnostic-boundary gap remains. Native PNG/JPEG admission itself continues to pass the reviewed corruption checks.

Exact HEAD: `16c8ceacefe1071b9bbdcacd2a564608d324a6fd`
Integrated main: `dcba9123c4b70fc2caa7b56267920f239fb35c12`
Correction runtime: `c0af7d70504fa2768812f252828120a4d7cbb521`
Reviewer: `/root/native_texture_integrity/review_texture`, parent-declared `gpt-6-astra` launch configuration (not independently runtime-introspected).
Detached worktree: `/tmp/hatch-fleet/review-native-texture-16c8cea`. No checkout modifications, shared environment changes, remote writes, or merge. Clean at completion.

## Finding

**P2 — Apply Model3D texture-error translation to secondary LOD imports too.** `scripts/model3d_validation.py:214–216`.

The correction translates native texture errors only when loading the primary `geometry` record. Additional `mesh_details` LOD files still take a separate `load_character_model` call whose `except RigError: raise` bypasses the new boundary. Consequently, the same texture failure produces different public codes depending on whether it occurs in the primary model or a secondary LOD.

Independently constructed a real valid primary GLB plus a second glTF LOD, with matching file inventories/digests and a JPEG data URI in the LOD. Actual `model3d validate` subprocess results:

- JPEG entropy cut100 plus EOI: exit 2, raw `RIG_TEXTURE_DECODE`, stage `rig`, asset null, no nested diagnostic; expected `MODEL3D_MODEL`, stage `model3d`, offending LOD path and nested native error.
- Valid JPEG with the verifier made unavailable: exit 2, raw `RIG_TEXTURE_VERIFIER_UNAVAILABLE`; expected `MODEL3D_DEPENDENCY` with native cause and install correction.

Both rejected safely and preserved source hashes, so this is diagnostic compatibility/coverage, not a corrupt-data admission bypass. It is nevertheless the exact public-boundary issue this CI correction claims to resolve. The new tests only put images in the primary geometry, so 133 passing tests do not cover this route.

Requested correction: use one Model3D import/error-translation authority for primary and additional LOD files, preserve their actual asset paths and nested native corrective instructions, and continue to rethrow unrelated native codes unchanged. Add actual damaged/missing-verifier LOD CLI and source-integrity regressions.

Reproducer: `/tmp/hatch-fleet/review-native-texture-16c-lod.py`
Observed actual CLI results: `/tmp/hatch-fleet/review-native-texture-16c-lod.log`
Worker notified and acknowledged the fix before this report completed.

## Independent validation

Ran the exact committed checkout using the complete existing `/tmp/hatch-strict-jpeg-env/bin/python`, with Blender/Khronos/browser flags enabled and the pinned tool/browser locations from `/home/complete/.cache/hatch-fleet/environment.sh`:

`PYTHONPATH=scripts:tests HATCH_RIG_INTEROP_TEST=1 HATCH_RIG_BLENDER_TEST=1 HATCH_RIG_BROWSER_TEST=1 PLAYWRIGHT_BROWSERS_PATH=$HATCH_RIG_PLAYWRIGHT_BROWSERS_PATH /tmp/hatch-strict-jpeg-env/bin/python -B -m unittest test_model3d_contracts test_rig_texture_integrity test_rig_jpeg test_rig_interop test_rig_contracts test_rig_skinning test_rig_compatibility test_rig_correction_validation -v`

**133 passed, zero skips, 52.864 seconds.** Actual independent consumers ran. Log: `/tmp/hatch-fleet/review-native-texture-16c-regression.log`.

Additional exact-head actual-file checks reran:

- Twenty native API+CLI corrupted JPEG cases across all five source routes: rejected, no run publication, source hashes unchanged.
- Thirty all-route baseline/progressive Gray/RGB/CMYK positives with opaque marker-like metadata: exact bytes preserved.
- Ten all-route original arithmetic positives: exact bytes preserved.
- Original malformed PNG row/footer/IEND corpus: all rejected, valid control accepted.
- 105 independently generated Adam7 mode/depth/dimension/fragment combinations: accepted.
- One-pixel PNG with compressed 1 GiB expansion: rejected under a real 1 GiB address-space cap; peak RSS 46,964 KiB.

Logs: `/tmp/hatch-fleet/review-native-texture-16c-routes.log`, `-png.log`, and `-resource.log`.

## Integration inspection and evidence

Reviewed the full task diff against current main, the merged changes since prior 10f, current Model3D snapshot/primary/LOD import paths, shared JSON-decoder integration, gate filtering, changed repository instructions, Git byte-preservation attributes, and current tests. The codecs, shared native import hook, OBJ implementation, optional requirement pin and strict-codec workflow are byte-identical to reviewed 10f. Prior arithmetic warning-versus-authenticity distinctions remain documented and unchanged.

The primary Model3D translation is narrowly limited to native texture diagnostics and preserves unrelated native codes; it is correct on that route. The original embedded-image bomb expectation remains intact. The OBJ correction compares the complete canonical source set rather than weakening membership checks; its added actual parent-alias test also checks every digest. Linux POSIX alias execution passed. Windows junction/macOS execution remains a hosted-CI requirement, not established by this local run.

All 176 hashes in the committed input snapshots match each other and the exact reviewed HEAD. The worker clarified that its first snapshot was captured during initial suite startup; these equal snapshots must not be characterized as proof that capture preceded its very first test. This review's independent run started from an already committed detached checkout and remained clean. Authored-file diff whitespace checks pass.

## Remaining boundaries

The requested LOD correction requires a fresh exact-head review; prior 10f PASS and this 133-test pass cannot substitute for it. Historical review reports remain unchanged. #52/#44/#139 remain open, and exact-head hosted Windows/macOS/Linux checks and root publication/merge gates remain required. Source admission does not authenticate original intended pixels of another valid bitstream, certify production artwork, or establish an OS allocation/filesystem sandbox.
