# Parent #153 — complete original mission audit

Review date: 2026-10-24. Default-branch source: `bb5a6810afd06cf57185ffdfe2234e0fdc6e6240`. Read-only review; no GitHub or implementation mutations.

## Read attestation

The entire 61,038-character body and entire 37,809-character continuation comment **6333974000** were semantically read in bounded, untruncated chunks: **98,847 characters**. Coverage includes the execution hierarchy, mission, sections 1–24, all 13 §8 categories, all production gates A–L and final success condition. `parent-153-coverage.json` records source IDs, exact UTF-8 SHA-256, contiguous read ranges and section/category-to-child ownership.

## Individual disposition

Keep #153 **open**, label `type:feature`, `area:physics`, `priority:p2`, `status:blocked`. Preserve the mandatory rigid-body-first roadmap and every category's applicable release gates. The parent is not complete when contracts, a backend smoke test or an isolated falling-object example exist. Retain legitimate optional-backend/installed-feature limitations explicitly; they do not turn missing mandatory implemented behavior into success.

Stage 0 and contract work are substantive completed subsets. Update progress around the actual merged audit/contracts and separately reviewed PR #212/#213, not around the original filing-time “no implementation” statement. The hierarchy itself already says tasks are not completion claims and appropriately preserves category gates.

## Verified source and test boundary

Main contains only `scripts/physics_contracts.py` as physics runtime source. Its opening explicitly says no solver is imported or executed. It defines strict `PhysicsWorld`, `PhysicsJob`, `PhysicsCache`, `PhysicsDiagnostic`, bounded semantic/reference validation, physical material request arithmetic and canonical document hashing. `references/physics-3d-contracts.md` expressly excludes backend probing, simulation, bake, rendering and visual approval; its structural example uses zero hashes deliberately and is not a measured simulation.

Newly executed on this exact main checkout: `../.venv/Scripts/python.exe -m unittest discover -s tests -p test_physics_contracts.py -q` — **22 tests passed in 0.771 seconds**. Bytecode writing was disabled; Git status remained clean. This is contract verification only, not physical correctness, full-suite regression, supported-platform proof, or a real backend run.

Separate PR-tree source inspection: #212 has substantive Blender/PyBullet lifecycle, admission, process bounds and cache primitives; it explicitly restricts features (e.g. unsupported joints/forces/initial velocity/masks/categories in specific paths). #213 contains rational scheduling and interpolation of completed states in `physics_timing.py`. These open-PR sources are not merged main behavior. No real solver execution from these PRs was performed here; their dedicated review controls readiness. Native transform JSON export alone is not the full animation/deformation interchange or cinematic render system.

## Requirement ownership — every original section

Mapping is ownership, not completion. The original parent remains the acceptance authority when a child checklist is abbreviated.

| Section | Original scope | Owning children/phases |
|---|---|---|
| 1 | Non-negotiable engineering requirements | #166, #167, #168, #169, #170, #187, #189 |
| 2 | Repository inspection and preservation | #166, #167, #189 |
| 3 | Target architecture | #167, #168, #183, #184, #185, #186, #187 |
| 4 | Real capability-aware backend adapters | #168 |
| 5 | Canonical physical-world contracts | #167, #171 |
| 6 | Exact stepping, numerical stability and reproducibility | #169, #189 |
| 7 | Staged roadmap | #154, #155, #156, #157, #158, #159, #160, #161, #162, #163, #164, #165 |
| 8 | Every required category | #170, #171, #172, #173, #174, #175, #176, #177, #178, #179, #180, #181, #182 |
| 9 | Cross-simulation interaction and coupling | #183 |
| 10 | Cache, bake, replay and invalidation | #184 |
| 11 | Simulation-to-animation conversion | #185 |
| 12 | Scene/render/silent-video/multi-shot integration | #185 |
| 13 | Fidelity and resource budgets | #169, #187 |
| 14 | Safe execution and numerical failure handling | #168, #187 |
| 15 | Preview and physical debugging | #186 |
| 16 | Command-line API | #186 |
| 17 | All category demonstrations and Physics Laboratory | #188 |
| 18 | Layered testing | #189 |
| 19 | Documentation and operations | #189 |
| 20 | Production gates A-L | #188, #189 |
| 21 | Definition of done per feature | #189 |
| 22 | Final deliverables and traceability | #189 |
| 23 | Engineering decision rules | #166, #168, #183, #184, #187, #189 |
| 24 | Final validation procedure | #189 |
| final | Final success condition | #188, #189 |

| Category | Scope | Leaf |
|---|---|---|
| 8.1 | Rigid dynamics/gravity/forces/impulses | #170 |
| 8.2 | Geometry/contact/filtering/queries | #171 |
| 8.3 | Physical constraints/mechanics | #172 |
| 8.4 | Character controller/grounding/authority | #173 |
| 8.5 | Grab/carry/drop/throw/handoff | #174 |
| 8.6 | Cloth/pinning/collider response | #175 |
| 8.7 | Soft-body deformation/replay | #176 |
| 8.8 | Physical hair/secondary motion | #177 |
| 8.9 | Seeded evolving particles | #178 |
| 8.10 | Real smoke and real fire | #179 |
| 8.11 | Real 3D liquid | #180 |
| 8.12 | Fracture triggers/dynamic fragments | #182 |
| 8.13 | Environmental forces and applicability | #181 |

## Concrete gaps and corrections

1. **Keep one spatial authority.** §5.1 *prefers* Z-up; it does not override mandatory reuse of actual repository contracts. `physics_contracts.py:22/29` imports `rig_contracts.COORDINATES`, uses +Y-up gravity `[0,-9.81,0]`, meters/radians and XYZW. `rig_contracts.py:8` is the shared authority. Accept this documented choice and require tested Blender `(x,y,z) -> (x,-z,y)` basis/gravity/inertia/rotation conversion. Do not globally flip existing rigs or reclassify correct Y-up behavior as a violation. Existing 2D pixels and audio contracts stay unchanged.
2. **Contract fields are not capability claims.** #167's bodies/constraints/cloth/gas/liquid/cache schemas create configuration semantics only. #168 must actually probe specific features using real execution, distinguishing discovery from verified readiness and backend version from adapter version. #169 must prove the effective solver timestep, rational `30000/1001` sampling, full-step brackets/interpolation and same-environment tolerances. Source `document_hash` is a full-document hash, not a selective cache key. A valid cache manifest with a path does not prove saved bytes or replay.
3. **Complete rigid behavior remains #170/#171.** Preserve off-center force/torque, inertia, mass-independent free-fall, impulse/angular response, gravity-off, damping/sleep/wake/resting, actual contact/friction/restitution and collision masks/exclusions. Falling transforms from a backend smoke test do not cover these. Unsupported dynamic concave geometry, tunneling/CCD, sensors, queries and contact data must be truthful; lack of a queried contact manifold cannot silently become claimed support. Physical material combination arithmetic must match the actual backend or be rejected.
4. **Mechanics and actor interactions need solver evidence.** #172 must validate joint anchors/limits/motors/stability through physical constraints. #173 distinguishes dynamic/kinematic/root-motion authority and geometry-constrained walking/jumping/slopes/walls from authored clips; torso collision is not foot planting. #174 must preserve stable prop/actor IDs, event order, world transform and release linear/angular velocity continuity, collision restoration and two-character handoff. A fixed attachment/Bezier throw is not a complete physical interaction.
5. **Retain every category, not only the combined-demo subset.** #175 requires both wind-driven flag and moving-character garment; #176 real impact deformation; #177 physical lag/overshoot/damped settling; #178 actual births/lifetimes/seeded particle trajectories; #179 both smoke and fire with evolving gas caches; #180 volumetric liquid/container/mesh evolution; #182 explicit physical threshold or declared authored trigger followed by real fragment dynamics. Static fog, shaders, sprite particles, sinusoidal cloth and static debris cannot substitute. Conditional backend support must remain visible and must not be counted as executed passing categories.
6. **Coupling is directional.** #181/#183 must name fully coupled, one-way, sequential, bake-driven or unsupported edges and test them. Wind affecting cloth does not prove rigid-body wind; an obstacle affecting fluid does not prove fluid pushes the obstacle. Independent solvers without state/force exchange are not fully coupled. Preserve actual upstream cache identities and dependency invalidation.
7. **Move minimal safety/recovery into early execution.** The original roadmap says shared infrastructure early. Although #187 is a late integration stage, timeout, memory/disk/geometry admission, isolation, cancellation ownership and nonfinite diagnostics must precede every real job in #168 onward. Likewise #184's dependency on full coupling #183 must not prevent earlier rigid-body cache integrity/replay/restart protection. Integrate these primitives early under their existing owners, retain later full-category tests, and avoid duplicate worker/controller authorities. Final review should distinguish this sequencing correction from unauthorized deletion of dependency gates.
8. **Do not overclaim resume or cache identity.** #184 requires actual file inventories/hash checks, atomic state transitions, corruption/partial-output tests, last-good preservation and killing an actual backend process. Resume is allowed only when all required solver state is restorable; otherwise report clean restart. Render exposure/resolution/color-only changes should preserve independent physics caches; collider/gravity/motion/upstream changes invalidate affected bakes. Complete native Blender cache compatibility is version-bound.
9. **Animation/render bridge is still real work.** #185 must preserve original physics scene plus separately versioned baked output; exact time/coordinate/quaternion semantics; verified import/export for the actual format; geometry/native caches for deformations that glTF cannot represent; multi-shot continuity without camera-cut world resets; arbitrary requested-frame replay without recomputation. Silent MP4/WebM stream inventories require FFprobe verification when encoding occurs. Do not remove legacy 2D audio to achieve 3D silence.
10. **Browser labels and controls must be honest.** #186 must implement working control/status/inspection and physical debug overlays, with meaningful unavailable behavior. Imported cached states are “baked replay,” not a browser solver. Approximate editor preview, live backend simulation, fixture output, rendered output and human visual approval remain separate.
11. **Mandatory final acceptance is larger than the laboratory.** #188's laboratory must include real environment/ground/obstacles, actor pickup/carry/throw/contact, cloth, particles, at least one real selected environmental effect, multiple cameras/shots and cache-driven renders; video is required when the intended pipeline is available. Separately execute all category examples and gates A–L, including both smoke/fire and liquid under supported backend availability. Optional advanced content in the *combined scene* does not make the separate soft-body/hair/destruction category gates optional.
12. **Evidence remains per feature and per environment.** #189 must retain §21's complete per-feature checklist: contract, backend, executed solver, capability, unsupported handling, budgets, self-contained example, real cache/state, renderable output, numerical tests, invalidation/failure handling, docs and regressions. Preserve tests for real interruption/relocation/platform behavior, inspect beginning/middle/end frames, numerical state changes, measured resource reports and replay/recompute tolerances. Mocks or a fixture JSON matching itself cannot pass a backend gate; an automated image metric cannot assert human review.

The existing leaves broadly preserve the original mission through explicit original-section references. Add the above detail to existing leaf acceptance/traceability where shorthand could mislead. There is no basis to close the parent or mark omitted categories complete, and no need to create a competing physics program.

## Verification limits

Full original source read, section coverage, selected child bodies, architecture audit, retained ledger and targeted source inspection were completed. Only the 22 main contract tests were newly executed. No new Blender/PyBullet simulation, browser session, render, video inspection, provider work or full regression suite ran here. Archived and sibling-review evidence must retain its own revision, command and environment attribution.
