PR: pending
Reviewed HEAD: `f70f565d76e6e23c8acccd6b6b47b3e319820240`
Reviewed baseline: `dcba9123c4b70fc2caa7b56267920f239fb35c12`
Reviewer: independent `review_matrix_scale` agent; model `gpt-6-astra` (assigned reviewer model).
Verdict: **CHANGES_REQUIRED**
Scope: partial #44/#52 matrix-scale correction, not parent issue closure.

- **P2 — Newly admitted reflected tiny transforms lose winding and tangent handedness.** The relaxed admission at `scripts/rig_gltf.py:182` accepts a finite-inverse matrix with scale `[-1e-108, 1e-108, 1e-108]`. Downstream `scripts/rig_import.py:220,236` still use the unnormalized determinant. It underflows to negative zero, so a successful real GLB import leaves triangle `[0,1,2]` instead of `[0,2,1]` and writes tangent W `0` instead of `-1`. A second ordinarily scaled mesh supplies measurable total geometry extent, so the import succeeds and the corrupted attributes are returned. This is newly reachable: the baseline rejects the same node with `RIG_SINGULAR_TRANSFORM`. Preserve orientation using a magnitude-independent determinant sign (or explicitly reject unsupported cases before accepting an import), with actual multimesh regression coverage.
- **P2 — Newly admitted tiny transforms can silently zero valid normals and tangents.** For the same real multimesh asset at scale `[-1e-160,1e-160,1e-160]`, the matrix inverse remains finite, but `transform_normals` squares inverse-transformed components around `1e160` in `np.linalg.norm`, overflowing the norm to infinity. The successful import returns zero normal/tangent vectors from unit input vectors. Use stable vector normalization or fail closed before returning corrupted geometry. The existing geometry validator checks finiteness/shape and does not catch these zero vectors. This is also newly reachable because the baseline rejects that scale. Extreme large-scale normal issues that were already accepted by baseline are not counted as new findings.

Required acceptance missing: actual tiny-transform multimesh import assertions for preserved reflection winding, nonzero unit normal/tangent directions, and tangent handedness. The new isolated-node tests establish finite inverses but not safe downstream import behavior at their expanded numerical boundary.

Independent tests and probes:

- Pinned rig Python 3.12 environment; own detached review worktree `/home/complete/.cache/hatch-fleet/worktrees/review-matrix-scale`.
- `TMPDIR=/home/complete/.cache/hatch-fleet/matrix-scale-review/tmp hatch_rig_python -m unittest discover -s tests -p test_rig_math.py -v`: **11 tests passed**, zero errors/failures/skips, 1.113 s. This includes the eight GLB/glTF import/export/reimport cases and both actual in-process CLI import successes and singular/nonmutation negatives.
- Independent seeded 2,000 randomly rotated anisotropic/reflected matrices across per-axis magnitudes `[1e-6,1e6]`: all admitted exactly, with unchanged caller input, 0.931 s.
- Independent near-dependent matrices at directional separations `1e-14`, `5e-13`, `2e-12`, `1e-10`, each uniformly scaled by `1e-100`, `1e-6`, `1`, `1e6`, `1e100`: rejection/acceptance invariant across units; below-threshold cases reject and above-threshold cases accept.
- Independent real-GLB boundary sweep from `1e-309` through `1e308`; inverse-overflow rejects at `1e-309`; single tiny meshes fail the pre-existing total extent guard. Three real multimesh GLBs establish the required-scale control and both newly exposed corruption findings above. Source bytes/hashes retained in evidence.
- Direct baseline `node_matrix` control using source loaded from `dcba912` confirms baseline rejection of `1e-6`, `1e-108`, `1e-160` reflected nodes.
- Retained evidence review: all **1,668** source snapshot SHA-256 entries match the reviewed worktree. The four evidence-only files added after `5e15682` do not alter runtime/tests/docs. Retained integrated log records **111 tests run, 0 failures/errors, 5 skips** (106 passed), 22.198 s unittest time / 22.528 s enclosing recorded time; these figures are consistent. This integrated run was performed by the owner, not independently repeated here.

Regression/security review: inspected entire source/test/docs/evidence diff, node parsing/affine validation, scene accumulation, skeletal import/decomposition/skin callers, normal/tangent/morph baking, winding, geometry validation, export, and schema-math gate inclusion. Finite-input, zero-axis, inverse-overflow and direction-dependence checks remain present; skeletal shear/reflection restrictions remain unchanged. Source/dependency resource limits are not changed. Existing near-affine tolerance is unchanged and is not a new finding. Live #44/#52 and parent #10 sections 9/11/21/28 plus #11 import/math/source-preservation requirements were consulted. No broad #44/#52 completion is claimed.

CI status: hosted CI **not executed or verified** by this reviewer. No Blender, browser, Khronos, native backend or external interoperability run was performed; the retained five optional skips remain limitations. No GitHub mutations or shared-environment modifications were made.

Evidence directory: `/home/complete/.cache/hatch-fleet/matrix-scale-review/`.
Evidence: `math.log`, `probe.py`, `probe.json`, `probe_multimesh.py`, `probe_multimesh.json`, `probe_numeric.py`, `probe_numeric.log`, retained GLBs, live issue JSON snapshots.
Key reproduction artifact SHA-256: `multimesh-1e-108.glb` = `f075786a26fcca30be39d3d2e7c20b53f47f5fe237dbff8df7423f7f69696253`; `multimesh-1e-160.glb` = `54228bd4c549aa2a91876c91d20f827dface19b2a689451ab229bcbfea51f181`.

Final recommendation: fix the two newly reachable numerical corruption paths, add actual workflow regressions, rerun affected checks, and request fresh whole-head independent review. Do not merge this reviewed head as passing.
