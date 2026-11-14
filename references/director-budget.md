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

## Atomic native dispatch fence (ledger version 2)

`begin_dispatch(reservation_id, native_owner=..., operation_sha256=...,
expected_revision=..., idempotency_key=..., evidence=...)` commits an admission
fence under the existing ledger ownership lock and expected snapshot hash before
asking an authoritative native job/acquisition owner to claim work. The first
return has `replayed:false`; an idempotent replay has `replayed:true` and **does
not grant another external call**. The fence is budget accounting, not a worker
lease or another generation claim. The original JobStore or acquisition token
continues to own execution.

The first dispatch writes explicit ledger state version 2. Existing version 1
snapshots and ordinary reservation/settlement receipt values remain unchanged.
History is append-only across both versions. Old readers reject the new version
rather than misinterpreting an admitted dispatch as an unstarted reservation.

`record_dispatch_outcome` records exact retained native owner evidence for
`claimed`, `uncertain`, `complete` or `failed`. An interrupted pending handoff
must reconcile the original native identity; neither a restart nor an expired
controller heartbeat authorizes a second paid call. Native uncertainty retains
both the original claim and the budget fence.

Active, uncertain or unaccounted native attempts prevent release and another
attempt. Once the native outcome is known, `settle(..., dispatch_id=...)` records
that attempt's actual usage with retained evidence. Unknown metrics do not
become zero. Every terminal attempt must have actual accounting before final
settlement; retries cannot exceed the allowance already reserved. A failed
attempt uses `outcome='retry'` to preserve its charged expenditure and remaining
capacity. A completed native operation cannot be retried as failed work.

Dispatch outcome evidence records native facts; it does not grant artistic
approval. The Director scheduler must still validate capabilities, input hashes,
ready dependencies, native resource admission and provider policy before asking
for a native handoff. The ledger never invokes a provider or creates a render
request as a stand-in for a non-render operation.
