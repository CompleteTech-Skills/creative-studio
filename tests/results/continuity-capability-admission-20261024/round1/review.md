# Independent review: continuity capability admission (#115)

**Verdict: CHANGES_REQUIRED**

Candidate: `53a4ee463cf15f1cc379c60aaa9fccc53cd02ef8`, branch `agent/issue-115-capability-admission-20261024`, read-only worktree `/tmp/hatch-continuity115`. Reviewer is independent of the implementation owner. No implementation edits, GitHub writes, backend child processes, browser launches, Blender/FFmpeg probes, or merge actions were performed.

This verdict applies to this exact candidate, not a future repaired/integrated head. The dependency #113 is independently BLOCKED pending its final repairs/review; the candidate must consume its final typed-reference API through a normal merge and be reviewed again. Native resource-loader work coordinated by the root is also not included in this candidate.

## Findings

1. **P1 — Bound rig pointer can identify a different file from the actual inspected rig.** `scripts/continuity_capability_owners.py:61–64,92–94` accepts any portable filename and hashes that file, but `load_rig(parent)` always consumes `parent/rig.json`. Actual repro: `RigOwner(root, 'rig/identity.json', 'evidence', 'geometry-inspection')`, with the exact advertised `owner.bound` in the request, returns `supported_fallback`. The pointer claims `kind=hatch-3d-rig` but binds identity SHA `1166f2cc77d0de4f672a66eaabe693bb4247579c51a0cc084a2d654e989c0b51`, while inspected `rig.json` SHA is `91a1d152d221535d858946b98a1f7d2c0bcd085bc4aaf897c5c4905a2478f90f`. Require the authoritative native manifest path, or bind exactly the manifest the authoritative loader consumes. Test rejected alternate filenames and changed actual rig bytes.

2. **P1 — Project revision can change during admission while result says integrity verified.** `scripts/continuity_capabilities.py:220–229,317–320` verifies the complete request initially, but after execution only verifies the check inputs. A deterministic concurrent-change repro rewrites `project.json` in the first cancellation callback after initial verification, returns False, and permits the actual native geometry operation to continue. Result: `supported_fallback`, `integrity=verified`, with the request project hash `218589...c5a6b9` differing from the actual `341508...8b88e`. Revalidate the exact project/request reference closure before final success (and retained snapshot/result evidence as appropriate), reporting integrity failure without discarding independent diagnostic results. Do not allow later checks to stale earlier bound input/evidence unnoticed.

3. **P1 — Memory admission ignores native decompression and retained texture data.** `scripts/continuity_capability_owners.py:94,102–104,112` loads using the rig manifest's native limit (2 GiB in the fixture) before checking the request estimate. The estimate counts only vertices/triangles, excluding textures, morphs, skin data, clips, and simultaneous reload allocations. Actual safe repro adds 48 valid 512×512 PNG image payloads through authoritative `save_geometry`. A request reserving **35,651,584 bytes** returns `supported_fallback` even though retained image bytes alone are **37,789,008 bytes**; its estimate is **33,822,720 bytes**. This is deterministic underaccounting, beyond the documented distinction between estimates and OS enforcement. Pass effective host/request caps into the authoritative loader before decoding, account for aggregate native working data, and avoid unbudgeted full reload duplication. Preserve native loader ownership; do not add a competing parser.

4. **P2 — Categorical fallback restrictions are not enforced for GUI/video.** `scripts/continuity_capabilities.py:332–350` clamps historical fixtures so they never satisfy `original-media`, but does not similarly clamp `python-cli-service` or `preserve-image-masters`. Policy-boundary repro adapters returning all requested fields produce `supported_fallback` with **no missing fields** for both GUI integration and video encoding. Trusted adapters are application code, not untrusted JSON plugins; this repro tests the admission policy's invariant enforcement, not a sandbox escape. A Python/CLI fallback must retain unavailable GUI integration, and preserved masters must retain unavailable video/timing/audio inspection, regardless of overbroad native field reporting. Add full 13-row invariant tests, especially categorical no-LLM/no-2D-substitution/visual-approval restrictions.

5. **P2 — Malformed owner responses escape final serialization and lose the structured report.** `scripts/continuity_capabilities.py:323–331,364–366` validates only the result keys, outcome and string-list shape; nested evidence, limitations, measurements, bounds and merged result shape are not validated within the per-check exception boundary. Actual repro native result `limitations=[None]` causes the entire `Admission.run` to raise `CONTINUITY_CAPABILITY_SCHEMA: None is not of type 'string'` at `_finish`, rather than record this failed check and return independent results. Validate each native response and final check result before appending it. Also cover maximum-length merging and a valid but aggregate-oversized report; do not lose results at final canonicalization.

6. **P2 — Expiry is checked against batch-start time for every operation.** `scripts/continuity_capabilities.py:243,289` passes a single `now` captured before the loop. Actual two-check repro: snapshot expiry `2026-10-24T05:42:09Z`; first execution starts `05:42:07Z`, second starts **05:42:10Z**, and both receive `supported_fallback`. Recompute effective admission time for each operation, including time spent waiting/reserving, using an injectable clock or a monotonic offset from the supplied test time. The second operation must not execute under an already expired capability.

## Additional acceptance gaps to resolve in the repaired candidate

- A check retains only `snapshot_sha256`, owner/backend/version, and execution evidence. The exact snapshot (probe-evidence pointers, compatible formats, limits, expiry and security flags) is neither embedded nor persisted; the trusted policy is absent too. The hash alone is insufficient to reconstruct the actual admission decision after adapter objects disappear. Retain the used snapshot and safe policy identity/content, and relevant actual rendering dependency versions. The implementation owner independently identified this gap while the candidate was frozen.
- Geometry admission calls its result complete without recording native `extensions_used` or declaring missing original mesh/node hierarchy. The matrix explicitly requires inspected hierarchy and unsupported extensions/channels. Existing native geometry is real, but the built-in adapter is a `hatch-3d-rig` adapter, **not** a direct GLB/glTF inspection adapter. Keep that distinction visible in the report and mandatory/unavailable fields. Do not turn missing inspectors or unavailable production owners into claimed implementation of later continuity production phases.
- Discovery is correctly execution-free, but only lists packages and capability names. Version-unknown/unavailable snapshots require a fabricated semver under the current schema. Consider a typed null version for unavailable/untested states while requiring an actual version for available states; name known owners/backends without claiming a version probe executed.
- `cpu_seconds` enforcement currently compares wall elapsed only, even though process CPU is measured. Ensure the documented CPU budget and deadline semantics match actual enforcement, particularly for native multithreaded work.

## Executed verification and retained evidence

All reviewer executions used `/home/complete/.cache/hatch-fleet/rig-py312/bin/python` and did not spawn children. Actual environment: Python 3.12.15; NumPy 2.3.5; Pillow 12.3.0; jsonschema metadata 4.25.1. The imported NumPy/Pillow versions and module paths were separately confirmed.

From `/tmp/hatch-continuity115`:

```text
/home/complete/.cache/hatch-fleet/rig-py312/bin/python -m unittest discover -s tests -p test_rig_continuity_capabilities.py -v
29 tests, 11.031 seconds, OK; no skips/failures/errors.
Log: /home/complete/.cache/hatch-fleet/review115/focused53a4.log
```

Independent adversarial commands (exit 0 means the reproduction script completed, not that the candidate met the requirement):

```text
/home/complete/.cache/hatch-fleet/rig-py312/bin/python /home/complete/.cache/hatch-fleet/review115/adversarial115.py
/home/complete/.cache/hatch-fleet/rig-py312/bin/python /home/complete/.cache/hatch-fleet/review115/resource115.py
/home/complete/.cache/hatch-fleet/rig-py312/bin/python /home/complete/.cache/hatch-fleet/review115/expiry115.py
```

Exact observed outputs are the adjacent `.json` files. Source and evidence SHA-256 values, actual versions and module paths are retained in `/home/complete/.cache/hatch-fleet/review115/evidence53a4.json`.

Key SHA-256 values:

- `scripts/continuity_capabilities.py`: `a9e9adc407e43cf31cd0a629944bde0b09ae4878afc4e9abc5985ae2eed1b0e7`
- `scripts/continuity_capability_owners.py`: `7da1111b9c9d9ce5e8f471eb5265fd735b3c39b2120488e8882aec5ead76bc23`
- `tests/test_rig_continuity_capabilities.py`: `17bdd7ebcb6ed0ae882213442b7aca79e2c5c53e32bca005124674f31688bc99`
- `adversarial115.json`: `7cee56221a4e9aa6ec7a271cde39d0bb4cc709ccf0d265f6a9a4cede7dbb0f9a`
- `resource115.json`: `531ef96985904183844aeffe0aaf90df9dd56887eeecbd93c60b5960b8a44a16`
- `expiry115.json`: `e78cf45815ce9563062c0835bf3f073702df3e67c92367b968c039cb358857b2`
- `focused53a4.log`: `85c5338d8df3d08427a2bf50821381e353f440039785ab89d6a95ad8ba582cc9`

The implementation owner's earlier 29-test/8.078s log and historical failed setup/test logs remain untouched. Reviewer `git status --short` was empty after all executions.

## Scope and remaining verification

Reviewed #115, closed baseline #112, parent #108, original #107 plus its continuation and all 13 fallback rows, native loading/geometry/FK/deformation/rendering/job-lock integration, shared #113 references, schema generation, focused tests, and candidate workflow/documentation. No production visual approval, Blender/native exporter, physics continuation, actual GUI, LLM execution, or direct-GLB adapter is established by this review. The existing 29 tests do establish real native fixture geometry/deformation/CPU rendering and image-master decoding, alongside several useful policy negatives, but they do not cover the failures above.

Legacy compatibility/CLI subprocess checks remain queued under the root's shared native-child slot; they were not run by this reviewer. Linux-only pure in-process execution does not establish Windows/macOS CI. A fresh exact-SHA review is required after repairs and final dependency integration. This candidate is not merge-ready.
