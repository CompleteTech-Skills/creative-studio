# Continuity foundation validation

The frozen execution at b7e0b13 (before the shared decoder integration) used
Python 3.12.15 and pinned public-delivery
dependencies. `regression.log` records 76 passing tests in 25.656 seconds with no
failures, errors or skips: 28 new contract/lifecycle cases plus existing
production-v1, optional-dependency/atlas/catalog compatibility and retained
production-pilot cases. The compatibility suite discovered 830 ordinary 2D tests;
it did not execute all 830. Public pilot cases exercised actual source/project
loading and existing provenance, timing, review and continuity protections.

`verification.json` hashes every script, all new generated schemas and focused
test/helper files before and after the frozen run, and checks protected original
inputs against base `a08569c`. It binds the raw log and records limitations. These
results describe those input hashes, not a future modified head. Generated-schema
parity, actual structured CLI validation, and exact native fixture byte
verification also passed. Retained fixture files are a 71-byte OBJ and 14-byte
PPM: this establishes exact bytes, not geometry inspection or rendered-reference
approval. Uninspected geometry statistics and bounds remain explicitly null.

The original reviewed commit 63f5810 had 69 passing tests, but independent Astra
reviewers exposed five security/numerical and three acceptance/representability
findings. Their correction adds real two-connection admission/CAS races,
project-source freshness, receipt-to-FIFO replacement, all-23-model integral-float
coverage, native asset/dependency migration closure, unavailable measurements and
cross-identity/index-integrity regressions. Those 76-test results are fresh after the continuity fixes, before the separately
reviewed shared decoder integration. `history-63f5810` preserves the exact old log/receipt; its internal paths
refer to that historical commit, not today's sibling files. Earlier uncommitted
development logs remain session-local under `/tmp/continuity113-*`.

Tests simulate explicitly labelled supplied review decisions only to exercise
promotion/rejection. They do not establish actual assistant/human visual
inspection, production approval, changed-asset compatibility, or full continuity
acceptance. Real asset inspection, event replay, rendered comparisons, native
review-adapter execution, scene integration and final production demonstrations
remain separate work. Windows/macOS execution, published-head CI and fresh
independent review remain gates; no cross-platform execution or new PASS is
claimed by this report.

After normal-merging independently reviewed shared decoder ffe872d, clean
committed source a5c6b24 ran all 28 focused continuity tests again in 4.228s,
zero skips/errors/failures. `post-helper-focused.log` and
`post-helper-verification.json` bind that exact committed code and confirm an
actual 5000-digit integer CLI input returns structured exit 2 without traceback.
The current standalone schema parity check passes. This targeted rerun is not
a claim that all 76 legacy-inclusive cases reran after the helper change. The
shared-helper's own retained tests and independent Astra report are separate
evidence. Any further source head needs fresh independent review/current-headCI.

A final narrow programmatic-input defect was confirmed at 7f5d19e: recursive
copying preceded the existing depth bound. Source e6a8205 checks the canonical
JSON/depth bound first. The new 1,500-level input regression verifies a typed
RIG_JSON_DEPTH error and unchanged caller data. `depth-guard-focused.log` and
`depth-guard-verification.json` bind 29 passing focused tests in 4.422s and schema
parity to that exact source, with no failures/errors/skips. The older 28-case and
76-case outputs retain their original source bindings; none is relabelled as a
later broad execution.

The final shared decoder candidate cf15e97 adds bounded UTF-8 byte accounting.
It was normal-merged under coordinator authorization. Committed source 96552d1
passed all 29 continuity tests in 4.231s without failures/errors/skips, schema
parity, a real configured UTF-8 byte-limit negative and the actual huge-integer
CLI error case. `final-decoder-focused.log` and `final-decoder-verification.json`
bind this source and log. Older outputs remain historical; no larger-suite rerun
or fresh independent PASS is inferred from these targeted integration checks.

Security review of 744bc40 found one remaining manifest-read race: a regular
manifest could become a FIFO between is_file and path.open. The loader now uses
the same bounded nofollow/nonblocking descriptor reader as retained receipts,
including pre-open type/size checks and opened-handle/final-path identity checks.
Two real syscall-boundary regressions cover FIFO and regular-file substitution.
`manifest-reader-focused.log` and its verification receipt bind 31 passing
focused cases in 4.862s to the exact committed fix; no failures/errors/skips and
schema parity passes. Earlier outputs keep their original code bindings.

Reviewed shared decoder/provenance candidate 91d14b8 was normal-merged without
rewriting any runtime or test source blobs. The additive attributes preserve both
the scoped decoder rules and recursive receipt/example/test-source rules, plus
frozen playback/catalog LF pins. Source ddbdcc1 passed all 31 focused cases in
5.193s without failures/errors/skips, and schema parity passed.

`final-provenance-crlf-proof.json` records an actual fresh Git checkout configured
with core.autocrlf=true. README genuinely became CRLF, proving conversion was
active. All 114 receipt-bound sources and 32 protected nested evidence, fixture,
test and frozen-contract files instead retained exact Git bytes. Historical
69/76 execution logs match their original Git objects; the checkout is clean.
The real CRLF checkout also passed schema parity and actual fixture CLI
validation. `final-provenance-verification.json` binds that proof and focused log.
This tests Git byte preservation on Linux, not native Windows execution. Earlier
results retain their historical source bindings; no broader rerun is implied.
