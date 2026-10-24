# Model3D #43 implementation audit and acceptance map

Starting default revision: `6943607c4e678a447536384566ea6230cc1a1d27`, after independently
reviewed PR #200 merged and #51 passed post-merge verification. The full roadmap
snapshot includes 180 issues, 168 native parent edges, 200 native dependency edges
and all 35 comments at capture time. Parent #10 and its complete continuation,
#43's checklist and the other subsystem integration requirements remain authoritative.
See the execution ledger on `agent/roadmap-capture-20261023` and #43's ownership comment.

## Reuse and boundaries

| Existing authority | Use in this slice |
|---|---|
| `rig_contracts` | Canonical Y-up/meters/XYZW basis and existing schema construction idioms. Independently named Model3D contracts; no replacement of rig contracts. |
| `rig_errors` / decomposition path policy | Existing structured errors, strict duplicate-key/nonfinite JSON, portable spelling, hashing and contained paths. |
| `rig_import`, `rig_gltf`, `rig_obj`, `rig_models`, `rig_geometry` | Decode real geometry, inspect hierarchy/skins/clips, enforce existing format/resource limits and measure bounds/counts. No parallel mesh or importer implementation. |
| `rig_jobs.code_signature` | Existing implementation fingerprint for the inspection authority; no duplicate job store. |
| Existing production acquisition, versioning, reviews and archives | Left authoritative and unchanged. Model3D job/export/review fields describe records; this slice does not instantiate a new lifecycle or grant approval. |
| Existing schema generator / test collectors | Generate the additive schema and isolate only the new 3D test namespace from dependency-free legacy execution. |
| Existing robotic GLB and source payload | Referenced unchanged by the executable example, with exact hashes and fixture status. No fabricated artwork, generation receipt or license. |

No AGENTS.md, CONTRIBUTING or CODEOWNERS was present in the captured full repository
tree. README, SKILL, DELIVERY_STATUS and the existing contract/import/persistence
boundaries were inspected. The implemented Y-up basis takes precedence over earlier
unimplemented Z-up suggestions in related issue prompts; an explicit basis adapter
is required at any boundary, never a silent reinterpretation of legacy pixels.

## #43 checklist and evidence

| Required work | Implemented behavior / executable evidence |
|---|---|
| Record baseline and preserve existing contracts | Full clean baseline at `7995804`: 853 tests, zero failures/errors, nine local environmental/optional skips. Initial host-startup NumPy isolation failure retained separately. Current source-delivery verification passes actual restored v3 payloads. Older unavailable historical/private media remains unavailable. |
| Strict request/source/asset/part/material/uncertainty/job/export schemas | `model3d_contracts.py` generates all fifteen definitions and runtime uses the same generated definitions. `generate_schemas.py --check` verifies parity. No legacy generated-schema bytes change. |
| Hashes, units, bounds, versions, graph references and unknown fields | Unit tests cover malformed values, unsafe paths, declared/actual geometry mismatch, missing and same-size corrupt payloads, cycles, conflicting immutable versions, graph depth, source modality, calibration, fixture provenance and unsupported fields. |
| Capability detection and isolated dependencies | `model3d.py capabilities` reports installed packages without importing the numeric backend or making network/provider calls. Existing rig dependency files are reused. `doctor.py` advertises the bounded read-only feature and explicitly says generation/provider execution/approval are false. |
| Real affected workflows | `model3d.py validate` decodes the retained robotic GLB: 706 vertices, 1,368 triangles, 11 skin bindings and three actual clips. GLB SHA-256 `38c95ce549f58bdef2f06ce4652ac2026ca99c744f9593f348d0d1696131296e`; original fixture source SHA-256 `0a1c4fe3927c93588964f3cea225c4b78abe0ce096fd3d6fae6159e4277274ad`. |
| Persistence/failure safety | Read-only source root, private staged snapshot, post-read identity checks, complete dependency validation, bounded copies and cleanup. Tests restore all declared payloads into a fresh directory, compare results, delete required content and require rejection. Concurrent readers return the same result without changing inputs. |
| Legacy regressions | Existing schema/math gate passes all 20 tests and still discovers all 790 2D tests with 3D imports blocked. Full changed-tree results and remote checks must be recorded against the actual published head before merge. |
| Documentation and honest capability limits | `model3d-contracts.md`, README/SKILL additions and executable example document semantics and exclusions. Structural, payload, geometry, visual and licensing evidence remain separate. |

The test that edits an original model after snapshot creation still runs the real
existing decoder against the verified copy. It tests snapshot isolation; it is not
a mocked model-generation or rendering success. Likewise, deliberately malformed
model fixtures are negative inputs, not substitute production artifacts.

## Unmet gates and scope

Independent review and current-head CI are required before merging this slice.
No self-review is independent approval. #43 stays open until its complete checklist,
review, checks, merge and post-merge verification succeed. Parent #10 and #44–#50
remain open. Actual model generation/reconstruction, optimization, full export
roundtrips and production-art approval are not supplied by this contract slice.
The existing #201 rig-correction branch remains owned by its earlier worker.

This slice creates no alternate registry, renderer, persistence database, paid
provider, review authority or scheduler. Future producers must emit these contracts
and pass real payload inspection; merely selecting a method in a request is not
an implementation of that method.

## Historical local results before review corrections

At original implementation head `c08a73a3ed93ec1dbf0c9e1d34448cf3a168d3a6`,
the local source-hash-bound run completed **893 tests, zero failures/errors, ten
explicit environmental/optional skips** in 245.662 seconds. All **31 new Model3D
tests passed without skips** in 5.714 seconds, including actual OBJ and an independent
pygltflib read. No source/test file changed during that historical full run. These results do not
cover subsequent fixture, security/portability or contract-completeness corrections. The targeted log, full-suite summary/checksum, source hashes,
capability discovery and actual GLB inspection are retained in
`tests/results/model3d-contracts-20261023/`. Remote CI and independent review remain
separate gates; these local results do not claim that either has completed.


## Review corrections and current integration evidence

The review corrections add the seven separately versioned §5.7 evidence contracts,
mandatory source/request/part/material/asset metadata, actual per-mesh/LOD
measurements, regional ownership and scoped uncertainty validation. They correct
embedded-image errors, supplementary calibrated observations, eight-influence
preservation, imported mesh IDs, aliased temporary paths, Windows creation-time
fingerprints, nested/cached memory admission, every-LOD fixture propagation,
immutable repair/provider/archive references and true-end token/path schemas.
Evidence envelopes preserve native owners; schema validity never authenticates
execution, reviews, licenses or approval.

At exact implementation `2d77a38b76f82e307bea9577ad23860ff5930db7`, the full
local suite passed **922 tests**, zero failures/errors and **10 explicit optional
or environmental skips**, in **697.825 seconds**. Source hashes before/after are
identical. Browser, native Krita and opt-in Blender/Khronos checks remain separate;
the retained log lists actual skip reasons. All **48 focused tests** passed and
the 20-test schema/math gate discovered all **802 current 2D tests** without optional
3D imports (main added twelve cases since the historical 790-test discovery).

The independently reviewed shared JSON decoder fix
`ffe872dc0330133c1f6e52e34913f30cd041a4a8` was then normally merged, and a real
public Model3D CLI 5,000-digit-integer regression was added. Actual affected checks
passed **49 Model3D tests** (7.457 seconds) and **26 schema/math tests** (6.904
seconds), zero failures/errors/skips, plus generated schema/template parity, actual
robotic GLB inspection and original source-delivery verification. The prior 922-test
receipt is explicitly retained against `2d77a38`; it is not presented as a rerun
after helper integration. Current-head native CI and fresh independent review
remain mandatory before merging.

Current and historical evidence are separate in
`tests/results/model3d-review-corrections-20261023/`. The helper-integrated summary
binds actual tested source hashes and commands. No publication, CI, independent
approval, native application exercise or production signoff is inferred from these
local results.


A subsequent independent review reproduced order-dependent cached geometry
admission through a stricter ancestor memory budget. The correction keys dependency
results by effective memory context, re-inspects geometry when necessary and retains
one observation per asset revision at its tightest successfully inspected budget.
Regression cases prove that both dependency orders reject an actual 800-vertex,
400-triangle mesh under a 64 KiB intermediate budget, while small geometry passes
both orders with one correctly budget-bound observation per revision.

The final shared decoder followup
`cf15e97027ab48d028de6dfd8dc8f2440f2cf2d8` adds bounded UTF-8 text-byte admission
and was independently reviewed before normal integration. With that dependency
and the cache correction, actual checks passed **50 Model3D tests** (8.283 seconds)
and **30 schema/math tests** (7.731 seconds), zero failures/errors/skips, plus schema/
template parity, real robotic inspection and original source-delivery verification.
`final-summary.json` records the exact tested source hashes. The earlier full
922-test run still applies only to `2d77a38`; final current-head CI and independent
review remain required. The final decoder must merge through its own qualified PR
before this dependent PR becomes eligible; main ancestry, review and CI must then
be reconciled against the actual final head.


The next independent review verified the 50-test candidate and its evidence hashes,
then caught child budget access occurring before child-contract validation in the
new cache lookup. Validation now precedes any child-limit access. Three public CLI
negative cases (missing, null and array limits) require structured schema failures
and prove unchanged inputs. All **51 focused Model3D tests** passed in **10.852
seconds**, no failures/errors/skips; generated schema/template parity also passed.
`latest-summary.json` binds this correction's exact tested source hashes. Earlier
`final-summary.json`, helper summaries and the 922-test full receipt remain scoped
to their explicitly recorded earlier implementations, not this later correction.
Fresh exact-head independent review and native CI are still separate merge gates.


## Two additional published-head findings

The published `14be87a` subsequently passed all 27 native checks, but a new review
correctly found two cases not covered by that green suite: an intermediate asset's
per-file ceiling could be bypassed by a larger transitive payload, and a valid JSON
integer written as `3.0` reached Python `read()` as a float. Independent Astra
reproductions confirmed both; the earlier PASS/CI result is not a final zero-finding
gate for that source.

The local correction propagates inherited file ceilings into snapshot admission,
native inspection and dependency-cache context, and also checks every member of
complete transitive closures. Regression cases cover both cache orders, admission
before opening an oversized descendant, and an exact 100,000-byte boundary.
Validated operational counts are normalized without modifying source manifests or
bytes; real CLI positives cover three-byte and multi-chunk integral-float sizes,
while booleans, fractional, zero, negative and textual counts remain rejected.

All **54 Model3D tests** passed in **13.391 seconds**, and **30 schema/math tests**
passed in **6.564 seconds**, no failures/errors/skips. Generated schema/template
parity, real robotic inspection and original source-delivery verification also
passed. Actual before/after reproductions and exact source/artifact hashes are in
`tests/results/model3d-new-review-threads-20261024/`. These corrections require a
new exact-head independent review, publication lease and fresh native CI. No review
threads have been resolved by the implementation author.
