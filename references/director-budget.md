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

Opted-in run relocation is deliberately unsupported, including sibling restore
and whole-project relocation. The persisted hashes of canonical run and owner
locations must still match before any handoff. An archive may preserve the run
for inspection, but a differently restored location cannot mint another token
against the original reservation. Restore ordinary legacy runs using their
unchanged portability workflow. A supported relocation/migration transaction
would require separate ownership and accounting reconciliation.

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
