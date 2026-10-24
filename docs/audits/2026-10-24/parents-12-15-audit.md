# Original parent missions #12–15: complete text audit

Read-only analysis on 2026-10-24. All four original bodies and every retained comment were semantically read in sequential bounded chunks, without truncation: **360,316 characters**. `parents-12-15-coverage.json` records exact source IDs, UTF-8 SHA-256, character counts, read ranges and every numbered heading mapped to delivery children. No original-text ranges remain unread. This covers the retained raw snapshot, not comments added afterward.

Runtime baseline is `bb5a681`; observed checkout `768c6e2` is the root worker's documentation-only update. Existing real rig primitives are preserved as implemented. Scene3D/Camera3D/LookDev foundations remain open PR code, not main runtime. No GitHub, implementation or repository changes were made by this worker. No fresh renderer, browser, full suite or art acceptance was executed.

## Source-complete dispositions

| Parent | Original text | Delivery scope | Disposition |
|---|---:|---|---|
| #12 Scene Builder | 84,195 characters; body + comment 6333762001 (§28–36) | #59–66 | Keep open. Existing rig tools and PR210 contracts/static transforms are useful foundations; they do not deliver arbitrary editable multi-scene construction and four complete environment lifecycles. |
| #13 Animation | 101,206 characters; body + comment 6333769465 (§23–38) | #67–74 | Keep open. Real rig clips/deformation, blending, IK, retargeting and attachments already exist. Extend them into the general exact-time multi-actor/scene evaluator and editor; do not invent a replacement skeleton. |
| #14 Camera | 70,858 characters; body + comment 6333772785 (§30–33) | #75–82 | Keep open. PR214's mathematical/contract foundation is partial, unmerged evidence. Actual optical behavior, deterministic motion, collision, transitions, authored-camera editing and continuous footage remain required. |
| #15 LookDev | 104,057 characters; body + comment 6333777370 (§19–34) | #83–90 | Keep open. Native rig material interchange and previews exist. PR215 validates resources/graphs but explicitly does not execute them; real shader, physical-light, color and comparison acceptance remains. |

## Required corrections and ownership decisions


### C1. Camera mission recommends Z-up while Scene3D, Animation3D and existing rig code use Y-up. Camera example [0,-6,2] also reflects the conflicting suggested world basis.

Original sections: 12:5, 13:3, 14:4.1-4.3, 15:4.3.

Retain right-handed Y-up, +Z forward object convention, meters and XYZW as canonical. Camera-local forward stays -Z. Rewrite camera recommendation/example and glTF conversion as an explicit Blender boundary adapter; test camera world-basis conversion independently from generic object basis conjugation. Never convert twice.

### C2. Four missions propose their own scene/shot/camera/light/animation schemas and evaluators; Camera3D allows additive v2 while Scene3D calls for separate namespaces.

Original sections: 12:6,19,20,23, 13:4,18,19,20,22,26, 14:3,7,18,21, 15:4,5,16,18.

Keep legacy v1 closed and unchanged. Scene owns scene/node/shot identity; Animation owns exact time, track registry and sequence sampling; Camera owns optics/evaluated camera; LookDev owns materials/lights/look. Bind references via explicit adapters and schema discriminators. One integrated frame result, no independent camera/light clocks or duplicate shot authority.

### C3. Original fallback text permits minimal rig/import/render foundations if absent; main now has real rig import, deformation, FK/IK, clips, retargeting, attachments, facial controls and rendering.

Original sections: 12:11,13,15, 13:7,14,15,23,26.5, 14:21, 15:2.5,4.3,15.5.

Replace absence premises with current reuse inventory. Extend native rig contracts, time adapters and geometry evidence; do not create a parallel skeleton, importer, or solver. Native capabilities are partial reuse, not proof of complete parent acceptance.

### C4. Scene/Camera/LookDev foundations are on open PR210/214/215, not runtime main.

Original sections: 12:2,4,6, 13:1,2,26, 14:1,3, 15:2,4.

Describe each exact PR head separately. Preserve shared schema generator and test registrations. PR210 inline scene camera representation and PR214 camera reference family need explicit bridge. PR215 structural graph validity does not execute shaders. Do not recreate open implementation or mark parents complete.

### C5. Historical missing retained production-media premise is stale after source restoration.

Original sections: 12:2, 15:2.4.

Retain original history with dated supersession to current DELIVERY_STATUS; verify current source transports before attributing failures. Use independently reproducible 3D demos, but never fabricate missing provenance or relabel fixtures as production art.

### C6. Each mission demands a complete interactive editor and its own API/save/undo workflow.

Original sections: 12:22,23,24, 13:5,6,21,27, 14:7.3,20,22, 15:19,21,29.

#17 editor program owns shared application shell, command transactions, persistence and undo/redo. These domain children own concrete panels/tools and integration tests. Use one authenticated local API and revision protocol. Keep each parent end-to-end editor acceptance as a consumer gate.

### C7. All four missions request Blender orchestration, output manifests, cache/resume and export; isolated implementations would compete for output ownership.

Original sections: 12:25, 13:25, 14:19,23,24, 15:18,20,28.

#16 rendering program owns common jobs/checkpoints/output publication; domain adapters contribute evaluated state, required dependency hashes and backend translation. Reuse existing jobs/atomic-publish. Camera changes invalidate affected frames, unused cameras do not; LookDev/color transforms contribute exact hashes. Preserve silent video as real zero-audio output, not blank WAV.

### C8. Shared byte-identical character/material assets must still yield independent actor, prop and camera instances; library and continuity authority overlaps later missions.

Original sections: 12:12,18,24, 13:10,16,20, 14:14,15, 15:5.4,15,17,29.

Use library #128 immutable resources/resolver and continuity #107 logical actor/world-event semantics. Asset hashes cannot substitute for instance identity; one prop owner at each frame, neutral-light material identity versus variable illuminated pixels. Domain parents consume shared contracts without competing stores.

### C9. Eight-child indexes and small foundation tests can conceal very substantial original release gates.

Original sections: 12:29-36, 13:32-38, 14:26-33, 15:25-34.

Keep parent acceptance open until original mandatory demos and lifecycle evidence pass. Foundation closure is only bounded child closure. Record schema/math, real backend execution, browser interaction, independent geometry/video validation and human art review separately.

### C10. Core glTF cannot preserve all visibility, optical, shader graph, lighting, state-machine and interaction semantics.

Original sections: 13:24, 14:23.3, 15:6.3,28.

Emit supported channels plus explicit loss report and versioned sidecars/bakes/native .blend where required. Validate sampled round trips and original graphs. Do not advertise full fidelity based solely on a syntactically valid GLB.


## Acceptance that must remain explicit

**Scene #12:** §30 requires four substantial editable environments: courtyard, workshop, fantasy and science-fiction. Courtyard includes ten selectable props and two distinct 3D character instances; workshop reuses two character identities. Across scenes reuse at least two exact versioned character assets, multi-node imported GLB, animated GLB or verified rig integration, nested prefab, multi-level hierarchy, attachments, visibility and collision-aware generation. §31 requires the entire 30-step create/save/close/new-process reopen/edit/resave/reopen/render/GLB export/independent reimport/prior-revision restore/rerender workflow for each. Keep actual openings, protected manual edits during regeneration, schema/semantic/geometric validation, atomic operations, revision conflicts and archive closure in #60–66. Neither terrain metadata nor a rig preview satisfies this gate.

**Animation #13:** §33 requires demos A–G: skeletal locomotion/transitions/contact; two independently animated rigged actors and continuous single-prop handoff with IK/gaze; facial layers independent of body; two differing compatible rigs retargeted; cinematic multi-shot/multi-scene sequence of **at least eight seconds**; edited animated GLB export/reimport sampled against source; and nonsequential frame evaluation `0→75→10→120→48→75→0`, interruption/resume and selective invalidation. §36 A01–A42 is retained in #74, not collapsed into “some motion plays.” Exact rational output time must explicitly adapt native rig seconds. Pure frame inspection and event advancement are separate: repeated frame inspection cannot duplicate events, seek cannot fire all historic events. Spring/state-machine history requires deterministic bake/checkpoint/replay. Terminal frame N may be a key but cannot become an extra rendered frame. Required interpolation includes TCB and quaternion SQUAD/equivalent, beyond native simple tracks. Root motion, source clip reuse and prop owner continuity need scene-level integration, not a claim that existing rig math is absent.

**Camera #14:** §27 requires at least five distinct shots, three independently configured cameras, two independently animated mesh subjects, a moving prop, depth/occluders, continuous movement at a practical 640×360/24fps target, real dolly parallax, tracked moving subject, orbit/focus or rack focus, exact hard cut, another actual rendered transition, plus orthographic output. §5 requires physical Blender DOF, validated lens/sensor fit and clip planes; animated resolution/aspect inside a shot is prohibited (§7). Hard cuts must not interpolate cameras; dissolves composite both actual views with explicit overlap/local-frame mapping and color space. Editor navigation camera must not mutate authored camera. Focus is optical camera-space depth, not an arbitrary world axis. Keep §32 A–H checklists and §33 reopen/reproduce workflow attached to #82.

**LookDev #15:** §26 A–J remains release-blocking: nine laboratory cases including real transmission, indirect emission and geometric displacement; the full **3×3 metallic (0, .5, 1) / roughness (.1, .5, .9) sweep**; six shadow variations with diagnostics; per-channel texture/UV effects and deliberate UV/normal error; opaque/alpha/clear/frosted/thick-tinted glass; environment rotation, bounce color and emissive GI; stylized materials; **three shots across two scenes** preserving semantic material identity; animated light translation/intensity/temperature/emission with moving shadows; actual browser/EEVEE/Cycles outputs. §14.3 separately requires **12 style mappings**, broader than the six examples named in demo G. Demo G is not permission to drop polymer-clay, stylized-3d, wooden-toy, frosted-glass, crystal or metallic-foil mappings. §27 requires effect-specific metrics plus real art review, not one SSIM cutoff. §29 requires existing review authority, exact evidence/configuration hashes and stale-approval propagation. Keep these together under #88–90.

## Concrete boundary risks to correct in child handoffs

- #59 / #75 / #83: phase-one contracts may be closed only against their bounded acceptance; parent completion remains open. Preserve PR210/214/215 code ownership and exact heads from `pr-ledger-a.json`; current-head facts must be refreshed by root before remote mutation.
- #63 / #72 / #78 / #88: explicitly declare evaluation phases and dependency direction. Scene resolves instances/transforms; animation samples time and motion; camera consumes final targets plus bounded collision/framing correction; LookDev resolves frame properties. No cyclic “camera owns scene evaluation” or independent light timer.
- #60 / #62 / #70 / #71: reuse rig import, IK, attachment and retargeting implementations, adding general scene adapters and tests. Authoring collision avoidance is not physics simulation; do not demand a new solver just to implement placement.
- #64 / #73 / #81 / #89: tie persistent panels and browser tests to the shared #17 command model. Natural-language Scene3D authoring must produce reviewable typed operations and an offline deterministic parser, with explicit substitutions; a separate paid planner is optional, not a dependency of basic scene generation.
- #65 / #73 / #80 / #89: common jobs need all significant dependency keys, safe output ownership, cancellation and previous-good-output retention. Silent 3D encoding uses zero audio streams and does not alter legacy 2D audio masters.
- #84 / #85 / #86 / #87: backend capability matrices must distinguish native, translated, baked, approximate and rejected. Physical photon/photometric unit conversion is not copying the same intensity scalar. Non-color textures are not gamma decoded. Alpha coverage is not transmission; AO is not GI; normal/bump maps are not displacement.
- #88: reconcile style override ordering with frozen style/identity authority: explicit user intent, approved identity, frozen resolved style, authorized material overrides, scene controls, backend defaults. Do not overwrite canonical coat color to emulate warm illumination; material identity checks require neutral calibration and exact references.
- #90: mandatory actual rendering CI cannot be silently changed to mocks. Retained ledger shows PR215's Windows symlink fixture privilege error (29 pass, one setup error) versus hosted green checks; neither state proves LookDev art acceptance. Correct fixture capability handling without weakening production path rejection.

## Verification limits

The audit consumed `architecture-audit.md`, focused retained issue/PR ledger fields, source function/coordinate anchors, and all original parent text. Actual source anchors checked include `rig_animation.py` evaluate_track/evaluate_animation/blend_poses/crossfade/bake_clip, `rig_ik.py` solve_ik, `rig_retarget.py` retarget_animation, and the trusted `rig_blender_worker.py` Y-up/Blender conversion and Cycles CPU preview. The initially attempted `references/3d-rigging-contracts.md` path was absent; no claim relies on that file. A broad ledger dump truncated; relevant ledger fields were reread in a bounded untruncated query. Original parent text reads did not truncate.

This report is an exhaustive original-text review and ownership/acceptance correction, not a new acceptance run. Source hashes prove snapshot coverage; they do not prove implementation or rendered quality.
