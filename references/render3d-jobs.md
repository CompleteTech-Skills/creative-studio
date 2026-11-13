# Durable Render3D jobs and verified frame cache

`render3d_jobs.JobStore` is the local, same-host persistence owner for Render3D.
It uses standard-library SQLite immediate transactions with synchronous durable
commits and exclusive directory publication. It does not import Blender or the
legacy 2D compositor. The backend owns subprocess supervision and actual pixel
rendering; `RenderArtifactVerifier.verify` owns independent source/image checks.

A store records its host and configured worker, memory, storage and GPU worker
capacities. Reopening with different capacities or on a different host rejects.
The request and effective backend identity determine a stable job ID; repeated
submission is idempotent. A destination can belong to only one job. Every frame
is planned with the canonical `make_frame_spec`; cache identity includes that
plan and the full actual backend/device identity.

```python
store = JobStore('output/jobs', workers=1, memory_bytes=4*1024**3)
job = store.submit(request, snapshot_root, backend_identity)
while store.run_one(backend, verifier.verify, job=job) is not None:
    pass
result = store.materialize(job, 'output/delivery', verifier.verify)
```

Each claim reserves declared worker/memory/storage capacities transactionally
and takes an unpredictable lease token bound to its owning process. Priority is
descending, then submission order and frame index. Progress, attempts, ownership,
heartbeats, revision and append-only events are persisted. Heartbeat expiry alone
never authorizes takeover of a live process. `recover()` requeues only dead
process claims, honoring maximum attempts and cancellation. This store requires
local same-host storage; it does not claim distributed leases or cross-host
failover.

`cancel(job, expected_revision=...)` records cancellation and prevents further
claims and publication. The backend receives a cancellation callback that also
updates heartbeat. Backend process-tree termination and execution timeouts must
be enforced by its supervisor. `resume(job, verifier=...)` rechecks existing
completed outputs and source snapshots; invalid cached outputs return to the
queue if attempts remain. A stale expected revision rejects the mutation.

Outputs are independently verified before and after copying into private cache
staging, then published through the repository's exclusive native rename.
Copying is bounded by declared bytes and checks the same bytes written; changed
or growing input files cannot silently enter the cache. Database completion is
recorded only after successful publication. A crash between publication and the
transaction commit leaves a reusable verified cache entry, not a false completion.
Corrupt cache bytes are retained in a named quarantine before a newly rendered,
verified replacement is installed. Failed backend output is preserved for
inspection.

`verify-reuse` rehashes and decodes artifacts before reuse. `refresh` bypasses
reuse and executes the backend again. The cache's original per-frame request and
backend provenance remain immutable and are retained when a different job or
output location reuses identical pixels. Source, camera, shot, settings, timing
or backend differences cannot qualify as the same cache entry.

`materialize()` revalidates the source and every frame cache, creates relative
frame paths and per-frame provenance files, independently verifies the complete
sequence, and publishes a complete portable delivery. Existing destinations
remain untouched. The full request, manifest, result and verifier identity are
hash-bound; the output is not a claim of artistic approval.

`list_jobs(limit=..., offset=...)` and `status(event_limit=...)` bound query output.
`cache_inventory()` marks every frame key referenced by any retained job as
pinned. `clean_cache(keys, apply=False)` previews exact unreferenced keys;
`apply=True` rechecks pinning transactionally and deletes only those keys. Jobs,
review evidence, failure history and pinned completed/interrupted frames are
never aged out by filename or timestamp heuristics.

The focused tests exercise real SQLite transactions, process exit recovery,
request/lease conflicts, image decoding, corruption, cancellation, strict
publication, source changes, relocatable cache reuse and portable assembly.
Synthetic Pillow images test storage/verifier contracts only; these tests alone
do not establish execution of a 3D renderer. Real Blender end-to-end acceptance
is recorded separately by the Render3D backend and acceptance owners.
