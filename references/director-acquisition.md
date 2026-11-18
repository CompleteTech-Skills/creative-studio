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
not erase authenticated spend; they still prevent ingestion and preserve the token. The native ingestion owner validates immutable candidate bytes, exclusively
publishes an independent original inside the run, and decodes those same bytes.
It keeps fixture provenance. Import/procedural receipts cannot masquerade as the
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
- `claim_id` is the original token for ingestion, failure and reconciliation;
  prepare/claim may omit it. `provider_request_id` must be null because native handoff does not
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

Run `PYTHONPATH=scripts:tests python -m unittest test_director_acquisition test_director_acquisition_recovery -v`.
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

## Recovery and original-byte boundary hardening

Canonical recovery authenticates the stored native input hashes, exact pinned
Director spec, original token and native reservation before any current generation
input reads. Running operations require the current active token. Once native
ingestion or known failure removes that active token, reconcile authenticates
the latest native claim-history entry and its exact input hashes. An older
attempt's token cannot reconcile a later attempt, even if the inputs match.
Native reservation authentication still verifies the authoritative owner,
operation, ledger row and original reserve receipt.

An enrolled ingestion records authenticated actual usage before checking current
prompt, request, profile, grounding or candidate bytes. These generation input
reads use the existing source byte bound, regular-file checks, nonblocking reads
and no-follow path handling. Missing, oversized or nonregular files reject
promotion after accounting; repeating the same observation does not charge twice.
Current request-file changes do not erase the identity retained by the original
native claim. A saved binding can reopen that history for recovery; it grants
no permission for new spending or generation.

The adapter passes immutable verified bytes to native ingestion. The native owner
performs raster validation, preserves a fresh independent original using pinned
exclusive publication, and decodes the same byte object. It does not adopt a
preexisting content-addressed path or reopen a mutable candidate path for
decoding. The native source path and hash remain the source provenance record.
Publication uses the existing filesystem capability checks and fails closed on
unsupported platforms; it does not claim isolation from a hostile process with
the same operating-system identity.

The additive recovery module contains 14 test methods covering actual local
native claims, PNG ingestion and the native ledger. Adversarial fixtures cover
missing/directory/FIFO/oversized inputs, durable accounting order, wrong receipt,
claim, reservation and input identity, terminal reconciliation, old-attempt
rejection, hardlink independence and candidate substitution at native handoff.
FIFO checks execute in a bounded subprocess; boundary injection tests still call
the real native ingestion owner. No provider runs. All prior acquisition tests,
resolver tests and historical evidence remain unchanged.

The initial prepared correction was source-inspected without a Python executor.
Local resume validation at `7f10d1c851c93b6db3935eeee628d4f4380d09ca` executed
`PYTHONPATH=scripts:tests python -m unittest test_director_acquisition test_rig_director_resolver test_director_acquisition_budget test_director_acquisition_recovery -v`
on Linux/Python 3.12: 82 tests ran in 117.986 seconds, with 73 passing and nine
errors. Every error was the missing `source_bytes` keyword in the native
`hatch.ingest` owner at this intermediate revision. All 28 resolver methods and
the bounded FIFO, oversized/missing/directory input accounting and failed-terminal
reconciliation cases passed. This is partial integration evidence, not a passing
acquisition gate. The native immutable-byte owner extension and propagated budget
foundation must be integrated before rerunning the complete command and the
operations/compatibility gates. All 21 preexisting acquisition, 17 resolver and
19 acquisition-budget test methods were retained; no assertions were removed.
This bounded hardening does not establish live provider, 3D acquisition, visual
or production acceptance, or close issues #33/#34.

## Integrated local validation — 2026-11-18

The native immutable-byte extension and corrected budget/admission foundation
were integrated at local commit `461c1b9067e2c0db91b4a79d07d979ac8fe2edce`,
tree `b307c813dda2a8330f5679d1c9e339a389f65e51`. The following observations
supersede the intermediate integration blocker above while retaining its failure
history. The working tree remained unchanged throughout these runs.

| Command | Environment | Observed result |
| --- | --- | --- |
| `PYTHONPATH=scripts:tests python -m unittest test_director_acquisition test_rig_director_resolver test_director_acquisition_budget test_director_acquisition_recovery -v` | Linux, Python 3.12.14, inherited runtime dependencies | 82 tests, 87.325 seconds, OK; no skips |
| `python scripts/test_rig_gate.py operations-archive` | Same inherited runtime, missing `pygltflib` | 224 tests, 140.132 seconds; one loader error for `test_rig_director_assets`, all other tests passed; no skips |
| `<rig-gates-venv>/bin/python scripts/test_rig_gate.py operations-archive` | Linux, Python 3.12.14, sibling user-owned system-site-packages venv with `requirements-rig-test.txt` installed | 245 tests, 131.322 seconds, OK; no skips |

The gate venv resolves the repository's declared versions: NumPy 2.3.5,
jsonschema 4.25.1, simplejpeg 1.9.0, Pillow 13.0.0, Playwright 1.55.0 and
pygltflib 1.16.5. Installation used the configured public package registry and
did not modify the primary runtime. System-site-packages reuse means this was
not a wholly isolated dependency reconstruction.

All fourteen new recovery cases executed successfully, including accounting
before unsafe current-input reads, terminal claim authentication, independent
source inode preservation and candidate substitution at the native handoff.
All 28 resolver cases executed successfully, including cooperative deadline
expiry around native geometry work. The complete operations gate also exercised
the enrolled native budget, recovery, admission and production receipt-path
suites. No tests were removed, weakened or bypassed. These local fixture results
do not substitute for current-head CI, cross-platform execution, the separate
compatibility gate, live provider execution, visual review or production
acceptance.

## Cooperative acquisition deadline boundaries

Canonical operations parse the deadline once and recheck it after exact
specification and receipt reads, current generation-input validation, native
budget/reservation verification and each paid-policy artifact read. Validation
does not write a new specification binding. New claims first authenticate the
request and reconcile native state, then recheck the deadline before binding
and again immediately before calling the native claim owner. Expiry during
these validation or reconciliation steps cannot allocate a token or attempt,
or introduce a new specification binding.

Ingestion retains authenticated native usage before checking the deadline at
the next boundary, then checks again after current-input validation, after the
candidate read and immediately before native ingestion. Expiry after accounting
preserves the measured spend and original active claim; the same observation
can be replayed with a valid deadline without charging it again. The adapter
also checks after native operations and around final result validation, so an
operation that crosses the deadline cannot return a successful result.

These are cooperative checks, not interruption or rollback of native work.
An indivisible native operation may finish after the deadline. A specification
binding, native token, usage observation or completed native transition already
committed before the next check remains authoritative; a deadline error never
means that no mutation occurred. Callers must recover through the original
native state/token instead of starting another attempt. The recovery suite
adds ten controlled-clock regression methods for these boundaries, retaining
all fourteen earlier recovery methods and every prior acquisition test.

Deadline correction validation ran on Linux/Python 3.12.14 at
`c3f0081acc64a60c07daa1f20f1355c3e39a2267`, tree
`9bcdb5662dfeb49adeebfd571658b6869bbb5186`, with the same pinned sibling venv
described above. Source files remained unchanged throughout both runs:

| Command | Exit | Observed result |
| --- | --- | --- |
| `PYTHONPATH=scripts:tests <rig-gates-venv>/bin/python -m unittest test_director_acquisition test_rig_director_resolver test_director_acquisition_budget test_director_acquisition_recovery -v` | 0 | 92 tests, 64.686 seconds, OK; no skips |
| `<rig-gates-venv>/bin/python scripts/test_rig_gate.py operations-archive` | 0 | 255 tests, 135.926 seconds, OK; no skips |

The controlled-clock cases call the real native validators, accounting and
claim owners before advancing time at the injected boundary. They demonstrate
local deadline handling and recoverable retained state; they do not execute a
provider or establish production acceptance. All 82 prior focused test methods
remain, with ten additive deadline cases. The separate compatibility gate and
fresh independent/current-head platform CI remain required release evidence.
