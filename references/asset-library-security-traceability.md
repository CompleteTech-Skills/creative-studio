# Asset library security and acceptance traceability

This is the working release checklist for issue #152 and parent #128, not a
completion certificate. Parent sections 23–27 and its continuation sections
28–34 remain authoritative. A test's presence proves source coverage only;
only a retained execution tied to the candidate revision establishes a result.
A passing subsystem cannot replace the mandatory two-scene export, restoration,
and rendering demonstration. Fixture results do not establish artistic review,
external provider execution, license permission, or production approval.

## Revision and evidence boundary

This inventory was captured on 2026-11-13 while dependency branches were still
changing. The #152 work started from main `30a5f22`. The following are isolated
candidate snapshots, not an assertion that all are integrated into main.

| Owner / requirement | Source snapshot inspected | Executed evidence available | Remaining boundary |
|---|---|---|---|
| #138 foundation | `asset138` / `404a1cd` | `asset/foundation138/handoff.json`: 25 focused, 5 compatibility; actual register/reopen/retrieve receipt | Final integrated head and hosted gates |
| #139 import | `asset-import` / `e099534` | `asset/import280-recovery-review.md`: independent 23 tests, zero skips, Blender 4.5.3 LTS and pinned Khronos; 14 math tests | Hosted and aggregate integration; native decoder is not a malicious-file sandbox |
| #140 technical validation | `asset-validation140` / `7fe12db` | `asset/validation140-recovery-final.log`: 14 tests passed | Full corrected-head independent review and integration |
| #141 durable storage | `asset-storage141` / `42bbae1` | `asset/storage141-42bbae1-review.json`; downstream search repeated all asset tests | Final whole-program integration; Linux execution does not prove Windows execution |
| #142 lifecycle | `asset-lifecycle142` / `80c827e` | `asset/lifecycle142-handoff.json`: earlier 56 integrated tests; final source review recorded | Exact-head rerun/review completion and actual search lifecycle integration |
| #143 dependency graph | `asset-deps143` / `ea2d3e5` | `asset/deps143-recovery-review.md`: morph-delta reproduction retained, corrected source reviewed | Corrected native unit-preservation reproduction was still pending in that report |
| #144 search | `asset-search` / `a10709b` | `asset/search144/handoff.json`, `recovery-review.md/json`: 69 tests, zero skips independently repeated; real OBJ demo | Actual lifecycle integration; optional real embeddings; hosted gates |
| #145 previews | `asset-preview145` / `eac7438` | `asset/preview145/handoff.json`: 11 native tests zero skips at `16d4976`, five retained cache configurations; final source review | Exact integrated native rerun and hosted gates; CPU Cycles only |
| #146 browser | `asset-browser146` / `1c1128f` | Source tests inspected; initial browser log is not a final result | Real browser execution and complete interaction/security coverage |
| #147 export | `asset-exports147` / `445c276` | `asset/exports147/unit.log`, `native.log`, `review.md` exist | Read exact outcomes/revisions before claiming final export acceptance |
| #148 packages | `asset-packages148` / `477f070` | Source package tests and initial log inspected | Final execution, adversarial archive coverage, integration |
| #149 reuse | `asset-reuse149` / `54cb155` | Source owner is active | Two-scene actual integration and selective invalidation evidence |
| #150 jobs | `asset-operations150` / `dc5f9fb` | Source job tests inspected | Final tests, adapter/API integration and recovery evidence |
| #151 demonstration | `asset-acceptance151` / `986ccd9` | Fixture setup log exists | Complete release-blocking stages A–G and negative repair, with actual artifacts |

Evidence paths above are relative to the private coordination evidence root,
`/home/complete/.cache/hatch-coordination-20261112`. They are local audit records,
not portable distributed artifacts. Final release evidence must be retained in
CI artifacts or an appropriate repository evidence directory with inventories,
hashes and commands. Never replace revision-bound logs with later source claims.

## Additional security package coverage

`tests/test_rig_asset_security.py` sends six malformed accessor/buffer-range
variants and ten remote, encoded and platform-specific resource URI variants
through the real import pipeline, then verifies an empty canonical registry and
unchanged original SHA-256 values. An actual symlinked dependency control also
fails before publication. The initial Linux run passed all three test methods
without skips (`asset/security152-first.log`); exact final-head and hosted runs
remain required. A Windows account unable to create a symlink reports that
specific platform limit; POSIX symlink failures cannot skip.

`.github/workflows/asset-security.yml` adds Python 3.12 Linux/macOS/Windows
execution of these negatives, existing asset tests and schema consistency. It
uses separately owned temporary directories and retains logs. The workflow's
presence is not an executed hosted result. It does not satisfy the separate
mandatory rendered Blender demonstration or real browser release gates.

## Resource and operational matrix — parent section 23

| Requirement | Existing source authority / tests | Status and additional proof required |
|---|---|---|
| Individual/total bytes, manifest bytes, source file count | `asset_library`, import limits; `test_asset_library.test_quota_failure_rolls_back_blobs_and_registration`; import `test_structural_json_and_obj_records_are_bounded_before_decode` | Source and import execution exist; exercise limits across package/jobs/preview on integrated head |
| Expanded archive bytes, entry count, compression ratio | Shared archive authority; package `test_package_symlinks_hardlinks_case_alias_and_oversize_rejected` | Full malicious ZIP fixtures and final integrated execution required |
| Dependency count/depth and cycles | `DependencyGraph`; `test_two_versions_in_runtime_closure_reject_and_limits_reject`, `test_self_cycle_and_untrusted_nested_requirements_fail_structurally`; validation deep prefab tests | Corrected full-head execution required; cached children must not bypass depth |
| Vertices/triangles/materials/bones/channels/keys/texture pixels | Native geometry reader and technical validator; `test_tightened_resource_bounds_and_invalid_requirements_fail`; search `test_resource_budgets_reject_partially_measured_payloads` | Recorded metadata filters are not measured geometry validation; integrated native limits required |
| Preview resolution/duration/sample memory | `PreviewService`; `test_strict_settings_and_rational_times`, `test_sample_memory_and_inspection_edges_are_bounded` | Native preview execution retained; final aggregate rerun required |
| CPU/runtime, RAM estimates, worker slots | Import subprocess timeout and resource launcher; job `test_limits_paths_bad_data_and_runtime`, `test_resource_slots_release_on_worker_exit_and_bound_concurrency` | CPU/OS restrictions and GPU estimates need precise supported/unavailable reporting; no universal resource sandbox claim |
| Indexed pagination/lazy loading | `SearchIndex`; stable cursor, catalog refresh, source invalidation, index relocation tests; browser selected-asset route | Search executed at `a10709b`; real browser memory/network behavior still needs evidence |
| Derived caches and exact invalidation | Preview corruption/cache tests; search annotation/vector/query identity tests; job idempotency | Targeted project invalidation still depends on #149 |
| Two-stage reachable-content GC | Storage `test_gc_retains_every_version_dependency_source_and_staging_file`, stale plan and alias tests | Integrated plan/apply must preserve all pinned versions, sources, jobs, packages and rollback retention; no filename-only cleanup |
| Stream/header preflight before decode | Bounded descriptor reader, import structural preflight, image/model parser owners | Add integrated large-header/oversized-before-allocation negatives where absent |

## Untrusted-input matrix — parent section 24

| Threat | Existing source/test location | Acceptance boundary / gap |
|---|---|---|
| Traversal, absolute/drive/mixed separator paths | Foundation semantic negatives and portable schema/runtime parity; importer unsafe dependency tests; package archive guard | Run same vectors through all public import/export/package/job/browser endpoints |
| Symlink, hardlink, junction escape | Foundation source/root/database tests; ancestor symlink import test; storage GC aliases; package aliases | Actual Windows junction execution remains required; Linux symlink tests are not Windows proof |
| Unicode/case/file-directory collisions | Foundation alias and portable path tests; importer `test_corrupt_glb_and_case_aliases`, `test_source_file_directory_prefix_alias_never_publishes` | Archive/index/browser paths need shared normalization and retained negative vectors |
| Duplicate/corrupt ZIP entries, bombs, ratio exhaustion | Shared existing archive checker and #148 package tests | Integrated archive negatives must prove no destination publication; add any missing vectors |
| Oversized/duplicate/invalid JSON and metadata | Contract canonical JSON/unknown-field checks, integral-count/surrogate tests; import predecode bounds | Include nested depth, duplicate fields, nonfinite numbers and errors through CLI/API |
| Excessive scene hierarchy recursion | Technical validator `test_prefab_deep_hierarchy_is_rejected_without_python_recursion`, cached-child depth test | Exact final head execution needed; no raw RecursionError acceptance |
| Malformed glTF accessors/offsets/indices | Native glTF authority; technical validation `test_invalid_mesh_indices_animation_targets_and_nonfinite_reject`; importer corruption tests | Dedicated accessor buffer range/stride/sparse cases must be executed through asset import, not parser mocks |
| External URI network/local disclosure | Import missing/unsafe dependencies and required-extension tests; source snapshot descriptors | Test HTTP/file/data policy, percent encodings and remote image buffers before subprocess/browser loading |
| Embedded native scripts/plugins/shaders | Fixed import worker; `test_untrusted_blend_and_unavailable_tool`, rejected unsupported native controls/materials | Trusted packed Blender boundary is explicit. No arbitrary malicious decoder/network sandbox claim |
| Unrequested Blender outputs | `test_native_compositor_cannot_publish_unrequested_files`; fixed script and isolated directory | Add retained script-execution/network isolation controls if claiming untrusted native-file support |
| Native timeout/cancellation | `test_actual_subprocess_timeout`, `test_cancellation_leaves_library_empty`; job cancellation and process-exit tests | Kill/cleanup and preservation must be checked on supported OSes |
| Source mutation and promotion races | Source snapshots, exclusive publication; export racing destination tests; storage lock/CAS/interrupted commit tests | Verify no public partial outputs, replacement of prior valid files or silent stale-handle fallback |
| SQL/FTS injection | Search `test_hostile_inputs_never_enter_sql_or_fts_syntax`; bound SQL parameters | Executed search test covers FTS; retain other SQL entry-point negatives |
| Browser XSS/HTML injection | Browser `test_untrusted_names_are_only_text_and_no_unlisted_asset_files` | Real browser must show harmless text, no executed payload, no unexpected requests |
| Browser origins/auth/serving/upload | Browser `test_auth_origins_routes_payload_and_path_boundaries`; loopback service owner | Verify CSP, local-only binding, auth/origin failures, controlled drops/uploads and safe resources in real browser |
| Unsafe temp roots | Existing descriptor/private-root checks; all native jobs use owned staging | Tests must set owned TMPDIR/TMP/TEMP; no shared temporary-tree deletion |

## Persistence, migration and package matrix — sections 25 and 27

| Requirement | Existing executable coverage | Remaining integrated evidence |
|---|---|---|
| Immutable IDs/versions, hashing/deduplication | Foundation actual bytes/restart/relocation, immutable versions, missing/corrupt payload and duplicate-writer tests | Full revised branch/platform rerun |
| Concurrent reads/writes and annotations | Storage readers-wait, migration waiting-writer/read guards, append-event CAS tests; foundation annotation CAS | Parent storage corrections `42bbae1` must be in every consuming branch |
| Lost/rebuilt index and relocation | Storage canonical records survive total index loss; search derived row corruption and relocation; backup/reopen tests | Jobs, shared libraries and scene package relocation still need aggregate proof |
| Interrupted writes/imports/atomic publication | Storage real process exit; jobs process exit after publication and retained-staging tamper; export races | Full import/export job adapters and exact no-duplicate resume required |
| Lifecycle/promotion/rollback | Lifecycle immutable creation, concurrent first-version, stale promotion, direct-registry snapshot, deprecate and rollback tests | Actual search collections and project integration remain pending |
| Migration preview/backup/source hashes/unknown schema | Lifecycle actual legacy-profile migration, changed/missing/unknown sources; storage legacy upgrade | Each claimed legacy adapter must preserve authoritative 2D bytes and reviews; do not imply all legacy categories migrated |
| Missing/incorrect payload and manifest-only package | #148 missing/corrupt/substitute, missing-license, resealed-index omission tests; native package missing-resource tests | Production-ready resolution/restoration must fail and exact expected blob repair must succeed |
| Multipart completeness | #148 missing/corrupt/reordered/assembled-hash tests; existing production archive authority | Final full dependency inventory and native restored rendering required |
| Restore twice to new absolute paths | #148 complete dependency restore to two destinations and archive roundtrip | Both actual scenes must resolve/render in isolated library environment |
| License/provenance vs integrity | Foundation origin/evidence validation; package license/use/review separation; lifecycle attribution restrictions | Rights/artistic approval are independent decisions, never inferred from hash validity |

## Browser and native interoperability matrix

| Required behavior | Candidate executable coverage | Release blocker still to resolve |
|---|---|---|
| Browser catalog/search/filter/select and metadata | #146 real search/annotation route tests and browser interaction test | Record actual browser commands/results and all mandatory interactions |
| Actual model load/orbit/animation/skeleton/material inspection | #146 real WebGL browser test; existing rig Three.js viewer tests | Must load actual library-returned GLB, not image/DOM mocks |
| Offline, missing assets, WebGL fallback, keyboard | #146 browser test names cover offline/fallback/keyboard; authentication/path unit tests | Final browser run must inspect meaningful diagnostics and absence of remote requests |
| Real GLB/glTF external resources and preservation | #139 external GLB/glTF/texture, axes, eight weights and native roundtrip tests | Full #147 export/reimport and #151 restored rendering |
| Blender save/reopen/render and loss reporting | #139 executed native profile; #145 executed CPU Cycles cache/render controls | Pins and successful subsystem jobs do not establish complete mandatory asset demonstration |
| Optional semantics and formats | Trusted provider gates, pinned local-model adapter; unsupported format errors | Actual semantic runtime unexecuted; other format adapters remain unsupported unless exercised |

## Release-blocking demonstration — sections 28–34

Every row below is mandatory. The #151 owner is responsible for executable
workflow artifacts; #152 records and verifies results without creating a
competing asset registry, renderer, package format or acceptance authority.

| Stage | Required actual proof | Current status |
|---|---|---|
| A: create/import/persist | Character geometry, compatible skeleton, nonempty animation, PBR material and texture, environment geometry, prop, camera/light; stable IDs/versions and valid hashes | #151 fixture construction in progress; no whole-stage result accepted here |
| B: discover/inspect | Search, inspect actual geometry/rig/material, animation preview, environment preview, dependency graph and stored hashes | Isolated search/preview tests exist; integrated demonstration pending |
| C: two scenes | Two different scene documents reuse the same immutable character/animation/material/environment references | #149/#151 integration pending |
| D: actual render | Both scenes evaluate motion and produce multiple actual PNGs, verified count/content/hashes | Release-blocking pending |
| E: export | Real GLB with rig/animation, scene dependency package, complete portable project inventory | Export/package source exists; integrated demonstration pending |
| F: independent restoration | Fresh path, empty/isolated library, original IDs/versions/hashes/closure; both scenes load/evaluate/render | Release-blocking pending |
| G: compare | Topology/counts, rig/animation/material/texture assignments, dependencies and rendered content within declared deterministic tolerances | Release-blocking pending; exact hashes only where demonstrated on pinned backend |
| Negative repair | Remove required payload; identify exact missing asset; incomplete status; block readiness; no fabricated replacement; safe restore failure; repair exact expected blob; succeed afterward | Package unit/native controls exist; mandatory full demonstration still pending |

## CI, documentation and final reporting

Existing `rig-3d.yml` pins checksum-verified Blender 4.5.3 LTS and contains real
rig Blender/browser jobs. Existing model/public-delivery jobs include Linux,
Windows and macOS matrices. These are shared infrastructure, not proof that the
new complete asset workflow has passed. The #152 workflow must run the mandatory
asset demonstration and real asset browser tests without converting mandatory
capability failures to skips or `continue-on-error`. Retain evidence on failure.

The final evidence record must include Python, OS, Blender, glTF tooling,
browser/Three.js versions; renderer/backend/device; relevant environment;
fixture classification; commands/results/skips; candidate commit; complete
artifact inventory and hashes. Native tasks serialize with the coordination
lock and use owned temporary directories. Reproduce in CI before declaring
complete; local missing optional capabilities do not waive the mandatory proof.

Documentation discovery must cover architecture/schema/storage/import/export,
Blender, CLI/Python/HTTP where implemented, browsing/search/versioning/dependency,
license/provenance, packages/migration/security/limits/repair/offline,
scene-renderer integration, demo and known limits. Update README and SKILL only
for exercised behavior. Do not describe extension points as completed features.

Run unchanged legacy regressions on the final integrated head. Discovery-only
compatibility reports (952/953/984 tests in earlier receipts) are not full suite
execution. Preserve and classify historical missing production content,
optional skips, and new regressions separately. Existing independent review
history, source receipts, state/style/profile and package contracts remain
binding. No test disabling or fabricated originals may remove a release blocker.

Final completion requires all parent section 31 functionality, packaging,
compatibility, end-to-end and engineering criteria, the 18 deliverable classes
in section 32, all section 33 prohibitions, and section 34's actual user workflow.
Each row must gain a final revision-bound result or remain explicitly incomplete.
