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
An uncertain failure appends its exact reason and original token to native job
history without releasing it. Only a known failure releases that token.

Ingestion requires the original token, unchanged inputs, exact source SHA-256,
native handoff receipt and a nonempty inspection note. The adapter preserves
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
- `input_sha256` is `gateway.input_sha256()`, the canonical object hash of the
  current native input hash mapping. It is checked under the native lock.
  When inputs disappear or change, reconcile/fail may bind the original recorded
  input hashes with the original active token; they preserve a stale-input warning.
  Missing inputs cannot authorize new work or ingestion.
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
  evidence remains part of the caller's hash-bound request.
- Prepare, claim and reconcile have no candidates or diagnostics. Expired
  requests, mismatched identity and stale state fail before a native mutation.
  The deadline is checked again after acquiring native ownership.

Each result references an immutable JSON copy of the native manifest under
`director-records/<sha256>.json`; this is evidence, not a second lifecycle store.
Subsequent native mutations cannot change that result's evidence. Authoritative
mutable state remains `imagegen-jobs.json`. A zero provider-call measurement means
this adapter operation invoked no provider; it is not a claim about external
work performed by the holder of a handoff.

Non-fixture claims require both the request and AssetSpec policies to allow
configured `imagegen` and paid work. Bounded policy requires a reservation ID
and native positive call/unit limits. Unknown-price policy requires a readable
exact policy evidence reference. The adapter does not validate a reservation
against the separate Director budget ledger or dispatch paid requests; that
integration remains the execution owner's responsibility. No production
provider acceptance is inferred from these local handoff tests.

Run `PYTHONPATH=scripts:tests python -m unittest test_director_acquisition -v`.
The tests execute native prepare/claim/ingest/fail/reconcile with an actual PNG,
concurrent duplicate claims and reopened history. Additional cases check exact
bytes/receipts, stale inputs, unavailable 3D acquisition, denied production
handoff, explicit native limits, deadlines and immutable result evidence.

## Current-main preservation boundary

This integration preserves the six unique owner paths from PR #315 at
`1b3175364b8094e1bb6dfee584622225b2c841d6` on main
`4b89ab1ce3e76ebef69a825212ab207d746c3112`, after procedural asset PR #448.
The original resolver, resolver tests and resolution guide are unchanged.
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

PR #484's native budget integration is a separate prerequisite for the paid
Director bridge. Before enabling that bridge, add native reserve-only admission,
verify the exact operation/reservation in its existing ledger, preserve usage
even when candidate validation fails, and route uncertain outcomes through native
accounting. Do not reserve again in another ledger for the same operation.
This preservation does not validate arbitrary reservation IDs, implement a live
provider, or claim #33, #34, P2 or production acceptance complete.

No runtime was available for this integration. Required fresh-head commands are
`python scripts/test_rig_gate.py operations-archive` and
`python scripts/test_rig_gate.py compatibility`. Run them in a supported
environment with the optional rig dependencies. Historical PR-body results
remain evidence only for their stated historical revision.
