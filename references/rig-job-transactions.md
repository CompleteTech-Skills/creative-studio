# Rig step transactions

Rig mutation steps now keep their active manifest private until the entire step
has passed native load verification, artifact hashing and job-schema validation.
`run_step` holds the existing run writer lock. `save_rig`, numerical-validation
writes and review writes use the same transaction-aware manifest publisher;
outside a step, their existing direct API behavior is preserved.

Each attempt has a UUID-owned directory under `rig/transactions/`. Its changed candidate
manifest and record are retained for diagnosis. Admission failures with an unchanged
candidate remove only their exact owned temporary files; their failure diagnostic
remains in the job journal. Content-addressed geometry, skin,
clips, corrections and evidence can be appended during the attempt, but failed
or interrupted candidates do not replace the active `rig.json`. Operation
reports use immutable `rig/artifacts/` references instead of overwriting a
previous report at a fixed path. Callers should follow the artifact references
returned by the step/journal rather than guessing report filenames. Historical
reports remain readable and unchanged.

Before publication, the transaction records its verified candidate hash and
complete successful step entry. The active manifest and job journal remain two
separate atomic files. A crash between them is recovered under the writer lock:

- An unprepared attempt remains interrupted; its candidate is not promoted.
- A prepared attempt is verified again and finished without invoking its action
  twice, whether or not its active manifest was already promoted.
- A changed active manifest that matches neither the original nor the prepared
  candidate raises `RIG_TRANSACTION_CONFLICT`. Recovery preserves that revision.
- Older running journals without transaction records are marked interrupted;
  the system does not invent a rollback history for earlier implementations.

This mechanism protects native operations routed through `run_step`. It is not a
sandbox for arbitrary Python callbacks, nor an authorization system against
someone independently replacing files with the same account. Direct file writes
that bypass the shared manifest publisher do not acquire these guarantees.
Originals, immutable evidence, abandoned candidate files and unrelated user files
are not deleted during recovery. Retention and garbage collection remain
separate operations. This change does not implement cancellation of arbitrary
long-running native calculations or complete the broader rig acceptance issue.

Validation uses real subprocess exits during action, after preparation, and after
active-pointer publication; it also checks retry budgets, stale-revision refusal,
invalid result admission, and immutable report reuse. See
`tests/test_rig_transactions.py` and the existing `operations-archive` gate.

Restore format diagnostics are distinct from transaction or semantic failures.
Malformed/truncated ZIP containers, CRC/decompression failures and corrupt native
ZIP bundles inside an otherwise hash-consistent package report
`RIG_ARCHIVE_FORMAT` at the restore boundary. An unsupported outer member ZIP
codec reports `RIG_ARCHIVE_CODEC`. These diagnostics preserve the original archive
path and describe corrective action; the destination is not published, and only
the attempt's private staging directory is removed. Existing semantic RigError
codes remain intact. Unrelated programming exceptions are not reclassified as
corrupt input.
