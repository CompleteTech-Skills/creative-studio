# Director capability admission

`director_capabilities.probe(request, evidence_root, configuration=...)` implements
`CapabilityRegistry.probe` from the canonical Director contracts. It returns a
strict Director result, and exclusively publishes `result.json`, exact evidence
receipts, and retained smoke-test geometry/image/video payloads into a **new**
directory. It never calls a provider, downloads weights, or installs anything.

The request's `operations` selects capabilities. `local_probes` must be a subset:
omitting an operation from that list permits discovery only. Discovery yields
`untested`, not execution approval. The fixed local workers can exercise:

- `blender.cpu`, `blender.gpu`: render and independently decode a real 8×8 Cycles
  cube image. GPU discovery is separate from actual successful GPU rendering.
- `codec.h264`, `codec.vp9`, `codec.av1`: encode one 16×16 frame, then use ffprobe
  to decode/count it and establish exactly one video stream and zero audio streams.
- `importer.obj`, `importer.glb`, `importer.gltf`: feed real triangle geometry
  through the existing native rig importer and check three vertices/one triangle.

`importer.blend`, `native.acquisition`, `module.scene3d`, `module.camera3d`,
`module.lookdev3d`, `module.animation3d`, `module.render3d`, `module.editor3d`,
and `planning.provider` return explicit unavailable/untested evidence. Finding
a contract module never proves a production implementation. Native acquisition
is not started by this probe. No 2D substitute is offered for a missing 3D
capability. Missing GPU may suggest **separately probed** CPU execution only
under the caller's explicit device policy.

Executable configuration accepts trusted local `blender`, `ffmpeg`, `ffprobe`
paths (null explicitly disables a tool), `timeout_seconds` (1–120), bounded
`production_limits`, and a public `planning_provider` descriptor (`id`,
`configured`, `price_per_call`, `currency`). It accepts no provider credential,
endpoint, arbitrary command, worker code or environment overrides. Provider
configuration is an assertion supplied by the caller, never authenticated
credential or live-provider proof. Unknown price is recorded as unknown.

Worker probes have a separate timeout, bounded output, one compute thread,
process-group termination, no stdin and a minimal environment without secrets.
On POSIX they also have CPU/output/core limits. They execute only fixed trusted
code in an owned temporary directory; failure text from executables is not
copied into public receipts. Required files are hash-bound and published through
the repository's exclusive rename primitive. A racing or existing destination
is preserved. Retained payloads are explicitly synthetic engineering fixtures.

The production admission policy defaults to 3,600 CPU/GPU seconds, 1.5 GiB RAM,
1 GiB storage and zero provider calls. Callers may configure positive bounded
CPU/GPU/memory/storage ceilings; this configures an allowed budget, **not** a
measurement or promise that a renderer can finish a job. Each consuming native
worker must enforce its own limits and measure execution separately.

`assert_admission(snapshot_or_result, required_operations, evidence_root=...,
resources=...)` verifies the capability snapshot digest and expiry, requires
available/execution-verified operation entries, and requires the evidence root to check exact retained
receipt/payload bytes. Resource estimates
use the canonical budget keys (`max_cpu_seconds`, `max_memory_bytes`,
`max_storage_bytes`, `max_provider_calls`, etc.) and must fit every required
capability's policy. An expired snapshot, mutated payload, missing operation,
unexecuted GPU/codec/importer, or excessive estimate raises a typed `RigError`.
Network planning additionally requires `allow-configured`, a reservation and
known pricing or an explicit unknown-price allowance. This local probe never
produces an execution-verified planning capability; an actual planning owner
must establish that independently.

The snapshot digest is the canonical `object_hash` of its value without the
`digest` field, including its expiry. Each receipt separately records a stable
dependency fingerprint over selected executable hashes, native source hashes
and package versions. Reordered requests produce the same ordered capability
entries and fingerprints; a new expiry intentionally changes the snapshot hash.
Do not treat a receipt as a cryptographic signature or a production visual review.

```bash
PYTHONPATH=scripts:tests python -m unittest test_director_capabilities -v
python scripts/director_capabilities.py --request probe-request.json \
  --destination new-capability-evidence --blender /trusted/path/blender
```

No Director scheduler, provider, asset generator, browser editor or complete
film is implemented here. These capabilities unblock explicit downstream
admission only. Parent Director acceptance remains outstanding.

## Retained validation

The [November 13 execution](../tests/results/director-capabilities-20261113/verification.json)
contains actual Blender 4.5.3 LTS CPU/GPU render receipts, FFmpeg 9.0.2 H.264,
VP9 and AV1 movies, exact frame/audio-stream verification, and three native
imported triangle files. All eight execution probes passed. Scene contracts and
native acquisition were explicitly untested; the planning provider was
unavailable. These historical one-hour snapshots are expired after their
recorded time and cannot authorize new work without fresh probing.

All 17 focused tests passed. The 14 ordinary tests also passed with optional
3D imports blocked. Three native importer/evidence tests are registered only in
the optional native schema/math gate, preserving the 2D dependency boundary.
No provider calls or production film acceptance is claimed.
