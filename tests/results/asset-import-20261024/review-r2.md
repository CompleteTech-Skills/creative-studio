# Independent review R2 — PR:newlocal139

- Repository: CompleteTech-Skills/hatch-pet
- Exact reviewed HEAD: `26481d1c1f406eda900607fa37bcf684009fe55e`
- Whole candidate base: `209569fa29d8be1b25b9a8cd014c26399e68a71b`
- Previous review: `695936e7949cd66e6033a0fdd3997b959c118d7b`, report `/tmp/hatch-fleet/review-asset139.md`.
- Reviewer: independent agent `/root/asset139/review139`. Parent reports the actual supported spawn call configured `fork_turns: none`, `model: gpt-6-astra`, `reasoning_effort: high`; this is parent-supplied launch metadata, not a runtime model identifier independently exposed to the reviewer.
- Verdict: **CHANGES_REQUIRED**.
- Scope: whole candidate carried forward from R1, all R2 changes, source conversion/loss handling, fixture classification, safe source reading, tests, receipts and CI. Read-only review; no source edits or GitHub writes.

## Resolved and improved

- R1 P2 is resolved: the original absolute source parent remains unresolved and therefore reaches the canonical descriptor reader's ancestor-symlink checks. The actual ancestor-symlink regression passes.
- The specific R1 Noise Texture graph now fails explicitly before publication. Material animation, unsupported modifiers and constraints also have explicit admission rejection.
- Fixture provenance is propagated into saved Blender scene metadata and the saved `.blend` negative relabeling test passes.

## Remaining findings

### P1 — Allowed Principled graph still silently loses unsupported material inputs

Location: `scripts/asset_import_worker.py:25-41`; initial conversion at `scripts/asset_library_import.py:648-657`.

The new profile checks node types and links but not the active Principled inputs. A two-node Principled-to-Output graph with `Subsurface Weight = 1.0` passes admission. Actual Blender conversion publishes a GLB with only ordinary base color, metallic and roughness; no subsurface representation exists. Both `normalization.losses` and `native_derivative_losses` remain empty and the status is `imported-payload-verified`.

This is the original no-silent-material-loss defect with a source inside the newly declared supported profile. A node allowlist does not establish that all socket values are exportable. Bound the supported shader parameters as well, or inventory and explicitly declare verified conversion losses. Add a real subsurface regression and audit other accepted nonportable Principled settings rather than adding only a Noise-specific negative test.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/subsurface_repro.py`
- `/tmp/hatch-fleet/review139-evidence/subsurface-output.json`
- Real retained source: `/tmp/review139-material-pmiskmtz/procedural.blend`

### P1 — Fixture GLB derivatives can be relabeled as non-fixture

Location: `scripts/asset_library_import.py:658-663`, with marker emission in `scripts/asset_import_worker.py:135`.

The worker stores `hatchAssetSourceFixture=true` on the Blender scene. `export_extras=True` exports that marker to `scenes[0].extras`, but GLB/glTF admission inspects only `asset.extras.hatchSource` and `asset.extras.hatchRig`. Therefore a library-produced fixture derivative carrying the new marker is accepted with `fixture=False`.

Actual reproduction:

1. Import the existing labeled fixture GLB with `fixture=True` and real Blender roundtrip.
2. Read `native/roundtrip.glb` from the canonical library and use it as a fresh source.
3. Confirm its scene extras contain `{"hatchAssetSourceFixture": true}`.
4. Reimport with `fixture=False`, a new version and `blender_roundtrip=False`.
5. Observed success: `imported-payload-verified`, published `provenance.fixture=false`, `provenance.method="import"`.

The source explicitly carries fixture evidence, so this is not missing external provenance that the importer could not know. It contradicts the documentation and repository prohibition on relabeling fixtures. Use a consistent recognized provenance marker across the runtime GLB, Blender derivative, generated GLB and subsequent imports; reject a false classification before publication. Exercise each supported output/reimport path.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/fixture_repro.py`
- `/tmp/hatch-fleet/review139-evidence/fixture-output.json`

## Tests and evidence

Independently executed from the reviewed worktree:

```sh
source /home/complete/.cache/hatch-fleet/environment.sh
PYTHONPATH=scripts:tests hatch_rig_python -m unittest tests/test_rig_asset_import.py -v
```

**13 tests PASS in 32.344 seconds, no skips**, with actual Blender 4.5.3 and Khronos validation. Log: `/tmp/hatch-fleet/review139-evidence/r2-tests.txt`. The subsurface and fixture adversarial repro scripts also completed through actual publication and exposed the two findings above.

All six source hashes in `source-receipt-r2.json` match the frozen Git blobs at reviewed HEAD. Worktree was clean immediately after test verification and HEAD was `26481d1c1f406eda900607fa37bcf684009fe55e`; the implementer subsequently began corrections while this report was being written. Those later working-tree changes are excluded from this verdict. Full legacy regressions and remote CI were not independently executed in this review.

## Security, CI and acceptance

R1's favorable observations about fixed scripts, autoexec disablement, process limits, exact-source retention and canonical transactional publication remain applicable. The ancestor-symlink fix restores the existing reader boundary. Neither the shader-loss finding nor the fixture finding requires executing untrusted Python; both use normal supported API paths.

The additive CI configuration remains appropriate: pinned Blender and Khronos tooling, real Blender job, retained logs and pipefail. Remote exact-HEAD CI is still pending evidence. A passing current suite is insufficient because its accepted-profile and derivative-provenance coverage misses the reproduced cases.

The complete PNG / strict JPEG decoder remains a separately owned **blocking dependency**. Verify-only image behavior has not been accepted as complete integrity validation. This review does not approve that dependency or imply #139 completion.

Recommendation: fix both P1 findings, integrate and exercise the reviewed decoder dependency, then obtain fresh exact-HEAD review and required CI. **Do not merge or close #139 on this review.** No approval transfers to later corrections.
