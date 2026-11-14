# Native asset CLI adapters

`scene3d_cli_assets` extends the existing `scene3d asset` parser. It delegates
GLB/glTF decoding, hierarchy, skin/clip retention, source snapshots and independent
Khronos validation to `scene3d_import.import_gltf`. Immutable versions and payload
hashes remain owned by `AssetLibrary`; project publication remains owned by
`snapshot_project`.

After the parent CLI wires the adapter:

```bash
python scripts/scene3d.py asset import --library /private/library --create-library \
  --source character.glb --asset-id character --attribution 'Source author' \
  --license-file license.txt
python scripts/scene3d.py asset version --library /private/library \
  --reference-file prior-reference.json --source revised.glb --version 1.0.1 \
  --attribution 'Source author' --license-file license.txt
python scripts/scene3d.py asset export --library /private/library \
  --reference-file exact-reference.json --destination new-package
```

Reference files contain the exact `reference` object returned by registration:
`id`, `version` and `sha256`. No command selects mutable “latest.” `--unit-scale`
is an explicit bounded source-to-meter conversion; fixture classification and
license evidence use the native import owner's existing rules. `--create-library`
explicitly initializes a new private library first. If subsequent source
validation fails, that initialized library remains a valid separate resource;
no asset version is reported as registered. Existing libraries are never replaced.

Registration and scene attachment are separate explicit operations. To place an
already registered native package into a project, use:

```bash
python scripts/scene3d.py asset attach --library /private/library \
  --reference-file exact-reference.json --project project --scene scene.json \
  --identifier character-instance --expected-revision 2 --expected-sha256 HASH \
  --destination new-project-snapshot
```

Attachment reads the selected immutable library package and delegates component
instancing to `add_import`. It publishes one complete independently reopened
Scene3D snapshot containing the exact imported asset manifest, normalized
geometry, original source bytes, native authoring data and evidence. `--dry-run`
performs the same project closure and destination admission without publication.
The library remains unchanged during attachment. Failed scene publication does
not roll back or delete a separately registered immutable asset version.

`asset version` registers a new version with an exact parent reference; it does
not rewrite an existing instance's selected version. Upgrading existing authored
instances requires the separate canonical replacement adapter and remains outside
these commands. Package export preserves the complete registered native package;
it is not a new geometry format converter or visual approval.

Integration boundary: existing project-backed `asset list`, `inspect` and
`validate` retain their current meanings. The parent CLI must call
`add_parsers(asset_subparsers)` and dispatch actions in `ACTIONS` to
`scene3d_cli_assets.execute` before the project-only asset handler. The adapter
contains no parallel registry, source parser, geometry engine or publisher.
