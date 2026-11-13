# Persistent local batches and owned workers

The additive `model3d_job_batches` API schedules the existing immutable local
jobs. It does not replace their geometry, source, revision or package authority.
Every item is an ordinary native job at `jobs/<item-id>` and retains its own
input snapshot, attempts and output revisions. `batch-request.json` records an
immutable versioned item list, exact native job/request/input identities, worker
resource limits and CPU concurrency. `batch.json` is the recoverable scheduling
journal. Item IDs are portable lowercase ASCII identifiers and batches contain
one to 128 items with one to four concurrent CPU workers.

```python
from model3d_job_batches import create_batch, run_batch, status, cancel_batch

create_batch("output/new-batch", [
    {"id": "box", "request": {
        "engine": "native-primitive",
        "parameters": {"kind": "box", "parameters": {}, "name": "box",
                       "normal_mode": "smooth", "format": "glb"}}}
], concurrency=1, resource_limits={"threads": 1, "runtime_seconds": 120})
result = run_batch("output/new-batch")
```

The standalone CLI exposes `create --items JSON --root NEW_DIR`, `run --root`,
`status --root`, and `cancel --root`. `run --retry-failed` is an explicit retry
of failed/interrupted items; verified completed siblings are reused. Cancellation
persists and requires a new explicit batch rather than silently clearing intent.
Source paths are supplied only when creating a batch; original bytes are captured
by the existing native job owner and the portable batch request records hashes.

Only the fixed `model3d_job_worker.py` executable is launched. Its engine import
names and dispatch functions are literal application code, never values decoded
from the batch JSON. The trusted adapter uses a constant local engine registry;
without that additive adapter the worker supports the existing native primitive
engine. No shell, arbitrary module, provider request or remote upload is selected
by a serialized executable path. A worker's handler signature must match the
captured launch signature before processing begins.

Each worker has a separate supervisor and engine process. The supervisor owns a
new engine process group, drains stdout/stderr into bounded log files, monitors
the wall-clock deadline and cancellation signal, and terminates only its own
birth-checked fixed-command engine group. The fixed Python executable, script,
mode and root must match the live process as well as its Linux birth identity.
Descendants collected from that anchored live parent are checked again by birth
identity before escalation. Cancellation never treats a PID in an editable batch
journal as sufficient ownership evidence. The batch scheduler writes a request
to the checked worker/root/token; the supervisor performs the actual signaling.

Before importing the engine, Linux workers install kernel `RLIMIT_AS`,
`RLIMIT_CPU`, `RLIMIT_FSIZE`, disabled core dumps and inherited CPU affinity.
Address space and CPU limits are per process; file size is per file. Descendants
inherit those limits and CPU affinity. The default numerical thread count is one.
The supervisor reports exact configured limits, affinity, wall duration, return
code, captured log bytes and available waited-child CPU/max-RSS measurements.
Max RSS is an operating-system process-family maximum, not summed resident RAM.
Monitoring is subject to normal scheduling latency and does not claim hard
real-time response. No fake GPU quota or aggregate process-tree RAM/disk quota is
reported. Unsupported quota keys fail explicitly.

Hard supervision currently requires Linux. Windows/macOS attempts return
`MODEL3D_WORKER_PLATFORM` before launching an unsupervised fallback. This is an
explicit implementation boundary, not tested cross-platform parity. Likewise,
resource limits and owned process groups are not an arbitrary-code security
sandbox; trusted engines must not deliberately detach descendants into unrelated
sessions. Native engine resource caps and final aggregate output verification
remain required. Disk retention and whole-host scheduling remain separate.

Supervisors are detached from the scheduler's session and continue when the
scheduler process exits. Restart verifies worker request ownership, the live
supervisor command/birth identity and cooperative OS lock, then reattaches to the
same record. Completed native checkpoints are rehashed and their handler signature
checked before acceptance. Persisted per-item records retain output inventories,
inventory hashes, attempts and resource outcomes. A successful sibling does not
rerun because a different item failed. There are no provider calls in this path.

The scheduler records a reserved worker directory before launching. A missing
state proves a reservation had not launched and can be retried. If a scheduler
exits in the narrow process-creation/identity-write window, the fixed supervisor
can establish its own identity; the scheduler waits instead of immediately
duplicating work. A reservation whose startup remains unprovable becomes an
explicit `MODEL3D_BATCH_START_UNKNOWN` diagnostic and is not automatically
replayed. Native job locks remain the final writer-exclusion authority. A lost
supervisor is not equivalent to a successfully completed engine, and operator
inspection may be required for ambiguous startup/process ownership.

Fixed diagnostic modes (`sleep`, `cpu`, `memory`, `file`, `tree`) are developer
fixtures for exercising actual kernel limits and descendant cancellation. They
never claim to generate geometry and cannot select arbitrary executable code.
Normal batch requests do not enable them. Geometry acceptance comes from real
trusted engine outputs, native reimport and checkpoint hashes.

This is additional #50 engineering work. It does not close parent demonstrations
A–M, complete provider reconciliation, portability migrations, artistic review,
GPU quotas, aggregate hard RAM/disk quotas, power-loss guarantees, or final
integrated acceptance. Reviewed local jobs and central dispatch files remain
unchanged. Heavy application tests choose their resource lane externally; product
worker code contains no Codex host lock paths and does not acquire testing locks.
