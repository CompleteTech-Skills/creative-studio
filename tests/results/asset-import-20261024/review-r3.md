# Independent review R3 — PR:newlocal139

- Repository: CompleteTech-Skills/hatch-pet
- Exact HEAD: `9678f0db06d4c164a8d931059e0a4e5f55b42cc5`
- Whole candidate base: `209569fa29d8be1b25b9a8cd014c26399e68a71b`
- Reviewer: independent agent `/root/asset139/review139`. Parent-provided actual launch metadata: `model=gpt-6-astra`, `reasoning_effort=high`, `fork_turns=none`. Runtime model identity is not independently exposed to the reviewer.
- Verdict: **CHANGES_REQUIRED**.
- Scope: whole candidate carried forward from R1/R2, all R3 changes, first conversion admission, provenance paths, safe reading, receipt/test/CI evidence. No source edits or GitHub writes.

## R1/R2 reconciliation

- Ancestor symlink rejection remains fixed and independently passes.
- The original Noise graph and ordinary nondefault Subsurface Weight cases now fail closed. An additional attempted renamed-socket bypass also failed after Blender reload; it is not a finding.
- Fixture metadata in Blender-produced scene extras is now recognized by GLB admission. The prior `native/roundtrip.glb` relabeling reproduction is covered and fails as required.
- Bounded worker error text now gives actionable diagnostics instead of claiming a deleted temporary workspace retains logs.

## Findings

### P1 — Non-node material animation skips the new rejection

Location: `scripts/asset_import_worker.py:32-54`, especially the early `if not material.use_nodes: continue`.

Material animation is checked only after the node-graph branch. Blender materials with `use_nodes=False` can still have animated `diffuse_color`, but that entire material skips admission checks. Actual source: a cube with a non-node material, diffuse color keyed red at frame 1 and green at frame 24. The trusted packed import succeeds as `imported-payload-verified`. Source inspection records `actions: ["AnimatedDiffuseAction"]`; runtime GLB contains a static red PBR material, `preserved_animations` is zero, and both loss lists are empty.

This remains silent loss of an actual source animation/material feature under #139 and #128 §§7/9. Check material animation before all early exits, including non-node materials; ensure other admitted source branches cannot bypass the same policy. Add a real negative regression.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/nonnode_animation_repro.py`
- `/tmp/hatch-fleet/review139-evidence/nonnode-animation-output.json`
- Retained real `.blend`: `/tmp/review139-material-3fan6jyq/procedural.blend`

### P1 — API fixture classification is not embedded in the authoritative runtime GLB

Location: `scripts/asset_library_import.py:672-681` and runtime payload publication.

Whole-document marker scanning fixes already-marked inputs, but the explicit `fixture=True` argument is only forwarded to Blender derivatives. `runtime/asset.glb` is read and published before any such marker is added. For a valid incoming GLB without an embedded fixture marker, an import explicitly classified as fixture publishes an unmarked runtime derivative. Extracting that runtime asset and reimporting it with `fixture=False` succeeds.

Actual reproduction removed the fixture metadata from a valid engineering GLB before its first import, imported it with explicit `fixture=True` and `blender_roundtrip=False`, read the authoritative runtime GLB from the canonical library, and reimported with `fixture=False`. Observed: first manifest `provenance.fixture=true`, second manifest `provenance.fixture=false`, success status `imported-payload-verified`. No byte edits occurred between extracting the published runtime and reimporting it.

The first explicit classification is known to this pipeline and should survive its derived runtime output. Add a reserved canonical fixture marker to the normalized runtime when `fixture=True`, while retaining original source bytes unchanged. Validate the final marked payload and include it in hashes/reports. Exercise both Blender and native-only publication, plus generated output reuse.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/runtime_fixture_repro.py`
- `/tmp/hatch-fleet/review139-evidence/runtime-fixture-output.json`

## Tests and evidence

Independently executed:

```sh
source /home/complete/.cache/hatch-fleet/environment.sh
PYTHONPATH=scripts:tests hatch_rig_python -m unittest tests/test_rig_asset_import.py -v
```

**13 tests PASS in 34.808 seconds, no skips**, actual Blender 4.5.3 and Khronos. Log: `/tmp/hatch-fleet/review139-evidence/r3-tests.txt`. The two new adversarial reproductions above independently completed actual successful publication, proving the missing negative cases. The attempted renamed-socket bypass was correctly rejected.

The R3 receipt hashes match frozen Git blobs at the reviewed HEAD. Worktree remained clean at completion of verification. No full legacy-suite or remote exact-HEAD CI rerun is claimed.

## Security, CI, acceptance and recommendation

The prior favorable fixed-worker, no-shell, autoexec, bounded-process, exact-source and canonical-registry observations remain applicable. The current failures are semantic integrity/provenance failures through ordinary supported APIs; neither needs malicious Python execution. CI remains additive and retains failures through pipefail, but current passing tests do not cover these source branches.

Shared complete PNG / strict JPEG decoding remains an independently owned **blocking dependency**; verify-only image checks have not been accepted. No finding in this report treats that dependency as resolved.

Fix both P1 cases, integrate and exercise the reviewed texture decoder, and obtain fresh exact-HEAD review and required CI. **Do not merge or close #139 on this verdict.** Later corrections do not inherit approval.
