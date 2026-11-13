# Persistent silent export jobs

`scripts/silent_jobs.py` manages encoding work over the canonical silent-video
encoder. It does not render frames or schedule render workers. A plan records the
exact source-frame manifest, encoding request, explicit source/timeline context,
FFmpeg and ffprobe bytes and reported versions, controller implementation hashes,
and resource budget. Changing those identities requires a new job.

Create a job with `create(request, source_root, job_root, destination)`, inspect it
with `status(job_root)`, and execute it with `run(job_root)`. `resume` requires an
interrupted checkpoint. Failed and cancelled work requires explicit `retry`.
`cancel` writes an ownership-checked marker; the encoder receives that marker
through its canonical cancellation callback. The CLI emits observed progress as
JSON lines on stderr and the final result as JSON on stdout.

Each job retains verified source frames and complete failed attempts. It never
appends to a partial MP4 or WebM. A complete private encode can be independently
verified and reused after interruption. Otherwise a retry starts a whole encode
and increments its actual attempt counter. Publication creates one immutable
directory containing the video and a hash-bound receipt; existing destinations
are preserved. A crash after publication is recovered by independently probing
the existing video before acknowledging completion.

Job ownership uses an exclusive nonblocking lock. A worker inherits that lock
and a lifetime pipe in its own process session. Loss of the parent closes the
pipe and terminates the worker's process group. No caller-supplied PID is used to
signal unrelated processes. The controller also enforces wall time, retained
work bytes, worker log bytes, attempt count and controller-run count. Admission
checks estimated decoded memory, peak disk use and available disk space. The
decoded memory value is an admission estimate, not an operating-system memory
limit. This CPU encoder estimates zero GPU bytes and uses at most two threads.

`run_batch` runs at most sixteen independently owned video or image-sequence jobs
sequentially, selecting the canonical controller from each captured plan kind.
Every video is verified as one video stream and zero audio streams. Completed
outputs are reused only while their exact artifact and receipt hashes still
match. `silent_sequence_jobs.create(source, source_root, job_root, destination,
provenance=...)` creates a byte-preserving imported PNG/EXR sequence job. Its
controller reuses `silent_sequence.export_sequence` and `verify_sequence`; it
does not convert PNG to EXR or render replacement frames. Each sequence has an
independently decoded complete hash inventory. No visual approval is implied by
these numerical and media checks.

The secure persistent worker currently requires Linux procfs, directory file
descriptors and the canonical scoped descriptor capability. Other platforms
report an explicit unavailable capability. The portable contracts run on all
three CI platforms; actual FFmpeg interruption and recovery tests run explicitly
on Linux. Preview batches, native render/timeline context authentication and
cross-platform worker support remain integration requirements
of issue #239; this video-job increment alone does not satisfy that issue.
