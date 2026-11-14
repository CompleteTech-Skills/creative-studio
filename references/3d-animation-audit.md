# Animation foundation audit — 2026-10-25

Baseline: `bb5a681` on `CompleteTech-Skills/creative-studio` (`origin/main`), isolated branch `agent/mod1-issue67-20261025`. Authenticated coordinator verified repository ID 1425032192. Issue #67 is unassigned, has no claim comments and no competing animation branch; the open PR inventory contains camera, lookdev, physics, scene, continuity, library and rig work. This work does not modify any of those branches. The repository has no tracked `AGENTS.md` or `CONTRIBUTING.md`; `SKILL.md` requires native source/review ownership, fixture classification, fixed pet compatibility and separate optional 3D dependencies.

## Existing ownership

| Area | Existing authority and compatibility boundary |
|---|---|
| Fixed atlas and state definitions | `hatch.py`, `profile_spec.py`, `state_catalog.py`, `state_selection.py`; unchanged dimensions, ordering, millisecond timing and catalog meanings. |
| Production cast, exact frames, tracks | `production_contracts.py`, `production_performance.py`, `production.py`; retain integer-frame/rational-fps 2D coordinates and existing clips. |
| Raster render, timing and AV | `production_render.py`, `production_adapter.py`; do not turn the existing audiovisual mux contract into silent 3D output. |
| Source/workflow/review and archive | `production_sources.py`, `production_workflow.py`, `production_bundle.py`, `asset_contracts.py`, `portable_runs.py`; preserve immutable provenance, native source status and review authority. |
| Real rigs and animation | `rig_models.py`, `rig_math.py`, `rig_animation.py`, `rig_skeleton.py`, `rig_skinning.py`; actual geometry, skeletons, skinning, local pose channels, interpolation and contacts already exist. Reuse them. |
| Native schemas and optional dependencies | `rig_contracts.py`, `model3d_contracts.py`, `generate_schemas.py`, `requirements-rig.txt`; new schema discovery has no numeric imports; numerical sampling remains optional. |
| Other active 3D owners | Open #210/#214/#215 and their branches define scene/camera/lookdev contracts; this branch does not clone their implementations or claim their integration. Physics #213 and registry #207 remain separate. |

Reviewed README, SKILL, delivery status, source API inventories, production/reference contracts and retained rig/asset audits. Historical delivery metrics remain bound to their recorded revisions; they are not this candidate's results. Existing `rig_animation` is continuous-time and validates native `hatch-3d-animation` clips up to its own configured rate/duration limits. New rational animation documents deliberately do not relax those native limits or replace the existing clip format.

## Design decisions and risks

Use additive `animation_*_3d` documents and new generated filenames. Keep strict syntax separate from actual source/owner readiness. Resolve native rig targets from saved hash-verified files, not user expressions. Reuse the native interpolator for single-channel sampling. Keep exact clock arithmetic until the bounded local segment boundary. Preserve all old schema generation entries and 2D test isolation; add the new test modules to the existing native schema/math gate. No workflow is added, activated or bypassed.

The parent mission defines full scene execution and cinematic evidence. This contribution supplies a foundation and a real rig-channel integration test only. It has no editor, renderer, external providers or human approval. Unsupported property owners, interpolation/extrapolation and dissolve sequencing fail rather than receiving a success-shaped result. Cross-document graph validation and transactional project snapshots need downstream owners. See [contracts and limitations](3d-animation-contracts.md).

## Verification and traceability

| #67 scope | Executed implementation/evidence | Remaining boundary |
|---|---|---|
| Audit and compatibility | This audit; old rig and 2D files preserved; native/2D gate separation | Hosted checks and all parent demonstrations remain independent gates. |
| Project/clip/target/layer/state/interaction/sequence/render schemas | 13 canonical generated schemas; every root validated against schema/runtime tests; typed references and local relationships | Full project dependency graph and every foreign target owner are not resolved. |
| Rational/subframe clocks and TRS/quaternion mathematics | Independent literal numeric oracles, required integer/NTSC clocks, terminal/large-index tests, native mathematics reused | No universal bitwise cross-hardware or render-equivalence claim. |
| Malformed/unsafe/unsupported rejection | Duplicate JSON/keys, nonfinite, type mismatch, path/hash tamper, missing rig target, malformed IK/layer/state checks | Solved contacts/constraints and geometric interaction checks remain downstream. |
| Actual affected workflow | Real procedural rig loaded and native skinned vertices moved by a sampled translation; nonsequential repeated sampling | Fixture geometry is engineering evidence, not production artwork. |

Tests and their exact command results are recorded in the coordinator's candidate delivery report. `test_animation3d_independent.py` is authored by an independent test worker. Native Python/NumPy behavior is locally exercised; unavailable external applications or hosted CI must remain unverified. No local pass closes the full parent mission.
