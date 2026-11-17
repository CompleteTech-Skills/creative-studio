# Independent review — PR:newlocal139

- Repository: CompleteTech-Skills/hatch-pet
- Reviewed branch: agent/issue-139-asset-import-20261024
- Exact reviewed HEAD: `695936e7949cd66e6033a0fdd3997b959c118d7b` (implementation commit `cd13417`)
- Diff base: `209569fa29d8be1b25b9a8cd014c26399e68a71b`
- Reviewer: independent agent `/root/asset139/review139`; GPT-6 family. Exact deployed model identifier is not exposed to this reviewer, so this report does not certify an Astra-specific review.
- Verdict: **CHANGES_REQUIRED**
- Scope: all nine files in the candidate diff, relevant canonical library / glTF reader / Blender authorities, README, SKILL, DELIVERY_STATUS, issue #139, parent #128 sections 7/9/16 and continuation requirements. No source edits or GitHub writes performed by the reviewer.

## Findings

### P1 — Initial Blender conversion silently discards source material features

Location: `scripts/asset_library_import.py:648-655`; `scripts/asset_import_worker.py:15-24,105-111` at reviewed HEAD.

A trusted packed `.blend` is exported to GLB and that converted GLB becomes the input to preservation accounting. Initial source inspection records only material counts, so it never identifies unsupported material node graphs or compares their meaning against the exported result. The later loss checks compare this already-degraded GLB with another roundtrip, not with the actual source.

Actual reproduction: create a cube with a material containing Noise Texture Color linked to Principled BSDF Base Color, save a packed `.blend` without scripts, linked libraries or external resources, and call `import_asset(..., fixture=True, trusted_packed_blend=True)`. The operation returns `imported-payload-verified`. Its runtime GLB has a material named `ProceduralNoiseSurface` containing only `metallicFactor=0` and `roughnessFactor=0.5`; it has no texture and no noise appearance. Both `normalization.losses` and `native_derivative_losses` are empty. The report also broadly claims the native GLB retains original materials.

This violates #139's no-silent-material-loss requirement and #128 §§7/9. Retaining original source bytes is valuable but does not make the converted runtime asset lossless. Establish a fail-closed source conversion profile, or explicitly inventory and declare unsupported/lost source features before publication, with source-to-output verification. Test the first conversion of procedural shaders and unsupported animation/geometry features, not only GLB-to-Blender roundtrips.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/material_repro.py`
- `/tmp/hatch-fleet/review139-evidence/material-output.json`
- Retained real source: `/tmp/review139-material-p0fr7ge2/procedural.blend`
- Reproduction command: `source /home/complete/.cache/hatch-fleet/environment.sh; PYTHONPATH=scripts:tests hatch_rig_python /tmp/hatch-fleet/review139-evidence/material_repro.py` from the reviewed worktree.

### P2 — Source parent canonicalization bypasses ancestor-symlink rejection

Location: `scripts/asset_library_import.py:195-201` at reviewed HEAD.

`source.is_symlink()` checks only the leaf. Immediately resolving `source.parent` removes ancestor symlinks before `read_source_bytes()` can apply its descriptor-based no-symlink checks. A path `linked/source.glb`, with `linked` a symlink to a real directory, is accepted. This contradicts the documented no-symlink source policy and bypasses the existing canonical source-reader boundary.

Actual reproduction created a real exported GLB under `real/`, symlinked `linked -> real`, and constructed `SourceSnapshot(root/'linked'/'source.glb', root/'snapshot')`. Observed output: `ANCESTOR_SYMLINK_ACCEPTED glb /tmp/review139-link-57yannwr/real/source.glb`.

Preserve the original absolute path through safe source reading so the canonical reader can reject every symlink/reparse component. Add an ancestor-symlink regression in addition to leaf/dependency checks.

## Acceptance and tests

Independently executed in the pinned environment:

```sh
source /home/complete/.cache/hatch-fleet/environment.sh
PYTHONPATH=scripts:tests hatch_rig_python -m unittest tests/test_rig_asset_import.py -v
```

Result: **12 tests PASS, 24.263 seconds, no skips**, including actual Blender 4.5.3 save/reopen, procedural generation, trusted packed Blender input, scripted-input rejection, native and Khronos validation, external buffers/textures, source restoration, missing resources, URI denial and process timeout. The material-loss reproduction also executed the real Blender/Khronos publication path successfully, exposing a missing negative assertion.

The retained implementation receipt's six source hashes match the reviewed files. The candidate's reported combined interoperability result was inspected as supplied evidence, not claimed as an independent rerun. The complete legacy regression suite and remote GitHub CI were not independently run for this review.

## Security and CI

Positive observations: fixed application-owned worker; no shell invocation; sanitized environment; factory startup and autoexec disablement; untrusted `.blend` refusal; bounded input closures; accessor/graph admission; Linux process-group termination; resource limits; use of the existing transactional registry and exact source restoration. The documentation accurately distinguishes process/resource isolation from an OS sandbox.

The workflow adds real Blender import acceptance with pinned Blender, Node and Khronos tooling. Existing top-level `bash ... -eo pipefail` preserves test failure through `tee`. The Blender test skips locally only when `HATCH_BLENDER` is absent, and the added Blender CI job sets it. Actual remote CI on this candidate remains unproven.

## Dependency blocker and recommendation

The shared complete PNG / strict JPEG entropy decoder is still pending; current `rig_import` verify-only behavior does not establish complete texture integrity. This is an explicitly owned external dependency, not a resolved acceptance item or a new fix assigned to this candidate. #139 must not be marked complete until the reviewed shared fix is integrated and exercised through this importer.

Address P1 and P2, integrate the texture dependency, and obtain a new review bound to the resulting exact HEAD. **Do not merge or close #139 on this review.** This verdict applies only to `695936e7949cd66e6033a0fdd3997b959c118d7b`; corrections begun after review do not inherit approval.
