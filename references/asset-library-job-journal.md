# Cooperative asset job journal foundation

This bounded increment of #150 (parent #135 / #128 sections 20–24; dependency
#149) adds a local journal and injected Python adapter protocol. It does not
complete native import/conversion/preview/archive/index workflows, browser or
CLI operations, preemptive resource containment, or broad #150 acceptance.

## Ownership and operation

`JobManager(root, create=True)` explicitly creates a store; reopen with
`JobManager(root)`. Use `submit(kind, parameters, resources=('io',), key=...)`,
`list()`, `status(id)`, `cancel(id)`, `resume(id)` and synchronous
`run(id, adapter)`. Supplying an adapter is explicit execution/publication
authorization. Journal data cannot select or load executable adapters.
`JobAdapter` supplies `stage`, `verify`, `recover` and `publish`.
Adapters own native validation, token-aware idempotent publication and an
authoritative recovery probe; the journal never replaces those authorities.
Current AssetLibrary registration, SQLite writer transactions, canonical Storage
publication/recovery, native validators and existing acquisition/review owners
remain authoritative and unchanged. There is no production adapter in this lane.

The existing `rig_errors.job_lock` owns process locks and automatic exit release.
FIFO applies to queued jobs sharing a pool; OS slots bound cooperating workers.
The private store must use a physical path with no symlinks/reparse points.
This journal does not accept Linux procfd capability aliases; current scoped
directory-capability semantics remain unchanged. Tests resolve temporary roots
before use so macOS temporary-directory aliases do not weaken path checks.
The caller owns the private root and installed trusted adapter code.

Defaults/hard ceilings: cpu/io/hash 2 slots, other named pools 1; runtime 300 s,
3 attempts, 4096 jobs, 1 MiB per journal record, 256 MiB / 4096 staged files.
Limits may only decrease and persist across reopen. Integral float counts are
normalized before range/read operations; booleans, fractions and oversized
numbers fail with typed diagnostics. Progress/cancellation/time/storage checks
are cooperative checkpoints, not sandboxing or process preemption. Adapters must
bound native operations independently. File-fsync/atomic replacement supports
process-exit recovery; no machine-power-loss durability claim is made.
Before replacement, admission measures the exact indented UTF-8 JSON plus newline
written by the existing atomic writer. An oversized stage result leaves the prior
record readable for failure recording and explicit resume. Public identifiers are
validated before constructing paths or taking locks, including JobContext IDs.
Under the journal lock, inventory scans recognize only the atomic writer's exact
record-temporary spelling, bounded size and single-link regular-file shape (plus
private ownership/mode on POSIX). They retain those bytes without interpreting
them as committed state or publication. Enumeration permits at most the configured
job count plus 4096 retained temporaries; other entries, links or unsafe temporary
shapes fail closed. No automatic cleanup deletes diagnostic evidence.

Retained staged bytes are hash checked before reuse and publication. Even at the
attempt ceiling, an authoritative published receipt can complete recovery without
a new attempt. Unknown state stays on hold; authoritative unpublished state cannot
republish at the ceiling. Exhausted unstaged work cannot resume. Verified work
requires publication-state recovery before cancellation.

## Preservation and evidence

Source owner: #351 / #432, frozen commit
`7b538c73426bd2fb112fedea2288e5fc97e5622a`.
Journal source blob: `9d4a511aa11f79eb560e3e68fc87767330b5667f`.
Original test blob: `773ee7c3e6d359d4fdc29310c3d1f3a160b24267`.
Fresh base: `e1258d26302d264c60dae017ef0617b251914540`.
All 13 standalone source cases/assertions are retained in
`tests/test_asset_library_job_journal.py`, with subprocess imports renamed and
six additional regression cases (numeric limits, persisted byte admission,
atomic-write interruption, temporary recognition/bounds, and identifier safety).
There are no new skips.
The original 14-case suite and old branches/PRs are unchanged.
The exact deferred case is
`tests/test_asset_library_jobs.py::JobTests.test_actual_staged_library_verify_and_final_publication_keep_inventory`
at the frozen commit above. Its 25-line native staged-library/inventory assertions
remain owned by #351/#432 for the operations increment with `OperationAdapter`;
that implementation is absent here. This is an explicit scope split, not passed
native-integration evidence or removed coverage for delivered adapter code.

Proposed commands: `PYTHONPATH=scripts:tests python -m unittest test_asset_library_job_journal -v`
and `python scripts/test_rig_gate.py compatibility`. The suite now contains 19 cases, including a subprocess that exits after the
atomic temporary is written but before replacement, then exercises reopened
listing, unrelated publication and explicit interrupted-job resume. Both commands
are **NOT RUN** locally:
the executor is unavailable. Evidence is source inspection/API readback until
exact-head hosted CI reports otherwise. The filename enters existing compatibility
discovery. Full #150 still needs executed native interruptions/recovery, CLI/API
diagnostics, geometry/texture/depth/storage admission and original-source receipts;
generalized native-document publication remains #149/#150, and active-job/package
reachability/GC stays with #141. Preserve exact commands, versions, source/output
hashes and limitations when recording that later evidence.
