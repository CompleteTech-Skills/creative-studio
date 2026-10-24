# Original parent missions #10-13: complete text audit

All four original bodies and every matching comment were read end-to-end in bounded, nontruncated segments. Hashes below refer to UTF-8 encoding of exact JSON body strings. No GitHub or repository mutation was performed. Audit artifacts are outside the repository. A broad intermediate ledger dump truncated; it was not used as evidence of original-text completeness. Each original parent source was read separately without truncation.

Code boundary: expected baseline `bb5a681`; shared checkout was observed at `768c6e2fa4a8010f5f26e75fded1389d6bb557b9`. The only differences are CONTRIBUTING.md, DELIVERY_STATUS.md, README.md and references/production-validation.md. Inspected implementation and rig acceptance files are identical to baseline. No full regression, Blender, browser, paid generation or fresh visual acceptance was executed by this worker.

Fresh verification: 7 skinning tests and 9 animation tests passed, including known-pivot real vertex movement, removed-skin/zero-weight negatives, morph-before-skinning, interpolation, layering and retarget-identity checks. Model3D contracts: 54 tests, 51 passed, three skipped for missing Windows symlink privilege; no failures. Historical fixture metrics from references/3d-rigging-acceptance.md remain historical evidence.

## Shared scope and dependency corrections

Preserve one canonical right-handed +Y-up/+Z-forward meter-based rig world frame, with camera-local -Z and explicit Blender/2D conversions. #10's +Z-up recommendation must be reconciled rather than silently creating incompatible geometry. Preserve three clocks: 2D milliseconds, production rational integer frames, and native rig seconds. #13 adapts these explicitly.

The child chains are scheduling choices, not universal technical prerequisites. Import-only/static-mesh fallbacks authorize Scene3D and Animation3D progress without waiting for all Model3D generation; native rig work is already substantial. Keep native blocking links faithful to genuine acceptance dependencies, and distinguish integrated acceptance waiting from independent work being ready.

Priority recommendations: parent missions type=epic (or existing project equivalent), subsystem-specific area label, priority=P1 for foundational integration and correctness; child foundational #44/#52/#59/#67 P1, remaining implementation and acceptance P2 unless an active regression elevates urgency. Preserve #43/#51 closed. Use partial/needs-evidence for substantial rig children; use blocked only for genuinely prerequisite-dependent portions. Do not label all rig work unimplemented or all future work ready.

## #10 — Implement production-grade 3D asset generation, reconstruction, editing, validation, and export

Fully read: body 55,704 characters; comments 6333724227 (16,948 characters).

- Keep open. #43 is a delivered bounded contract slice; #44-50 remain required. model3d.py exposes only capabilities and validate, explicitly denying generation, reconstruction, provider execution, jobs, editing and approval.
- Section 11 recommends +Z up / -Y forward, conflicting with #11 mandatory +Y up / +Z forward and #12 Y-up. Preserve the already implemented rig convention and require explicit import/export adapters; record the reconciliation in #43/#44 without rewriting the historical prompt.
- Required offline methods are procedural assemblies, silhouette extrusion, revolution/heightfield and calibrated visual hull. rig_demo.py fixtures and contract-only camera records do not satisfy #45/#46.
- Keep observed/inferred surfaces and source/normalized-image/geometry identities distinct. #47 must own actual semantic mesh edits, UV generation, appearance and identity mapping; #48 owns measured distinct LOD geometry and provenance-preserving repair.
- #49 must retain real Blender edit-save-reopen-export evidence, not just scene creation; provider execution is conditional on configured authorization and availability, but functioning offline backends are mandatory.
- #50 must retain demonstrations A-M, interrupted real jobs, archive restoration, independent export checks, and strict evidence states. Existing restored 2D source transports supersede the prompt historical missing-source premise.

| Child | Primary original sections | Disposition |
|---|---|---|
| #43 | 2, 3, 4, 5, 11, 29 | closed; accepted bounded scope |
| #44 | 1, 9, 11, 17 | open; ready |
| #45 | 6 | open; blocked |
| #46 | 7, 8, 16 | open; blocked |
| #47 | 10, 12, 13, 15, 19, 20 | open; blocked |
| #48 | 9, 14, 23, 24 | open; blocked |
| #49 | 17, 18, 21, 22 | open; blocked |
| #50 | 25, 26, 27, 28, 30, 31, 32, 33, 34, 35, 36 | open; blocked |

Cross-cutting security, testing, documentation and acceptance sections map to the final child as integration ownership; their constraints also apply to every implementing child. Mapping is not permission to narrow the original requirements.

## #11 — Implement complete 3D character rigging and skeletal animation system

Fully read: body 63,680 characters; comments 6333758651 (17,963 characters).

- Keep parent open with substantial implementation recognized. #51 remains closed for its bounded accepted contract scope; #52-58 require granular acceptance reconciliation rather than a replacement rig engine.
- Actual skinning exists: adjacency diffusion, distance fallback, rigid mode, locked manual weights, inverse bind validation and matrix-driven vertex deformation in rig_skinning.py. FK, IK, constraints, attachments, face controls, clips and semantic retargeting also exist.
- The original acceptance explicitly permits procedural or hand-authored labeled fixtures. Lack of a user production model alone is not a missing three-character engineering-demo requirement; preserve pending subjective review separately.
- Remaining explicit limitations include only partial native-control conversion to Blender, browser loop trajectory reset versus native accumulating root motion, browser four-influence profile versus native eight-influence profile, and uncertified surface intersection/bone-surface/velocity diagnostics. Map these to #54/#57/#58 and require an evidence-backed disposition, not blanket unfinished labels.
- #56 must retain semantic compatibility, correct source/target identities, proportions, contacts and missing-channel reports. Its float-second/integer-FPS rig contract must remain authoritative behind an explicit rational-frame adapter for #13.
- Full external application acceptance and retained historical Blender/browser evidence are separate from fresh test execution. Human visual approval cannot be inferred from small contact errors or identical hashes.

| Child | Primary original sections | Disposition |
|---|---|---|
| #51 | 2, 3, 4, 5, 6 | closed; accepted bounded scope |
| #52 | 7, 8, 9, 10 | open; needs-evidence |
| #53 | 11 | open; needs-evidence |
| #54 | 12, 13, 16 | open; needs-evidence |
| #55 | 14, 15 | open; needs-evidence |
| #56 | 17, 18, 19, 20 | open; needs-evidence |
| #57 | 21, 22, 23, 24 | open; needs-evidence |
| #58 | 1, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38 | open; needs-evidence |

Cross-cutting security, testing, documentation and acceptance sections map to the final child as integration ownership; their constraints also apply to every implementing child. Mapping is not permission to narrow the original requirements.

## #12 — Implement production-grade 3D Scene Builder with persistent editing and rendering

Fully read: body 63,243 characters; comments 6333762001 (20,952 characters).

- Keep open. Main contains no scene3d runtime modules; open PR210 contract/math work is a foundation slice only. Preview scenes in rig_blender_worker.py do not implement the requested scene authoring engine.
- #59-60 must distinguish immutable asset ID from independent scene-node identity, preserve world transform on reparenting, reject unsupported shear, support nested prefab overrides and use one authoritative model across CLI, browser and renderer.
- #61-62 require actual editable primitives, real door/window openings, terrain, deterministic layouts, spatial index, broad/narrow collision checks and surface raycasts. Authoring collision avoidance is distinct from a physics solver.
- #63 overlaps camera #14, lookdev #15 and animation #13. Define shared contract ownership and adapters rather than duplicating cameras, materials or rig state. A static mesh or self-contained animated GLB fallback means Scene3D is not wholly blocked by unfinished text-to-3D.
- #64 needs durable browser edits with expected-revision conflicts, transactional operations, natural-language typed plans and explicit substitution approval. UI markers and server routes alone do not meet functional editing.
- #65-66 must preserve exact 30-step save/edit/new-process reopen/render/export/reimport/restore/rerender acceptance for four environments, two reused exact character assets, nested prefabs, independent actors and actual lighting/camera changes. Contract validation cannot close them.

| Child | Primary original sections | Disposition |
|---|---|---|
| #59 | 1, 2, 3, 4, 5, 6 | open; needs-review |
| #60 | 7, 8, 11, 12, 13, 18 | open; blocked |
| #61 | 9, 10 | open; blocked |
| #62 | 14, 15 | open; blocked |
| #63 | 16, 17, 19, 20 | open; blocked |
| #64 | 21, 22, 23 | open; blocked |
| #65 | 24, 25, 26, 27, 28 | open; blocked |
| #66 | 29, 30, 31, 32, 33, 34, 35, 36 | open; blocked |

Cross-cutting security, testing, documentation and acceptance sections map to the final child as integration ownership; their constraints also apply to every implementing child. Mapping is not permission to narrow the original requirements.

## #13 — Implement complete 3D animation system with timeline editing, interactions, and cinematic rendering

Fully read: body 63,773 characters; comments 6333769465 (37,433 characters).

- Keep open. Main has real reusable rig animation, but no general animation3d project/timeline/state-machine runtime. #67 is a ready foundational adapter task, not evidence that the whole subsystem starts from zero.
- #67-68 must provide exact rational output clocks including 120 fps and NTSC rates, half-open intervals and terminal keys; rig clips presently use seconds and bounded integer FPS. Conversion must not redefine rig or 2D clocks.
- #68-69 must retain Bezier, Hermite, Catmull-Rom, TCB and SQUAD-or-equivalent support, editable curve handles, sparse/zero-weight mixer behavior, 1D/2D blend trees, safe declarative transitions, trigger consumption and deterministic interruption.
- #70-71 must add terrain-aware contacts and stateful procedural checkpoint/bake semantics, measured drift, actor-instance-scoped tracks, interval event advancement distinct from inspecting one frame, and a single prop with continuous ownership transfer.
- #72-73 require common typed scene/camera/light/material targets, source-to-output rational retiming, cross-shot overrides/continuity, a durable real editor, preserved nonstandard-channel sidecars or explicit losses, and one evaluator shared by preview/render/export.
- #74 retains all A01-A42 gates and demonstrations A-G: at least eight seconds silent multi-shot cinematic with two actors, moving camera and light, independent facial/body motion, retargeting, reusable clip and continuous prop handoff; master frames, ffprobe zero-audio proof, nonsequential repeated-frame and interrupted-render/selective-invalidation evidence are mandatory.

| Child | Primary original sections | Disposition |
|---|---|---|
| #67 | 0, 1, 2, 3, 4 | open; ready |
| #68 | 5, 6, 7, 8, 10, 22 | open; blocked |
| #69 | 9, 11, 12 | open; blocked |
| #70 | 13, 14 | open; blocked |
| #71 | 15, 16 | open; blocked |
| #72 | 17, 18, 19, 20 | open; blocked |
| #73 | 21, 23, 24, 25, 26, 27 | open; blocked |
| #74 | 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38 | open; blocked |

Cross-cutting security, testing, documentation and acceptance sections map to the final child as integration ownership; their constraints also apply to every implementing child. Mapping is not permission to narrow the original requirements.

## Evidence and limitations

Code verified directly: scripts/model3d.py; rig_skinning.py; rig_animation.py; rig_retarget.py; rig_skeleton.py; rig_blender.py. Interface inventory and documented boundaries: references/3d-rigging.md and references/3d-rigging-acceptance.md. Main has no scene3d* or animation3d* modules. PR210/214/215 are unmerged foundation work, not main functionality; their complete review belongs to the PR audit.

The requirement-to-child tables are full section-level ownership maps, not exhaustive bullet-to-test proof. Full original text was read; no claim is made that every implementation path or every retained artifact was freshly executed. Parent closure requires granular acceptance reconciliation, current required checks, and any explicit human visual decisions.
