# Camera3D interchange

`camera3d_export.export_gltf(destination, optics, render, world_matrix)` exports
one already evaluated camera to a new directory. It uses the existing canonical
Camera3D validation and optical gate, the shared exclusive publication helper,
and the pinned independent Khronos validator. It does not evaluate a second
camera timeline or create a scene renderer.

The directory contains `camera.gltf`, the complete supplied resolved state in
`canonical.json`, an explicit `losses.json`, the independent validator report and
a receipt of exact file hashes. Existing destinations are never replaced.
`validate=False` permits unverified preparation and is recorded as such in the
receipt; it does not establish interchange acceptance.

The static glTF camera preserves centered perspective/orthographic projection,
near/far clipping, effective gate aspect and overscan, and the evaluated rigid
world pose. Camera3D and glTF share the Y-up world and camera-local -Z view/+Y up
conventions, so the pose is serialized column-major without another basis
conversion. glTF orthographic `xmag`/`ymag` are half the effective view width and
height. These mappings follow the [Khronos glTF camera specification](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#cameras).

Core glTF does not represent physical focus/aperture, exposure, shutter motion
blur or an off-center principal point. Active unsupported effects fail unless
the caller explicitly supplies `allow_loss=True`; that choice writes a loss
entry for every active omission. The original values remain in the canonical
snapshot. Focal length and sensor dimensions describe authoring intent retained
in that snapshot; core glTF stores their resulting projection, not those physical
controls. No camera animation or geometry is exported by this static adapter.

Reproduce independent interchange validation with the optional rig dependencies,
pinned `tools/rig` Node dependencies and Blender 4.5.3 installed:

```bash
python scripts/verify_camera3d_export.py --prepare --destination /path/to/new-evidence
HATCH_CAMERA_TEST_SITE=/path/to/python311-schema-dependencies blender \
  --background --factory-startup --disable-autoexec --threads 2 \
  --python-exit-code 1 --python scripts/verify_camera3d_export.py -- \
  --verify --destination /path/to/new-evidence
```

The first command independently validates perspective, orthographic and
explicit-loss glTF files. The second imports both supported projection types
through Blender's actual glTF importer and compares native projection/world
matrices against the canonical source. It records source/file hashes and measured
errors. This is static camera interchange proof, not physical-effects parity or
the complete Camera #80 render/export/cache acceptance. Native Blender save,
production frame rendering, silent media, full target/path/transition cache
identity and resumed-render verification remain required owner integrations.

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
