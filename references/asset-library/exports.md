# Verified asset payload exports

`asset_library_export.ExportPipeline` reuses the canonical `AssetLibrary` resolver,
importer's bounded glTF reader/Khronos gate, fixed Blender adapter and native
geometry/skin/animation evaluators. It creates no asset records or alternate
registry. Original library versions, payloads and receipts remain immutable.

```python
from asset_library import AssetLibrary
from asset_library_export import ExportPipeline

pipeline = ExportPipeline(AssetLibrary("/private/library"), blender="/opt/blender/blender")
receipt = pipeline.export(exact_reference, "/private/new-export", format="gltf",
                          required_capabilities=["geometry", "skins", "animation"])
```

The destination is a **new directory**, whose ancestors must be actual directories rather than symlink/junction aliases. Canonicalize trusted system temporary roots before choosing a destination (for example macOS `/var` aliases). GLB exports preserve exact canonical
runtime bytes. glTF exports preserve the exact decoded document/accessors and
binary buffer while exposing `asset.bin` as an external dependency. Embedded
texture buffer views remain valid glTF; the external buffer contains their bytes.
Both input and output pass the actual Khronos validator and native accessor and
hierarchy admission. The canonical default runtime is `runtime/asset.glb`;
`runtime_path` can explicitly select another self-contained registered GLB.
Unknown formats, missing capabilities, external runtime URIs and incomplete or
corrupt source/dependency inventories fail before publication.

Native export requires a registered `native/asset.blend` and its import report,
`trusted_native=True`, and explicit `allowed_losses` containing every recorded
native derivative loss string. It does not silently run arbitrary authoring
formats or generate an unrelated Blender file. The application-owned Blender
worker opens the exact packed source with scripts/autoexec disabled, rejects
linked data/drivers/unpacked external media, saves/reopens in distinct processes
and exports a diagnostic GLB. Khronos independently validates that actual output.
The native evaluator compares rest triangle corners, winding, normals/tangents/colors/UVs, morph values,
PBR material factors and decoded texture pixels, skin associations, bounds and
named sampled animation. Source named scene parents/world transforms and named joint parent/rest/inverse-bind data remain bound across backend-added wrapper nodes. Unnamed/ambiguous native node profiles and multiple source scenes fail explicitly. Numeric tolerances are 1e-5 m for rest geometry/morphs,
1e-6 for UV/material/weights and the existing evaluator's 0.01 m sampled-animation
maximum. Triangle/vertex/mesh order and normal splitting may change; changed
surface data fails. Unsupported object-only animation and changed clip names
fail explicitly. Arbitrary procedural shader equivalence remains unsupported;
visual review remains independent from technical validation.

All actual output files and an exact inventory/hash-bound `export.json` receipt
are staged before one exclusive atomic directory rename. Existing and racing
destinations are preserved. Cancellation before publication leaves no public
output; completed earlier batch exports remain valid if a later request fails.
Batches contain at most eight requests and run sequentially (concurrency one).
Runtime memory/byte/time/closure budgets, including conservative decoded triangle-comparison allocation, are bounded. Missing optional geometry
dependencies produce `LIBRARY_EXPORT_DEPENDENCY`; optional Blender execution
requires the existing exercised Linux resource/process adapter.

Receipts include exact source identity/attribution, provenance, publication
status and closure pins. Export payload verification does not grant a license,
public distribution authorization, artistic approval, installation or complete
closure-package readiness. Asset packages and isolated restore belong to the
existing package owner. Export does not infer lifecycle promotion or approval.

Run `python scripts/demonstrate_asset_export.py --help` to retain GLB/glTF and
optionally native outputs from an existing exact library reference. The
`--allow-recorded-native-losses` demonstration flag is an explicit acceptance of
that selected source's recorded derivative losses. Synthetic fixture provenance
and absent artistic approval remain in the receipt.

Validation: `PYTHONPATH=scripts:tests python -m unittest discover -s tests -p
 test_rig_asset_export.py -v`. Configure the pinned Khronos validator as for asset
import. Set `HATCH_BLENDER` to run the native save/reopen case; otherwise that
specific integration case reports an explicit skip. Use the shared native test
lock and owned TMPDIR/TMP/TEMP paths in the coordinated validation environment.
