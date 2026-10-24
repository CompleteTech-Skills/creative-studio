# Asset import and generation (issue #139)

`scripts/asset_library_import.py` implements the #138 canonical `AssetLibrary`
contract. Import accepts a genuine GLB/glTF 2.0 resource closure, a limited OBJ
profile (three-component positions/normals, two-component UVs, polygon faces;
only v/vn/vt/f/o/g/s/mtllib/usemtl directives, one named MTL per declaration,
plain `map_Kd`; freeform curves/surfaces are unsupported), or an explicitly trusted,
packed Blender file. The initial Blender/OBJ conversion profile admits only a
simple Principled-to-Output material graph (only Base Color, Metallic, Roughness,
Alpha, and mapped glTF IOR may differ from Blender factory socket defaults) and armature modifiers; procedural
shader graphs, material animation, other modifiers and constraints fail explicitly
until a verified baking adapter exists. Native source objects are limited to
MESH/ARMATURE/EMPTY; collection/particle instances, physics and pose constraints
are unsupported. Positive animation admission covers object transforms, bound
pose transforms and shape-key values, including actual layered action bags and
NLA action bindings; non-finite values and out-of-range bound components fail.
Armature modifiers require local, unmasked linear vertex-group binding; envelopes,
dual-quaternion preserve-volume, segmented Bendy Bones, envelope multiplication,
nonstandard inheritance, weighted non-deforming bones and multiple modifier
blending are unsupported. Canonical OBJ coordinates preserve source XYZ through
the fixed Blender import/export basis adapter; requested normalization applies
explicitly afterward.
Every influence is exported. A separate decoded source/derivative comparison checks
rest-space vertex/joint-weight associations at explicitly reported precision
(1e-5m position, 1e-6 weights); unnamed/ambiguous joints require a separate
correspondence adapter. Input glTF primary weights remain exact; initial native Blender export uses
Blender numeric precision and declares its uncertainty. Visibility and other unsupported channels fail. First
conversion explicitly declares sampled animation and omitted source world lighting
and authoring metadata. Exact native originals retain that source information. Inventoried OBJ texture maps can therefore
be refused by this narrower conversion profile. Unknown formats and unsupported glTF extensions fail before
Blender runs. FBX, USD, Alembic, STL and PLY adapters are not implemented.

The ordinary 2D gate does not import NumPy, Blender, or the Khronos validator.
Real import requires the optional rig environment and pinned Khronos Node reader.
Blender conversion requires Blender 4.5 and Linux `/usr/bin/prlimit`; other hosts
report the backend unavailable. This is process/resource quarantine, not an OS
sandbox against a malicious Blender binary or native decoder exploit. Use a
trusted Blender installation. Untrusted `.blend` files are refused without opening
them. Explicit `trusted_packed_blend=True` is a source trust decision, never inferred
from a filename. The fixed worker uses a private process working directory, disables automatic scripts, rejects text blocks,
linked libraries, object/data drivers, sequencer media and unpacked images/movie/audio;
no user Python executes. Diagnostic rendering explicitly disables source
compositing/sequencing and uses an application-owned neutral world, so native
compositor file-output nodes cannot write outside the import workspace.

## API

Construct `ImportPipeline(AssetLibrary(private_root), blender=trusted_executable)`.
`probe(source)` captures a bounded inventory and reports that geometry has not yet
been validated. `capabilities(probe_blender=True)` executes a real version probe.
`import_asset(source, identity=..., version=..., metadata=..., license_file=...)`
requires the #138 identity/metadata schemas and explicit license evidence. Missing
license bytes and invalid metadata fail. Fixture inputs must remain `fixture=True`.
No human approval is produced. Explicit fixture classification is embedded in the
normalized runtime payload as reserved provenance metadata, including otherwise
unmarked sources, and survives saved Blender/GLB reimports. Original bytes remain
unchanged. The default runs actual Blender save/reopen,
independent GLB validation and a diagnostic 128-pixel render.

The CLI supports `probe`, `import`, `generate`, and `capabilities`; `--library`
must name an already initialized canonical library, `--request` is a bounded JSON
mapping of API keyword arguments, and `--source` is the input file. For example:

```sh
python scripts/asset_library_import.py import --library /private/library \
  --source /source/character.gltf --request /source/import-request.json \
  --blender /trusted/blender
```

`generate` supports real Blender box and equal-axis sphere geometry, explicit
size/color and optional one-second translation animation. It records parameters,
worker hash, actual Blender version and actual generated payload hash. It does
not claim an external AI provider. `import_batch` accepts at most eight explicit
requests, with one atomic publication per item; an earlier successful item remains
published if a later item fails. Cancellation is checked before publication.

## Preservation and validation

Original source and dependencies are read once through descriptor-safe bounded
file readers, copied into a private tree, hashed, and retained byte-for-byte in
`source/`. The relationship map retains original names even where library-safe
logical names are hashes. `restore_original` reconstructs that exact tree into an
exclusive new destination and preserves any racing existing destination.

URI preflight rejects network, absolute, parent traversal, schemes, aliases and
symlinks. Limits include 128 original files, 96 MiB aggregate source, 64 MiB per
file/output, 8-item batches, bounded nodes/accessors/depth, conservative memory
admission (applied to source bytes before parsing), JSON nesting/structural-token
budgets, streamed OBJ/MTL records with bounded line size, CPU/wall timeout and process-group termination. Linux limits are defense
in depth around a trusted fixed executable; quarantined data is never published
before native and independent output checks pass.

`runtime/asset.glb` packs resources and retains source hierarchy, skins, morphs,
materials and animation channels. Explicit root matrices apply requested source
unit scale, handedness/up basis and optional supported native ground alignment.
Only known glTF integer fields are normalized in a copy; original JSON bytes are
retained. Unsupported native rig profiles require actual Blender inspection.
Blender `.blend` and roundtrip GLB are derivatives: the report explicitly lists
animation renaming/resampling and omitted optional extensions rather than claim
perfect derivative identity. Raw Blender scene counts can include importer bone
shape helper meshes; the independently reopened GLB counts are separate.

`evidence/import.json` records actual reader observations, source hashes,
normalization and subprocess execution. The preview is a diagnostic artifact;
it is not #145 preview publication, visual review, human approval, nor production
release evidence. #140 rig mapping, #141 broad library recovery, #142 export,
#143 browser, and #144 scene editing remain separate acceptance scopes.

Validation: `PYTHONPATH=scripts:tests python -m unittest tests/test_rig_asset_import.py -v`
in the pinned rig environment, with `HATCH_BLENDER` and
`HATCH_GLTF_VALIDATOR_ROOT` set. Blender tests skip explicitly when unconfigured;
such a skip is not evidence of Blender acceptance.
