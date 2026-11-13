# Director v1 contract foundation

This implements the data and interface boundary of [issue #24](https://github.com/CompleteTech-Skills/creative-studio/issues/24).
The parent Director phases and production mission remain open. Validation never
invokes a model, generates assets, mutates a native project, renders, spends a
budget or records an artistic approval.

## Architecture and reuse

The implementation was based on `bb5a6810afd06cf57185ffdfe2234e0fdc6e6240` and the
existing [Director baseline](director-baseline.md) and [requirements map](director-requirements-map.md).
The actual tree provides 2D acquisition/review/production authorities, rig
import, geometry, skeleton, skinning, animation, native export and local HTTP
services, plus Model3D contracts and file inspection. None is replaced here.
Pending scene, camera, look-development and asset-library PRs remain their
owners' work; no competing implementation was copied from those branches.

`scripts/director_schema.py` owns the closed Draft 2020-12 definitions and the
32-method registry. `scripts/director_contracts.py` adds semantic admission,
strict shared JSON decoding, bounded exact-byte closure and exact time/position
adapters. It imports the canonical rig coordinate declaration, shared portable
path policy and typed error contract. Schema discovery does not load NumPy,
Blender, browser packages or provider SDKs. The central schema generator
registers the additive `profiles/director.schema.json`; it does not rewrite
legacy definitions.

The schema's six root documents are CreativeBrief, StoryPlan, ShotPlan,
AssetSpec, CharacterIdentity and DirectorProject. Common ArtifactRef,
OperationContext, Diagnostic, Usage and Milestone definitions are embedded in
the same authoritative schema. Requests use `director-request` with `method`,
`context` and a closed method-specific `input`. Results use `director-result`
with the same method, status, input digest, typed value, artifact references,
diagnostics, usage and provenance receipts. Definitions and generated schemas
are returned as independent copies; callers cannot mutate admission globally.

Core documents use `schema_version: 1` and an explicit semantic version, with
references bound to that exact declared version. Unknown schema versions are rejected. There is no older
Director format to migrate; future incompatible versions require a separate
explicit migration rather than permissive field dropping. References to
native assets preserve their own exact versions and identifiers.

## Data, space and time

An ArtifactRef pins an artifact ID, native kind/schema version, semantic version,
project-relative portable path, exact SHA-256, byte size, media type, provenance
reference, fixture flag and complete dependency references. A directory is
never read as a file: represent it with a `director-inventory` JSON array of
exact file references. Every member must be declared in the bounded closure.
No artifact path may escape the project, use a symlink/device/FIFO, introduce
case/Unicode aliases or conflict with another binding. The reference closure
is limited to 4,096 files, 256 MiB per file and 1 GiB total.

`validate_bundle(documents, root=None)` checks document IDs, Director links,
exact versions, cross-document story/beat links and dependency cycles. With
`root`, it also checks exact regular-file bytes and ensures referenced Director
document bytes equal the supplied document. It uses descriptor-relative,
no-follow reads; platforms without those primitives return a typed unsupported
platform error. This is a read-only snapshot, not a transactional store or a
native-format decoder. Native asset acceptance must still come from the
appropriate importer/validator; a hash alone is not valid geometry or a review.

FPS is a positive bounded rational `{numerator, denominator}`. Half-open shot
ranges, marker bounds, explicit dissolve overlaps and coverage are validated.
`duration_frames(30, {"numerator":24,"denominator":1})` returns 720;
`frame_timestamp(719, fps)` is exactly `719/24` and boundary 720 is exactly
30 seconds. Fractional durations that cannot represent an integral frame count
are rejected, never rounded. Legacy 2D milliseconds remain separate.

The authoritative rig space remains right-handed +Y up, +Z forward, meters,
XYZW quaternions and column-vector transforms. `convert_position` explicitly
maps canonical `(x,y,z)` to Blender `(x,-z,y)` and back. It is a position adapter;
native rig math still owns quaternion/TRS and skeletal conversion.

## Method contracts and permitted effects

Each row has separate strict request input and response value definitions under
`$defs/input-*` and `$defs/output-*`. All 32 exact names are enumerated by `METHODS`.
The registry describes adapter obligations; it does not expose an executable
provider, scheduler or controller.

| Method(s) | Input → output | Permitted effect in the future adapter |
| --- | --- | --- |
| `CapabilityRegistry.probe` | Operations/local probe policy/provider refs → versioned capabilities, limits, probe evidence, expiry and digest | Read-only by default; only requested probes |
| `PlanningProvider.generate_structured` | Stage, original prompt, exact context, output schema, locked constraints, template hash, bounded repairs → content ref, assumptions, provider/model, raw response and repair history | Configured planning call only |
| `AssetResolver.resolve` | Specs, registry snapshot, reuse/license policy → exactly one disposition per spec, candidates, reasons, conversions and needs | Read-only; no purchase/generation |
| `AcquisitionGateway.prepare`, `.claim`, `.ingest`, `.fail`, `.reconcile` | Spec/input hashes, existing binding/claim/state, provider request ID, candidates, diagnostics → native binding/claim/state/receipt and known/uncertain outcome | Delegate existing acquisition lifecycle; preserve original uncertain claim |
| `AssetBuildAdapter.build` | Spec, inputs, provider/capability, seed, limits, authorized acquisition → actual mesh/material refs, bounds, counts, units, uncertainty, origin, receipt | Owned staging and immutable promotion |
| `RigMotionAdapter.prepare` | Exact mesh/identity/rig/motions, anatomy/actions/landmarks/contacts → native rig/binding/clip refs, deformation checks and unsupported actions | Versioned derivatives, preserve originals |
| `SceneAssembler.assemble` | Story/shot/assets/rig refs and layout/look requirements → native scenes, IDs, cameras/lights/materials/timelines, spatial/native evidence | New native revisions; no render implied |
| `FrameEvaluator.evaluate` | Immutable scene/shot, exact rational frame and FPS, dependency snapshot → native evaluated transform/bone/skinning/morph/camera/light/material/contact/ownership/clip snapshots | Pure evaluation; no event duplication |
| `GraphCompiler.compile` | Validated plan/capability/resolution refs, budget → typed nodes, exact input edges, output schemas, resource classes, cache keys, gates and critical-path estimate | Read-only |
| `BudgetLedger.reserve`, `.settle`, `.release` | Operation/idempotency, estimated bounds/retries/rates, expected ledger revision; actual Usage for settlement → reservation, ledger revision, remaining limits, state and evidence | Transactional native accounting |
| `ExecutionService.submit`, `.status`, `.resume`, `.cancel` | Immutable graph or existing job/revision/request; reconciled dependencies/ownership for resume → original job identity/state/attempts/workers/claims/reservations/progress/diagnostics/artifacts | Persist/delegate native transitions; submission stays queued/running |
| `RenderAdapter.preflight`, `.submit`, `.status`, `.cancel`, `.verify` | Pinned scene/shot/ranges/FPS/settings/engine/device/budget, or existing job; exact candidates for verification → preflight, native job or complete ordered independently decoded frame evidence | Delegate renderer; inspect outputs independently |
| `ExportService.export`, `.verify` | Verified sources, target/codec/options or existing export and expected counts → outputs, format losses and independent verification | Stage/publish owned output; preserve masters |
| `QAService.validate`, `.record_review` | Artifacts/checks/thresholds or identified reviewer/scope/decisions/evidence → measurements/unmeasured findings or separate receipt | Append evidence/reviews; never overwrite history |
| `RevisionService.plan`, `.apply`, `.impact` | Original instruction/locks/budget/capabilities; patch/expected revision/authority; patch/graph → plan, immutable child revision or conservative changed/stale/preserved sets | Plan/impact read-only; apply atomic |

`validate_method_request` and `validate_method_result` enforce method identity.
Supplying the request to result validation binds the exact canonical input
digest and checks operation-specific schema/template, resolution cardinality,
evaluation sample, native job identity, render inputs/counts, export target,
review identity/decisions/evidence and revision parent consistency. An adapter
still owns transactions, replay, leases, actual execution and source decoding.

## Failure, authorization and evidence

The result status enum is `succeeded`, `partial`, `blocked_capability`,
`blocked_budget`, `awaiting_external_result`, `awaiting_review`, `failed`,
`canceled`. Non-success requires diagnostics; only succeeded/partial may carry
a typed value. Native durable job states remain distinct. Successful submission
is queued/running, not a completed production. Failed/blocked/canceled results
cannot smuggle a success payload.

Unknown measurements and prices are null with explanations, never invented
zeroes. Network/paid contexts require an explicit policy reference; paid work
also requires a reservation. Credentials have no schema field and unknown
fields are rejected. Deadline format includes a timezone; expiry/admission is
the capability/scheduler owner's responsibility, not a claim made by parsing.

The nine milestone states remain independent: planning, assets, scene, motion,
rendering, video, native editability, visual QA and delivery. A passed milestone
needs evidence. Automated authority cannot pass visual QA. A complete project
requires every milestone passed, but declared evidence is not authenticated by
this foundation; authoritative QA/review adapters must validate it. An assistant
receipt never becomes human approval by schema admission.

## Reproduction and acceptance mapping

```bash
python scripts/director_contracts.py --check
python scripts/generate_schemas.py --check
python scripts/director_contracts.py --bundle examples/director-contracts/contracts.json --root .
PYTHONPATH=scripts:tests python -m unittest test_director_contracts -v
```

The fixture bundle contains six core documents and one request/result pair for
each of the 32 methods. All files are self-authored synthetic contract examples.
Their success-shaped results exist to exercise types, not to assert that
providers, frames, payments, approvals or native jobs ran.

| #24 acceptance | Implementation and verification boundary |
| --- | --- |
| Six canonical document families plus ArtifactRef/Context/Result/Diagnostic/Usage/Milestone | Authoritative closed definitions; complete synthetic roundtrips |
| Generated schemas, strict versions/IDs/paths/hashes/finite values/references | Shared decoder, schema parity, semantic negatives and exact-byte bundle checks |
| Every mandatory method, status and side effect | 32-method independent enumeration; strict inputs/output values; typed failures and request/result matching |
| Exact clock, common coordinates, versions and independent success/review | Rational arithmetic, shot coverage, explicit position conversion, exact references and separate milestone authorities |

The corresponding test receipt records fresh local commands and results. Native
provider/render execution, a persistent Director runtime, cancellation behavior,
visual quality, P1/P2/P3 demonstrations and hosted checks are outside #24's
contract-only evidence. No local pass establishes those later gates.
