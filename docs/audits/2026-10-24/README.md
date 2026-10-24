# Creative Studio repository audit — 24 October 2026

This audit covers every original **184 issue and 31 pull request**, all retrieved conversations/review threads, repository metadata, native hierarchy, dependencies and default-branch architecture. The source baseline is [`bb5a681`](https://github.com/CompleteTech-Skills/creative-studio/commit/bb5a6810afd06cf57185ffdfe2234e0fdc6e6240). Original parent mission bodies and continuation comments were read in full; their hashes and coverage records are retained. Changes below were made on GitHub and then read back. The documentation PR itself is additional administrative work, not an implementation completion claim.

## Inventory and evidence

| Collection | Initial complete inventory |
| --- | ---: |
| Actual issues | 184: 171 open, 13 closed |
| Pull requests | 31: 9 open, 22 merged |
| Issue/PR conversation comments | 102 |
| Inline review comments | 13 across 12 threads |
| Branches / tags / releases / milestones | 28 / 0 / 0 / 0 |
| Native parent-child / blocking links | 168 / 200 |
| Accessible workflow records / historical runs | 10 / 237 |
| Workflows present on main | 3 |
| Linked Projects / item project memberships | 0 / 0 |
| Original labels / assigned labels | 10 / 0 |

REST issue totals were separated from PRs and independently reconciled with GraphQL. All 215 original items were checked for Project membership. All paginated PR file counts match their reported changed-file totals, including the PRs too large for GitHub's full-diff endpoint. Local Git supplied those diffs. No release, milestone, Project, missing collection, or inaccessible branch-protection resource is silently treated as a successful implementation.

Machine-readable and tabular deliverables:

- [Every issue: CSV](issue-ledger.csv) and [full JSON](issue-ledger.json).
- [Every PR: CSV](pr-ledger.csv) and [full JSON](pr-ledger.json), including exact reviewed heads, CI and review boundaries.
- [All dependency and parent edges](dependency-graph.json), [branch ledger](branch-ledger.csv), [labels and usage](labels.json).
- [Every registered workflow](workflow-audit.json) and [redacted secret-scan triage](gitleaks-triage.json).
- [Mutation audit](mutations.json), [snapshot reconciliation](snapshot.json), [full parent-source coverage](parent-source-coverage.json).
- [Architecture / all twenty subsystem areas](architecture-audit.md), [security/dependency review](security-history-audit.md), [program graph findings](program-graph-findings.md).
- Original mission crosswalks: [Model/Rig/Scene/Animation](parents-10-13-audit.md), [Scene/Animation/Camera/LookDev](parents-12-15-audit.md), [Render/Editor/Director](parents-16-18-audit.md), [Continuity](parent-107-audit.md), [Asset Library](parent-128-audit.md), [Physics](parent-153-audit.md).

The complete raw API snapshots and execution logs also remain in the local audit archive. Public ledgers retain identities, source links and hash references without publishing new copies of local user-environment inventories.

## Identity and metadata changes

Retain **creative-studio** as the canonical repository. Preserve **hatch-pet** as the installed skill name, invocation and directory. The rename has already happened; no further repository/package/CLI rename is warranted by this audit. Existing source and interface compatibility matter more than making every historical name identical. No split repository is justified while the shared acquisition, review, provenance and rig contracts remain central.

The old description, “Hatch style-flexible Codex pets - skill from openai/skills,” was replaced with:

> Codex skill for reusable 2D characters, sprite animation, layered productions and optional 3D rigging, with provenance and review workflows.

All six original topics were retained: `3d-animation`, `agent-skills`, `codex-pets`, `codex-skill`, `completetech`, `python`. Added `2d-animation`, `character-animation`, `sprite-animation`, `rigging`, `blender`, `gltf`. Twelve topics stay below GitHub's twenty-topic limit. These describe existing code; no physics-simulation, full AI-director or reconstruction claim was added. Recognized [character-animation](https://github.com/topics/character-animation) and [rigging](https://github.com/topics/rigging) terminology and [GitHub's topic rules](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics) informed the additions.

The documentation changes correct active clone commands, preserve `skills/hatch-pet`, separate incompatible test environments, and mark the old missing-public-payload report as historical. Historical owner URLs, review identities, source receipts and provenance remain intact. The Apache-2.0 license blob exactly matches the current upstream `openai/skills` hatch-pet license (`13e25df86ce06eb6488e6a6bc5c5847f5dedc352`). Vendored Three.js has an MIT notice/hash inventory; original Flite attribution is retained. This does not grant rights to arbitrary imported assets or certify every transitive binary license.

| Metadata / setting | Disposition |
| --- | --- |
| Name / default branch / visibility | Retain `creative-studio` / `main` / public; no rename or split performed. |
| Homepage | Retain empty: no verified deployed product homepage identified. |
| License / provenance | Retain Apache-2.0 and original upstream/third-party notices; skill identity stays compatible. |
| Issues / Projects | Enabled; no linked Project or item membership exists. Preserve native issue hierarchy rather than create another board. |
| Wiki / discussions / Pages | Disabled; no change justified by current delivery. |
| Merge methods / auto-merge | Existing merge/squash/rebase options retained; auto-merge remains disabled. No merges or protection bypasses. |
| Branch rules / review requirements | No protection or rulesets found; enforcement requires the tested policy work in #216. |
| Secret scanning / push protection / dependency alerts | Disabled in inspected metadata; #216 tracks configuration and readback, without claiming an existing guard. |
| Tags / releases / milestones | None; no speculative version, deadline or milestone invented. |

| Topic | Action | Evidence-based purpose |
| --- | --- | --- |
| `3d-animation` | Retain | Implemented rig clips and deformation. |
| `agent-skills` | Retain | Repository provides an installable agent skill. |
| `codex-pets` | Retain | Fixed Codex pet atlas remains a supported workflow. |
| `codex-skill` | Retain | Existing skill invocation and installation identity. |
| `completetech` | Retain | Repository organization/brand discovery. |
| `python` | Retain | Runtime and CLI implementation language. |
| `2d-animation` | Add | Layered character performance and raster production. |
| `character-animation` | Add | Shared character profiles, clips and rig controls. |
| `sprite-animation` | Add | Sprite strips, atlases and playback. |
| `rigging` | Add | Actual skeleton, skinning and IK/FK code. |
| `blender` | Add | Implemented native adapter and independently exercised historical CI. |
| `gltf` | Add | Real GLB/glTF import/export and interoperability gates. |

## Taxonomy and backlog reconciliation

The audit created 37 labels across type, area, priority, workflow and hierarchy dimensions. Seven unused ambiguous legacy labels were retired after checking their live usage. Conventional `accessibility`, `good first issue` and `help wanted` signals remain available. The resulting 40-label taxonomy is documented in [CONTRIBUTING.md](../../../CONTRIBUTING.md); the complete colors, descriptions and read-back usage counts are in [labels.json](labels.json), and the original inventory is in [labels-before.json](labels-before.json).

Every original issue and PR received type/component labels; open issues received an evidence-based priority and actionable workflow classification. Completed historical work has no modern workflow label. No speculative feature was labelled P0. Assignees and legitimate reviewer history were preserved rather than inventing ownership or requesting review from the same shared account.

[Director phase #19](https://github.com/CompleteTech-Skills/creative-studio/issues/19) was reopened: only baseline child #23 was complete, while mandatory contract/admission/graph work #24–26 remained open. Other closed issues retained their bounded audit/fix dispositions; all 22 merged PR merge commits are present in the audited main ancestry. A closed leaf or successful foundation PR does not close its program.

Twelve parent missions received individual current-state/compatibility clarifications, retaining the complete original engineering requirements. The original Model3D, Camera and Render Z-up recommendations now explicitly defer to the implemented Y-up shared world with external conversion. Checklist entries were updated only for verified completed native children; no unnamed acceptance test was checked merely because a PR merged.

Native hierarchy was largely sound. No duplicate epics or alternative Project board were created. The existing 200 blocking edges were acyclic and every explicit leaf dependency was represented. One demonstrated integration edge was added: [#168](https://github.com/CompleteTech-Skills/creative-studio/issues/168) is blocked by [#59](https://github.com/CompleteTech-Skills/creative-studio/issues/59), matching PR #212's actual dependency on #210. The resulting 201 edges remain acyclic. Native child completion is a count of closed scopes, not proof of integrated production acceptance.

## Open PR dispositions

All nine original open PRs received a commit-anchored COMMENT review. None was merged, approved or automatically marked ready.

| PR | Disposition and main finding |
| --- | --- |
| [#206](https://github.com/CompleteTech-Skills/creative-studio/pull/206) | Bounded material validation reviewed; reconcile main conflicts and preserve sibling texture/codec gates. Ten focused Windows tests passed. |
| [#207](https://github.com/CompleteTech-Skills/creative-studio/pull/207) | Registration/resolver foundation can proceed to final review; 24 focused tests passed. It is not the full Asset Library. |
| [#209](https://github.com/CompleteTech-Skills/creative-studio/pull/209) | Requires changes: schema-valid mappings misclassified, repeated full revision hashing, and broken selected revision prevents recovery. All three existing unresolved threads independently reproduced and left unresolved. |
| [#210](https://github.com/CompleteTech-Skills/creative-studio/pull/210) | Scene foundation reviewed; 67 contract + 9 math tests passed. Preserve shared math/owner contracts in later integrations. |
| [#211](https://github.com/CompleteTech-Skills/creative-studio/pull/211) | Codec/texture change reviewed; conflicts and shared gate integration remain. Nineteen native-route tests passed with one optional validator skip; twelve codec tests passed. |
| [#212](https://github.com/CompleteTech-Skills/creative-studio/pull/212) | Blocked on #210 and conflicts. POSIX/native scope and preserved native evidence do not qualify an untested integrated head. |
| [#213](https://github.com/CompleteTech-Skills/creative-studio/pull/213) | Failing required compatibility: unconditional cache test imports optional NumPy into 2D-only CI. Preserve pure timing tests and move real native cache admission to its explicit native gate. Also blocked on #210/#212 and conflicts. |
| [#214](https://github.com/CompleteTech-Skills/creative-studio/pull/214) | Camera contract/math foundation reviewed; 22 focused tests passed. Full cinematic acceptance remains open. |
| [#215](https://github.com/CompleteTech-Skills/creative-studio/pull/215) | P2 portability finding: unconditional symlink setup fails with Windows privilege error 1314; 29 tests passed, one setup error. Hosted green checks do not establish this local capability. |

Shared test-gate lists must retain all material, texture, scene and camera families when branches integrate. Older successful CI, duplicated push/PR check names and model reviews under a shared account are not interchangeable with current-head native approval.

## Verification and health

The audited default branch has fifteen successful current check names across all three main workflows. Separately executed Windows checks included **117 tests: 114 passed, three documented platform skips**, plus schema/template parity and original-source verification. This combined environment is not the pinned CI environment. Additional reviewer runs, errors, skips and exact heads are in the ledgers. An initial 82-case local attempt had a missing optional dependency and symlink-privilege errors; it is retained as a failed attempt rather than recast as passing. No fresh full-suite, Blender render, artistic review, paid generation, installation or deployment is claimed.

| Area | Assessment |
| --- | --- |
| Architecture | Substantial reusable 2D and rig implementation; new general 3D systems mostly contracts/backlog. Preserve one owner per shared interface. |
| Code/test quality | Meaningful negative, numerical, archive and external-tool gates exist. Current PR findings and dependency-isolation failure require correction. |
| CI | Three main workflows cover delivery/native rig/Model3D; seven additional workflow records are branch-scoped operational or pending-feature workflows, not seven installed main release gates. All ten sources were inventoried. |
| Security | Specific historical fixes remain present. Boundedness limits and direct-version advisory matches remain; no blanket security or exploitability claim. |
| Documentation | Rich traceability but stale owner/installation and historical-delivery presentation needed correction; missing old local evidence links remain explicitly historical. |
| Contributor/release governance | No original branch protection/rulesets, CODEOWNERS, release policy or private reporting policy; no tags/releases. Follow-up [#216](https://github.com/CompleteTech-Skills/creative-studio/issues/216) tracks verified enforcement, not merely policy prose. |
| Release readiness | Existing scoped delivery evidence is substantial; full Director/scene/editor/library/continuity/physics production acceptance remains open. |

Dependency matching found 41 OSV records against the optional rig-test Pillow 12.3.0 pin; this is not 41 proven reachable vulnerabilities. The delivery 13.0.0 pin had no matches in that query. The existing [rig hardening issue #58](https://github.com/CompleteTech-Skills/creative-studio/issues/58) now records the required resolved-environment/reachability check, fixed-version update and preserved decoder/native regression gates. For example, [GHSA-22hg-r758-rrw9](https://github.com/python-pillow/Pillow/security/advisories/GHSA-22hg-r758-rrw9) requires optional MIC handling; current principal source/import format guards reject that format before frame-count access. No sensitive payload or new exploit is published by this audit.

Gitleaks 8.30.1 scanned all 175 reachable commits (40.73 MB). Each of its 111 generic-key findings was individually reconciled against the original commit/path/line: 58 evidence digests, 52 historical local generation claims and one Python parameter expression. No confirmed service credential was identified. Values were not tested against services or published; this result does not prove that no secret exists in every binary or external artifact.

## Prioritized delivery order

1. Correct #209's confirmed data/registry defects and #213's compatibility failure; reconcile #206/#211 conflicts without dropping sibling safety gates. Complete the #58 dependency follow-up before qualifying a new release.
2. Finish shared foundations using their existing owners: #24 Director contracts; #44 model import; #52 rig acceptance; #59 scene; #67 animation; #75 camera; #83 look-development; #91 render; #99 editor; #113 continuity; #138 library. These are bounded parallel workstreams, subject to their actual native blockers and ownership.
3. Land and verify Scene3D before #168 backend acceptance, then #169 exact stepping. The deepest original dependency chain is #166 → #167 → #168 → #169 → #170 → #171 → #172 → #173 → #174 → #175 → #181 → #183 → #184 → #185 → #186 → #187 → #188 → #189. It describes dependency depth, not an invented elapsed-time schedule.
4. Deliver the Director P1 vertical slice (#27–31): exact 120-frame local production, genuine media verification, persistence/recovery and targeted revision. Only then treat the independent P2/P3 provider and 720-frame acceptance as qualified.
5. Complete real integration, portability, reliability and review evidence before closing parents. Preserve full scene/editor/animation/material demonstrations, two-scene library/continuity proofs and all thirteen physics categories. Do not substitute metadata, static geometry or authored motion for authentic physical simulation.

## Completion boundaries

Every original issue and PR has an individual recorded assessment. All twelve large source missions and their comments were read; every native dependency and parent edge was enumerated. Safe metadata, labels, selected parent/relationship corrections and PR reviews were applied. No PR merge, forced push, history rewrite, source deletion or other-repository publication occurred.

Remaining engineering and governance work is explicitly recorded in existing issues or #216. Original evidence unavailable in a current checkout was identified rather than fabricated; optional tools and human/artistic acceptance were not inferred from test results. The final GitHub read-back checks the mutation ledger, counts and taxonomy separately from future implementation readiness.
