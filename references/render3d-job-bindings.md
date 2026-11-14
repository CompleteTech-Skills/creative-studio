# Resolving RenderJob context through the native JobStore

The `JobStore` in `render3d_jobs.py` remains the only scheduling and persistence
authority. A job ID is the canonical hash of its exact native request and backend
identity. The request itself is already persisted in the `jobs` SQLite table;
there is no second request document, scheduler or job state model.

The foundation `RenderJob.id` and `RenderJob.request_sha256` fields have this
executable resolution path for jobs submitted through that owner:

```python
store = JobStore(store_root, **original_store_capacities)
request = store.resolve_request(record['id'], record['request_sha256'])
current = store.status(record['id'])
```

The store instance is the explicit lookup authority, as a project root is for
project-relative references. The resolver validates the persisted request and
backend contracts, recomputes their native job ID, and checks the caller's exact
request digest. Missing jobs, stale digests and tampered native inputs reject;
the resolver never creates a job or silently substitutes a different request.
Returned data is decoded from the native row, so caller edits cannot alter it.
Resolution binds expected source hashes; it does not approve current source
bytes, pixels or artistic quality. Source and output revalidation stay with the
existing snapshot/verifier and reuse/resume/materialize paths.

`status()` includes a `request_binding` containing that job ID, request digest,
owner and resolver name. Its additional context is a projection of the native
request, frames and storage reservations. It is not a separately persisted job
schema and cannot grant execution or change lifecycle state.

| Required RenderJob information | Existing authoritative field and executable representation |
| --- | --- |
| Project, scene and shot/version identities | Exact resolved `RenderRequest.project`, `.scene`, `.shot`; also projected by `status()` |
| Snapshot and canonical request identity | Resolved request's hash-bound snapshot file reference; `status().request_sha256` and existing frame specifications |
| Backend/executable identity | Native `jobs.backend`; included in native job ID verification and `status().backend` |
| Priority | Exact request priority; the persisted scheduling priority must agree before resolution/status succeeds |
| Resource policy | Exact request `settings.resources` and device policy |
| Active resource reservations | `status().reservation`: active frame/worker/GPU reservations, declared memory and worker storage, and native job-bound cache staging bytes |
| Retry policy | Resolved `max_attempts`; `status().retry_policy` uses the same retryable category constant as `run_one()` |
| Frame progress | Counts are computed from the native frame rows, including cancellation separately from failure |
| Completion percentage | Exact reduced completion ratio plus display percentage; settled percentage includes failed/cancelled frames without claiming rendered completion |
| Ownership, attempts and heartbeat | Native frame PID, lease token, attempt count and heartbeat projected by `status().frames` |
| Cancellation and lifecycle history | Existing native job cancellation/state and append-only events; the projection does not introduce another transition authority |
| Verified outputs and diagnostics | Existing verified frame caches, native failure/events and materialized result/manifest; resolution alone grants no output-verification claim |

Reservations describe capacity held, not measured memory/disk consumption. Cache
stage reservations are linked to their existing native job. Opening an older
store adds only a nullable job association to that existing reservation table;
old unassociated live reservations remain present and are explicitly reported as
`unattributed_store_staging_bytes`, rather than attributed to the wrong job.

The store remains local and same-host. These bindings do not implement remote
lookup, cross-host leases or an independently portable archive. Full archive
closure and full renderer acceptance remain separate requirements.

`test_render3d_job_binding.py` executes actual SQLite submission/reopen/resolution,
request/backend tampering, lifecycle progress and cancellation, active worker and
cache reservations, and preservation of older unassociated reservations. These
are owner/API workflow tests with decoded synthetic PNG fixtures; they do not
claim a real Blender render. The separate real JobStore native workflow retains
that responsibility.
