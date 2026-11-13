# Native Blender camera assets

Inside Blender, `camera3d_native_export.export_camera_library(destination,
optics, render, world_matrix, ...)` writes an appendable `camera.blend` and its
canonical resolved state and exact file receipt to a new directory. It calls
the existing physical camera adapter in a private scene, and writes only that
scene and its indirect camera/collection data using Blender's
[library writer](https://docs.blender.org/api/4.5/bpy.types.BlendDataLibraries.html#bpy.types.BlendDataLibraries.write).

Existing project scenes, objects, active cameras and the open project filepath
are retained. Temporary IDs belong to the operation and are removed on success
or failure. Publication uses the existing exclusive directory primitive, so an
existing destination is never replaced. Unsupported physical camera settings
fail through the canonical adapter; this native asset format does not silently
apply glTF's lossy fallback policy.

The library preserves the supplied evaluated rigid pose, perspective/orthographic
projection and physical lens/DOF/exposure/shutter controls. It contains one static
camera asset. It does not export scene geometry, animation, full production render
settings, source dependency packages, or a rendered sequence. The caller must
obtain the sampled state from the authoritative scene/camera evaluators; the
serializer never evaluates another timeline. Those remaining Camera #80 owner
integrations are still required before the package can be considered complete.

Run actual acceptance using Blender 4.5.3 and its isolated Python 3.11 schema
dependencies:

```bash
HATCH_CAMERA_TEST_SITE=/path/to/python311-schema-dependencies blender \
  --background --factory-startup --disable-autoexec --threads 2 \
  --python-exit-code 1 --python scripts/verify_camera3d_native_export.py -- \
  --destination /path/to/new-native-evidence
```

The fixture checks a real unsupported-lens failure for output/data leaks, exports
perspective and orthographic camera assets without changing the source project,
opens each resulting Blender file independently, and compares projection/world
matrices and physical optical state. Reports retain source hashes, exact native
file hashes and measured errors. The optional automated test is enabled with
`HATCH_CAMERA3D_NATIVE_EXPORT_TEST=1`, `HATCH_BLENDER` and
`HATCH_CAMERA_TEST_SITE`; otherwise it explicitly skips the native gate.

Directory publication currently requires the tested Linux procfs/descriptor adapter.
Planning and core glTF loss analysis remain portable. Publication fails closed
on unsupported hosts before creating output. A pinned, mode-0700 outer wrapper
holds an inner payload; verification and exclusive publication use that same
open directory. Concurrent replacement of the public wrapper cannot substitute
output or redirect cleanup. The shared helper intentionally retains its empty
outer wrappers for owned maintenance. The glTF validator reads a parent-process
descriptor path while that parent holds the descriptor until validation exits.
This is namespace race protection, not isolation from a hostile process with
the same user identity and access to the process descriptors.
