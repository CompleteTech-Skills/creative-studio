"""Named CLI adapters over existing native Scene3D typed transactions."""
from __future__ import annotations

from pathlib import Path

# Each edit delegates exactly one existing typed operation. Parameter files have
# the same shape and limits as the canonical API; no parser invents graph rules.
OPERATIONS = {
    'object': {'add': 'create', 'remove': 'delete', 'duplicate': 'duplicate',
               'transform': 'transform', 'reparent': 'reparent', 'visibility': 'node-flags',
               'configure': 'configure-node', 'rename': 'rename', 'memberships': 'memberships'},
    'prefab': {'create': 'prefab-create', 'instantiate': 'prefab-instance',
               'update': 'prefab-upgrade', 'override': 'prefab-override',
               'reset': 'prefab-reset', 'detach': 'prefab-detach'},
    'camera': {'create': 'create', 'configure': 'configure-node'},
    'lighting': {'configure': 'configure-node'},
    'environment': {'configure': 'configure-world'},
}


def add_parsers(sub):
    for category in ('scene', 'asset', 'object', 'prefab', 'camera', 'shot', 'lighting', 'environment'):
        group = sub.add_parser(category, help='Native ' + category + ' inspection and typed operations')
        commands = group.add_subparsers(dest='action', required=True)
        if category in ('scene', 'asset', 'prefab', 'camera', 'shot'):
            command = commands.add_parser('list')
            command.add_argument('--project', required=True, type=Path)
        if category in ('scene', 'asset', 'object', 'prefab', 'camera', 'shot'):
            for name in ('inspect', 'validate'):
                command = commands.add_parser(name)
                command.add_argument('--project', required=True, type=Path)
                command.add_argument('--identifier', required=True, help='Exact stable ID or project-relative document path')
                if category in ('object', 'camera'):
                    command.add_argument('--scene', required=True)
        for action in OPERATIONS.get(category, {}):
            command = commands.add_parser(action, help='Apply a canonical typed parameter JSON file')
            command.add_argument('--project', required=True, type=Path)
            command.add_argument('--scene', required=True)
            command.add_argument('--file', required=True, type=Path, help='JSON object of native operation parameters')
            command.add_argument('--expected-revision', required=True, type=int)
            command.add_argument('--expected-sha256', required=True)
            command.add_argument('--destination', required=True, type=Path)
            command.add_argument('--dry-run', action='store_true')

    for category, action in (('terrain', 'generate'), ('room', 'create'), ('building', 'generate'), ('layout', 'generate')):
        group = sub.add_parser(category, help='Bounded native procedural geometry')
        commands = group.add_subparsers(dest='action', required=True)
        command = commands.add_parser(action)
        command.add_argument('--project', required=True, type=Path)
        command.add_argument('--scene', required=True)
        command.add_argument('--file', required=True, type=Path, help='Canonical recipe object, or recipe list for layout')
        command.add_argument('--expected-revision', required=True, type=int)
        command.add_argument('--expected-sha256', required=True)
        command.add_argument('--destination', required=True, type=Path)
        command.add_argument('--dry-run', action='store_true')


def execute(args):
    from scene3d_contracts import load_project
    from scene3d_graph import SceneGraph, require
    bundle = load_project(args.project)
    if args.command in ('terrain', 'room', 'building', 'layout'):
        from asset_library import read_source_bytes
        from rig_errors import decode_json
        from scene3d_environment import apply_recipes, plan
        graph = SceneGraph.from_bundle(bundle, args.scene, source_root=args.project)
        require(type(args.expected_revision) is int and graph.revision == args.expected_revision and
                graph.digest == args.expected_sha256,
                'Scene changed after the source revision was inspected.', 'SCENE3D_CONFLICT')
        value = decode_json(read_source_bytes(args.file, 1024 * 1024))
        recipes = value if args.command == 'layout' else [value]
        require(isinstance(recipes, list) and 1 <= len(recipes) <= 16 and
                all(isinstance(recipe, dict) for recipe in recipes), 'Expected one to sixteen typed geometry recipes.')
        generators = {'terrain': {'terrain'}, 'room': {'rooms'},
                      'building': {'rooms', 'workshop', 'fantasy', 'science-fiction'}}
        if args.command != 'layout':
            require(all(recipe.get('generator') in generators[args.command] for recipe in recipes),
                    'Recipe generator does not match the requested CLI category.')
        geometry = [plan(recipe) for recipe in recipes]
        require(len({recipe['id'] for recipe in recipes}) == len(recipes), 'Duplicate recipe IDs in one batch.')
        require(sum(len(item['mesh'].triangles) for group in geometry for item in group) <= 200000,
                'Aggregate generator batch triangle budget exceeded.')
        require(sum(map(len, geometry)) + len(graph.document()['nodes']) <= 10000,
                'Aggregate generator batch node budget exceeded.')
        if args.dry_run:
            return {'dry_run': True, 'recipes': recipes, 'generated_nodes': sum(map(len, geometry)),
                    'triangles': sum(len(item['mesh'].triangles) for group in geometry for item in group),
                    'destination': str(args.destination)}
        return apply_recipes(args.project, args.destination, args.scene, recipes)
    if args.action in OPERATIONS.get(args.command, {}):
        from asset_library import read_source_bytes
        from rig_errors import decode_json
        from scene3d_edit import apply_commands
        from scene3d_prefabs import PrefabLibrary
        from scene3d_snapshot import snapshot_project
        graph = SceneGraph.from_bundle(bundle, args.scene, source_root=args.project)
        require(type(args.expected_revision) is int and args.expected_revision >= 0 and
                graph.revision == args.expected_revision and graph.digest == args.expected_sha256,
                'Scene changed after the source revision was inspected.', 'SCENE3D_CONFLICT')
        parameters = decode_json(read_source_bytes(args.file, 1024 * 1024))
        require(isinstance(parameters, dict), 'Expected a typed operation parameter object.', 'SCENE3D_COMMAND')
        if args.command in ('camera', 'lighting'):
            if args.action == 'create':
                require(isinstance(parameters.get('node'), dict) and parameters['node'].get('type') == 'camera',
                        'Camera creation requires a canonical camera node.', 'SCENE3D_COMMAND')
            else:
                expected_type = 'camera' if args.command == 'camera' else 'light'
                matches = [n for n in graph.document()['nodes'] if n['id'] == parameters.get('identifier')]
                require(len(matches) == 1 and matches[0]['type'] == expected_type,
                        'Configuration target has the wrong native node type.', 'SCENE3D_COMMAND')
        batch = {'schema_version': 1, 'commands': [
            {'operation': OPERATIONS[args.command][args.action], 'parameters': parameters}]}
        applied = apply_commands(graph, PrefabLibrary.from_files(args.project, bundle), batch)
        if args.dry_run:
            return {'dry_run': True, 'revision': graph.revision, 'scene_sha256': graph.digest,
                    'operations': len(applied['results']), 'destination': str(args.destination)}
        result = snapshot_project(args.project, args.destination, graph, scene_path=args.scene,
                                  derived_documents=applied['derived_documents'])
        result['operations'] = len(applied['results'])
        return result
    if args.command in ('object', 'camera') and args.action != 'list':
        graph = SceneGraph.from_bundle(bundle, args.scene, source_root=args.project)
        matches = [n for n in graph.document()['nodes'] if n['id'] == args.identifier]
        require(len(matches) == 1 and (args.command != 'camera' or matches[0]['type'] == 'camera'),
                'Select an existing exact node ID.', 'SCENE3D_TARGET')
        return {'status': 'valid', 'revision': graph.revision, 'scene_sha256': graph.digest,
                'node': matches[0], 'base_pose': graph.evaluate_base(args.identifier)}
    if args.command == 'camera':
        return {'cameras': [{'scene': reference['path'], 'id': node['id'], 'name': node['name']}
                for reference in bundle['project']['scenes']
                for node in bundle['documents'][reference['path']]['nodes'] if node['type'] == 'camera']}
    field = {'scene': 'scenes', 'asset': 'assets', 'prefab': 'prefabs', 'shot': 'shots'}[args.command]
    references = bundle['project'][field]
    if args.action == 'list':
        return {field: references}
    matches = [reference for reference in references if args.identifier in (reference['id'], reference['path'])]
    require(len(matches) == 1, 'Document identifier is missing or has multiple immutable versions; select an exact path.',
            'SCENE3D_TARGET')
    reference = matches[0]
    return {'status': 'valid', 'reference': reference, 'document': bundle['documents'][reference['path']]}
