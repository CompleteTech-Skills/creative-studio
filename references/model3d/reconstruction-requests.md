# Versioned local reconstruction requests

`model3d_reconstruction_cli.py` is an isolated executable adapter over the existing #330 reconstruction engines. It contains no alternate geometry algorithm and never executes providers or code from a document. Central CLI/schema registration and durable job integration remain shared-owner wiring steps.

```sh
python scripts/model3d_reconstruction_cli.py schema
python scripts/model3d_reconstruction_cli.py capabilities
python scripts/model3d_reconstruction_cli.py validate-request --file examples/model3d-reconstruction/requests/contour-v1.json
python scripts/model3d_reconstruction_cli.py request --file examples/model3d-reconstruction/requests/contour-v1.json --root . --destination output/new-contour --export-format gltf --report
```

`--help` and `request --help` expose actual commands. `schema`/`capabilities` need no numerical backend. `validate-request` checks fields, JSON bounds and portable spellings without reading observations; it does not establish geometry/calibration validity. `request` runs the real method and exports GLB/glTF/OBJ through the accepted native owner. `--report` prints the actual reconstruction/acceptance report; the default prints a concise result. An explicit `--export-format` overrides the optional request `export_format`; the returned report states the selected format. A new existing-parent destination is required; existing/racing destinations are preserved.

## Request v1

Every request has `schema_version: 1`, `kind: "hatch-model3d-reconstruction-request"`, and one `method`. Optional shared fields are `export_format` (`glb`, `gltf`, `obj`) and `notes` (at most 4096 characters). Unknown methods, versions and unsupported fields at every level reject. Numeric values must be finite JSON numbers, never booleans. Exact original request bytes are retained as `request.json`, bound by SHA256 in `reconstruction.json` and `acceptance.json`. Programmatic calls without original serialized bytes retain an explicitly canonical JSON representation.

| Method | Required fields | Optional fields |
|---|---|---|
| `contour-extrusion` | `outer: [[X,Y],...]`, `thickness_m` | `holes: [[[X,Y],...],...]`, `sources` |
| `profile-revolution` | `profile: [[radius,Y],...]`, `segments`, `cap_ends` | `sources` |
| `heightfield` | `heights_m: [[height,...],...]`, `spacing_m: [X,Z]`, `base_thickness_m` | `sources` |
| `mask-extrusion` | `source`, `interpretation`, `pixel_size_m`, `thickness_m` | `mask`, `origin_m: [X,Y]` |
| `visual-hull` | `views`, `bounds_m: [[minX,minY,minZ],[maxX,maxY,maxZ]]`, `resolution: [X,Y,Z]` | none |

A source record is exactly `{"path":"relative/image.png","role":"source"}`; roles are `source`, `mask`, `depth`, `sketch`, `reference`. `sources` optionally preserves up to 16 observations behind authored contour/profile/height samples. Their presence does not authenticate the author's derivation or infer physical calibration.

An interpretation is `{"channel":"alpha","threshold":127}` or an explicitly selected `luminance` channel. Mask extrusion permits optional integer `stride: 1..32`, defaulting to 1. The point-grid sample begins at raster [0,0]; `pixel_size_m` is per original pixel and is multiplied by the explicit stride for reconstructed cell spacing. Original bytes/full dimensions remain unchanged. A separate `mask` must have exactly the same original dimensions as `source`. No cleanup or segmentation is silently inferred.

Each visual-hull view contains exactly `name`, `mask` (source record), `interpretation` and `camera`. View masks use original dimensions; stride is 1. The camera requires `calibration: "known"|"estimated"|"unknown"`, `near_m`, `far_m`, exactly one `camera_to_world` or `world_to_camera` 4×4 matrix, and exactly one `intrinsics` 3×3 K or `orthographic: [left,right,bottom,top]`. Unknown calibration remains a valid status to describe input but is rejected by execution; it never becomes invented precision. Canonical world is right-handed +Y up, +Z forward meters; camera-local view is -Z, up +Y; raster coordinates start at the top-left pixel edge. Masks must be complete foreground observations. Native numerical, contradiction, topological, voxel and reprojection checks remain authoritative.

## Paths, closure and limits

Documents cannot specify filesystem roots, publication destinations, Python callbacks, commands or providers. `--root`/API `root` explicitly select a trusted source root. All source paths must be portable relative NFC names; traversal, absolute paths, device names, symlinks, case aliases and file/directory prefixes reject. Source files are read through the existing no-follow descriptor authority with pinned root identity, captured once into a private closure, then verified by the strict reconstruction image owner. Geometry and delivered source evidence use captured bytes even if original files later change. Original observations are never rewritten.

Requests are bounded to 4 MiB and 200,000 JSON nodes before copies. Existing reconstruction limits apply: images at most 64 MiB/four million pixels each, aggregate captured observations at most 256 MiB, bounded contours/profile grids/heightfields and bounded visual hulls. Excessive or unsupported work fails rather than silently reducing quality. Decode/resource/topology failures leave no public partial output. Publication occurs once from a complete private artifact directory; request bytes, original images, occupancy and exported mesh/report inventories remain relative to that artifact.

## Programmatic and job wiring

```python
from model3d_reconstruction_cli import execute_request, validate_request, schema, schemas

validated = validate_request(request)
result = execute_request(validated, root=context.inputs,
                         destination=context.output / 'model',
                         format='glb', on_stage=lambda stage: context.check())
```

`execute_request(request, *, root, destination, format=None, on_stage=None, request_bytes=None)` returns `schema_version`, `ok`, `method`, `destination`, `export_format`, `acceptance`, and the actual `report`. All persisted file references are output-relative. `destination` is an absolute runtime locator; an outer job engine may project it to its own relative artifact path. `request_bytes`, when supplied, must decode to the validated request and are retained exactly.

`on_stage(name)` is a Python-only hook at `capture`, `compute`, `export`, and `publish`. Raising cancels before final publication and cleans only owned temporary state. Existing reconstruction methods are synchronous and bounded; this API does not claim mid-loop preemption, durable resume or provider cancellation. A job engine should call its context cancellation checker at these boundaries. No duplicate algorithm or separate store is introduced.

Stable wiring names: `main(argv=None)`, `capabilities()`, `validate_request(value)`, `execute_request(...)`, `schema()`, and `schemas()` returning `{"model3d-reconstruction-request.schema.json": schema}`. Central `model3d.py` may lazily delegate a `reconstruct` command to `main`, and the shared generator may register `schemas()`; this change itself edits neither shared owner.

`test_model3d_reconstruction_cli.py` executes every local method, request schema/help/discovery, exact request/source preservation, genuine three-format native output/reimport, hole volume, mask thickness, multiview IoU/occupancy, unsafe paths/unknown fields, source mutation after capture, cancellation and racing publication. These are engineering checks, not artistic approval or full #46/#10 completion.
