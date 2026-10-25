# Render3D contract and snapshot foundation

This is the bounded foundation for [issue #91](https://github.com/CompleteTech-Skills/creative-studio/issues/91), based on main `bb5a6810afd06cf57185ffdfe2234e0fdc6e6240`. It does not complete #91 or the full renderer mission #16. The current executable source adapter freezes an existing native rig for preview consumption. Full Scene3D snapshot adaptation awaits the Scene3D owner in #59; real Blender rendering, scheduling, image decoding and output publication belong to #92–#98.

## Current ownership audit

| Existing owner | Retained responsibility | Render3D integration |
| --- | --- | --- |
| `production_contracts.py`, `production_performance.py` | Legacy production v1 validation, exact frame timing and 2D performance | No schema or interpretation changes; explicit Render3D v1 documents |
| `production_render.py` | Pillow compositor, locks, checkpoints, source-over semantics, PCM and FFmpeg | No implicit 2D-to-3D conversion; not imported by the new contracts/snapshot modules |
| `production_workflow.py`, `production_sources.py`, `production_bundle.py` | Existing project identity, provenance/review, source delivery, archive ownership | Preserve originals and review history; new snapshots do not confer review approval |
| `atomic_publish.py` | Cross-platform exclusive directory publication | Reused directly; existing destinations, even empty ones, are never replaced |
| `rig_models.py`, `rig_contracts.py` | Native geometry, skins, animation, source identity and review evidence | `NativeRigAdapter` calls `load_rig` before and after copying the complete referenced closure |
| `rig_math.py` | +Y-up, +Z-forward, meters, XYZW, column-vector transforms | Reused for explicit canonical-to-Blender conjugation |
| Scene3D #59, Camera3D #75, LookDev #83, Animation3D #67 | Separate upstream scene/camera/look/animation authorities | No duplicate scene, material, light or camera schema; absent adapters remain unsupported |
| `doctor.py`, `rig_blender.py` | Current optional dependency discovery and rig preview Blender execution | New capability documents separate discovery, initialization and execution; no availability is asserted by schema validity |

The source-delivery report currently records recovered original production source, speech and review transports. The historical missing-source condition in the original #16 prompt is not the current documented state. This change does not regenerate, modify or reapprove that material. Baseline source and references were read directly in this checkout; hosted CI is outside this local validation.

## Versioned contracts

`render3d_contracts.py` owns six generated schemas: snapshot, request, frame, job, result and capabilities. Unknown fields, versions, modes, nonfinite numbers, unsafe paths, nonreduced rational numbers and impossible frame ranges reject. Configuration does not assert backend support; PNG/EXR policy combinations are bounded, and adapters must reject settings they cannot actually execute.

Requests bind project/scene/shot versions, exact snapshot file bytes, half-open frames, rational output rate and source timing, camera ID, engine/device policy, samples/seed, passes/color/alpha, resource bounds, retry/priority/cache policy and destination ownership. Jobs hold the request/snapshot digests, backend identity, counts, lifecycle/worker/heartbeat, cancellation and failure data. Results partition requested frames into successful/failed/skipped/reused sets, bind image/pass inventories and separate verification evidence. `complete` requires every requested combined frame, a manifest and a passed verifier record; this is a structural prerequisite, not proof of file contents.

`RenderBackend` declares capability, probe, validation, prepare, render, cancel and version operations. `ArtifactVerifier` remains independent of the worker and must decode actual files and check snapshot/settings/backend correspondence. The stable error taxonomy distinguishes invalid input, missing/stale assets, unsupported features/backends/devices, device initialization, GPU/system memory, storage, timeout, crash, cancellation, corrupt/incomplete output, stale cache, verification, external-tool and ownership failures. No implementation returns success for a missing renderer.

## Executable snapshot behavior

```bash
python scripts/render3d.py snapshot-rig --source PATH_TO_NATIVE_RIG --destination NEW_SNAPSHOT_DIR
python scripts/render3d.py verify-snapshot NEW_SNAPSHOT_DIR
python scripts/render3d.py validate request.json
python scripts/render3d.py frame --request request.json --snapshot NEW_SNAPSHOT_DIR/snapshot.json --index 0
```

A snapshot includes original source/identity, native geometry, skin arrays, animations and all referenced validation/review/evidence files. `NativeRigAdapter` validates actual source semantics, copies at most 4,096 files / 1 GiB total / 256 MiB each into private staging, revalidates the copied source and publishes with the existing exclusive primitive. Snapshot metadata binds each exact file hash and byte count and the source/adapter versions. No hardlink to mutable source data is used. The output can move or the original can change without changing the snapshot. Reuse checks all file bytes and reruns the owner validator. Symlinks, path traversal, cross-platform path aliases, metadata-path collisions, changed bytes, existing destinations and incomplete closures reject. No metadata-only snapshot counts as a valid native rig.

`SceneAdapter.describe` is the extension boundary for a trusted in-process owner. It must return a complete hash-bound closure and validate the same scope from frozen staging. A user-supplied document cannot register executable code. The only adapter exposed by the CLI is native rig preview. This adapter does not synthesize a camera, lights, full scene or scene-level animation.

Frame planning verifies the request-bound snapshot file bytes and the full owner-validated frozen closure first. Output index `n` has presentation time `n/fps`; source time uses the request's explicit source start and its local selection index. Blender frame one represents zero seconds; integer frame and subframe remain exact rational values until the backend API boundary. Frame fingerprints bind timing, camera ID, source closure, snapshot and settings. Destination is excluded from the pixel identity, so relocation does not invalidate the semantic cache key. This foundation does not read or accept cached pixels.

## Validation and remaining acceptance

Run:

```bash
python scripts/generate_schemas.py --check
python scripts/test_rig_gate.py schema-math
python -m unittest discover -s tests -p 'test_render3d*.py' -v
```

The dedicated tests use labeled synthetic contract data and an actual generated native robotic mesh rig. They execute closure capture, owner reload, relocation, corruption rejection, exact NTSC timing and coordinate math. This is local contract/native-source evidence; it does not establish a Blender render, decoded PNG/EXR acceptance, GPU support or production visual approval. The test family is explicitly excluded from the 2D-only discovery path and included in the existing native schema/math gate. No workflows are enabled or changed.

| #91 acceptance | State |
| --- | --- |
| Audit existing locks, provenance, jobs, frame timing, FFmpeg and delivery | Audited; legacy ownership preserved |
| Strict request/job/frame/result, source assets and errors | Implemented as additive schemas with structural and semantic validation |
| Immutable snapshots, shared scene adapters and exact conversions | Native rig closure and exact conversions implemented; full Scene3D adapter remains blocked on its owner |
| Backend identities, output ownership, independent verifier contracts | Typed protocol/contract boundary implemented; executing workers and verifiers remain downstream |
| Actual affected workflows and legacy regression | Local source snapshot workflow and selected regressions run; no hosted/native render acceptance claimed |

Rollback removes only the new Render3D modules/schemas, their test registration and documentation. Existing source formats, outputs, cache ownership and review receipts do not require migration. Keep #91 and #16 open until their remaining mandatory gates actually pass.

The local [validation receipt](../tests/results/render3d-foundation-20261025.json)
records 38 foundation tests, 31 production-contract tests, 20 production-render
regressions and five compatibility tests passing. The compatibility probe
collects 831 legacy tests without optional 3D imports; collection is not an
831-test execution claim. The native schema/math gate passes 69 tests in the
recorded test environment. Its initial default-environment run failed because
`pygltflib` was missing; the dependency-equipped rerun does not assert an exact
match to every optional requirements pin. The standalone CLI captured and
verified a 706-vertex / 1,368-triangle synthetic robot source snapshot, with
identical returned snapshot records. Exact source and log hashes are retained.

Independent full-diff review exposed numeric-admission and source-semantic gaps
after the initial 34-test candidate. Separate corrective commits preserve that
history: typed integral-float/overflow rejection, representable derived rational
times, and the existing owner's validation of each declared skeleton, controller,
skin and clip. Optional unrigged static imports remain supported; neither missing
animation nor missing skins is fabricated. The final foundation suite has 38
tests, including regressions for every reviewed failure.
