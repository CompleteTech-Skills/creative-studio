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
bounded nonnegative exact decimals. No floating arithmetic accumulates balances.
Memory admission belongs to the capability/execution owner, not this cumulative
consumption ledger. A zero monetary budget may omit currency; every positive
monetary budget requires an explicit three-letter currency.

`reserve` requires an operation identity, provider, complete estimate,
`retry_allowance`, exact `expected_revision`, and globally unique
`idempotency_key`. It holds `(retry_allowance + 1) × estimate` against both
project and provider limits. Unknown nonmonetary estimates are rejected.
Unknown monetary price requires explicit `unknown_price=True`, a finite
`unknown_price_cap`, and retained policy/rate evidence. Monetary estimates also
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
copies referenced evidence bytes from the supplied root, preserves the input
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
