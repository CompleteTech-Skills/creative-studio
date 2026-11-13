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

Memory, elapsed time and staged-file storage budgets are checked while the
worker runs. Linux memory observations include descendants through procfs.
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
