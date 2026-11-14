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
heartbeats, revision and append-only events are persisted. Retained cache and
staging bytes, including failed or orphaned worker outputs, reduce capacity
available for the next claim. Live worker outputs are covered by their existing
reservation and are not counted twice. Heartbeat expiry alone
never authorizes takeover of a live process. `recover()` requeues only dead
process claims, honoring maximum attempts and cancellation. This store requires
local same-host storage; it does not claim distributed leases or cross-host
failover.

## Shared admission and resource reservations

New stores use scheduler marker version 2; immutable cache records retain
version 1. Older clients therefore reject a new resource-aware store rather
than ignoring its reservations. Existing version-1 stores remain readable and
render-resumable without changing their marker. They cannot acquire generic
reservations: create a new version-2 store for that workflow. Check
`store.supports_resource_reservations` before adding orchestration tables;
`store.store_version` exposes the admitted marker version.

`claim(job, admit=hook)` and `run_one(..., admit=hook)` call
`hook(connection, record)` inside the same immediate transaction as the native
frame claim. The record binds job, frame, request, backend, snapshot and cache
key. The hook must return a strict boolean and use bounded SQL checks or updates
only. A false result rolls back its savepoint and defers that candidate without
incrementing attempts; an exception rolls back the whole transaction. Do not
perform filesystem/provider calls, acquire budget locks, reenter JobStore, or
control the transaction from this hook. Persist a budget dispatch fence before
asking the native owner to claim work.

`reserve_resources(operation_id, resources, admit=hook)` reserves capacity for
an operation whose execution claim remains owned by its native subsystem.
The complete resource vector contains nonnegative integer `cpu_workers`,
`gpu_workers`, `provider_workers`, `memory_bytes` and `storage_bytes`; at least
one worker is required. CPU capacity is the existing `workers` setting, GPU
capacity is `gpu_workers`, and explicit `provider_workers` defaults to one.
Provider capacity is persisted in version-2 SQLite settings and cannot silently
change on reopen. Generic reservations and ordinary frame claims count against
the same capacities, including retained output bytes.

The returned lease contains exactly `operation_id`, `token` and `pid`.
Repeating an identical reservation from the same process returns its original
lease without readmitting or consuming another budget fence. Different resource
requirements for that operation reject. A process nonce also prevents a reused
PID from acquiring an earlier process's lease. `heartbeat_resources(lease)`
returns its cancellation flag; `cancel_resources(operation_id)` requests
cancellation without freeing capacity; `release_resources(lease)` requires the
original process and token. The native owner must reconcile execution before
the caller releases resources.

`recover_resources(admit=hook)` never reclaims a live PID based on heartbeat
age. Dead CPU/GPU-only reservations can be released. A departed provider
reservation stays held by default: its SQL admission hook must confirm an
already persisted, verified native terminal outcome before release. Pending or
uncertain external execution cannot qualify. `resources_status()` reports
capacity and reservations while redacting tokens and process nonces. These are
declared reservations, not process isolation, provider execution or a second
execution queue.

`cancel(job, expected_revision=...)` records cancellation and prevents further
claims and publication. The backend receives a cancellation callback that also
updates heartbeat. Backend process-tree termination and execution timeouts must
be enforced by its supervisor. `resume(job, verifier=...)` rechecks existing
completed outputs and source snapshots; invalid cached outputs return to the
queue if attempts remain. A stale expected revision rejects the mutation.
Dead-owner recovery and resume transitions share the final revision-checked
transaction, so a fresh revision does not reject its own recovery and a stale
revision cannot partially recover a job.

Outputs are independently verified before and after copying into private cache
staging, then published through the repository's exclusive native rename.
Copying is bounded by declared bytes and checks the same bytes written; changed
or growing input files cannot silently enter the cache. The complete cache copy and bounded encoded metadata are reserved in SQLite
before any stage files are written. Concurrent claims and publications account
for those reservations. Source and copied verification observations must agree.
Dead reservation owners release their reservation while actual partial bytes
remain accounted and retained. Database completion is recorded only after
successful publication. A crash between publication and the
transaction commit leaves a reusable verified cache entry, not a false completion.
Corrupt cache bytes are retained in a named quarantine before a newly rendered,
verified replacement is installed. Both the retained evidence and its replacement
count against cache capacity; repair fails without moving either entry when that
capacity is insufficient. Failed backend output is preserved for
inspection.

`verify-reuse` rehashes and decodes artifacts before reuse. `refresh` bypasses
reuse and executes the backend again. The cache's original per-frame request and
backend provenance remain immutable and are retained when a different job or
output location reuses identical pixels. Source, camera, shot, settings, timing
or backend differences cannot qualify as the same cache entry.

`materialize()` revalidates the source and every frame cache, creates relative
frame paths and per-frame provenance files, independently verifies the complete
sequence, and publishes a complete portable delivery. Every published image and metadata byte, including provenance, manifest,
verification and result JSON, counts against the request storage budget. Output
usage reports the complete directory size. Existing destinations remain untouched. Store/cache metadata uses bounded nonblocking regular-file reads and strict JSON
decoding. The full request, manifest, result and verifier identity are
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

The optional native job workflow can be reproduced with an installed Blender
4.5 LTS and the independent verifier dependencies:

```sh
HATCH_BLENDER=/path/to/blender HATCH_RENDER3D_REAL_TESTS=1 \
  PYTHONPATH=scripts:tests python -m unittest test_render3d_jobs.RealJobWorkflow -v
```

It executes actual per-frame native rig rendering, independently decodes the
portable delivery, reuses verified pixels without a renderer, repairs one
corrupted frame while retaining its bytes, and cancels a real worker through the
durable job callback. The fixture is synthetic and carries no visual approval.

Portable delivery retains historical per-frame receipt JSON and current pixel
verification. Raw historical worker logs/reports remain in the JobStore cache;
the archive integration must include them for a full executed-evidence archive.

The [native request binding reference](render3d-job-bindings.md) documents the
executable `RenderJob.id`/request-digest resolver and the context, reservation,
retry and progress fields projected from this same owner.
