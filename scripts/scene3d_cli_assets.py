"""Scene CLI adapters for the existing immutable native asset import authority.

These commands register or materialize exact versions in the canonical private
AssetLibrary. Existing Scene3D nodes remain pinned; importing a version does not
silently create an instance or upgrade an authored scene.
"""
from __future__ import annotations

import math
from pathlib import Path

ACTIONS = frozenset({'import', 'version', 'export', 'attach'})


def add_parsers(commands):
    """Extend the existing ``scene3d asset`` subparser (not a second CLI)."""
    for action in ('import', 'version'):
        command = commands.add_parser(action, help='Register an exact native GLB/glTF asset version')
        command.add_argument('--library', required=True, type=Path)
        command.add_argument('--source', required=True, type=Path)
        command.add_argument('--version', default='1.0.0' if action == 'import' else None, required=action == 'version')
        command.add_argument('--attribution', required=True)
        command.add_argument('--license-file', required=True, type=Path)
        command.add_argument('--license-summary', default='Evidence retained; distribution restricted pending rights evaluation')
        command.add_argument('--unit-scale', type=float, default=1.0, help='Explicit source-to-meter conversion')
        command.add_argument('--fixture', action='store_true', help='Classify procedural engineering input honestly')
        if action == 'import':
            command.add_argument('--asset-id', required=True)
            command.add_argument('--create-library', action='store_true',
                                 help='Explicitly initialize a new private library before importing')
        else:
            command.add_argument('--reference-file', required=True, type=Path,
                                 help='Exact prior library reference JSON containing id/version/sha256')
    command = commands.add_parser('export', help='Materialize an exact registered native package')
    command.add_argument('--library', required=True, type=Path)
    command.add_argument('--reference-file', required=True, type=Path)
    command.add_argument('--destination', required=True, type=Path, help='New destination directory')

    command = commands.add_parser('attach', help='Instance an exact native library version into a new project snapshot')
    command.add_argument('--library', required=True, type=Path)
    command.add_argument('--reference-file', required=True, type=Path)
    command.add_argument('--project', required=True, type=Path)
    command.add_argument('--scene', required=True)
    command.add_argument('--identifier', required=True, help='New scene instance root ID')
    command.add_argument('--parent', default=None, help='Exact existing parent node ID')
    command.add_argument('--expected-revision', required=True, type=int)
    command.add_argument('--expected-sha256', required=True)
    command.add_argument('--destination', required=True, type=Path)
    command.add_argument('--dry-run', action='store_true')


def _reference(path):
    from asset_library import read_source_bytes
    from asset_library_contracts import validate
    from rig_errors import decode_json
    value = decode_json(read_source_bytes(path, 1024 * 1024))
    return validate(value, 'AssetRef')


def execute(args):
    """Delegate every operation to the current canonical import/library owners."""
    from asset_library import AssetLibrary
    from rig_errors import require
    from scene3d_import import import_gltf, materialize, resolve_import
    require(args.action in ACTIONS, 'SCENE3D_ASSET_COMMAND', 'Unsupported asset command.')
    if args.action == 'attach':
        return _attach(args)
    if args.action == 'export':
        reference = _reference(args.reference_file)
        library = AssetLibrary(args.library)
        # Refuse non-native library entries before choosing any output path.
        resolve_import(library, reference)
        report = materialize(library, reference, args.destination)
        return dict(report, kind='scene3d-asset-package-export',
                    contents='Exact registered source, normalized geometry, native authoring data and evidence',
                    production_approved=False)
    # argparse normalizes numeric CLI input to a float. Guard this adapter too
    # before calling an older importer with arbitrary Python integer objects.
    require(type(args.unit_scale) is float and math.isfinite(args.unit_scale) and 1e-6 <= args.unit_scale <= 1e6,
            'SCENE3D_ASSET_UNITS', 'Source unit scale must be a finite float between 1e-6 and 1e6.')
    reference = _reference(args.reference_file) if args.action == 'version' else None
    library = AssetLibrary(args.library, create=args.action == 'import' and args.create_library)
    if reference is not None:
        library.resolve(reference)
    asset_id = reference['id'] if reference is not None else args.asset_id
    imported = import_gltf(args.source, library, asset_id=asset_id, version=args.version,
        attribution=args.attribution, license_file=args.license_file, unit_scale=args.unit_scale,
        fixture=args.fixture, license_summary=args.license_summary, prior_reference=reference)
    return dict(kind='scene3d-asset-registration', reference=imported.library_reference,
                scene_asset_reference=imported.asset_reference, source_sha256=imported.report['source_sha256'],
                geometry=imported.report['geometry'], independent_validator=imported.report['independent_validator'],
                hierarchy_preserved=imported.report['hierarchy_preserved'],
                original_bytes_preserved=imported.report['original_bytes_preserved'],
                previous_reference=reference, fixture=imported.report['fixture'],
                scene_instances_unchanged=True, production_approved=False,
                losses=imported.report['losses'])


def _attach(args):
    """Keep library registration separate; atomically publish only project state."""
    import hashlib
    from asset_library import AssetLibrary
    from rig_errors import require
    from scene3d_contracts import load_project
    from scene3d_graph import SceneGraph
    from scene3d_import import add_import, resolve_import
    from scene3d_snapshot import _new_destination, snapshot_project
    _new_destination(args.destination)
    bundle = load_project(args.project)
    graph = SceneGraph.from_bundle(bundle, args.scene, source_root=args.project)
    require(type(args.expected_revision) is int and args.expected_revision >= 0 and
            graph.revision == args.expected_revision and graph.digest == args.expected_sha256,
            'SCENE3D_CONFLICT', 'Scene changed after the source revision was inspected.')
    reference = _reference(args.reference_file)
    library = AssetLibrary(args.library)
    manifest = library.resolve(reference)
    # Bound immutable package materialization before reading payloads. Final
    # exact retained-history/file/node/byte budgets remain the snapshot owner's.
    require(sum(record['bytes'] for record in manifest['files']) <=
            min(bundle['project']['budgets']['max_bytes'], 128 * 1024 * 1024),
            'SCENE3D_ASSET_LIMIT', 'Native package exceeds the project attachment byte profile.')
    imported = resolve_import(library, reference)
    asset_path = imported.asset_reference['path']
    payloads, derived = {}, {}
    for record in manifest['files']:
        raw = library.read_file(reference, record['path'])
        require(len(raw) == record['bytes'] and hashlib.sha256(raw).hexdigest() == record['sha256'],
                'SCENE3D_SOURCE_CHANGED', 'Registered native bytes changed during project attachment.')
        if record['path'] == asset_path:
            if asset_path in bundle['documents']:
                require(bundle['inventory'][asset_path]['sha256'] == record['sha256'],
                        'SCENE3D_SOURCE_CHANGED', 'Project already contains different bytes for this asset path.')
            else:
                derived[asset_path] = raw
        else:
            payloads[record['path']] = raw
    require(asset_path in derived or asset_path in bundle['documents'],
            'SCENE3D_ASSET_COMMAND', 'Registered package lacks its exact scene asset document.')
    instance = add_import(graph, imported, args.identifier, parent=args.parent)
    report = snapshot_project(args.project, args.destination, graph, scene_path=args.scene,
        derived_documents=derived, extra_payloads=payloads, dry_run=args.dry_run)
    return dict(report, library_reference=reference, scene_asset_reference=imported.asset_reference,
                instance=instance, library_unchanged=True, production_approved=False)
