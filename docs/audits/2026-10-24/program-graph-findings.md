# Continuity, Asset Library and Physics reconciliation

Scope: 84 individual phase/task/bug issues above #106, excluding original parent missions #107/#128/#153 assigned separately to the coordinator. Complete issue text and inventoried comments read; native dependency/child relations compared. No GitHub mutations by this reviewer.

## Ownership and duplication

- Continuity #107 → phases #108–111 → tasks #112–127. Existing `production_performance.validate_continuity` owns legacy 2D shot boundary checks. #191/#194 fixes only repeated actor identity. #118 owns persistent logical prop transfers; Physics #174 owns actual grab/release dynamics; Animation #71 owns authored coordinated interactions. These are shared interfaces, not duplicate scopes. Preserve all three requirements and require one persistent entity/event identity across them.
- Asset Library #128 → phases #129–136 → tasks #137–152. #138/#207 owns initial immutable file registry/resolver; #141 expands recovery/index/GC, #142 expands lifecycle/multi-root scope. Do not ask those issues to build another registry. Header recognition is not #139 import validation or #140 geometry acceptance.
- Physics #153 → stages #154–165 → tasks #166–189. #167/#208 only validates contracts. #168/#212 adds bounded real rigid-body workers; #169/#213 adds exact timing. Neither completes #170 generalized dynamics or any later nonrigid category. Existing rig IK, authored sway and raster effects cannot count as physical simulation.
- #195/#198 and #197/#199 fixed different publication points and share the exclusive rename primitive. Preserve both historical bug records; neither duplicates the general future storage task #141.

## Dependencies and sequencing

All explicit leaf dependencies in the assigned range match native `blocked_by` records. Ten additional native phase prerequisites exist without a dedicated dependency paragraph in their bodies: #109→#108, #110→#109, #111→#110; #130→#129 through #136→#135. They are consistent phase integration dependencies, not spurious edges. `dependency-mismatches-b.json` is a machine comparison, not a request to remove these edges.

Immediate foundation streams can proceed independently: bounded Asset Library #138/#207; Continuity #113/#209 after fixing existing review findings; Scene3D #59/#210 under another reviewer. Physics #168/#212 waits for actual #210 merge and exact integrated verification; Physics #169/#213 waits for #212 and needs mandatory 2D test isolation repaired. A local stacked branch does not satisfy an actual predecessor merge gate.

After foundations, follow leaf-level edges rather than treating every phase as a mandatory serial implementation lane. Asset export #147 explicitly needs #140 and #142, not UI completion #146. Physics soft body #176 and particles #178 branch from #171/#169, while gas/fire #179 branches from #168/#169. Cloth #175 explicitly needs character interaction #174; hair #177 needs #173/#169. These different prerequisites are deliberate; do not infer numeric-order edges.

Safety/cache support must accompany each solver. #184/#187 complete the shared lifecycle and broad diagnostics; they cannot justify postponing essential crash/resource/output bounds while implementing earlier solver categories. No cycles found in the inspected assigned leaf/native relations. Full repository cross-program cycle analysis remains coordinator-owned.

## Evidence and current review blockers

- PR #207 `1a210164a5698a1149995df580603fdec46df83a`: 24 focused tests passed freshly on Windows Python3.14.3/jsonschema4.25.1. Initial command without `PYTHONPATH=scripts` failed import and was retained. Ready for final bounded-scope review, not a broad library/production approval.
- PR #209 `d92fc03351a84fd40f9719d7a1cc170e7a868c78`: all three existing unresolved native review findings independently reproduced. Schema-valid dictionaries trigger wrong bounds/timestamp semantics; shared revision pins repeatedly hash identical dependencies (100 pins →201 verification calls each registration/read); corrupt old receipt prevents switching to a valid destination revision. Requires changes. Local suite: 32 attempted, 30 passed, 2 errors from missing Windows symlink privilege, not demonstrated runtime regressions.
- PR #212 `6ef18445dbb7ae852974bb092d594ef6d73eeed1`: conflicts and actual #210 predecessor hold. POSIX-only backend/cache implementation; local Windows full suite is unqualified (48 attempted, 8 failures, 3 errors, 32 skips, 5 passes), mostly platform admission expected-code mismatch. Probe says Blender unavailable. Native Linux results were read from retained evidence/CI, not rerun here.
- PR #213 `5cbd2e352021ef760588df56b08a697251df5334`: failing mandatory delivery/2D checks. CI job120793309919 run40067362103 identifies unconditional retained-cache test importing NumPy through `_source_admission`; 907 tests,1 error,5 skips. Preserve pure math in core; move actual cache/owner checks to explicit native dependency gate and rerun. Three local pure timing/quaternion/stability tests pass. #210/#212 actual merges and conflict reconciliation remain prerequisites.

## Coverage limits

Issue original parent missions #107/#128/#153 and their giant prompt continuations are explicitly coordinator-owned, not claimed reviewed here. All 84 other assigned issue texts and comments were read individually. Issue ledger includes scoped code evidence; historical test transcripts are attributed historical evidence, not reexecution. PR complete file inventories were reconciled and runtime/interfaces/tests reviewed; generated evidence and inherited large stacks were inventoried and inspected by provenance/contract and targeted review, not every artifact independently reproduced. No native solver rerun or visual production approval was performed. No clean whole-tree approval is issued. All substantive limitations are also in `pr-ledger-b.json`.
