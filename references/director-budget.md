# Director budget ledger

`director_budget.BudgetLedger` owns local accounting only. It does not execute
providers, reserve money with a third party, authorize network calls, or imply
that planned production has completed. The Director capability registry and
native acquisition/execution owners must independently admit and execute work.

Create a new ledger with explicit project limits, per-provider limits and one
currency for all monetary quantities:

```python
from director_budget import BudgetLedger, METRICS
limits = {metric: 100 for metric in METRICS}
ledger = BudgetLedger.create('/owned/new-ledger', limits,
                             provider_limits={'local': limits}, currency='USD')
revision = ledger.snapshot()['sha256']
```

The metrics are provider calls, input tokens, output tokens, CPU seconds, GPU
seconds, storage bytes and monetary amount. Counts must be integral; values are
bounded nonnegative exact decimals (at most 1e15, 64 fractional places and 80 significant digits). No floating arithmetic accumulates balances.
Memory admission belongs to the capability/execution owner, not this cumulative
consumption ledger. A zero monetary budget may omit currency; every positive
monetary budget requires an explicit three-letter currency.

`reserve` requires an operation identity, provider, complete estimate,
`retry_allowance`, exact `expected_revision`, and globally unique
`idempotency_key`. It holds `(retry_allowance + 1) × estimate` against both
project and provider limits. Unknown nonmonetary estimates are rejected.
Unknown monetary price requires explicit `unknown_price=True`, a finite
positive `unknown_price_cap`, and retained policy/rate evidence. Monetary estimates also
require retained evidence. Unlisted providers and over-budget reservations fail
without changing the last valid revision.

Copy actual accounting evidence into the ledger with `retain_evidence(path)`;
it returns an exact retained path/hash reference. Reads reject symlinks and
reparse points using the existing Asset Library bounded-reader authority.
`settle` requires actual usage deltas, an outcome and retained evidence. Each
unique settlement records one observed usage update; callers reconcile an
uncertain attempt using only newly measured deltas, not its entire cumulative
usage again. Distinct rate-limit/retry attempts retain their expenditure.

- `retry` records consumed usage while preserving capacity for further work.
- `uncertain` retains all remaining capacity; null measurements remain unknown.
- `complete` or `failed` records known actual usage and releases unused capacity.
- Unexpected actual consumption above the reserved estimate is preserved and
  returns `over_budget=True`. It is not erased to make the ledger look healthy.
  Accumulated actual spend retains up to all 128 accepted deltas per reservation,
  including totals above the per-delta bound.
  If total usage exceeds a hard project/provider limit, all further admission
  against that limit fails.

`release` accepts only known unstarted or known canceled remaining work and
requires evidence. An uncertain reservation cannot be released. Cancellation
never deletes previous actual usage. Replaying the same idempotency key and
same normalized payload returns its original receipt; reusing it for different
work fails. New mutations require the exact current SHA, preventing stale writes.

Persistence uses immutable, canonical-JSON revision files with a SHA-linked
parent chain. Existing native OS locks serialize cooperating writers; the
exclusive publication primitive prevents overwriting a racing file or directory.
Files are fsynced before atomic publication. A process dying before publication
leaves the previous complete revision authoritative; an unpublished staging file
is ignored and retained for diagnosis. The directory is an application-owned
write trust boundary. Do not let unrelated processes write into it. Original
history, estimates, observed deltas and exact evidence remain present. Missing,
changed, unsafe, conflicting or out-of-order history fails closed.

`execute(ledger, request, evidence_root=...)` adapts the existing strict Director
`BudgetLedger.reserve`, `.settle` and `.release` request/result contracts. It
enforces the request provider allowlist and paid-work policy before reservation,
and copies referenced evidence bytes from the supplied root, preserves the input
digest and returns the current immutable ledger revision. The direct ledger API
additionally exposes known cancellation and retry outcomes. A receipt is
accounting evidence; it is not a provider execution receipt or visual approval.

The implementation bounds history to 2,048 revisions, 512 reservations, 128
usage observations per reservation, 256 providers, 128 evidence references per
operation and four MiB per snapshot/evidence object. Full archival rotation and
long-running production orchestration remain with their separate owners.

Validation covers real two-process reservation contention, forced process exit
before publication, stale writes, exact retry accounting, uncertain outcomes,
duplicate settlements, changed evidence and strict Director request adaptation.


## Opt-in native Hatch accounting

The bounded native lane adds accounting hooks to the existing Hatch owner.
It never invokes a provider. Legacy runs without an opt-in keep their existing
behavior. Configure a fresh run before its first claim:

```python
from director_budget import bind_native_budget
bind_native_budget(run, ledger.root, owner_root=project,
    provider="imagegen", estimate=per_attempt_quantities, retry_allowance=2,
    evidence=retained_rate_or_policy_evidence)
```

The run and ledger must be under the declared owner directory. The binding pins
the ledger genesis, canonical run location, run identity, fixture label, original
request, provider and policy. A `director-budget-owner.json` accounting association
is established under the existing Hatch owner lock; a second ledger for that
owner is rejected before any claim or reservation. Its fields identify a ledger
and location, not another acquisition controller or reservation store.

Production acquisitions must declare their containing project as owner.
Enrollment also pins the association in the existing production workflow state.
Production binding and claim paths verify that association against each native
run. Configure fresh acquisitions before binding them; this slice does not
retroactively account for historical unbudgeted work or migrate groups of
previously bound unbudgeted runs. Standalone legacy runs remain outside this
opt-in project accounting scope.

One cumulative reservation covers each native job and its bounded attempts.
Native claim history still owns tokens and call counts; these counts are not
measurements of actual provider billing. A bare reservation ID is never an
accounting authorization.

Ingest receipts add a `budget_usage` object. Native `fail` and production
`fail_generation` take the same object as an optional keyword argument, required
for opted-in runs. It has exactly `id`, `outcome`, `usage`, `evidence`,
`measurement_source`, and `resolves`. Usage contains all seven actual delta
metrics; evidence references must already be retained by this ledger. Outcomes
are `complete`, `failed`, `retry`, or `uncertain`. Null measurements require
`uncertain`. Known reconciliation lists every outstanding uncertain observation
ID for that token in `resolves` and supplies only newly measured usage deltas.
Never submit cumulative usage again as a new delta.

`hatch.record_budget_usage` can record evidence for the original token even when
source bytes cannot be ingested. Native ingest records actual spend before
validating/promoting the output. A bad output never refunds provider usage.
`hatch.release_budget` accepts an evidenced `unstarted` or `canceled-known`
remainder only when the native job is not running. Both functions, like existing
Hatch mutations, require the caller to hold `hatch.ownership(run)`.

The native manifest persists each accounting intent before ledger mutation.
Reconciliation replays accounting idempotently; it never creates a provider call,
clears an uncertain token or declares source ingestion complete. If accounting
commits before native finalization, replay the original fail/ingest operation.
An old accounting receipt remains replayable, but can finalize native work only
when it is the current observation, its ledger state still agrees, and no newer
uncertainty remains. A retry receipt preceding a later uncertain observation
cannot clear that token. Resolve the newer observation explicitly first.
A pending preclaim intent reuses its token after restart. Estimates, known spent
totals, held capacity and individual observations are reported separately;
unavailable metrics remain visible in observations.

Restoration of opted-in accounting authority is deliberately unsupported at
every location, including rollback to the original canonical path. The native
and project restore owners reject budgeted archives before publishing a restored
directory. This prevents a preclaim archive from replacing newer claim history
while the original work and reservation may still be outstanding. Original
directories, archives and current ledgers are preserved.

The accounting hooks also reject existing native `restored` and workflow
`project-restored` history markers, including enrollment of a restored legacy
run; restored legacy projects cannot be newly enrolled either. Canonical-location
hashes remain an additional relocation check; they are
not proof that a same-path snapshot is current. No budget restoration or
migration transaction is implemented in this slice. Such support requires
explicit reconciliation of the original claim and current ledger, not removal
of restore history. Ordinary unbudgeted legacy restore and claim behavior
remains unchanged.

This first lane supports one budgeted operation per job. Explicit repair cannot
reopen a settled reservation or erase spend; new work requires a separately
identified run. Native CLI flags and canonical AcquisitionGateway reservation
binding are separate integration work. PR #315's non-null reservation field alone
does not verify this ledger. No live provider, production rendering or artistic
acceptance is established by these hooks or their synthetic tests.

Proposed validation (not executed while preparing this patch):
```sh
PYTHONPATH=scripts python -m unittest discover -s tests -p 'test_director_budget*.py' -v
PYTHONPATH=scripts python -m unittest discover -s tests -p 'test_workflow.py' -v
PYTHONPATH=scripts python -m unittest discover -s tests -p 'test_production_workflow.py' -v
```


### Enrollment survives an older run manifest

The existing owner association retains a bounded, append-only enrollment inventory
of run identities, canonical-location digests and original-request digests. It is
written under the existing owner and run locks before the run policy is saved.
It does not own tokens, reserve capacity or create another acquisition controller.
Native claims cannot enter the legacy path when that retained owner inventory
already identifies the run or location, including an archive captured before
enrollment. Interrupted enrollment fails closed and can replay the same binding.
Unrelated, never-enrolled legacy runs retain their existing restore/claim behavior.

This check depends on the current authoritative owner association and ledger being
preserved. It is not an off-host or global rollback detector for replacement of an
entire owner and all its accounting records. The broader canonical project/account
policy and AcquisitionGateway integration remain open under #34. Original owner
records and expenditure history must be retained for any future migration.


### Explicit recovery without current generation inputs

Authentic observed usage must survive a missing, changed or unsafe current
request, profile or prompt. `hatch.manifest(root, recovery=True)` reads the
retained native journal through the existing bounded source reader, validates
its original job/claim/provenance structure, and returns recovery authority.
It does not read current generation inputs or advertise them as valid. The
ordinary manifest path still validates the current profile, request, state
selection and native job inventory.

`NativeBudget.for_run(root, data, recovery=True)` authenticates the recorded
request digest against the existing external owner enrollment, canonical run
location, exact policy, retained operations and original ledger admission.
Recovery may replay an existing reservation receipt and retain usage. It cannot
reserve new capacity, allocate an attempt or issue a native token; native claim
and normal budget admission reject a recovery-only journal. Default admission
continues to require bounded current inputs and the original request identity.
Missing original ledger admission is never manufactured during recovery.

Native ingestion authenticates the original job, token, fixture classification
and complete receipt before recording usage. Only after that durable accounting
does it validate the current manifest and generation-input hashes. Damaged
inputs therefore prevent promotion while the authentic observation survives.
Before any ledger replay, every published claim must belong to its original
operation and retain the operation's admitted input hashes. A running job must
retain that same identity. Recovery rejects a changed job/claim identity before
replaying even authentic pending usage. Unpublished preclaim intents remain
recoverable but cannot be reported as executed claims.
Invalid receipt/token/enrollment identity cannot charge another operation.
Failure and reconciliation retain their original native state and uncertainty
rules. A complete accounting observation alone is not asset acceptance.

Current input reads reuse the existing source owner's regular-file, no-follow,
encoded-size and changed-file checks, with the existing 64 MiB source bound.
The state-selection owner accepts this reader without duplicating its evidence
validation. Native input failures remain structured native errors; FIFO and
oversized input paths cannot become an unbounded read while ownership is held.
These changes do not globally redefine hashing of unrelated legacy outputs.

Reservation normalization retains its explicit 128-digit decimal context during
both admission and recovery. A caller's ambient decimal precision cannot round
an already admitted reservation into a different identity.

This recovery boundary is the native Hatch API. The production workflow's
generation wrappers still validate their current project/run bindings and
generation inputs before invoking the native operation. Recovery through those
wrappers when current inputs are damaged remains separate integration work;
callers holding the native ownership lock can use the explicit native recovery
path described above.

The 14 independent recovery regression methods exercise real local native
journals and ledgers with synthetic observations. They include missing/changed
request/profile/prompt admission, durable accounting order, uncertain outcomes,
invalid identities, recovery authority restrictions, existing-receipt replay,
bounded special-file input checks, precision above 28 significant digits under
changed ambient contexts, and forged job/claim hashes with authentic pending
usage. Their author did not inspect the fix.
Original native tests remain unchanged. No provider, paid service, production
host or live API is invoked. Runtime evidence is bound to its tested revision;
the presence of these tests is not platform or production acceptance.

### Owned receipt paths and platform checks

The production wrapper writes an operation-owned temporary receipt before
calling native ingestion. It resolves that owned directory's canonical path so
OS temporary-root aliases and Windows long-path spelling reach the same strict
reader. Untrusted receipt inputs still use the existing no-follow reader; this
does not authorize following arbitrary source symlinks.

The focused production receipt test supplies a real temporary-directory alias,
performs native ingestion, and verifies retained metadata and complete adoption
history. It reproduced the reader failure before the correction and passed with
the correction. The special-input recovery test keeps actual POSIX FIFO and
symlink refusal, and uses an owned actual Windows named pipe on Windows. It does
not add an optional skip or relax the delivery skip policy.

On Linux with Python 3.12.14, correction commit `bba9f339` passed 68 budget tests,
38 production and receipt-path tests, and 21 legacy workflow tests, with no
skips. These results are local synthetic/native execution. Corrected Windows and
macOS hosted runs must independently establish those platform outcomes on the
integrated PR head; the earlier failing artifacts remain historical evidence.
