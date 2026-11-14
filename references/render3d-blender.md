# Trusted Blender CPU execution

`scripts/render3d_blender.py` executes a captured Render3D snapshot with the fixed
`scripts/render3d_blender_worker.py` entry point. It requires Blender 4.5 LTS and
launches background, factory-startup, disable-autoexec workers. Pass an explicit
executable to `BlenderBackend(executable)` or configure the existing
`HATCH_BLENDER` discovery setting.

The adapter accepts owner-verified native-rig previews and Scene3D snapshots.
It captures the source closure before preparing sanitized GLB derivatives, binds
frame specifications to the requested shot, and rechecks executable and worker
identities after execution. A caller-owned destination is never overwritten.
Failed staging directories retain `failure.json` and the bounded Blender log;
they are diagnostic evidence, not published render results.

The current adapter supports Cycles CPU, PNG and EXR combined output. Auxiliary
passes, GPU device selection, HDRI environments and material overrides require
their respective adapters and currently fail explicitly here. Native animation
uses imported NLA actions and explicit local sample times. Scene temporal
features require the Scene3D evaluation owner. Successful image decoding does
not establish artistic approval or full-film acceptance.

Metadata admission reserves the frozen closure, its metadata, the overlapping
private native validation copy and estimated output before owner decoding.
Per-call source caps prevent a changed closure using unused request headroom.
Prepared asset boundaries and worker-job writes recheck staged bytes while
reserving output and later source verification. Memory, elapsed time and
staged-file storage budgets are also checked while the worker runs. Linux memory observations include descendants through procfs.
Cancellation and budget failures terminate the process group. Logs retain at
most 2 MiB. This is cooperative application supervision, not an OS sandbox or a
hard kernel memory quota. Resource admission also runs before expensive source
preparation. The independent output verifier decodes output pixels before
publication.

Run focused process and contract checks with the optional render dependencies:

```sh
PYTHONPATH=scripts:tests python -m unittest \
  test_render3d_contracts test_render3d_independent \
  test_render3d_blender test_render3d_verify -v
```

The process tests execute actual child processes for successful completion,
worker failure, cancellation, timeout, memory and storage exhaustion. Actual
Blender fixture rendering is separate evidence and must run through the shared
native validation queue documented by the coordination workspace. Keep the
request, captured snapshot, worker report, image hashes and decoded verification
report when reporting such a run; a unit test pass is not a render claim.

Enable the real native regression in a host with the optional rig and decoder
dependencies plus a configured Blender executable:

```sh
HATCH_RENDER3D_REAL_TESTS=1 HATCH_BLENDER=/absolute/path/to/blender \
PYTHONPATH=scripts:tests python -m unittest test_render3d_blender -v
```

Acquire the coordination workspace's native-validation lock around that command
when sharing a host. This regression renders a small synthetic articulated rig,
checks actual evaluated vertex and pixel changes, and decodes PNG 8/16-bit and
EXR half/full-float outputs. It does not establish PBR texture fidelity, data
passes, a complete film, or production visual approval.

## Bound output and native passes

Unmarked combined-only requests preserve the original backend path. To use the canonical quality adapter, call `render3d_quality_binding.bind_quality_request(request)` before queue submission. This returns a new request carrying optional `settings.quality_sha256`; it does not mutate an existing request or queue identity. Auxiliary pass requests require this binding. `validate_quality_settings(settings)` must run before a consumer accepts any completed job or cached frame, as well as before worker execution. A changed helper requires rebinding and a new cache identity.

The digest covers the canonical output plan and the exact trusted quality helper/binding/dependency bytes. Frame settings hashes therefore change with helper code and output settings. The supervisor and worker check helper bytes before and after execution, retain the bound plan in the worker report/manifest, and independently decode every requested image before publication. Output storage admission reserves all requested passes and per-frame temporary images.

Bound PNG8/16 supports combined output; EXR16 supports combined and albedo; EXR32 supports combined, depth, normal and albedo. Transparent albedo uses the beauty coverage alpha. Half-float depth/normal reject before source decoding. `quality-bound-passes` in capabilities advertises this explicit request binding requirement, rather than implicit handling of unmarked requests.

The bound path also captures the selected Blender 4.5 executable's bundled OCIO config and complete canonical LUT inventory. Pass the same `executable=` to `bind_quality_request` and to consumer revalidation. Custom `OCIO`/`BLENDER_OCIO` overrides reject on the bound path. The same quality digest includes the canonical color closure, so a changed LUT invalidates queue/cache identity. The supervisor pins `OCIO` to the retained private profile before Blender starts, and the worker verifies its bytes before and after execution. This follows [Blender's documented OCIO environment mechanism](https://docs.blender.org/manual/en/4.5/render/color_management.html).

`manifest.json` references `color-context.json` and the retained `color-config` inventory. The worker reports actual display/view/look/exposure/gamma, image settings, native pass flags and alpha threshold. Numeric chromaticities and alpha association remain explicitly unverified in this receipt; media consumers must obtain their authoritative validation before color conversion or compositing. Mask equality alone does not prove straight alpha association. These paths require the optional canonical color package's host capture dependency; the Blender worker's profile verification uses standard-library helpers.

`render3d_quality_jobs.BoundQualityJob(store, backend, bound_request, snapshot_root, verifier)` is the thin generic JobStore consumer. It revalidates current helper/profile bytes and the backend identity before submit/status/run/resume/materialize, including an already-complete cache return. It delegates all scheduling and file publication to JobStore. Existing authoring owners can supply their backend/verifier wrappers, an explicit `executable`, an `identity_provider`, and a `preflight` callback. The constructor never rewrites an already-submitted request.

Optional `RenderResult.artifacts` carries companion provenance as ordinary bounded file references (`path`, SHA-256, byte count), up to 4,096 files of at most 256 MiB each. An absent list has legacy empty-list semantics. The bound renderer lists the context receipt and every captured profile file here; they are not logs. Cache, materialization and archive consumers must retain these files and preserve each original manifest's relative bundle roots. This extension is not publication-ready until those canonical consumers are integrated and checked.
