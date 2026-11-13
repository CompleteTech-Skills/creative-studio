# Director MVP render and media verification

`director_delivery.render_mvp(performance_root, destination)` renders the two
canonical laboratory shots through `BlenderBackend`, with 120 opaque PNG frames
at 24 fps and 640×360 using Cycles CPU. Use a new output directory. The exact
portable Scene3D snapshot, request/result documents, Blender logs and independently
decoded previews remain with the output. Native geometry and animation are
measured independently; every split-shot animation boundary must agree with the
corresponding full native performance. A reconstructed canonical Scene3D binding
must match the supplied binding inventory before rendering.

Install the independent PNG decoder (`pypng`) and the native rig dependencies before execution. The runner checks decoding and H264 encoding availability before rendering.

Run heavy validation under the shared host queue lock:

```sh
flock -w 3600 /home/complete/.cache/hatch-coordination-20261112/native-validation.lock \
  env PYTHONPATH=scripts OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  python scripts/director_delivery.py PERFORMANCE_ROOT NEW_DELIVERY_DIRECTORY \
  --blender /path/to/blender
```

Only independently verified PNG masters reach the authoritative silent-video
encoder. The resulting MP4 is independently decoded and must contain 120 video
frames, five seconds of presentation time within its timebase tolerance, and
zero audio streams. The adapter executes actual missing-master, corrupt-master,
static-native-motion and audio-present controls. Frame controls restore the
original exact bytes; the audio control remains as a separately identified
negative artifact. Contact sheets are inspection aids. Visual review remains
unreviewed, and this engineering fixture does not imply production art approval.

Failures retain private `.director-delivery-*` diagnostics and never publish a
successful destination. The orchestration does not implement resumable jobs;
Director #31 owns persistence, interruption and selective rework acceptance.


A supervised optional-decoder failure can continue with
`--resume-retained-stage PRIVATE_STAGE --retained-inventory TRUSTED_CHECKPOINT`.
The external checkpoint supplies the original supervisor's backend identity and
exact frame index/path/byte/hash inventory. Recovered frames must match it and
pass independent full decoding before publication. Process usage lost before
publication remains unavailable. This is continuation integrity from a trusted
checkpoint, not retrospective authentication of an untrusted producer's claimed
execution. It is separate from the native job resume acceptance in Director #31.
