# Render3D foundation audit and acceptance map

This is the full audit required by [#91](https://github.com/CompleteTech-Skills/creative-studio/issues/91) and [#16 §2–5](https://github.com/CompleteTech-Skills/creative-studio/issues/16). It supplements the original audit rather than changing historical evidence. The audited foundation is `c809829debf565f2030feebad3b6066b50dc43c4`, including main `4d83833`. The seven audit deliverables below are explicit; interface representation and release gates remain separate from writing an audit.

The baseline was checked against `CONTRIBUTING.md`, `SKILL.md`, the current workflows, dependency files and source-delivery report. No repository `AGENTS.md` was found. The repository is Creative Studio; the installed skill name and legacy directory remain `hatch-pet`. Existing acquisition, review, source, coordinate, archive and CLI authorities remain authoritative.

The [machine-readable audit](../tests/results/render91-audit-20261113/audit.json) records source hashes, evidence boundaries and field-level requirement links. The [current source-workflow receipt](../tests/results/render91-audit-20261113/source-workflow.json) retains exact synthetic source bytes and actual command outputs. This document does not certify downstream rendering, hardware, silent-video, artistic approval or the completion of #16.

## 1. Dependency map and examined reusable owners

| Examined owner | Existing API/behavior retained | Render integration decision |
| --- | --- | --- |
| `production.py` | `dispatch`, production render/validate/scene/shot/recovery commands | Keep its dispatcher and production-v1 meanings; explicit `render3d.py` entry point |
| `production_render.py` | `renderer_identity`, `render_shot`, `render_scene`, `_lock_output`, checkpoint validation, PNG cache, PCM and FFmpeg | Reuse semantics and evidence conventions; do not route layered scenes through a mesh renderer |
| `production_contracts.py` | Versioned project/cast/clip/scene/shot validation and file references | Preserve public schemas, pixel units and existing bounds |
| `production_performance.py` | `evaluate_track`, `evaluate_clip`, instance evaluation, continuity and dialogue timing | Preserve zero-based integer production frames and rational time; no millisecond reinterpretation |
| `production_workflow.py` | `shot_dependencies`, `record_review`, freshness, dependency snapshots, acquisition ownership, archive/restore | Retain provenance and review authority; a render snapshot is not an approval |
| `production_sources.py` | Original source transport verification and restoration | Use verified original bytes; never replace missing source or speech with generated media |
| `production_bundle.py` | Portable project parts, archive and restoration constraints | Reuse path/hash/ownership constraints; Render3D archival closure remains its own downstream acceptance |
| `production_adapter.py` | OpenRaster export, editor handoff, reconstruction and optional real Krita exercise | Preserve source-over handoff; 3D geometry cannot be labeled an editable layered drawing |
| `doctor.py` | Local tool/package capability discovery | Report actual availability; declaration or executable discovery is not initialized/rendered support |
| `assets/production-preview.html` / `.js` | Exact frame/shot display, rational interval text, audio seek, pinned comparisons and resource controls | Preserve the existing 2D preview and its evidence identity; add 3D consumers separately |
| `atomic_publish.py` | Exclusive publication preserving existing destinations | Reuse the canonical primitive; do not overwrite a competing destination |
| `rig_models.py`, `rig_contracts.py`, skin/skeleton/animation validators | Native source geometry, rig semantics and reviewed source references | Native adapter validates bounded private regular-file copies through these owners |
| `scene3d_contracts.py` | `load_project`, exact document closure and version/reference validation | Scene adapter delegates semantics; its stable-input owner boundary is explicit |
| `rig_math.py`, Camera3D, LookDev and Animation3D owners | Canonical +Y-up/+Z-forward/meters/XYZW transforms and typed subsystem data | Explicit conversion and owner adapters; no second camera/material/animation registry |
| Tests, requirements and delivery references | `test_production_render.py`, related contracts/compatibility tests, `requirements-test.txt`, `DELIVERY_STATUS.md` and the four production references | Baseline and release evidence remain revision-bound; optional tools stay isolated |

The existing renderer identifies itself as `hatch-reference-rgba`: 8-bit encoded-sRGB source-over with straight output alpha. Its lighting declarations do not become physical 3D lights. Its locks and checkpoints are distinct from process-local validation and from a new Render3D job store. A new controller must consume the existing job authority rather than interpreting a 2D checkpoint as a 3D lease.

`DELIVERY_STATUS.md` records the recovered original production transports, speech and review history. The historical October missing-source limitation is retained as history, not repeated as the current state. The current focused baseline below uses explicitly synthetic fixtures; it does not independently repeat every production-pilot reconstruction or replace the retained public-delivery CI evidence.

## 2. Baseline and regression report

| Evidence | Revision and observed result | Limit |
| --- | --- | --- |
| Current production/compatibility baseline | `c809829`; `PYTHONPATH=scripts:tests python -m unittest test_production_contracts test_production_render test_rig_compatibility -v`: **56 passed**, zero skips/failures/errors | 31 production-contract, 20 renderer and 5 compatibility cases; synthetic layer/audio fixtures, including an actual FFmpeg movie check |
| Current foundation source tests | `c809829`; **59 focused Render3D tests passed** in the newly spawned whole-feature review | Actual source capture/reopen, field boundaries, exact clocks, FIFO and source-growth negatives; no rendered-image claim |
| Combined current schema/math gate | `c809829`; **178 passed**, schema/template parity and diff checks passed | Compatibility discovers **955** 2D tests; discovery is not a 955-test execution |
| Current actual source workflow | `c809829`; native capture, moved-original/relocated snapshot verification and native geometry reopen; Scene3D capture/verify outputs agree exactly | Synthetic native source has **4 vertices / 2 triangles**; 25 retained artifacts, 34,121 bytes; no Blender or visual approval |
| Historical foundation baseline | [Original receipt](../tests/results/render3d-foundation-20261025.json): 39 foundation, 31 contract, 20 renderer, 5 compatibility and 70 schema/math passes | Original revisions, commands and hashes remain unchanged; initial missing `pygltflib` environment failure is retained |
| Current hosted and postmerge gates | Recorded separately in `audit.json` and subsequent immutable receipts | A local pass or old-head CI is not current-head hosted success; postmerge verification remains mandatory |

Exact baseline command, environment, log SHA-256 and source-file SHA-256 values are in `audit.json`. Original production-art/review receipts are not edited. No pre-existing functional failure appeared in the current 56-test run. Optional dependency availability is an environment fact and must be reported separately from a code regression; an unexecuted native/application gate is not converted to a pass.

Recheck the retained source workflows from a fresh checkout with the declared native dependencies installed:

```bash
python scripts/render3d.py verify-snapshot tests/results/render91-audit-20261113/native-source-snapshot
python scripts/render3d.py verify-snapshot tests/results/render91-audit-20261113/scene-source-snapshot --adapter scene3d
python scripts/render3d.py snapshot-rig --source tests/results/render91-audit-20261113/native-source-snapshot --destination NEW_NATIVE_SNAPSHOT
```

Use a new destination. The native fixture, identity, geometry and clips are actual retained bytes. Scene3D verification establishes its document/closure semantics, not decoded mesh topology or pixels. The source-workflow receipt lists the original executed commands and every retained artifact hash.

## 3. Explicit 2D/3D compatibility matrix

| Concern | Existing 2D contract | Additive Render3D contract / compatibility gate |
| --- | --- | --- |
| Dispatch and formats | `production.py`, production v1 schemas | Separate Render3D v1 documents and CLI; unknown versions/modes reject |
| Assets | RGBA raster layers and reviewed original transports | Exact native rig/Scene3D source closure; no implicit raster-to-mesh conversion |
| Coordinates | Screen/pixel and existing layer/camera semantics | Meters, +Y-up, +Z-forward, XYZW, column-vector transforms; explicit Blender conjugation |
| Timing | Zero-based frames, half-open ranges, rational fps; legacy pet milliseconds remain separate | Exact presentation/source fractions and explicit Blender frame/subframe conversion |
| Compositing / alpha | Encoded-sRGB source-over, straight output | Explicit format/color/alpha declaration; schema support is not backend fidelity proof |
| Lighting | Existing planning declarations | Physical lighting stays with LookDev and an executing backend; no relabeling |
| Animation / camera | Layer/character tracks and shot-local 2D cameras | Owner-validated rig/Scene3D/Camera3D data and explicit source evaluation |
| Provenance and review | Original source/review ledger and freshness rules | Snapshots preserve evidence; neither fixture nor successful validation grants approval |
| Ownership / locks | Existing OS render locks and checkpoints | Existing exclusive publication plus native job leases/reservations; independent state namespaces |
| Cache identity | Existing renderer/dependency/PNG signatures | Frame/settings/source/backend fingerprints; old 2D cache entries are not accepted as 3D pixels |
| Output / archive | Existing PNG/PCM/movie/editor/source archive formats | Separate image/pass/result contracts and downstream verified closure; no silent archive migration |
| Audio / FFmpeg | Exact existing PCM sample accounting and movie verification | Rendering does not rewrite speech; silent video is explicit and separately independently verified |
| Optional packages | 2D discovery works with 3D imports blocked | Native/Blender/decoder/hardware dependencies are opt-in and tested in their own environment |
| Failure / resume | Existing typed production failures and recovery | Stable Render3D taxonomy; no success-shaped fallback for missing tools or unknown features |
| Existing destination | Existing collision/ownership preservation | New exclusive publication also preserves even a racing empty destination |
| Production acceptance | Existing historical engineering/artistic distinctions | Source/schema/local tests, rendered output, independent decoding, visual review and production approval remain distinct |

The compatibility gate is `test_rig_compatibility.py`; the 20 renderer regressions exercise pixels, alpha, filters, transforms, resumability, stale cache, rational audio and a real offline movie. The separate public-delivery workflow remains the complete legacy release gate.

## 4. Module and contract layout

| Owner | Public boundary | Release meaning |
| --- | --- | --- |
| `render3d_contracts.py` | Strict dictionary/schema utilities, `RenderBackend`, `ArtifactVerifier`, `frame_times`, `make_frame_spec`, `canonical_to_blender` | Interface and deterministic planning, not execution |
| `render3d_snapshot.py` | `NativeRigAdapter`, `Scene3DAdapter`, `create_snapshot`, `verify_snapshot` | Immutable exact-byte source admission and owner revalidation |
| `render3d.py` in foundation | Validate, snapshot, verify and frame planning commands | No fake render/probe/completion alias |
| Six `profiles/render3d-*.schema.json` files | Request, snapshot, frame, job, result and capability schemas | Generated from the one contract authority; no hand-edited divergent schema |
| Native `render3d_jobs.JobStore` follow-up | Exact persisted request resolver and derived context/progress/reservations | Same scheduler/store authority; no duplicate job representation or parallel request store |
| Backend, verifier, hardware, quality, CLI, archive and acceptance follow-ups | #92–#98 and their existing owners | Real images, device readiness, cache/recovery and complete deliveries require their own actual acceptance |

Existing dictionary/schema conventions were retained instead of adding a new modeling dependency. Structural schemas and explicit semantic checks coexist: a valid capability document cannot claim initialization/execution from discovery, and a complete result requires a passed verifier record and complete frame accounting. No protocol ellipsis is evidence that its backend operation executed.

The field-level map in `audit.json` covers every foundation document field and every mandatory parent §4 semantic requirement. Job-only context is resolved by exact native job ID plus canonical request digest through the explicit `JobStore` instance. The store is the lookup authority, just as a project root resolves project-relative references. It is local/same-host, not an unspecified remote registry. The job follow-up documents and tests tamper rejection, strict priority, native frame partition, reduced completion fraction, retry policy and actual capacity reservations. Reservations are not measured usage.

## 5. Migration and rollback

1. Keep existing production, pet/profile, source, review, PCM and archive formats untouched. Select Render3D explicitly; never infer 3D from a legacy camera or lighting field.
2. Capture a new immutable source snapshot into a new destination. Preserve original source, receipts, prior render outputs and existing caches.
3. Replan pre-release FrameSpecs that lack required project/scene/shot selection. Do not rewrite them in place or reuse their pre-binding cache identity.
4. Resolve jobs through their original native store and exact request digest. A status projection cannot mutate ownership or introduce a second transition authority. Additive store migrations preserve older reservation rows and report unattributed reservations honestly.
5. Keep request/backend/source identities and measured-versus-unavailable resource values intact across follow-up integration. Missing tools, mismatched identities and unsupported features fail closed.

Rollback removes or reverts only additive Render3D entry points/modules/schemas and their specific schema/test registrations. Preserve the Editor3D, physics and other registrations in the same shared generators/collectors. Existing 2D source formats and historical receipts require no data migration. Disable new rendering entry points before rolling back their runtime owner; keep already published output and original store/source evidence. Do not delete an existing destination, preservation branch, corrupt evidence or user work to make a rollback appear clean.

## 6. Dependency and hardware-support plan

| Layer | Dependency / discovery | Evidence required before advertising support |
| --- | --- | --- |
| Existing 2D | `requirements-test.txt`, Pillow/jsonschema/Playwright and existing FFmpeg/ffprobe checks | Complete public-delivery/2D gates; 3D packages remain unnecessary for 2D discovery |
| Contract/planning | Repository schema utilities and exact rational arithmetic | Current contract/boundary/schema parity tests; no GPU needed |
| Native source adapter | Declared rig dependencies, NumPy/geometry tooling and pinned Khronos validator configuration | Actual mesh/skin/clip source decode, immutable capture/reopen and invalid-source rejection |
| Scene source adapter | Canonical Scene3D document/reference owner | Actual project closure validation and relocation/selection tests; declared geometry metrics are not pixels |
| Blender backend | Explicitly supported LTS version/API, executable/worker identity and bounded subprocess resources | Version-gated actual CPU geometry renders, preparation/output admission, cancellation and independent image inspection |
| PNG/EXR inspector | Declared optional decoders with tested precision/channel policies | Actual byte-level decode, corruption/HDR/alpha/pass/identity negatives; declarations alone do not establish support |
| GPU / fallback | Actual Blender backend enumeration, driver/environment identity, short geometry initialization/render probe | CPU/GPU execution separately recorded; `gpu-required` cannot succeed through CPU fallback; no universal driver/OS claim |
| Quality / helper integration | Bound settings, canonical owner plans and helper-source fingerprints | Actual format/pass/color/alpha outputs, cache invalidation and independent pixel inspection |
| Silent video / archive | Existing FFmpeg/ffprobe and exclusive archive/publication owners | Actual frame stream/timebase/zero-audio verification and complete exact closure roundtrip |

The recorded execution environment is Linux. The new descriptor-pinned private publication helper is Linux/procfs-only and fails closed elsewhere; its consumers need separate capability-preserving integration and tests. That helper approval does not establish portable 3D runtime support. Preserve the existing 2D cross-platform gates and make unsupported 3D host/filesystem capabilities explicit.

The audit receipt records the exact installed environment used for its observations. Declared requirement ranges and an observed environment are different claims. Keep optional 3D/decoder dependencies in their dedicated gate; report a missing package/tool as an environment failure with the original log. Discovery, initialization, execution and cached historical evidence remain separate capability states. CPU engineering tests do not establish CUDA/OptiX/HIP/Metal/oneAPI support or production visual acceptance.

## 7. Implementation sequence and acceptance gates

| Gate | Authority and required evidence | Closure rule |
| --- | --- | --- |
| A — Existing baseline | Current instructions/owners, 2D source-delivery status, selected regression and exact receipts | Keep pre-existing failures and collection counts explicit |
| B — #91 interfaces / snapshots | All parent §4 fields or tested exact native equivalents, strict errors, immutable source workflows, exact timing/coordinates | Complete field traceability, newly spawned whole-final-head review, current hosted checks, normal merge and postmerge receipts |
| C — #92 backend | Bounded preparation/execution, actual CPU geometry/animation/camera outputs, version/source/output ownership | No schema-only or fake-worker rendering success |
| D — #93 image/quality boundary | Independent PNG/EXR decode plus actual pass/color/alpha/precision evidence | Reject unsupported precision and corrupt/partial output |
| E — #94 hardware | Real device probes, explicit policies and controlled fallback with actual hardware identity | No device-name-only availability claim |
| F — #95/#96 jobs/cache/recovery | Existing native JobStore leases/reservations, actual interrupted render/reuse/repair, capacity/ownership negatives | No second scheduler; completed status derives from verified frame state |
| G — #97 tools/delivery | Coherent CLI, preview, silent export and portable source/output evidence | Real command execution and verification, not only adapter mocks |
| H — #98 / parent #16 | Required complete multi-shot films, source/export/reopen, cancellation/recovery, independent media checks and explicit visual review | Keep parent/downstream issues open until their complete acceptance and postmerge checks actually pass |

Partial source-only PR #317 may merge as a bounded dependency without closing #91. The separate job-binding and audit follow-up must receive a newly spawned whole-final-head review and current hosted checks. After normal merge, rerun meaningful source/schema/legacy verification against the merged revision, wait for main hosted postchecks, and re-audit #91. A partial job/schema audit, local pass or merged dependency is not authority to close the leaf or its parent.
