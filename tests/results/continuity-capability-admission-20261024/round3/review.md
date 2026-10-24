# Independent review round 3: continuity capability admission

**Own-scope verdict: CHANGES_REQUIRED. Overall gate: BLOCKED.**

Exact candidate: `6b2cde05a3b9e7392ec2d9185a90b10f12d61bc2`. Working tree remained clean. This review covers the full own admission logic and the added retained probe/workflow statically; the retained probe was intentionally not executed before the native resource dependency is integrated.

## Repaired findings verified

The prior quadratic duplicate hashing and missing native review gate are repaired. `ReferencePass` hashes a deduplicated closure initially and freshly at completion, observes file changes between checks, and retains final exact-byte checking even when metadata fingerprints are unreliable. Sixteen identical checks now require two full reference passes. Native `needs_visual_review` always supplies the missing visual-approval field and gate.

Whole-admission CPU and wall accounting now includes validation, hashing and final report processing, and complete-budget overrun cannot return successful acceptance. Per-check and aggregate report-size handling remains bounded. Earlier source-binding, integrity, fallback, malformed-response and expiry fixes remain covered. Real geometry remains partial, automated CPU renders remain separate from visual approval, and unavailable production owners are not relabeled as implemented.

## Remaining own finding

**P2 — Execution can start after admission work has already exhausted the complete budget.** In `scripts/continuity_capabilities.py`, the loop checks the complete budget before `_check`, but `_check` then validates owner snapshots, verifies newly introduced probe evidence and obtains native reservation without rechecking the complete meter before `adapter.execute`. Expiry and cancellation have a post-reservation check; the resource deadline needs the same boundary.

Deterministic clock-boundary reproductions cover both new probe verification and native reservation. Each consumes two simulated CPU units against one reserved before the native execute call; the execute call is nevertheless observed at two units. The final result correctly becomes `failed` with `resource-admission`, but the avoidable operation has already started after known exhaustion. These are explicitly clock simulations, not claims of actual large-asset runtime or CPU performance. Check the shared complete budget after probe verification/before reservation and again immediately before execution after reservation. Add regressions proving that the native execute call count stays zero in both cases. This does not demand hard preemption of a synchronous native call that began within its budget.

Evidence: `/home/complete/.cache/hatch-fleet/review115/adversarial6b2c.py` and adjacent `.json`.

## Actual validation

```text
cwd: /tmp/hatch-continuity115
/home/complete/.cache/hatch-fleet/rig-py312/bin/python -m unittest discover -s tests -p test_rig_continuity_capabilities.py -v
45 tests in 12.440 seconds: OK, no failures/errors/skips.
/home/complete/.cache/hatch-fleet/rig-py312/bin/python /home/complete/.cache/hatch-fleet/review115/adversarial6b2c.py
Exit 0: both deterministic boundary reproductions completed and observed the finding.
```

The reviewer used Python 3.12.15 and the same pinned in-process environment as prior rounds. No backend child processes, CLI subprocess tests, network/GitHub writes, implementation edits or native application probes were performed. The known unresolved native memory issue was not rerun.

Exact source/artifact hashes are in `/home/complete/.cache/hatch-fleet/review115/evidence6b2c.json`:

- admission source: `fc5e062138d4c4fb832871327f769c1041809da7574d66547d3bb30d13ed9360`
- native owners: `de856c591bb97f33774767d3ce7c6089db1be5c16c2839f1eef7fb0f98d43433`
- focused tests: `9f4963e9bd518b500fe33f709d229b72dd81122e368e62d0b9ad21793feca09f`
- retained probe source: `281a4134be28365f39ceb64cff1b5b752e0cf45ff239d3ef15b7bf27a1d7e1e4`
- focused log: `81494e6ac062ef39962684039264c3106737f76952c64cf2e6b29965a3f8db00`
- adversarial output: `378a5cba837d4748d80bbb23ed47ded3f958f1ffd2b22775691144cad78e8ce2`

All earlier reports, repros and failure history remain preserved. Runtime may be unfrozen after this report for the scoped budget-boundary repair.

## Blocking dependencies and final acceptance

The original native memory/decompression reservation finding remains a real blocker until the authoritative native loader helper is approved and consumed. Final #113 typed-reference fixes likewise require their approved normal merge and API adoption. Final exact-integrated-head review must rerun meaningful checks, execute and inspect retained native probe evidence, and include slot-authorized legacy/CLI compatibility and required CI results. No overall PASS or production continuity/GUI/physics/video/visual approval is granted here.
