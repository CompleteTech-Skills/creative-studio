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
An `unstarted` release is rejected after a retained native claim publication,
even when no usage observation has arrived. An older native reservation without
the publication protocol described below cannot prove that it was unstarted.

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
run. Fresh acquisitions can enroll before binding, or already-bound never-claimed
acquisitions can enroll one at a time in either order. The existing production
owner validates every binding identity; enrollment holds the existing run locks
through publication and admits at most 512 bindings. Every bound acquisition must
join the same ledger before either a production claim or a raw native claim can
proceed. Partial enrollment cannot authorize generation. Historical claims,
restored journals, changed identities and a different ledger remain rejected;
this is not retroactive accounting for earlier unbudgeted work. Standalone legacy
runs outside a budgeted production owner retain their existing behavior.

One cumulative reservation covers each native job and its bounded attempts.
Native claim history still owns tokens and call counts; these counts are not
measurements of actual provider billing. A bare reservation ID is never an
accounting authorization.

Before returning a native claim handoff, the existing journal durably saves the
claim and the existing immutable ledger retains a `native-claim` publication
event. It binds the original operation, reservation, run, canonical location,
job, token, attempt ordinal and input hashes without recording usage or changing
held capacity. It does not become a second acquisition controller. All retained
reserve intents must still have their original native operations, and every
published token must remain in the matching native claim history. Rolling back
only the native journal therefore cannot erase admitted or published authority.

A new native reserve intent records publication protocol version 1. It binds
the presence or absence and exact hash of optional extraction settings and the
complete captured profile digest into the operation identity, in addition to the
original request, generation projection and prompt hashes. Current admission
also includes `@generation-job`, a digest of the ordered attachment descriptors
and the primary/retry prompt roles. Equal file hashes do not authorize changing
attachment order or notes. A pending intent must retain this exact descriptor
identity; restoring it recovers the original unpublished token. Older protocol-1
intents without this digest remain available for accounting recovery but cannot
issue another handoff under the stronger descriptor boundary. An older intent
without that marker cannot prove that a missing
publication event means no handoff was issued. Its authentic historical usage
remains recoverable. Further work, every `unstarted` release and a direct
generic `canceled-known` release are refused.

For a historical reservation, the original native owner may explicitly attest
that **all outstanding attempts** have stopped by submitting `canceled-known`
with retained nonexecution evidence. Use this path only when the evidence covers
every outstanding attempt, including work absent from the recovered journal.
The current on-disk journal must show ordered, failed claims and measured retry
usage for each, with all native and direct uncertainty reconciled. The ledger
retains the attestation, its original evidence, the claim-history digest, latest
token and latest observation identity. It rechecks these bindings under its
writer lock. A genuine historical snapshot may omit a later unmeasured handoff.
The native owner's all-attempt nonexecution attestation covers that gap. Neither
a per-token usage measurement nor the journal's current contents can establish
historical completeness.

A protocol-1 pending token created before publication remains reusable after an ordinary
interruption, including an input-change refusal before claim publication. Once
publication is retained, an older preclaim snapshot fails closed even if it
retains that token. If publication itself fails after saving the native claim,
the call returns no handoff and leaves the original claim running for explicit
reconciliation. Reporting actual usage for a historical retained claim remains
available; issuing further work requires complete publication evidence. Never
infer zero usage merely from a missing caller response. The added event is
accepted by the current v1 ledger reader; older readers fail closed on it.

Publication rechecks the current reservation, remaining per-attempt capacity,
retry ordinal and aggregate project/provider limits inside the ledger's existing
writer lock, using the explicit exact-decimal context. A separate preflight
keeps an already-known capacity refusal pending before the native claim is saved.
An overrun that races after that preflight still prevents a returned handoff;
the saved claim remains running for explicit reconciliation. Neither an absent
usage report nor a failed caller response proves that published work never ran.
Transient ledger lock or revision contention retries the same local accounting
mutation at most eight times, with bounded backoff. It never allocates another
native token or provider attempt. Current capacity is rechecked on each actual
publication mutation; public ledger mutations still require their exact revision.

Generic ledger operations may use any valid operation identifier, including one
that resembles a native hash. Native admissions are recognized by authenticated
retained native intent evidence and their reserve identity. A different
operation's native intent is ordinary provenance; neither its proof kind nor a
native-shaped operation name can give it authority over this reservation.
Direct generic
settlements and cancellations remain authoritative in the ledger without needing
an invented native journal event. Actual native observation events must still
retain their matching native journal entries. A directly released reservation
remains readable but cannot issue more work or block an unrelated enrolled run
merely because its release was recorded through the generic accounting API.

Direct ledger uncertainty remains authoritative even when it has no native
observation ID. A known native observation cannot silently clear it with an
empty `resolves` list, or by resolving only a later native unknown observation.
First reconcile the direct ledger observation through the existing generic
settlement API with explicitly measured deltas and retained evidence; then
resolve any native observations by their IDs. This is checked before retaining
a known native intent and again under the ledger mutation lock. A concurrent
unknown observation therefore preserves the hold even after a native intent
has been saved. Status and unrelated sibling operations remain readable.
Conversely, generic terminal settlement cannot clear token-bound native
uncertainty. Every native unknown must be resolved by its observation identity
before either `complete` or `failed` may release the remaining hold. Known
generic deltas and ordinary generic-only reconciliation remain available.

`canceled-known` does not imply that an earlier published attempt cost nothing.
Cancellation of a native reservation requires reconciled usage for its latest
published token. An older attempt's measurement cannot authorize release of a
newer publication. Generic known deltas can be retained first, followed by an
explicit token-bound native reconciliation using only additional measured
deltas. Never-published generic work and known native remainders remain
cancellable without discarding already recorded spend.

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

Focused validation commands (execution results must identify their revision):
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
enrollment. New entries also retain the hash of the complete normalized budget
policy. Interrupted enrollment fails closed and can replay the same binding
only before the ledger has admitted any native operation for that run identity
or canonical location. Replay authenticates the retained intent and its exact
operation hash from every native reserve event, including released reservations
and admissions interrupted before token publication. Changing the current
prompt cannot hide a previous admission. An admitted run with an older manifest
must recover its original claim journal; reenrollment cannot manufacture a new
token against already used capacity.

Completed legacy owner entries without a policy hash remain readable and are
preserved unchanged. An incomplete old entry cannot prove the interrupted policy
and therefore cannot use enrollment replay. A bare enrollment flag never grants
claim authority. These checks reuse the existing owner association, immutable
ledger evidence and native journal instead of adding a second accounting store.
Unrelated, never-enrolled legacy runs outside a budgeted production owner retain
their existing restore/claim behavior. A run inside a budgeted production project
cannot evade that project's admission policy by using the raw native entry point.

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

Claim publication captures bounded prompt, request, profile and applicable
extraction settings before admission. The same bytes supply input hashes,
prompt history, identity/style fields and handoff warnings. After admission, the
owner rechecks the current hashes and captured handoff inputs with bounded
readers, then publishes from the captured values without ordinary file rereads.
A changed file or substituted symlink, FIFO or directory refuses publication.
The durable reservation and unpublished token intent survive that refusal;
restoring the admitted inputs lets an explicit retry reuse the same pending
token without allocating a second attempt. This protects native handoff identity;
it does not claim that a downstream provider has executed or retained those bytes.
Settings that were absent at admission must remain absent. A row claim also
checks that captured settings match its approved prompt calibration. Production
binding rechecks, prompt reads and base-review input checks use the same bounded
readers. Production prompt augmentation retains its existing parent through
the bounded read, staging, replacement and cleanup. POSIX uses the canonical
no-follow directory descriptor and descriptor-relative payload reader; Windows
pins every ancestor with the existing native directory owner. A changed POSIX
pathname cannot redirect publication or cleanup and refuses subsequent claim
authority. A substituted leaf is never followed.
The accounting adapter receives the captured settings/profile identity, rather
than independently rebinding a later read. Input checks run again after capacity
preflight and after durable publication. A change before saving the claim leaves
the original intent pending; a change after publication returns no handoff and
leaves the original running claim for reconciliation. Returned attachment paths
retain the admitted lexical run-relative spelling after containment checks;
transient aliases cannot substitute a cached resolved target from another
attachment. Returned file paths are not a promise that a downstream consumer
cannot edit them after a successful
handoff; the retained hashes and captured native history identify admitted bytes.

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
Original native tests remain, with three enrollment regressions added for both
orders, partial-admission refusal and invalid sibling identities/policies. Two
additional claim regressions exercise post-admission input replacement and
bounded special-file refusal while retaining the real synthetic reservation.
The original preparation was unexecuted; later local and hosted results must
identify the exact integrated revision instead of inheriting that status.
No provider, paid service, production host or live API is invoked.

Independent journal-authority tests cover older enrollment, pending, retry and
uncertainty snapshots, positive interrupted-publication recovery, and refusal of
ambiguous historical publication. Independent dispatch tests cover generic
unstarted release, a concurrent overrun, settings byte/presence changes, and
restoration of the originally admitted settings. These test authors worked from
the preceding default branch without reading the corrective implementation.
Four additional independent input-path tests substitute real POSIX FIFOs or
Windows directories at production request/prompt and native base-review reads,
with regular-file controls and retained authority checks. POSIX prompt coverage
also preserves an external symlink target. A child-process timeout makes an
unbounded read an explicit failure rather than hanging the test runner.
Twelve independent publication-edge regressions additionally cover generic
accounting compatibility, in-run prompt aliases, captured settings/profile
identity, and input changes during actual preflight, with five positive controls.
Local contention tests distinguish injected scheduling failures from actual
multi-process claims; neither is provider execution evidence.
Nine additional accounting/prompt regressions fail behaviorally on the preceding
published tree and pass with the boundary correction. They cover the independent
generic-uncertainty oracle, interleaved generic/native observations, a ledger
change between durable intent and settlement, cancellation before usage or after
a newer token, and real parent-directory replacement during prompt staging.
Windows exercises the native pinned-parent refusal when directory renaming is
denied; POSIX verifies the foreign replacement is untouched and no claim issues.
Eleven independent interoperability cases additionally cover generic terminal
settlement over native uncertainty, ambiguous historical cancellation, foreign
intent provenance, and transient in-run attachment aliases, with positive
generic/native accounting, regular attachment and bounded FIFO controls.
The historical fixture was checked against actual public claim/fail-retry/claim
behavior on the preceding default branch; committed tests are self-contained.

### Complete retained identity and readable recovery journals

Every native usage or release observation is checked against its complete
immutable retained proof before replay: operation, observation ID, token, usage,
outcome, original evidence, measurement source and uncertainty resolutions.
The journal's event key must derive from that same operation and observation.
Changing a mutable journal cannot rename an unknown observation or manufacture
a resolution. Direct generic evidence stays generic unless the reserve key and
proof both identify that reservation's own native operation; another operation's
authentic proof cannot block an unrelated native run.

Native cancellation runs the ledger's own eligibility check before retaining a
new cancellation intent, and the ledger repeats it inside its mutation lock.
A deterministically refused historical `unstarted` cancellation therefore does
not poison subsequent explicit measured recovery. This does not infer zero
usage or discard an existing durable accounting intent after a transient error.

Generation prompt paths must match the job's prepared primary and retry paths
before claiming or augmenting either prompt. Row claims compare the captured
canonical-base digest with its approved review, in addition to the captured
calibration digest. Descriptor and byte checks are repeated at publication;
validating an earlier version of a path does not approve its replacement.

Native journal publication bounds the exact encoded UTF-8 JSON, including
escaping, metadata and all accumulated history, by the same `MAX_SOURCE_BYTES`
limit used for reopening it. A refusal retains the preceding readable journal.
If reservation admission already committed, its original pending intent remains
for explicit recovery or cancellation; no handoff is returned and no history is
deleted to make the payload fit.

Two independently authored modules add 28 regression/control methods. On the
preceding published `1e9aa0fa`, the identity/recovery module ran 13 methods with
10 assertion failures, and the retained-payload module ran 15 methods with 31
failure/subtest assertions, with no errors or skips. With the correction all
28 passed in 31.260 seconds, exit 0, zero skips. Fixtures use actual local native
journals, immutable ledger revisions and synthetic image bytes. The journal-size
cases use a consistently lowered 128 KiB policy with real JSON encode/save/read;
these are scaled-policy executions, not full 64 MiB payload or provider evidence.
Exact-head full gates, independent correctness/security review and hosted
platform checks remain required.

### Complete retained identity and readable recovery journals

Every native usage or release observation is checked against its complete
immutable retained proof before replay: operation, observation ID, token, usage,
outcome, original evidence, measurement source and uncertainty resolutions.
The journal's event key must derive from that same operation and observation.
Changing a mutable journal cannot rename an unknown observation or manufacture
a resolution. Direct generic evidence stays generic unless the reserve key and
proof both identify that reservation's own native operation; another operation's
authentic proof cannot block an unrelated native run.

Native cancellation runs the ledger's own eligibility check before retaining a
new cancellation intent, and the ledger repeats it inside its mutation lock.
A deterministically refused historical `unstarted` cancellation therefore does
not poison subsequent explicit measured recovery. This does not infer zero
usage or discard an existing durable accounting intent after a transient error.

Generation prompt paths must match the job's prepared primary and retry paths
before claiming or augmenting either prompt. Row claims compare the captured
canonical-base digest with its approved review, in addition to the captured
calibration digest. Descriptor and byte checks are repeated at publication;
validating an earlier version of a path does not approve its replacement.

Native journal publication bounds the exact encoded UTF-8 JSON, including
escaping, metadata and all accumulated history, by the same `MAX_SOURCE_BYTES`
limit used for reopening it. A refusal retains the preceding readable journal.
If reservation admission already committed, its original pending intent remains
for explicit recovery or cancellation; no handoff is returned and no history is
deleted to make the payload fit.

Two independently authored modules add 28 regression/control methods. On the
preceding published `1e9aa0fa`, the identity/recovery module ran 13 methods with
10 assertion failures, and the retained-payload module ran 15 methods with 31
failure/subtest assertions, with no errors or skips. With the correction all
28 passed in 31.260 seconds, exit 0, zero skips. Fixtures use actual local native
journals, immutable ledger revisions and synthetic image bytes. The journal-size
cases use a consistently lowered 128 KiB policy with real JSON encode/save/read;
these are scaled-policy executions, not full 64 MiB payload or provider evidence.
Exact-head full gates, independent correctness/security review and hosted
platform checks remain required.

### Preserved enrollment, accounting and terminal journal capacity

Enrollment uses one captured request digest for both the pending journal and
immutable owner, then checks that original identity again before publication.
Returned attachment paths keep their captured lexical run name; publication
pins that name without resolving a replacement alias. Captured malformed
settings reject before a legacy convenience reader can reopen them.

Generic terminal accounting cannot discard unresolved token-bound native
observations. A durable native cancellation may acknowledge an independently
committed generic release only when reservation, outcome and the ordered
original evidence agree exactly. Recovery retains that release's actual receipt,
key and revision; it does not invent a native ledger event or erase its intent.
Conflicting outcomes, evidence or reservations still reject.

New handoff identities bind `@journal-headroom` to
`native-terminal-recovery-v1`. Before returning a token, and on subsequent
journal saves, a pure in-memory size calculation reserves a terminal path for
every active token sharing that journal. It includes 127 canonical retained
evidence references, a 128-character observation ID, all pending uncertainty
IDs, a 1024-codepoint measurement source at its worst JSON-escaped width,
bounded seven-metric quantities, the actual-shaped receipt, and concise native
finalization. Placeholder sizing values are never persisted as evidence.
The raw measurement source, before stripping, is limited to 1024 codepoints.

Each save uses the same exact UTF-8 encoder for its current data and its future
terminal envelope. If ordinary indentation exceeds the existing reader cap,
compact JSON preserves every decoded field and history entry. If even compact
encoding cannot fit the required envelope, the write refuses before publication.
Larger optional annotations and further observations may be refused while the
reserved concise terminal path remains available. Historical tokens retain
their recovery writes and can benefit from compact encoding; this policy does
not retroactively promise capacity for earlier admissions. The separate ledger
revision, snapshot-byte and attempt bounds continue to apply.

Ledger evidence limits apply to each individual accounting operation. Snapshot
reopening verifies every distinct retained path/hash pair across the bounded
history in batches of at most 128, so valid operations cannot cumulatively make
their own ledger unreadable. Conflicting pairs remain visible to verification.

Two additional independent modules retain 26 regression/control methods. Their
authors did not inspect the corrective implementation. On published `7a15059e`,
the 12 claim-boundary methods produced five assertion failures and three errors;
the 14 recovery/capacity methods produced nine assertion failures, zero errors.
All had zero skips. The latter includes five additive maximal-envelope methods
which independently found the aggregate-evidence reopening defect. Tests use
actual local journals, canonical retained proofs, native claims, exact JSON
reopening and synthetic images under a consistently scaled 128 KiB policy.
They are not full 64 MiB payload or provider execution evidence. The historical
compact-save case is explicitly a decoded-value-preserving JSON recovery fixture.
Exact-head full gates and independent reviews must establish the final result.

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

A committed historical cancellation also binds the ordered claim/failure history and attempt-token list during recovery. Exact receipt replay cannot acknowledge a changed history. A subsequent explicit native repair may reset current job status and attempt count; its separate repair record leaves the attested original claim/failure history unchanged. Independent synthetic lost-acknowledgement and repair controls distinguish that permitted replay from changed failure details or reordered/extra attempt identities.

## Native observation origin at settlement

Retaining a JSON document proves its byte identity, not native owner authorization. A new native-shaped settlement must match the enrolled run’s current on-disk observation and committed admission while the ledger writer lock is held. The run locator is revalidated; naming a sibling run cannot authorize the event. Current-protocol attempt rows record a ledger-generated native_owner_verified marker. A generic caller cannot set that marker. Unmarked historical observations may retain uncertainty but cannot independently resolve it or establish a latest-attempt cancellation. The enrolled owner can recover its authentic journal and reconcile those records without rewriting finalized history. Earlier writer versions retain their earlier behavior; all writers must use the corrected boundary.

Historical fixture setup now persists its genuine observation journal before the current writer replays it. All prior behavioral assertions remain, and the original four-field historical attempt wire format is asserted explicitly. A separate prior-version process produced authentic unmarked ledger bytes; corrected generic finalization/cancellation refused without changing them, while owner cancellation preserved the measured call and amount. No provider was invoked.

Captured extraction settings must be an object before claim warnings or admission. JSON null must not select an ordinary pathname-reader fallback. Seven additional synthetic origin/settings tests exercise public complete/failed/retry attempts, latest-token cancellation evidence, valid owner replay, null refusal and a valid captured-object control. Four origin cases and the null boundary failed on the preceding source; the current correction passes them without changing the retained uncertainty or source authority on refusal.

### Durable owner finalization and interruption capacity

A generic terminal observation can still record real measured expenditure,
including an overrun, while an independent native owner is unfinished. Its
receipt remains reserved; it does not release the native remainder or discard
a native observation that has been saved but not yet charged. Unresolved native
unknowns still require their token-bound reconciliation. Cancellation requires
immutable evidence that the published native token has finalized.

For a measured retry, Hatch first saves the original failed owner and only then
retains a `native-finalize` ledger event. That event binds the token, exact
observation, retained evidence and ordered original claim/failure history. A
crash before the owner save leaves the running token able to report later
uncertainty. A crash after that save replays closure without charging another
delta. An already terminal native settlement supplies its own closure and does
not acquire an extra ledger revision. Later repair records may coexist with the
unchanged original history; rollback or edits to retained finalization reject.
A repair that returns an inactive job to `pending` can recover that same
original failed transition if closure was interrupted. A running job or one
still holding a token cannot use this recovery path.

Capacity calculation also covers a pending reservation acknowledgement, every
missing observation receipt, and a concise cancellation path before a handoff
is published. A journal that cannot retain those facts refuses before the
corresponding irreversible accounting mutation. The future envelope preserves
all existing fields and history; it never persists placeholder measurements.
The envelope for a measured retry also reserves the cancellation receipt that
the resulting failed owner will require. If the combined records cannot fit,
the retry refuses before its accounting delta or native observation is saved,
leaving the admitted token's concise measured terminal path available.

Earlier descriptor formats without `@journal-headroom` remain recovery-only.
For an original unstarted reservation, retain nonexecution evidence and call
`hatch.release_budget` under `hatch.ownership`, using the original recovery
manifest, original job and `outcome='unstarted'`. Start fresh work in a separate
newly prepared and enrolled run. Do not rewrite the original admission hashes,
append a capacity marker retroactively, or delete its ledger/history. Published
or uncertain historical work must follow its measured recovery and explicit
owner attestation instead.
