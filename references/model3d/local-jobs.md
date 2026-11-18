# Local Model3D job checkpoints

`model3d_jobs` supplies a local CPU job lifecycle around trusted Python engine
handlers. Requests contain an engine identifier and explicit parameters. The
identifier selects application code; it never becomes a shell command or an
import name. Input paths are supplied separately by the Python caller, captured
before execution, retained unchanged, and bound into every output checkpoint.
The standalone CLI currently exposes the real native primitive engine only.

```bash
python scripts/model3d_job_cli.py create --root output/box-job --request box-request.json
python scripts/model3d_job_cli.py run --root output/box-job
python scripts/model3d_job_cli.py status --root output/box-job
python scripts/model3d_job_cli.py archive --root output/box-job --destination output/box.zip
python scripts/model3d_job_cli.py restore --archive output/box.zip --destination output/box-restored
python scripts/demonstrate_model3d_jobs.py --destination output/new-job-demo
```

An executable request is:

```json
{
  "engine": "native-primitive",
  "parameters": {
    "kind": "box",
    "parameters": {"width": 2, "height": 1, "depth": 0.5},
    "name": "box",
    "normal_mode": "smooth",
    "format": "glb"
  }
}
```

The Python API exposes `create_job`, `run_job`, `status`, `cancel_job`,
`clear_cancellation`, `run_batch`, `archive_job` and `restore_job`. Public records
use schema version 1 and explicit structured errors. A trusted handler receives
a `Context` containing the frozen request, input directory, private output
directory, budgets and `check()` method. Handler versions must identify code and
dependency configuration. The native CLI hashes its Model3D/rig modules; changing
that signature requires an explicit retry instead of reusing old results.

Jobs retain attempt counts, timestamps, active attempt tokens, request hashes,
input hashes, immutable revision inventories, handler versions and failure
diagnostics. A successful handler writes only into its private output directory.
Outputs must be nonempty, regular, portable, bounded files. The job verifies
source bytes again, records exact output sizes/hashes, publishes a new revision
with exclusive rename, then atomically updates the active job record. A failed
attempt preserves the prior active revision. Retries never replace old outputs.
Completed work is reused only after request, handler and payload verification.

A process exit before checkpoint publication leaves an interrupted attempt and
requires `run --retry`. A process exit after publication is recovered by checking
the exact owner/request/input/handler/attempt and all output hashes, then promoting
the already published checkpoint without replaying the engine. The tests execute
both exit points in separate Python processes. Concurrent writers use the shared
OS locking authority and fail with an actionable busy diagnostic.

`cancel` records a cancellation request without waiting for the running writer.
Handlers must call `Context.check()` around bounded stages; that check enforces
the cancellation signal and wall-clock budget. A cancellation is persistent:
inspect status and use `clear-cancellation` before retrying. Cancellation and
runtime limits are cooperative; arbitrary handler code is not sandboxed or
forcibly terminated. The CLI primitive handler checks before and after native
primitive construction and export. The batch API executes at most 128 jobs
sequentially and records each failure without deleting successful siblings.

ZIP packages contain the declared job state, captured inputs, and all referenced
immutable revision payloads. Restoration checks file counts, expanded bytes,
portable paths, aliases, file types, ZIP central-directory allocation bounds,
inventory membership, exact hashes and the restored native job closure before
exclusive publication into a new directory. Unknown schema versions, traversal,
duplicate members, symlinks and corrupt payloads fail. Unpublished interrupted
temporary work is omitted from portable packages. Active jobs must be reconciled
first. Archives do not grant review approval or authenticate third-party claims.

This is an additive part of #50, with no changes to the central Model3D dispatcher
or 2D workflows. It does not establish full #50 completion. Persisted per-item
batch scheduling, provider-call reconciliation, hard process/CPU/GPU quotas,
storage-retention cleanup, authenticated human review, schema migrations beyond
rejecting unknown versions, power-loss durability, broad engine integration and
parent demonstrations A–M remain separate work. Files are atomically replaced and
flushed; no machine power-loss recovery claim is made. Native geometry/export
validation stays owned by the actual engine. Generic callback success cannot
substitute for geometry, interoperability or artistic acceptance.

`test_model3d_jobs.py` executes real GLB output/reimport and archive restoration,
process-exit checkpoint recovery, cache nonexecution, source/output corruption,
cancellation, runtime checks, failed revision preservation, attempt exhaustion,
writer exclusion, sibling batch isolation, destination races, traversal and
bounded hostile ZIP directory rejection. The demonstration records real mesh
counts and exact GLB/archive/restoration hashes for three procedural fixtures;
it explicitly records unexecuted Blender, browser, rendering and provider gates.
