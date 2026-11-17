# Director inventory probes, November 17

Executed at the `executed_head` recorded in `verification.json`, on Linux with
CPython 3.12. That file also lists the implementation SHA256 values and every retained
file's hash. It supersedes the unmerged November 13 evidence from #401, which
recorded an earlier head. These are engineering capability probes, not
production, visual or artistic approval.

Execution-verified: `runtime.python`, `resource.memory`, `resource.storage`,
`format.image` (four exact RGBA PNG pixels), `procedural.native` (robotic
fixture GLB round trip), and `importer.obj`/`importer.glb`/`importer.gltf`
(one triangle each through the native rig importer).

Initialized only (`initialized: true`, `execution_verified: false`, status
`untested`): the six `module.*` contract owners (fresh-interpreter import plus
`schemas()` checked as Draft 2020-12) and `native.acquisition`
(`asset_contracts.versions()`, no model call, no generation).

Not executed: Blender was not run on this host, so `blender.cpu`,
`blender.gpu`, `blender.engines` and `importer.blend` record `missing-blender`.
`planning.provider` is unconfigured, with unknown pricing. Native Blender gates
are covered by hosted CI only. Resource values are transient observations, not
reservations. The one-hour snapshot has expired and cannot authorize later work.
