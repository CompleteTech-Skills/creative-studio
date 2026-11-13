# Exhaustive parent audit: Rendering #16, Editor #17, Director #18

Read-only review dated 2026-10-24. Runtime evidence is pinned to `bb5a681`; the inspected checkout HEAD is documentation-only `768c6e2fa4a8010f5f26e75fded1389d6bb557b9`. This worker changed only audit artifacts, not repository source or GitHub state. No new runtime, browser, Blender, GPU, provider or production test execution is claimed.

## Read completeness and evidence boundary

Every line of the three original issue bodies and all five comments was read in bounded chunks. The companion `parents-16-18-coverage.json` records exact UTF-8 text hashes, comment IDs, character/byte/line counts, contiguous read ranges and all 108 numbered source sections. #16 includes comment 6333780798 (sections 14–24); #17 includes 6333787159 (38–44); #18 includes 6333798260 (31–40), mandatory phased/interface addendum 6333798568, and hierarchy addendum 6333895917. The last two comments supplement rather than replace the original mission. The literal trailing instruction in the Director continuation, “Add a phased MVP boundaryDefine mandatory interfaces precisely”, is retained in its raw source and fulfilled by the separate addendum, not an omitted section.

All direct rendering/editor child bodies and Director phase/task bodies were read for mapping. The initially truncated combined child read was corrected by rereading #30 and #31 in full. Parent source reads were not truncated. Child comment/history/live-state auditing is owned by the main issue ledger, not claimed again here.

Architecture evidence: `architecture-audit.md`; current `scripts/rig_render.py`, `rig_blender_worker.py`, `rig_blender.py`, `production_render.py`, `rig_http.py`; `references/director-baseline.md` and the targeted ownership/contract rows of `references/director-requirements-map.md`; current `DELIVERY_STATUS.md`. The Director baseline is historical and explicitly only #23. Its audit begins at `7b966a6`; its test results are not fresh tests at the current head. A map of proposed interfaces is not their implementation.

The main snapshot has real per-rig geometry previews and controlled Blender preview rendering; it has substantial rig import/animation and 2D persistence, source restoration, media, and review primitives. It does not establish the complete production Render3D engine, persistent general Editor3D application, or Director runtime. No `render_3d`, `scene_editor` or Director execution module was found in the current scripts inventory. Open foundation work and its PR checks cannot be represented as merged, end-to-end production acceptance.

## Shared amendments and owner boundaries

1. Preserve the canonical implemented right-handed +Y-up, +Z-forward, meter, XYZW contract. #16 section 5 proposes Z-up and #18 allows Blender-internal conventions; #17 proposes Y-up. Resolve this explicitly in #91/#99/#24 through the Scene3D owner #12 and adapters, not by making three canonical models. Existing Blender conversion `(x,y,z) -> (x,-z,y)` and its inverse belong at backend boundaries. State the camera local-forward convention separately from world forward and test transforms, normals, mirrored/nonuniform scale and roundtrip.
2. Preserve integer half-open production frames/rational FPS and explicit conversion to rig seconds/Blender frame origins. Existing atlas milliseconds, 2D pixel tolerances and rig seconds must not be silently reinterpreted as world-space time/contact metrics.
3. #16 owns production render jobs, backend workers, device admission, output verification and cache publication. #17 owns authoring commands/session presentation and calls that service. #18 owns production-graph coordination and references its native job IDs. Existing acquisition claim/token workflow and existing review stores remain authoritative. Three schedulers or three review stores would violate the missions.
4. Assign scene hierarchy/spatial schema to #12, animation/evaluator to #13, camera behavior to #14, materials/lighting to #15, rig format to #11, and reusable library semantics to #128. These missions require usable integration and should link required producer contracts without duplicating their ownership. A contract-only producer does not fulfill a consumer's real workflow acceptance. Map minimum supported adapters to actual interfaces and track unresolved features explicitly.
5. Original missing-media paragraphs are historical. Current `DELIVERY_STATUS.md` reports restored original five transports, 129 logical files/462 parts, retained original reviews and public media. Amend active planning notes to revalidate those originals and preserve provenance, not to reproduce the now-stale blanket missing-source claim. Do not erase original history or synthesize replacement art/audio/reviews. New 3D acceptance scenes must remain self-contained. Source-restoration proof and production-art approval remain distinct.
6. Keep fixture, import, procedural generation, live model inference, structural verification, actual visual inspection and aesthetic approval separate. A CPU rig preview, schema pass, process exit code, valid PNG container, or text report cannot close any full mission.
7. Canonicalize active navigation to `CompleteTech-Skills/creative-studio` while preserving original prompt provenance and legacy hatch-pet invocation. Repair mojibake section/range markers in child summaries if confirmed in live text. Neither maintenance edit narrows acceptance.

## #16: Render3D disposition and concrete child amendments

Keep #16 and #91–98 open absent their required execution evidence. Existing rig preview rendering is valuable reuse, not the complete engine. The inherited scope is unusually detailed; child checklists summarize it and must explicitly retain all source subsections.

| Owner | Amendment to scope/acceptance |
|---|---|
| #91 | Freeze additive 2D/3D mode selection, strict shared schemas, immutable source and per-frame snapshots, backend protocol, machine errors, exact frame mapping and adapter ownership. Legacy projects default to 2D; incompatible overrides fail. Include baseline/migration/rollback and no hidden format claims. |
| #92 | Extend the trusted existing Blender worker rather than introducing arbitrary generated Python. Pin/test Blender LTS versions; argument-array background/factory/disable-autoexec invocation, bounded structured I/O, controlled environment and process-tree cancellation. Render actual geometry, armature skinning/morphs, camera and light animation in bounded reusable chunks; independent PNG and EXR decode is the first vertical gate. Explicitly cover Cycles CPU and capability-tested Eevee. |
| #93 | Add an inspectable versioned preset matrix, explicit effective settings, texture color/data semantics, DOF and shutter evaluation, transparency/straight-premultiplied edges, linear HDR and display transforms. Independently inspect true 8/16-bit PNG, half/full float EXR, channel/pass types, finite depths/normals and HDR values above display range. Never claim high precision after 8-bit quantization. |
| #94 | Separate OS enumeration, Blender enumeration, initialization, real small render and full-scene admission. Define distinct cpu/gpu/auto/gpu-preferred/device-id policies. Allowed fallback retains failure and creates a compatible new backend identity; gpu-required must fail. Cache by relevant executable/driver/environment and expiry. Report physical-device tests separately from mocks and unavailable hardware. |
| #95 | Transactional persistent job/frame states, atomic transitions, priority chunks, per-device reservations, bounded retries/backoff/logs, actual resource measurement classification and durable cancellation. Reclaim leases only after confirming no live owner. Enforce runtime/storage/memory/thread/frame limits and document best-effort VRAM limits. Exercise cancellation at import, render, verification and publication. |
| #96 | Snapshot all geometry/material/texture/animation/camera/light/environment/pass/color/backend/device/seed dependencies, including motion-blur neighbors and history-dependent simulation. Separate operational counters from cache keys; exclude incidental checkout paths. Stage, independently verify, atomically publish and commit; resume redecodes actual artifacts. Test partial PNG/EXR/manifest writes, tampering, stale source/settings, restart and safe quota/reference-aware eviction. |
| #97 | Implement the full named operation set (capabilities/validate/plan/render/status/cancel/resume/retry/verify/export/cache/benchmark), both sync and genuinely progressing async paths. Display real frames, incomplete errors, checkerboard, matched comparisons and mapped data passes through bounded labeled proxies. Preserve PNG/EXR masters. Export zero-audio video using manifest-derived exact timing; independently decode/probe, hash and issue receipts. Color-manage EXR-to-video. Portable restore and editor handoff must distinguish editable source from flattened media. |
| #98 | Own the complete section 19 test matrix and section 20 cinematic acceptance, not just a small smoke render. CI must execute pinned Blender Cycles CPU without silently skipping the authoritative gate; capture real supported-OS and optional GPU evidence. Run actual process-kill recovery, concurrent ownership/security tests and measured cold/warm/median/p95/resource/cache benchmarks. Keep unmet gates explicit. |

Mandatory #98 demonstration: continuous 5–10-second multi-shot sequence at documented rational FPS, separate low-resource smoke profile; two independently controllable 3D characters or one rigged character plus separately animated multipart object; multiple meshes, receiving floor, multiple functional lights, distinguishable PBR/emissive/transparent materials, true skeletal deformation, camera parallax and at least two shots/camera cut. Deliver complete PNG and true HDR EXR sequences, at least normal/depth passes, dedicated transparent shot and verified silent video. Retain actual image contact sheet, source snapshots, process logs, per-frame/pass hashes, independent decoder/semantic measurements, interrupted-resume and stale-cache proof, missing-asset/resource failures, legacy regressions and measured performance. Full sequence completeness, not a few unrelated stills, gates closure.

## #17: Editor3D disposition and concrete child amendments

Keep #17/#99–106 open. A rig review UI is not a persistent general scene editor. The original specifically requires no-code authoring and actual save/close/reopen/render roundtrip, not merely a viewport screenshot.

| Owner | Amendment to scope/acceptance |
|---|---|
| #99 | Shared generated/contract-tested frontend/backend types and additive schemas; separate canonical persistent state, session state and rebuildable runtime projection. Adopt the shared coordinate/evaluator contracts; specify local authentication/origin/payload/optimistic revision boundaries. Framework choices are recommendations, not mandatory duplicate stacks. |
| #100 | All persistent edits pass typed apply/inverse transactional commands. Drag preview commits one undo entry or fully cancels. Include delete hierarchy restoration, compound rollback, redo invalidation, bounded history, separate durable revisions, backend rejection rollback, interrupted atomic saves, autosave excluding unfinished drags, lock/conflict handling and portable restore. A save-success UI needs persistence acknowledgement. |
| #101 | Real geometry navigation/picking, synchronized viewport/outliner/inspector/timeline selection, numeric and world/local/parent gizmos, hierarchy/collections and editor-vs-render visibility. Test negative/mirrored transforms, world-preserving reparenting and nondecomposable shear policy. Context loss/restoration and resource disposal must preserve unsaved canonical edits. |
| #102 | Actual validated GLB/glTF dependencies, textures/HDRI and supported primitives; inspect content, topology, bones, animation, hashes, licenses/provenance and real thumbnail. Coordinate library #128 for search/filter/version/relink/instances. Support deterministic surface/ground snapping and bounded overlap warnings; do not claim full physics from AABBs. Disable remote fetching by default; enforce path/archive/resource safety. |
| #103 | Real supported material slots/UV/color semantics, lights and camera optics/path/focus, independent camera preview, and actual imported rig bone controls/constraints/morphs. Persist and render these values; capability warnings cannot conceal a missing mandatory direct-bone editing workflow. Preserve source rig/clip identity. |
| #104 | Actual multi-track key/curve/tangent editor, autokey, quaternion/discrete interpolation and exact out-of-order evaluation; clips, skeleton compatibility, offsets/speed and non-destructive reuse. Multi-scene/shot create/reorder/trim/cameras/cuts must alter persisted render sequence, not browser-only playback. |
| #105 | Submit exact saved revision to #16; show native progress/failure/cancel/retry. Independently verify image sequences and supported MP4/WebM silence; EXR through compatible production backend. Compare same scene revision/shot/camera/frame/color data; document preview approximation and don't confer review approval. Keep image export usable without optional movie encoder. |
| #106 | Preserve every section 38 reference-project item, all 30 demonstration actions, and all AC-01–AC-28 as individual acceptance rows with artifact/test/reviewer refs. Include actual Chromium/Firefox/WebKit and Edge/Safari support evidence/limitations, WebGL2 baseline, resize/DPI/zoom/back-forward/context-loss, keyboard alternatives, WCAG 2.2 AA conventional UI target, responsive functional layouts, measured resource profiles, security, crash/recovery, lint/types and all legacy regressions. |

Mandatory reference project: ground plane; at least three distinct meshes; at least two independently positioned objects; parent-child hierarchy; reusable imported GLB; rigged/skinned GLB with editable bones; differing PBR materials; at least two lights/cameras/shots; object transform animation; animated camera or light; bone animation; timeline marker; renderable multi-shot sequence. Construct using supported editor/import workflows.

Mandatory interactive evidence must create the project/scene, cube/sphere/import, select via viewport, drag and observe inspector, numerically rotate/material-edit, parent/move child hierarchy, add lights/camera, keyframe/scrub, select/pose/key imported bone, add second shot, save, close, reopen and verify all nodes/assets/tracks/shots, undo/redo new edit, render saved source, export/verify, and compare actual preview/final. Retain a separate relocated archive restore, interrupted autosave recovery and missing-asset relink. No enabled persistent control may be disconnected. Diagnostic placeholders may help open broken projects only when clearly non-exportable and retaining original references.

## #18: Director disposition and concrete phase/task amendments

Keep #18 and incomplete #19–22 phases open. #23's baseline is a valid narrow audit deliverable; it cannot close #19 while #24–26 remain undone. Follow the main ledger's exact current state and PR evidence; this worker does not alter issue state.

| Phase/owners | Concrete amendment |
|---|---|
| P0 #19 / #23–26 | Keep baseline result revision-specific; generate every common/interface schema and semantic validator rather than just document a crosswalk. #24 owns ArtifactRef/OperationContext/Result/Diagnostic/Usage/timing/space/identity/authorization envelopes and all method contracts. #25 records real capability evidence vs untested states and authorized probes. #26 builds pure validated DAG compilation, exact edges, gates, critical path and invalidation. No paid/provider execution in compilation. |
| P1 #20 / #27–31 | Explicitly freeze the addendum's limited parser/structured brief vocabulary; exactly 120 frames, 24 fps, 640×360, two shots, two persistent procedural mesh robots, independent articulated motion, moving prop, actual cameras/lights/materials, Blender CPU. Save reusable GLBs/native Blender, reopen in separate process, decode all PNGs/movie with exactly 120 frames/zero audio; real cancellation/resume retains job identity; targeted camera or lighting revision preserves unaffected asset hashes. No general AI or full mission claim. |
| P2 #21 / #32–37 | #32 requires real configured brief/story/shot/revision provider calls, schema repair limits, model/template hashes, usage availability and injection handling. #33 extends native acquisition/resolver, preserving uncertain attempts and one claim. #34 reserves/settles/releases transactionally with concurrency, retries and unknown prices accounted. #35 owns graph coordination with native renderer references, not a second renderer. #36 provides typed lock-respecting revisions and selective stale descendants. #37 implements all nine separate milestones, evidence-bound QA, bounded repairs and usable CLI/dashboard. Mocks or unavailable credentials leave the live gate unverified. |
| P3 #22 / #38–42 | #38 owns complete narrative/interaction feasibility and identity continuity; #39 complete final rendering and all supported exports; #40 all native sources/receipts/portable archive plus separate-process reopen and rerender; #41 full security/resource/provider/OS/regression/failure matrix; #42 actual motion/visual/narrative inspection, all original requirements, documentation/example classes and final traceability. A parent checklist must reference every unsatisfied source subsection rather than close on summaries. |

Mandatory P3: exactly two persistent primary robot identities exploring a genuine abandoned laboratory, at least five shots, 720 decoded frames at 24 fps (30 seconds), final at least 1280×720, zero-audio MP4, reusable 3D characters/assets and native editable Blender. The recommended beat allocation is 120/144/168/144/144 frames (establish, explore, discover, cooperate, resolve); artistic refinement may change details while preserving required narrative and total. Preserve full action/interaction scope: real independent motion, supported articulation/rigs, gaze/contact, reachable grips, once-only prop ownership, continuous world transform and frame-bound handoff or accepted equivalent cooperative interaction. Evaluate actual handoff frames and multiple viewpoints, not ownership metadata alone.

Retain original prompt, brief/story/shots, both planning storyboards and actual geometry previs distinctly, asset plan and classifications, identities, capability and budget reports, DAG, authoritative acquisition/provider receipts, reusable GLBs/environment/motion, native source, all real PNGs, final movie/probe/full decode, actual visual QA/reviews, failure/repair history and final acceptance. Restore elsewhere and reopen/reinspect real meshes, hierarchy, actions, cameras/lights; rerender representative frames. Neither archive creation nor `.blend` existence proves editability.

The nine milestone gates are Planning Complete, Assets Ready, Scene Ready, Animation Ready, Render Complete, Video Complete, Editable Project Verified, Visual QA Complete and Delivery Complete. Each retains status/timestamp/required inputs/artifact refs/validation/evidence/failure/remaining work. Structural checks, paid execution, native reopening, stream integrity and visual approval never imply each other.

## Exact section-to-child ownership map

The JSON includes source line ranges and individual headings. The compact tables below assign all numbered sections, including crosscutting delivery requirements. These are routing assignments, not claims of implementation or permission to narrow original requirements.

### Parent #16

| Section | Original heading | Responsible children |
|---|---|---|
| 1 | Mission and repository | #91, #98 |
| 2 | Mandatory repository audit before implementation | #91 |
| 3 | Architecture and rendering-mode separation | #91 |
| 4 | Required typed interfaces | #91 |
| 5 | Versioned 3D project and scene contracts | #91 |
| 6 | Blender as the primary headless backend | #92 |
| 7 | Rendering engines and backend compatibility | #92, #94 |
| 8 | Hardware detection, GPU selection, and CPU fallback | #94 |
| 9 | Scene preparation and exact animation evaluation | #92, #93 |
| 10 | Render quality, materials, lighting, and output formats | #93 |
| 11 | Durable render orchestration and scheduling | #95 |
| 12 | Frame caching, snapshots, and invalidation | #96 |
| 13 | Render manifests, provenance, and independent verification | #93, #96 |
| 14 | FFmpeg integration and silent-video export | #97 |
| 15 | Preview and production editor integration | #97 |
| 16 | Configuration, CLI, and programmatic APIs | #97 |
| 17 | Security and isolation | #95, #98 |
| 18 | Resource budgets and performance management | #94, #95, #98 |
| 19 | Mandatory automated testing | #98 |
| 20 | Required demonstration and acceptance artifacts | #98 |
| 21 | Dependencies, installation, and documentation | #97 |
| 22 | Implementation execution order | #91, #92, #93, #94, #95, #96, #97, #98 |
| 23 | Definition of done | #98 |
| 24 | Final engineering instructions | #98 |

### Parent #17

| Section | Original heading | Responsible children |
|---|---|---|
| 1 | ROLE AND PRIMARY OBJECTIVE | #99, #106 |
| 2 | MANDATORY REPOSITORY RECONNAISSANCE | #99 |
| 3 | ARCHITECTURAL PRINCIPLES | #99 |
| 4 | PROPOSED MODULE BOUNDARIES | #99 |
| 5 | CANONICAL 3D PROJECT DATA MODEL | #99 |
| 6 | SCENE VIEWPORT | #101 |
| 7 | OBJECT SELECTION AND MANIPULATION | #101 |
| 8 | SCENE HIERARCHY AND OUTLINER | #101 |
| 9 | ASSET BROWSER AND IMPORT WORKFLOW | #102 |
| 10 | OBJECT PROPERTY INSPECTOR | #101 |
| 11 | MATERIAL EDITOR | #103 |
| 12 | LIGHTING EDITOR | #103 |
| 13 | CAMERA EDITOR | #103 |
| 14 | CHARACTER RIGGING AND POSE EDITING | #103, #104 |
| 15 | ANIMATION TIMELINE | #104 |
| 16 | DETERMINISTIC ANIMATION EVALUATION | #104 |
| 17 | ANIMATION CLIPS AND SEQUENCING | #104 |
| 18 | SCENE AND SHOT MANAGEMENT | #104 |
| 19 | OBJECT PLACEMENT AND SPATIAL TOOLS | #102 |
| 20 | UNDO, REDO, AND COMMAND ARCHITECTURE | #100 |
| 21 | STATE MANAGEMENT | #99, #100 |
| 22 | BACKEND SERVICE AND API CONTRACT | #99, #100 |
| 23 | PROJECT SAVE, LOAD, AND RESTORATION | #100 |
| 24 | INTEGRATION WITH EXISTING HATCH PET CONTRACTS | #99, #106 |
| 25 | 3D RENDERING INTEGRATION | #105 |
| 26 | PREVIEW AND PRODUCTION COMPARISON | #105 |
| 27 | RENDER EXPORT INTERFACE | #105 |
| 28 | USER INTERFACE LAYOUT | #106 |
| 29 | KEYBOARD SHORTCUTS | #106 |
| 30 | ACCESSIBILITY | #106 |
| 31 | SECURITY OF IMPORTED DATA | #102, #106 |
| 32 | PERFORMANCE AND RESOURCE MANAGEMENT | #101, #106 |
| 33 | ERROR REPORTING AND DIAGNOSTICS | #100, #106 |
| 34 | FRONTEND/BACKEND CONSISTENCY | #100 |
| 35 | CLI INTEGRATION | #105, #106 |
| 36 | BROWSER COMPATIBILITY | #106 |
| 37 | TESTING STRATEGY | #106 |
| 38 | REQUIRED END-TO-END REFERENCE PROJECT | #106 |
| 39 | ACCEPTANCE CRITERIA | #106 |
| 40 | ENGINEERING EXECUTION PLAN | #99, #100, #101, #102, #103, #104, #105, #106 |
| 41 | DOCUMENTATION DELIVERABLES | #106 |
| 42 | QUALITY AND DELIVERY REQUIREMENTS | #106 |
| 43 | REQUIRED FINAL IMPLEMENTATION REPORT | #106 |
| 44 | DEFINITION OF DONE | #106 |

### Parent #18

| Section | Original heading | Responsible children |
|---|---|---|
| 1 | Mission and Primary Objective | #22, #42 |
| 2 | Repository Inspection and Integration Requirements | #23 |
| 3 | Non-Negotiable Product Requirements | #24, #37, #42 |
| 4 | Target System Architecture | #24, #26, #35, #37 |
| 5 | Canonical Creative Request and Project Schemas | #24, #27 |
| 6 | Natural-Language Interpretation Engine | #27, #32 |
| 7 | Story Generation and Silent Visual Storytelling | #27, #32, #38 |
| 8 | Shot Generation and Storyboard Planning | #27, #32, #38 |
| 9 | Asset Resolution and Reusable Asset Discovery | #28, #33 |
| 10 | Character Design and Persistent Identity | #28, #33, #38 |
| 11 | Genuine 3D Asset Generation and Import | #28, #33 |
| 12 | Environment and Procedural Scene Construction | #28, #29, #38 |
| 13 | Animation Direction and Motion Planning | #29, #38 |
| 14 | Character Interactions and Prop Handoffs | #29, #38 |
| 15 | Cinematic Camera Director | #29, #38 |
| 16 | Lighting, Materials, and Visual Style Direction | #28, #29, #38 |
| 17 | Production Dependency Graph | #26, #35 |
| 18 | Capability Detection and Explicit Fallback Strategies | #25 |
| 19 | Model- and Provider-Neutral Integration Interfaces | #24, #25, #32, #33 |
| 20 | Agent Prompt Architecture | #32 |
| 21 | Budgeting and Cost Control | #25, #34 |
| 22 | Scheduling, Concurrency, and Recovery | #31, #35 |
| 23 | Selective Regeneration and Dependency Invalidation | #31, #36 |
| 24 | Natural-Language Revisions | #32, #36 |
| 25 | Blender Integration and Native 3D Assembly | #29, #40 |
| 26 | Rendering Pipeline | #30, #39 |
| 27 | Multi-Shot Assembly and Silent Video Export | #30, #39, #40 |
| 28 | Automated Structural and Visual Quality Assurance | #37, #42 |
| 29 | Review and Approval Architecture Integration | #37, #42 |
| 30 | User Interface and CLI | #37 |
| 31 | Security and Trust Boundaries | #41 |
| 32 | Operational Resilience and Portability | #31, #40, #41 |
| 33 | Testing Strategy | #41 |
| 34 | Evaluation Framework | #41, #42 |
| 35 | Mandatory End-to-End Demonstration | #38, #39, #40, #42 |
| 36 | Success-State and Evidence Requirements | #24, #37, #42 |
| 37 | Documentation and Developer Experience | #42 |
| 38 | Implementation Sequence | #19, #20, #21, #22 |
| 39 | Engineering Quality Standards | #41, #42 |
| 40 | Final Acceptance Criteria | #22, #42 |

## Final disposition

Parent missions remain implementation-and-evidence obligations. Retain current reusable rig/2D/source-restoration evidence, correct stale baseline descriptions, amend child ownership and acceptance explicitly, and keep unmet runtime/provider/hardware/visual gates visible. This audit authorizes no implementation, publication, issue closure or approval and makes no fresh production-completion claim.
