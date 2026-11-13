# Independent follow-up review: #115 admission repairs

**Own-scope verdict: CHANGES_REQUIRED. Overall delivery gate: BLOCKED.**

Reviewed runtime/test candidate: `853d5202e05a807230da724ca02f16214f8ae4b0`. During the review, the owner added retained-probe/workflow artifacts at `7e550fce8382db14854063a082dfc8aa44b1f999`; the three reviewed runtime/test files remain byte-identical to `853d520`. This review does not approve those additional artifacts or a later integrated head.

## Confirmed repairs

The candidate repairs five original admission defects with real regressions: alternate rig filenames are rejected; changed project/request and earlier retained evidence invalidates integrity; GUI/video/historical fallback fields remain unavailable; malformed and oversized native/check reports become structured failures; expiry is checked per operation and after reservation. The complete native memory resource defect remains deliberately unresolved pending the authoritative loader integration.

The candidate also retains used snapshots and safe host policy, explicitly reports missing original source hierarchy and native unsupported channels, accepts unknown backend versions only for unavailable/untested states, uses schema-directed pointer traversal, and checks owner CPU as well as conservative wall time. Real native geometry remains partial, with no direct GLB parser or visual approval claim.

## New findings

1. **P2 — Integrity verification is quadratic and outside the admission CPU/deadline budget.** `scripts/continuity_capabilities.py:264,280–286,295–297,359–378`. The complete request is rehashed repeatedly; after each check every preceding evidence list and snapshot evidence list is separately rehashed. Reused pointers are not coalesced across these calls. Actual small-fixture instrumentation measured **41/113/353 verifier calls for 4/8/16 checks**, respectively. Total observed CPU was 0.071/0.152/0.344 seconds, while reported operation CPU was only 0.0012/0.0025/0.0053 seconds. These are small-fixture measurements, not a large-asset benchmark. Large probe artifacts magnify this into quadratic I/O/CPU, and the cost is outside the resource enforcement interval. A separate deterministic clock-boundary simulation (explicitly not actual CPU execution evidence) assigns one CPU unit per verification: a single check uses eight units against one reserved, returns `supported_fallback`, and reports one unit. Budget all admission work, including verification, snapshot/response validation and final reporting; deduplicate the exact reference closure and avoid rehashing the same retained artifact once per preceding result. Preserve final exact-byte integrity and changed-reference detection without weakening them.

2. **P2 — Native review-required outcome can omit its unsatisfied review field/gate.** `scripts/continuity_capabilities.py:413–415`. The added `status == 'needs_visual_review'` disjunct is defeated by the following `status in ('executed', 'supported_fallback', 'partial')` condition. A valid native `visual-reference` response returning `needs_visual_review` and all automated fields yields `missing_fields=[]` and `unsatisfied_gates=[]`. The status and top-level `visual_approval=false` do preserve nonapproval, but the promised precise missing fields/gates are absent. Normalize all native review-required outcomes to include `visual-approval`, irrespective of the request flag, and add a targeted regression.

## Actual verification

No implementation edits, network/GitHub writes, subprocess/backend children, or native application probes were performed. The existing documented resource hole was not rerun against this known-incomplete candidate.

```text
cwd: /tmp/hatch-continuity115
/home/complete/.cache/hatch-fleet/rig-py312/bin/python -m unittest discover -s tests -p test_rig_continuity_capabilities.py -v
41 tests in 14.084 seconds: OK, zero failures/errors/skips.
/home/complete/.cache/hatch-fleet/rig-py312/bin/python /home/complete/.cache/hatch-fleet/review115/adversarial853d.py
Exit 0: reproduction completed; both findings observed.
```

Environment: Python 3.12.15, NumPy 2.3.5, Pillow 12.3.0, jsonschema metadata 4.25.1. Complete logs and reproduction script/output are under `/home/complete/.cache/hatch-fleet/review115/`. `evidence853d.json` retains source and evidence SHA-256 values.

- admission source: `c63a20a9c11cd19a8a339db3835b2a1d4da99be867170659886e762addc0523a`
- native owner source: `de856c591bb97f33774767d3ce7c6089db1be5c16c2839f1eef7fb0f98d43433`
- focused tests: `899979dd8ebac6cffbe7efa5379ea3beeca33d2caf761a98449bf4c2f309a88c`
- `focused853d.log`: `be67b3ab58fc755b68cdc500858b9cc2e9c2f558859fa303150edb97630247f1`
- `adversarial853d.json`: `ccfdc53c6d4ddc2787145735ea8afc6ca929f0abc3ac57ac471504b780e3e724`

Original 53a4 review/repros and historical failure logs remain untouched. Runtime unfreeze is safe after this report; further repairs need a new review.

## Remaining gates

The authoritative native memory/resource loader helper and final #113 typed-reference fixes must be independently approved, normally merged, and actually used by #115. Fresh exact-head verification must then cover all changes and retained native probe/CI artifacts. Legacy/CLI child-process validation remains governed by the root's shared slot and is not newly claimed here. No unavailable physics, Blender, GUI, production renderer, LLM, historical restoration, or visual approval becomes complete through this admission review.
