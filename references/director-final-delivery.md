# Final Director delivery

`director_final_delivery.py` composes the native narrative verifier, native
`JobStore`, `RenderArtifactVerifier` and SilentVideo. The final profile is exactly
720 global frames at 24 fps, lasting 30 seconds, with at least five production
shots and consistent even dimensions of at least 1280 by 720. Masters use opaque
8-bit sRGB PNG; MP4/H.264 and WebM/VP9 exports each undergo independent decoding
and must have exactly one video stream and zero audio or other streams.

## Before rendering

The narrative owner supplies `verify_narrative_acceptance(project_root,
evidence_root, expected_proof_ref)`. Its proof must identify the actual frozen
project bytes, the ordered production subset, numerical motion/contact checks
and the existing inspected preview artifacts. The project may register extra
proof-camera shots; they are excluded from the production frame count.

Call `preflight_final_render(snapshot_root, requests, narrative=...)` before
submitting the requests through `ExecutionService`. This delegates narrative
verification, compares exact production shot identities and ranges, requires the exact approved source_start and playback_rate for every shot, preserves
the accepted framing aspect, and verifies each native shot's endpoint frame
specifications. Capability, budget, resource and worker admission remain with
the scheduler and native job owner. No render is started by preflight.

## Completed native output

Each binding is an exact dictionary containing:

- `job`: the native render job SHA256 identity.
- `job_revision`: its revision **after** successful materialization.
- `request_sha256` and `backend_sha256`: the admitted native identities.
- `directory`: the materialized output directory relative to the delivery root.
- `snapshot_directory`: the frozen source directory relative to that root.

`verify_native_materialization(store, root, binding, required_scope=...)` is the
stateless evidence adapter also available to Director QA control-frame checks.
It requires a real native `JobStore`, rechecks exact request/backend/revision and
complete frame states, matches the actual `result.json` SHA256 to an authoritative
`delivery-published` event, independently verifies the snapshot and all pixels,
then checks the live job again. A claimed verified result file cannot replace
that native publication history. The adapter creates no job or review store.

`assemble_final_frames(store, root, bindings, narrative=...)` additionally
requires the complete Scene3D scope, exact ordered global frames 0 through 719,
disjoint output ownership, one immutable project/source snapshot, and the
explicit accepted production shots. It decodes the entire sequence through the
renderer verifier and then independently admits the exact PNG inventory through
SilentVideo. It preserves every per-frame cache provenance record and reports
actual observed successful-worker wall time/memory, attempts and recorded cache
reuse. Unknown failed-attempt consumption remains explicitly unavailable;
historical reused worker consumption is not mislabeled as new execution.

## Media and recovery

`encode_final_media(store, root, bindings, destinations, narrative=..., ...)`
requires explicit `h264-mp4` and `vp9-webm` destinations. It delegates each encode
to SilentVideo and independently revalidates the published artifact. Frame count,
exact rational rate, duration, geometry and all stream counts must pass. Native
revisions are checked throughout encoding and again before accepting each output.
It returns only after both formats pass; a failed second format preserves the
already completed first artifact and cannot produce a completed final result.

Existing media requires `retained_receipts={profile: file_ref}` with externally
pinned `path`, `sha256` and `bytes`, read from `receipt_root` (or `root`). Every
retained receipt and actual movie is reverified before reuse. Neither a filename
nor a mutable producer-side verified flag authorizes reuse. The existing execution/media controller owns
persistence, cancellation, checkpoints and publication of the returned result.
The current scheduler runs the native render graph; media phases remain
synchronous calls to the canonical media authority until its native driver and
atomic admission hook are available. No unregistered export driver is implied.
The required trusted `checkpoint(profile, receipt, request)` callback persists
an immutable receipt before the next profile can begin and returns its exact
file reference. The adapter reads those bytes back from `receipt_root` and
requires exact receipt equality. Thus a later WebM failure leaves the controller
with the pinned MP4 receipt needed for safe reuse. The returned result includes
these `receipt_references`. The adapter creates no extra controller, encoder,
claims or approval authority.

The exported result carries `visual_review: not-performed`. Native narrative
readiness, decoded image integrity, video verification, actual visual review,
editable-project verification and complete delivery remain distinct gates.

## Acceptance status

The focused tests exercise final timing/selection, malformed and stale inputs,
exact raw-byte evidence, rejection of fabricated verified metadata, and an actual
cancelled SQLite native job. These fixtures do not constitute a 720-frame film.

The full final render, complete MP4/WebM decoding, owned-copy missing/corrupt
master controls, actual audio-stream rejection, and geometry/camera/light pixel
controls must execute against the new accepted narrative after the authoritative
scheduler, backend and publisher dependencies are ready. Earlier 120-frame MVP
and durability evidence remains immutable and is not relabeled as final output.
Native editable scene export and restore are tracked separately by Director40.
