# Director render-effect controls

`scripts/director_render_effects.py` is a read-only QA consumer of the native
render authorities. It creates no renderer, job store, artifact publisher or
approval journal. Its engineering result does not grant artistic approval.

Call `verify_control_pair(store, root, before_binding, after_binding, *, effect,
before_frame, after_frame, actor_ids=(), moving_nodes=(), light_ids=(),
noise_binding=None, noise_frame=None, thresholds=None)` with a real
`render3d_jobs.JobStore`. Each binding contains its exact `job`, `job_revision`,
`request_sha256`, `backend_sha256`, `directory` and `snapshot_directory`, as
required by `director_final_delivery.verify_native_materialization`.

That canonical adapter checks the complete current job, exact immutable source
snapshot, backend/request hashes, native publication event and actual decoded
output bytes. The effect consumer reuses it, evaluates the exact source time
with `scene3d_evaluate`, independently decodes selected combined images, then
rechecks live job revisions and source specifications. All controls require the
same source snapshot, backend, seed and render settings. Different shots,
selected cameras and frame ranges may choose the controlled state; they cannot
excuse numerical changes in another physical domain.

The three supported controls are:

- `geometry`: selected `actor_ids` must exhibit nonzero actual actor-local mesh
  deformation. Actor root transforms stay fixed. Only declared `moving_nodes`
  may move separate props. Camera, lights, exposure, materials, visibility,
  geometry identities and vertex topology stay fixed.
- `camera`: the canonical selected camera world matrix must show translation or
  rotation. A lens change alone does not establish camera movement. Actual
  sampled world geometry, lights and exposure stay fixed.
- `lighting`: only intensity of the selected `light_ids` may change. All light
  matrices, types, colors and other properties, plus geometry, camera and
  exposure, stay fixed. An exposure change is not a light-intensity proof.

Every control additionally requires a meaningful decoded RGB difference. The
default mean absolute difference floor is `1/255` and at least 0.5% of pixels
must change. PNG comparisons use normalized encoded display values; EXR uses
decoded linear values. The report records comparison space and file hashes.
The thresholds are explicit, positive, bounded engineering policy, not a
perceptual quality score.

An optional independently rendered repeat must have identical physical state
but a distinct native cache key. Its observed mean drift raises the required
signal floor by `noise_gain` (default 5). Without a repeat the report explicitly
records `noise.measured=false`, the fixed-state/seed policy and no statistical
confidence. It does not request an extra render automatically. A measured
baseline also supplies no statistical confidence interval.

`compare_evaluations` and `image_metrics` are separately usable numerical and
image helpers. Both explicitly report `native_execution_verified=false`:
owner-shaped JSON, synthetic arrays or image files alone cannot attest native
execution. Only successful `verify_control_pair` reports native verification.
Its report preserves fixture provenance, exact selected frames, source/cache
and output hashes, numerical deltas, noise limitations, verifier source hashes
and `production_art_approved=false`. Publish a successful report through the
existing artifact authority and record review through the existing QA workflow.

Run the optional native tests with a dependency-equipped Python interpreter:

```sh
python -m unittest discover -s tests -p test_rig_director_render_effects.py -v
```

The tests exercise real PNG decoding, source snapshot loading, exact shot-time
evaluation, SQLite admission and causal numerical controls using labeled
engineering fixtures. They do not claim successful production control pairs.
Actual production pair bindings come from the narrative and delivery owners
after their few controlled frames complete; no existing film needs rerendering.
