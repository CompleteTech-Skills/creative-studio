# Director native acquisition adapter

`NativeAcquisitionGateway(run_root, asset_kind="raster", binding=None)` reopens
one existing Hatch run. It uses the native ownership lock, job tokens, input
hashes, retry/call limits, ingestion, source provenance and history. It does not
call a provider or replace the native controller. Mesh acquisition is explicitly
unavailable until a native 3D acquisition owner exists.

`prepare`, `claim`, `ingest`, `fail` and `reconcile` expose native lifecycle
operations. Save the returned `binding` with the project and supply it when
reopening. It pins run ID, original request bytes and fixture status. Repeated
active claims return the same token; uncertain or stale claims remain owned.
For enrolled runs, failure delegates its exact usage observation to native
accounting. An uncertain failure preserves its exact reason, original token and
held reservation. Unenrolled fixtures retain native history without accounting.
Only a known failure authorized by the native owner releases its token.

Ingestion requires the original token, unchanged inputs, exact source SHA-256,
native handoff receipt and a nonempty inspection note. For enrolled runs, the
adapter authenticates the receipt and records its actual usage before reading or
validating candidate source bytes. Missing, changed, invalid or stale inputs do
not erase authenticated spend; they still prevent ingestion and preserve the token. The adapter preserves
source bytes inside the run before native image validation and ingestion. It
keeps fixture provenance. Import/procedural receipts cannot masquerade as the
native imagegen handoff. The native controller does not expose provider request
IDs or provider idempotency guarantees: retry safety is **unavailable**, and
only the existing active native token can be replayed safely. No automatic
remote retry is implemented.

## Director request mapping

`AcquisitionGateway(native_gateway, run_root, job)` accepts the canonical
`AcquisitionGateway.*` Director requests and returns validated result envelopes.
The artifact project root is exactly the native run root. The request fields map
as follows:

- `spec` is an exact Director AssetSpec whose `logical_id` is the native job and
  whose `asset_type` is `raster`. The first prepare/claim pins the exact spec
  reference in native job history and metadata before work starts. Existing
  unbound running/generated native jobs cannot be adopted or relabeled.
  A different specification cannot
  adopt that active acquisition, including changes to design or licensing.
- `input_sha256` is `gateway.input_sha256()`. Pending and failed jobs use the
  native `generation_inputs` projection, which removes retained import/derivation
  fields without mutating them. Running jobs use their original recorded input
  identity. Verification occurs under the native lock. Reconcile/fail and
  ingestion accounting authenticate the original claim when inputs disappear or
  change. Missing/stale inputs cannot authorize new work or ingestion.
- `binding_id` is null or the pinned run ID. `expected_state` is the native job
  state (`pending`, `running`, `failed` or `generated`). An identical active
  claim may replay its pre-claim pending/failed state and receives the same token.
- `claim_id` is the original token for ingestion/failure; other operations may
  omit it. `provider_request_id` must be null because native handoff does not
  expose it. A terminal result retains the submitted claim as historical identity;
  its `native_state` records whether the native token is still active.
- Ingest `candidates` contains exactly an `acquisition-source` image ArtifactRef
  followed by an `acquisition-receipt` JSON ArtifactRef. Both must retain the run's
  fixture classification. Source bytes and the receipt are verified before use.
- Failure has no candidates and exactly one diagnostic. Its code is
  `NATIVE_ACQUISITION_OUTCOME`, `entity_id` is the job, `field_path` is `outcome`,
  `observed` is exactly `known` or `uncertain`, and `retryable` is false. The exact
  `recommended_action` text is preserved as the native failure reason. Diagnostic
  evidence remains part of the caller's hash-bound request. Enrolled failure
  requires exactly one diagnostic evidence ArtifactRef with kind
  `acquisition-budget-observation` and the native fixture classification.
  Its exact JSON uses the native six fields: `id`, `outcome`, `usage`,
  `evidence`, `measurement_source`, and `resolves`. Inner evidence is already
  retained by the same native ledger. Diagnostic known/uncertain must agree with
  the observation; native accounting alone authorizes finalization.
- Prepare, claim and reconcile have no candidates or diagnostics. Expired
  requests, mismatched identity and stale state fail before a native mutation.
  The deadline is checked again after acquiring native ownership.

Each result references an immutable JSON copy of the native manifest under
`director-records/<sha256>.json`; this is evidence, not a second lifecycle store.
Subsequent native mutations cannot change that result's evidence. Authoritative
mutable state remains `imagegen-jobs.json`. A zero provider-call measurement means
this adapter operation invoked no provider; it is not a claim about external
work performed by the holder of a handoff.

## Native reservation admission

Enroll a fresh run through `director_budget.bind_native_budget` using one existing
owned ledger before its first claim. The adapter never enrolls automatically.
Then reserve native capacity without publishing a claim:

```python
admission = native_gateway.reserve_budget(job)
request['context']['budget_reservation_id'] = admission['reservation_id']
result = gateway.claim(request)
```

This is a separate native reserve-only operation. It allocates no attempt or
token and performs no provider call. The subsequent canonical request receives
full existing schema and semantic validation; no dummy reservation or partial
validation is used. Every enrolled canonical operation authenticates its supplied
reservation against the original native operation, owner, inputs where applicable,
ledger row and reserve receipt. Arbitrary or foreign IDs are rejected.

New non-fixture claims and explicit paid/configured fixture claims require both
request and AssetSpec policies to permit configured `imagegen`, exact policy
evidence and the enrolled native reservation. Bounded non-fixture claims retain
positive native call/unit limits. Native unknown prices require
`allow-unknown-price` in both policies and the native finite cap. An existing
active claim may replay without admitting another paid attempt.

Historical accounting, ingestion and fail/reconcile authenticate the existing
claim and reservation even when the current request denies new paid work.
Accounting recovery does not grant new spending authority. Enrolled synthetic
fixtures can exercise the same accounting under deny/deny policies; unenrolled
fixtures keep their original lifecycle. No same-operation second ledger is used.

Enrolled ingestion receipts include the existing native `budget_usage` object.
The exact receipt, original token, spec and reservation are authenticated first.
Usage is recorded once before source reads, hash checks or image validation.
Native ingestion replays that same observation and requires complete known usage.
An authentic observation remains retained if candidate bytes fail validation;
the original token remains active. Known failure can then finalize using the
current native observation. Uncertain failure routes through `hatch.fail`, so
native held capacity, usage deltas, explicit reconciliation IDs and stale-receipt
protections remain authoritative. Provider-call usage in the Director response
still measures only this adapter operation; native snapshots retain externally
observed accounting separately. No production provider acceptance is inferred.

Run `PYTHONPATH=scripts:tests python -m unittest test_director_acquisition -v`.
The tests execute native prepare/claim/ingest/fail/reconcile with an actual PNG,
concurrent duplicate claims and reopened history. Additional cases check exact
bytes/receipts, stale inputs, unavailable 3D acquisition, denied production
handoff, explicit native limits, deadlines and immutable result evidence.

## Current-main preservation boundary

The source-preservation predecessor preserved the six unique owner paths from PR #315 at
`1b3175364b8094e1bb6dfee584622225b2c841d6` on main
`4b89ab1ce3e76ebef69a825212ab207d746c3112`, after procedural asset PR #448.
The resolver, resolver tests and resolution guide remain unchanged.
The acquisition adapter uses the current canonical `parse_rfc3339` helper for
both deadline checks; an additive lowercase timestamp regression covers that
interface adjustment. All 19 original acquisition tests and 17 resolver tests
are retained. Counts here are source inspection, not executed passes.

The preserved owner files total 1,252 original lines. This exceeds the usual
800-line review unit because recovering the existing paired adapters and their
full regression coverage is one coherent source-preservation change. No tests
are removed or weakened to reduce the count. Shared gate integration must add
`test_director_acquisition.py` and `test_rig_director_resolver.py` to the current
`operations-archive` list without replacing other enrolled suites.

The resolver consumes #448's native `build_assets(destination, seed=...)`
outputs through exact sealed manifests and canonical AssetLibrary resolution.
The builder owns generation, complete bundle inventory and exclusive publication;
this adapter creates no parallel asset registry or acquisition state machine.
Native Hatch tokens, history and receipts remain authoritative.

The bridge is built on the native budget owner and reserve/verification APIs in
integration base `9d39e1f9a7735f9e0312406b847ae8639d14a333`. It uses that owner's
single reservation and token/attempt history. The original 19 acquisition methods
and all their assertions remain; the paid positive setup now uses real offline
enrollment and admission, with additional arbitrary-ID rejection. The lowercase
deadline regression is retained, and an additive missing-candidate regression
checks exact usage retention and known finalization under deny policy.
This source integration does not implement a live provider or claim #33, #34,
P2 or production acceptance complete.

No runtime was available for this integration. Required fresh-head commands are
`python scripts/test_rig_gate.py operations-archive` and
`python scripts/test_rig_gate.py compatibility`. Run them in a supported
environment with the optional rig dependencies. Historical PR-body results
remain evidence only for their stated historical revision.
