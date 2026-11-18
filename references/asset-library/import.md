# Native GLB and glTF import

`ImportPipeline` in `scripts/asset_library_import.py` imports one local glTF 2.0
or GLB into an existing canonical `AssetLibrary`. Pass an `AssetIdentity`,
a semantic version, `AssetMetadata`, and a file with license evidence.
`probe(source)` inventories exact source files and embedded data and estimates
native geometry load without producing a library version.

```python
from asset_library import AssetLibrary
from asset_library_import import ImportPipeline, resolve_import

library = AssetLibrary("/path/to/library")
reference = ImportPipeline(library).import_asset(
    "/path/to/asset.gltf",
    identity=identity,
    version="1.0.0",
    metadata=metadata,
    license_file="/path/to/license.txt",
    unit_scale=1.0,
)
report = resolve_import(AssetLibrary("/path/to/library"), reference)
```

The importer captures a private copy of the source and its local buffers and
images. Resource paths must fit the existing Scene3D portable path profile.
The source format is determined by its glTF content and must match the
`.glb` or `.gltf` extension. The original filename, resource paths, roles,
size and hashes are retained. A probed embedded buffer is hashed at its
declared byte length. Exact original file bytes are available under
`source/`, including any original buffer padding. License evidence is available
as `license.txt`. Preserve the exact version reference returned by the import
to retrieve those bytes after reopening.

Khronos validates the captured original and the resulting self-contained GLB.
The importer combines buffers and embeds URI-backed PNG or JPEG images, then
reopens the result using the native geometry and rig reader. It keeps source
node indices, materials, skins, morph targets and animation declarations.
A non-default source unit scale in the range `[0.001, 1000000]` applies an
explicit uniform transform above the source scene roots and scales reserved
native world-space bone tails. Camera clipping planes, orthographic camera
magnitudes and punctual light ranges retain their declared values. The source and
output convention is glTF right-handed Y-up. The retained conversion and
`import-report.json` record the requested scale and checked hashes.
`diagnostic.png` shows the imported base mesh vertices before applying morph weights or skinning
and ships as technical evidence. Reviewed preview production remains in #145.

This profile accepts native triangle geometry and the recognized glTF material
and texture transform extensions. Optional punctual light declarations can
be retained in the GLB. The native reader's declared extension, geometry,
rig and texture restrictions still apply. Unknown extensions require a
separate adapter. Input limits are the tighter of the library, snapshot
and caller limits. The native preflight estimates aggregate geometry
work before decoding. The importer checks the projected packed GLB size and
reserves combined geometry and preview memory before assigning the remaining
budget to the native texture reader. General Blender ingestion, OBJ, arbitrary
basis changes, batch import, and general asset generation remain open in #139.

Run the focused tests with the optional rig Python dependencies, Node.js,
and the pinned offline `gltf-validator` package from `tools/rig/package-lock.json`.

```sh
HATCH_GLTF_VALIDATOR_ROOT=/path/to/node_modules/gltf-validator \
  PYTHONPATH=scripts:tests python -m unittest test_rig_asset_library_import -v
```

## Packed Blender triangle fixture

On Linux with `/usr/bin/prlimit` and `wait4`, set `HATCH_BLENDER` to Blender 4.5.3
build `67807e1800cc` and use the same library and license metadata.

```python
reference = ImportPipeline(library).import_blender_fixture(
    identity=identity,
    version="1.0.0",
    metadata=metadata,
    license_file="/path/to/license.txt",
)
```

The fixed worker builds a static, untextured triangle in a private workspace,
packs its dependency closure, and writes the scene to a Blender library file.
The library stores scene data without the workspace file-browser state. A
separate Blender process reopens the exact original with auto-execution
disabled. Both calls share the configured Blender process deadline. Linux
enforces the virtual address space limit, set to eight times the configured
resident budget to allow Blender's memory mappings. Each child's
kernel-reported peak RSS must fit the resident budget before the adapter
admits its output. The fixture API takes no source path, and
`import_asset` rejects `.blend` paths before launching Blender.

The canonical version retains exact Blender bytes at `source/fixture.blend`,
the direct export at `source/model.glb`, and the normalized runtime payload at
`normalized.glb`. Both GLBs pass the pinned Khronos Validator 2.0.0-dev.3.10.
The native reader checks the triangle's coordinates and winding in right-handed
Y-up meters. `import-report.json` retains both process observations, the
Blender build and worker hash, and the source and derivative digests.
To replay the recorded argument arrays, use a fresh private working directory
for `<workspace>`, the configured Blender executable for `<blender>`, and the
packaged `scripts/asset_import_worker.py` for `<worker>`. Run `create` before
`reopen` in the same directory within the reported shared deadline.
The fixture covers one mesh without external resources, materials, rigs, or animation.

Run the focused suite with `HATCH_BLENDER` and the validator path set to
exercise the Blender roundtrip.
