# Continuity mission #107: complete source and decomposition audit

Audited 2026-10-24. Source: retained `raw/issues.json` and `raw/comments.json`; code spot-check: default-branch commit `bb5a6810afd06cf57185ffdfe2234e0fdc6e6240`. This is a read-only semantic audit, not implementation, a new acceptance receipt, or a fresh full test run.

The complete 63,256-character issue body and all three comments were read without truncation: continuation **6333904743** (29,644 characters), scope/fallback addendum **6333904978** (11,525), and reopening correction **6349645995** (802). Total: **105,227 characters**. Every #108–127 body was also read completely. `parent-107-coverage.json` records source hashes, read ranges, section mapping and findings.

## Decision

Keep #107 open. Keep phases #108–111 and implementation tasks #113–127 open. #112 is a completed, bounded baseline audit; its completion does not deliver persistent 3D continuity. The final reopening comment explicitly corrects an accidental closure by PR #190 and preserves all mission requirements. Do not use a closing-keyword phrase referring to #107 in a foundation PR, even in a negated sentence.

The decomposition preserves the full mission **by explicit incorporation**, and every original numbered section has a plausible task owner. It is a coherent delivery hierarchy, not a self-contained replacement for the original specification: each roughly 1,400-character task necessarily omits numerous detailed acceptance cases. Closing all children based only on their short checklists would not establish parent acceptance. #127 must retain a granular requirement-to-code/test/evidence matrix against the original body, continuation and addendum.

The central distinction is sound: P0 foundations, P1 structural persistence, P2 evaluated 3D and visual evidence, P3 actual selective production repair and final acceptance. No phase should claim stronger evidence than it executes. The fallback addendum supplements rather than weakens the 38-section mission.

## Complete section crosswalk

| Original section | Accountable tasks | Preservation and acceptance implications |
|---|---|---|
| 1 Mission | #112, #126, #127 | Persistent executable state across full productions; final integration cannot be replaced by architecture prose. |
| 2 Reconnaissance | #112 | Completed audit is a historical baseline; refresh changed code, branch ownership and evidence before new work. |
| 3 Principles | #113, #114, #116, #122, #123, #127 | Identity, immutable revisions, evaluated truth, real visuals, determinism, scoped exceptions and legacy isolation remain mandatory. |
| 4 Architecture | #112, #113, #115, #125 | Twelve logical responsibilities do not require twelve services. Reuse existing Python authorities. |
| 5 Identity/revisions | #113, #114 | Logical identity differs from world entity, asset revision and render object. Preserve lineage, drafts, promotion, pinning, deduplication and selected migration. |
| 6 Geometry/materials | #114, #122 | Inspect actual geometry, measurements, UVs, shader inputs and textures; hashes/counts alone do not prove appearance. |
| 7 Rig/animation compatibility | #114, #120 | Exact/retargetable/partial/incompatible/unverified states require bind/rest/skin evidence, validated maps, and facial/contact handling. |
| 8 Costumes/accessories | #117, #119, #122 | Persistent equipped state, versions, attachments and authored offscreen changes; visual checks remain separate. |
| 9 World/scene state | #113, #116, #117, #120 | Explicit units, scene mappings, entity identities and appropriate world/timeline scope; never pixel-to-meter relabeling. |
| 10 Movement/presence | #117, #120 | Occlusion is not absence; preserve history and evaluate geometry/relationships. |
| 11 Cameras | #121, #122 | Real projection, eyelines, axis and framing, with context-sensitive exceptions and perspective-aware identity comparison. |
| 12 Pose/motion | #116, #120 | Terminal N differs from displayed N−1; preserve pose, phase, velocity, contacts and simulation provenance. |
| 13 Props/handoffs | #118, #120 | Owner differs from attachment; exactly-once persistent transfers require evaluated contact before physical acceptance. |
| 14 Environment/look | #117, #121, #122 | World light/weather state differs from camera grading; rendered evidence is needed for appearance. |
| 15 Narrative | #116, #117, #119 | Scoped facts, uncertainty, prerequisites and chronology; branches/merges need explicit supported semantics. |
| 16 Time/snapshots | #116 | Exact rational timing, event sides, replay, cache consistency and histories. |
| 17 Rules/exceptions | #119 | Versioned catalog, deterministic IDs, contextual tolerances and scoped authorized waivers. |
| 18 Validation layers | #119, #120, #121, #122, #123 | Layers A–H span several phases; P1 cannot claim unavailable motion/visual/review checks passed. |
| 19 Real inspection/visuals | #114, #122, #123 | Exact-source receipts, controlled references, actual shot frames, uncertainty and independent visual judgment; false-metadata negative case required. |
| 20 Impact graph | #124, #123 | Fine-grained causal dependencies and separately classified impacts; unchanged files may still have stale inherited state. |
| 21 Repairs | #124, #125 | Least-destructive typed plan, expected revision, authority, atomic execution, rollback and idempotence. |
| 22 Lifecycle | #119, #123, #125 | Lifecycle differs from operation outcome; preserve history, concurrency control and publication gates. |
| 23 Integration | #112, #115, #120–125 | Consume authoritative asset/rig/scene/camera/look/render/editor/Director APIs; missing GUI permits tested adapters, not a completed-GUI claim. |
| 24 Schemas/portability | #113, #125, #127 | Strict finite bounded versioned contracts, safe paths, canonical hashes, nonlossy opt-in migration and portable restoration. |
| 25 API/CLI | #125, #115 | All listed capabilities, bounded pagination, progress/cancellation, stable errors and authorization where networked. |
| 26 Reports | #119, #125, #127 | Stable schema, IDs, frames, units, expected/actual, cause, evidence/hash, provenance and resolution history. |
| 27 Reviews | #123 | Reuse native review store; bind reviewer kind and actual inspected evidence; preserve partial staleness. |
| 28 Security/performance/recovery | #115, #124, #125, #127 | Untrusted files, budgets, canceled/interrupted jobs, checkpoints, reproducibility and safe rollback throughout development. |
| 29 Real demonstration | #126 | At least three actual sequential shots across two scenes; procedural genuine meshes allowed; fourth panel-activation shot is additional coverage. |
| 30 Fifteen deliberate defects | #119–124, #126 | Costume, owner, position, orientation, geometry, marking, rig, motion, camera, light, missing dependency, stale review, prerequisite, isolation and exception. All remain required. |
| 31 Tests | #112–127 | Behavioral unit/integration/negative/property and cross-platform coverage belongs to each owning task; #127 consolidates it. |
| 32 Selective repair demonstration | #124, #126 | Actual affected-frame regeneration plus unaffected-byte reuse, counts and review dimensions; an impact report alone is insufficient. |
| 33 Documentation | #125, #127 | Architecture/contracts/operations/API/validation/recovery/demo documentation must reflect executed behavior. |
| 34 Execution phases | #108–111, #112–127 | Four delivery phases refine ten implementation steps without changing final acceptance. Essential architectural tests occur early. |
| 35 Deliverables | #113–127 | All twenty deliverables remain in scope, including actual artifacts, correction/recovery and machine/human reports. |
| 36 Acceptance | #127 | Every category requires traceable executed evidence; incomplete capabilities remain unsatisfied. |
| 37 Final report | #127 | Exact commands/counts/versions, scene/asset IDs, reviews, hashes, reuse/rerender counts and limitations. |
| 38 Directives/ultimate definition | #108–111, #127 | Real working vertical slices, preserved authority, explicit limitations, no invented approvals, end-to-end persistent continuity. |

## Clarifications needed before task closure

1. **P1 branch breadth:** #116 explicitly includes timeline branches and conflict-aware merges while the addendum bounds P1 to two characters, one prop, two locations and three shots. Treat a validated single branch plus explicit rejection of unsupported branch operations as a bounded MVP only. Full nonlinear behavior remains open for P3 until tested. Do not delete the original branch/merge requirement or claim a narrow MVP satisfies it.
2. **Review/graph sequencing:** #123 requires selectively stale review dimensions before #124 delivers the extended transitive graph. Define dependency-reference identity and per-dimension projection contracts in #113; #123 uses the existing native projections and tests its supported scope; #124 extends transitive propagation and reruns #123 acceptance. This avoids silently building a second graph or circular dependency.
3. **Security and concurrency are early invariants:** #127 owns consolidated evidence, not permission to postpone untrusted-file bounds, safe paths, immutable writes and expected-revision checks until the final phase. #113/#114/#115/#124/#125 must enforce their relevant boundaries as implemented.
4. **P0 capability evidence:** #115 can probe existing capabilities before all future adapters exist. Distinguish the probe/receipt contract from each later adapter's executed compatibility proof. A software installation is not a successful backend run.
5. **No umbrella blocking:** #120–122 may use exact tested capabilities supplied by #11–16 or documented bounded adapters. They should not depend on completion of whole unrelated umbrellas. Conversely, unavailable channels stay explicitly unverified.
6. **Logical actor regression:** Preserve distinct world actors when several actors share one immutable model revision. Test same-model handoffs and repeated actors; content deduplication must not deduplicate physical identity.
7. **Detailed acceptance is inherited, not optional:** Keep explicit subcases for facial state, skin/morph/retarget validation, physics-cache replay, safe migration, journal recovery, cancellation, archive aliases and bounded histories in the task/evidence matrix. Their absence from short child checklists is not a scope waiver.
8. **Video and review language:** The original conditional encoder acceptance permits image masters if encoding is unavailable, but does not prove the ultimate silent-video outcome. Record export as incomplete. Likewise, controlled reference candidates cannot become approved references merely by passing automated checks.

## Authority and interface ownership

| Authority | Continuity owns/consumes | Required handoff |
|---|---|---|
| #10 model assets; #128 asset library | Consume canonical asset revisions, bytes, rights and provenance; own logical continuity identity links | Exact immutable reference and manifest/hash purpose; no second general asset library. |
| #11 rigs | Consume skeleton/rest/bind/weights/morphs, FK/deformation and socket results | Exact rig revision, evaluator version, supported channels, unavailable fields and units. |
| #12 scenes | Consume evaluated hierarchy and scene/world mappings | Stable world entity separate from shot/render IDs; complete parenting/constraint provenance. |
| #13 animation | Consume evaluated pose/phase/root/velocity/contact | Exact rational query time converted only at explicit evaluator boundary; terminal side and tolerance. |
| #14 cameras | Consume view/projection, optics and tracking | Calibrated camera snapshot tied to scene/time; unknown calibration cannot produce a pass. |
| #15 look | Consume material/texture/light/HDRI and grading state | Physical world change separate from shot look; color-management and shader limitations recorded. |
| #16 rendering | Submit continuity-bound render work through native jobs | Frame manifests bind snapshot, asset dependencies, backend/configuration and output hashes; actual reuse counts. |
| #17 editor | Expose typed commands, diagnostics, comparisons and revision checks | No replacement editor; GUI acceptance only after actual available UI behavior is exercised. |
| #18 Director | Offer admission, state queries, rules and authorized repair plans | Planning intent cannot waive integrity/approval; preserve acquisition and job ownership. |
| #153 physics | Consume compatible state/cache where available | Engine/configuration/seed/input hashes and exact continuation limits; no new physics solver. |
| Native review/workflow | Reuse dependency projections, evidence store, archive and transaction primitives | Reviewer kind, inspected bytes, exact revision and per-dimension staleness; no parallel approval ledger. |

Across adapters, require the addendum's explicit outcomes (`executed`, `supported_fallback`, `partial`, `blocked_missing_capability`, `needs_visual_review`, `failed`, `canceled`). These are operation outcomes, not replacements for lifecycle or review verdicts. Queries must include project revision, world/timeline/branch, exact time and boundary side, and return hashed dependencies plus unavailable fields. Repair application needs expected revision, typed plan, applicable authority, atomic writes and idempotency.

## Stale premises and source conflicts

- The body still says all work is open and no implementation has executed. The final comment is more precise: #112 alone is complete, PR #190 merge `79958045b794c77ddb726002268bb3bbf9d4f6ad`; persistent continuity and acceptance remain outstanding. Preserve the original capture and correct the current delivery summary.
- Original URLs name `CompleteDotTech/hatch-pet` or `CompleteTech-Skills/hatch-pet`. The current repository is `CompleteTech-Skills/creative-studio`. Update active navigation/ownership references without silently rewriting historical project names, source attribution, the `hatch-pet` skill entry point or historical receipts.
- The original missing-media caution remains a necessary integrity rule, but is not a current blanket blocker. The retained baseline reports restored original source transports and review history. That is archival verification at its pinned revision, not a new full video render or new artistic approval.
- `references/continuity-baseline-audit.md` is explicitly pinned to older main `7b966a6…`. Its repeated-character prop-owner collapse finding is superseded on current `bb5a681`: `production_performance.py:283` now builds `repeated_characters` and includes the owner instance ID in comparisons. Do not reopen that exact defect without reproducing it. Persistent global world state, attachment/contact continuity and broader identity requirements remain separate unfinished work.
- Current `evaluate_instance_performance` still computes two-dimensional root offsets and permits terminal N; `prop_owner` still evaluates a shot-local ownership list; `validate_continuity` still follows declared links and validates a bounded set of fields. These are reusable foundations, not a persistent 3D event engine. The source spot-check supports this limited conclusion only.
- The baseline document mentions two workflows at its historical starting point; the architecture audit records three on this main. Dated inventory is not current readiness evidence.

## Acceptance disposition

There is no substantive conflict between immutable identity and approved variants: variants require explicit revision/state/approval. There is no conflict between contextual cinematic exceptions and integrity checks: a pose/axis exception does not waive identity, hash or ownership. There is no conflict between a genuine procedural test fixture and final artistic review: they support different claims.

Retain the original full requirements and current hierarchy. Refine the eight execution/acceptance clarifications above in accountable task handoffs; do not replace the original mission with a short summary. Final closure needs a machine-readable matrix demonstrating all applicable original criteria, actual two-scene renders, evaluated contact, all fifteen defect scenarios, selective repair/reuse, current review evidence and honest remaining limitations. This audit neither establishes nor claims those results.
