# Managed local asset jobs

`asset_library_jobs.JobManager(root, create=True, limits=...)` explicitly creates
a private local journal. Reopening with `JobManager(root)` loads its persisted
resource configuration; competing workers cannot raise concurrency by supplying
different options. Configuration is immutable for that store. Limits may be
lowered at creation within the documented hard ceilings. A new store is required
to change them. This journal is an execution record, never an asset registry.

The typed Python interface is:

```python
manager = JobManager('output/asset-jobs', create=True)
job = manager.submit('import', request, resources=('cpu', 'io'), key='request-1')
result = manager.run(job['id'], installed_adapter)
manager.status(job['id'])
manager.list()
manager.cancel(job['id'])
manager.resume(job['id'])  # explicit retry authorization; does not execute it
```

Requests, resource estimates and receipts contain bounded finite JSON only.
An idempotency key names one exact request, kind, estimate and resource selection;
a conflicting request is rejected. FIFO order applies to queued jobs sharing a
resource pool. Independent OS resource-slot locks enforce separate conversion
CPU, Blender, preview, archive, I/O, hashing and semantic-index concurrency limits.
The defaults/hard ceilings are respectively 2, 1, 1, 1, 2, 2 and 1 workers. A busy
pool returns `ASSET_JOB_BUSY`; an earlier queue entry returns `ASSET_JOB_QUEUED`.
A synchronous `run` is intended to execute in a worker process. The manager does
not launch threads or execute serialized code.

Each job has a stable identifier/token, sequence, type, exact parameters/hash,
resources, estimates, limits, progress, operation, timestamps, attempt count,
retained retry history, staged inventory, verification receipt, publication
receipt and structured failure. A view adds queue position. Records distinguish
queued, running, staged, verified, publishing, published, failed, cancelled and
interrupted. A lease probe identifies a process exit without trusting a recycled
PID. OS locks release on exit, while their inodes are retained.

Installed adapters implement `stage(context)`, `verify(context, staged)`,
`recover(context, staged, verified)` and `publish(context, staged, verified)`.
Stage writes only to `context.stage_dir`; it returns a JSON object. The manager
retains an exact sorted SHA-256/byte inventory after staging and rejects a changed
inventory before verification/publication or on resume. Verification returns a
JSON report, and must refuse invalid inputs rather than return a passing-looking
placeholder. The canonical converter/validator remains responsible for checking
geometry, materials, animation, original source closures and applicable budgets.

Recovery must probe the actual canonical publisher using `context.token`. It
returns either `{'status': 'published', 'receipt': exact_receipt}` after validating
that publication, or `{'status': 'unpublished'}` only after establishing it is safe
to publish. An ambiguous result fails closed. Publishers must be idempotent for the
token and use their existing atomic/exclusive transaction authority. A process
exit after canonical publication but before the journal's final write resumes by
recovering the existing receipt. It does not stage, verify or publish a second
version. This guarantee depends on the adapter's authoritative recovery probe;
the manager cannot infer external side effects from a callback's return value.

Cancellation is an independent retained request. Adapters call
`context.check_cancelled()` and `context.progress(fraction, operation)` during
bounded work. The manager checks cancellation at each transition. Final
publication and cancellation are serialized by the journal lock; publication is
a short critical section after all expensive work. `publish` must not call
`progress` because it already holds that lock. Cancellation after completed
publication is refused and directs callers to the canonical lifecycle API.
Cancelled/failed work retains staged evidence and requires an explicit resume.
The original source tree and unrelated output directories are never cleaned up.

The runtime ceiling is 300 seconds, attempts 3, records 4096, journal record 1 MiB,
staged bytes per job 256 MiB and staged files per job 4096. Adapters must propagate
the remaining deadline to subprocess timeouts and enforce tighter geometry,
texture, archive expansion, dependency, memory and canonical storage limits.
Checkpoints bound staged output, but this cooperative adapter API is not a
sandbox, preemptive CPU/memory governor or a filesystem quota. An adapter that
never checks cancellation cannot be safely interrupted within the same process;
the worker owner may terminate that process and then explicitly resume. Adapters
must make partial staging retryable. No network calls or paid generation are
performed by this module. Power-loss durability of multiple stores is not claimed.

The journal root is a private single-trust-boundary directory. Canonical paths,
regular owned records/staged files, no symlink/reparse traversal and hardlink
rejection are checked. It does not defend against an adversary concurrently
rewriting the trusted journal directory. Immutable parameters, bounded contracts
and retained inventory detect accidental or out-of-band mutation at recovery.

`tests/test_asset_library_jobs.py` independently exercises real deterministic
file publication, cancellation, FIFO and cross-process resource exclusion,
explicit resume, process termination after staging/verification/publication,
source bytes and output hashes, ambiguous publication, staging tamper, corrupt
journals, links, resource ceilings and runtime failure. These are scheduler
engineering fixtures; native import/preview/export/index workflows require their
canonical owner adapters and separate integration evidence.
