# Director native reliability adapters

`director_reliability.apply_camera_or_light_patch` consumes an existing
`EditorRepository` and its exact revision/hash. It submits one canonical
`configure-node` command through the Scene3D owner, reopens the committed revision,
and reports affected shots and local half-open frame intervals. A camera change
affects shots selecting that camera; a light change conservatively affects every
shot of the current scene. The receipt compares entire native asset documents,
including their independent review records. It does not create approval records.

Rollback uses `EditorRepository.undo`, `redo` or `restore_revision`, each with its
own expected revision/hash. There is no Director project storage format.

`resume_verified_job` delegates to `render3d_jobs.JobStore.resume` with the native
independent artifact verifier. The same request and job identity must survive.
The receipt distinguishes preserved verified frames from pending work. The native
owner enforces attempts, live process leases, cache publication and ownership.
There is no Director cache or worker storage format.

The focused persistence regression executes a native camera revision, reopens it,
checks exact asset/review preservation, rolls it back, and rejects stale hashes,
stale revisions and invalid native component values without altering the last
valid project. These tests use an explicitly synthetic triangle source.

The explicit Linux integration test additionally launches real Blender, records
its PID, cancels a running native worker through the job store, verifies process
exit, resumes the same identity, preserves a completed frame, corrupts an actual
cached PNG, rerenders only that frame and independently decodes delivery. Run it
under the shared native validation flock with `HATCH_DIRECTOR31_NATIVE=1`. Set
`HATCH_DIRECTOR31_EVIDENCE` to a new directory to retain source, job database,
backend logs, native cache and proof receipt. The test uses an engineering rig;
this is infrastructure evidence, not acceptance of the Director 120-frame MVP.
The complete issue #31 gate still requires running the same interruption and
project identity checks against the actual issue #30 MVP source.

`director_job_graph.RenderGraphBindings` persists immutable graph definitions,
exact input hashes, graph revisions and native job/request/backend/snapshot
bindings in additive tables in the same native JobStore SQLite database. The
render owner explicitly approved using `store._transaction()` for this narrow
Director adapter. Job/frame/lease/event state is always read from native tables;
this adapter has no scheduler or copied execution state. Registration, status and
resume verify exact graph inputs and native bindings. A stale graph hash/revision
or changed snapshot fails before changing native state.

The limited MVP supports independent `RenderAdapter.submit` nodes only. Its edge
set must be empty because all source dependencies must already be materialized;
it rejects arbitrary execution methods and pending dataflow edges. This is not an
implementation of the broader execution service or compiler. The native proof
reopens this same graph mapping, observes the native controller lease while an
actual Blender worker exists, cancels and resumes through the pinned graph
revision, and checks the final state derived from native jobs/events.
