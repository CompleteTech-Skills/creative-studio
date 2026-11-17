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

The complete parent inventory additionally has explicit identifiers:

- `runtime.python`: records the actual executing interpreter, implementation and
  pointer width after the Director validators import. This is not verification
  of the complete supported OS/device matrix.
- `resource.memory`, `resource.storage`: measure available host physical memory
  and free space on the evidence destination filesystem. Receipts separate these
  observations from policy ceilings. Resource admission checks both the observed
  bytes and policy. Observations can become stale and do not reserve resources,
  establish per-user quota availability, or replace native worker enforcement.
  Missing supported memory APIs produce explicit unavailable evidence.
- `blender.engines`: starts Blender and checks accepted render-engine settings;
  it records the actual installed engine identifiers/version. This does not
  render through every engine; CPU/GPU renders require their separate probes.
- `format.image`: saves and independently reads four RGBA PNG pixels using
  Pillow, retaining the actual PNG and exact alpha values.
- `rigging.native`, `animation.native`: discover the existing native tool owners;
  they remain untested until those owners supply actual execution evidence.
- `procedural.native`: creates real robotic fixture geometry through
  `rig_demo.make_geometry`, exports GLB and reimports/counts its vertices and
  triangles through the native geometry owner. It neither creates a full film
  nor certifies arbitrary creative specifications.
- `geometry.provider`, `motion.provider`, `visual-qa.provider`: record explicit
  unconfigured or configured-but-untested provider states. Public configuration
  descriptors use `geometry_provider`, `motion_provider`, `visual_qa_provider`
  with the same strict fields as `planning_provider`; credentials and endpoints
  are rejected. No descriptor authenticates a provider or starts a paid call.

Every receipt includes backend, supported operation/format descriptions,
permissions, policy limits and explicitly unknown performance information.
Declared formats are not execution proof; availability and execution verification
remain separate. New probes retain the v1 request/result interface and existing
capability names.

`importer.blend` uses Blender to author one real triangle and save a `.blend`
project, then decodes it through the existing native importer
(`rig_import.load_character_model`, which routes `.blend` through the pinned
`rig_blender` worker) and checks three vertices/one triangle. The `.blend`
payload is retained as `application/x-blender`.

Discovery, initialization and execution are three separate receipt fields.
`module.scene3d`, `module.camera3d`, `module.lookdev3d`, `module.animation3d`,
`module.render3d`, `module.editor3d` and `native.acquisition` are *discovered*
when their owner module file exists. When explicitly listed in `local_probes`
they are *initialized* in a fresh, bounded interpreter: the module is imported
and one dry, side-effect-free constructor runs (`<module>.schemas()` checked as
Draft 2020-12 schemas; for acquisition, `asset_contracts.versions()` with no
model call and no generation). A successful initialization records
`initialized: true`, `execution_verified: false`, status `untested` and reason
`initialized-not-executed`; it can never satisfy admission. A missing owner gives
`missing-native-module`; a failed import or constructor gives
`initialization-failed` (or `initialization-timeout`) without retaining worker
output. Finding or initializing a contract module never proves a production
implementation, and native acquisition generation is never started.
`planning.provider` remains explicit unavailable/untested evidence. No 2D
substitute is offered for a missing 3D capability. Missing GPU may suggest
**separately probed** CPU execution only under the caller's explicit device policy.

Executable configuration accepts trusted local `blender`, `ffmpeg`, `ffprobe`
paths (null explicitly disables a tool), `timeout_seconds` (1–120), bounded
`production_limits`, and a public `planning_provider` descriptor (`id`,
`configured`, `price_per_call`, `currency`). It accepts no provider credential,
endpoint, arbitrary command, worker code or environment overrides. Provider
configuration is an assertion supplied by the caller, never authenticated
credential or live-provider proof. Public IDs start with an ASCII letter or digit,
followed by up to 127 ASCII letters, digits, periods, underscores, colons or
hyphens. A known price requires a currency of three uppercase ASCII letters.
Invalid descriptors raise `DIRECTOR_CAPABILITY_CONFIG` before staging.

Worker probes have a separate timeout, bounded output, one compute thread,
no stdin and a minimal environment without secrets. On POSIX they also have
process-group termination and CPU/output/core limits. On Windows the worker
timeout terminates the launched process. They execute only fixed trusted
code in an owned temporary directory; failure text from executables is not
copied into public receipts. Worker scratch directories live outside the
evidence stage. Required files are hash-bound and published through the shared
`atomic_publish` primitives. The pinned authority must match the parent directory
identity captured during destination admission before the stage is created
(`pinned_directory`/`private_stage` on Linux, the descriptor-only
`pinned_directory_fd`/`private_stage_fd` on other POSIX hosts, and the
handle-pinned `atomic_windows` stage on Windows), and the payload is published
by exclusive rename. On POSIX a parent identity check immediately before the
rename reports `DIRECTOR_CAPABILITY_DESTINATION` if it detects a swap. The
rename uses the held directory capability even if the public name changes
after the check. On Windows the held handles prevent renaming the parent
while publication is in progress. Hosts without a pinned publication adapter
fail closed with `DIRECTOR_CAPABILITY_PLATFORM`. Exclusive rename preserves a
racing or existing destination. The shared primitive retains an empty private
`.director-probe-*` wrapper after successful publication. Retained payloads
are synthetic engineering fixtures.

The production admission policy defaults to 3,600 CPU/GPU seconds, 1.5 GiB RAM,
1 GiB storage and zero provider calls. Callers may configure positive bounded
CPU/GPU/memory/storage ceilings; this configures an allowed budget, **not** a
measurement or promise that a renderer can finish a job. Each consuming native
worker must enforce its own limits and measure execution separately.

`assert_admission(snapshot_or_result, required_operations, evidence_root=...,
resources=...)` verifies the capability snapshot digest and expiry, requires
available/execution-verified operation entries, and requires the evidence root to check exact retained
receipt/payload bytes. Admission uses the canonical secure descriptor-relative
Director reader. A platform without that reader fails closed with
`DIRECTOR_PLATFORM` even if it can publish receipts through another adapter.
The Windows handle-pinned adapter currently covers publication. Windows
end-to-end admission would require a secure Director reader and native tests.
Resource estimates
use the canonical budget keys (`max_cpu_seconds`, `max_memory_bytes`,
`max_storage_bytes`, `max_provider_calls`, etc.) and must fit every required
capability's policy. An expired snapshot, mutated payload, missing operation,
unexecuted GPU/codec/importer, or excessive estimate raises a typed `RigError`.
Network planning additionally requires `allow-configured`, a reservation, an exact provider allowlist containing the identity in every verified execution receipt, and
known pricing from every exact hash-verified executed receipt, a matching budget currency and amount, or an explicit unknown-price allowance. Explicit price arguments must agree with retained receipt pricing; discovery descriptors alone confer no execution admission. This local probe never
produces an execution-verified planning capability; an actual planning owner
must establish that independently.

The snapshot digest is the canonical `object_hash` of its value without the
`digest` field, including its expiry. Each receipt separately records a stable
dependency fingerprint over selected executable hashes, native source hashes
and installed package metadata. These observations are separate from execution
and can include dependencies that a worker did not load. Version scope is
recorded as reported during the probe, reported by an initialized component,
a probe protocol label, or unknown. The codec smoke tests have an unknown
execution version. The BLEND receipt records the versions reported during
authoring and native import as separate execution details. The combined
authoring/import operation has an unknown version. Matching receipt and snapshot
versions verifies their recorded agreement. The caller must retain and trust
the original snapshot or pin its digest independently. The expiry bounds use of
that snapshot, whose references pin its exact receipt bytes. Rebuilding a
snapshot around older standalone receipts and assigning a new expiry provides
no evidence of fresh execution. Establishing the age of independent receipts,
the identity of dependencies in the running process, their deployed versions,
or the current execution host requires separate verification.
Reordered requests produce the same ordered capability entries and
fingerprints when the observed inputs stay the same. A new expiry changes the
snapshot hash. Do not treat a receipt as a cryptographic signature or a
production visual review.

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

The [November 17 inventory](../tests/results/director-inventory-20261117/verification.json)
records the exact earlier revision it exercised. Its one-hour snapshot has
expired. It includes the runtime, resource, PNG and real native geometry
probes plus initialization-only results. It includes no execution evidence
for Blender, codecs or providers.
