"""Apply bounded native Scene3D edits and publish a complete new snapshot."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from asset_library import read_source_bytes
from rig_errors import RigError, decode_json, finite_json
from scene3d_contracts import load_project
from scene3d_graph import SceneGraph, require
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project


def apply_commands(graph, library, commands):
    """Atomically promote a successful bounded batch on a single-owner graph.

    Operation-level expected revisions refer to sequential working revisions;
    the successful batch itself commits one public graph revision.
    """
    working = SceneGraph(graph.document(), external_references=graph._references,
                         asset_bounds=graph._asset_bounds)
    catalog = PrefabLibrary()
    # Catalog values are immutable by API: only the three mapping containers
    # change during registration, avoiding a duplicate copy of all payload bytes.
    catalog._documents = dict(library._documents)
    catalog._payloads = dict(library._payloads)
    catalog._references = dict(library._references)
    result = _apply_commands(working, catalog, commands)
    committed = graph.replace_document(working.document(), expected_revision=graph.revision)
    library._documents, library._payloads, library._references = catalog._documents, catalog._payloads, catalog._references
    return dict(**result, revision=committed['revision'])


def _apply_commands(graph, library, commands):
    """Execute only against the attempt's private working objects."""
    finite_json(commands)
    require(isinstance(commands, dict) and set(commands) == {'schema_version', 'commands'} and
            type(commands['schema_version']) is int and commands['schema_version'] == 1 and
            isinstance(commands['commands'], list) and 0 < len(commands['commands']) <= 256,
            'Expected a version1 batch containing 1–256 commands.', 'SCENE3D_COMMAND')
    derived = {}
    original_payloads = set(library.payloads())
    results = []
    operations = {
        'create': (graph.create, {'node'}, {'expected_revision'}),
        'delete': (graph.delete, {'identifier'}, {'policy', 'expected_revision'}),
        'reparent': (graph.reparent, {'identifier', 'parent'}, {'preserve_world', 'expected_revision'}),
        'duplicate': (graph.duplicate, {'identifier', 'new_id'}, {'parent', 'preserve_world'}),
        'reorder': (graph.reorder, {'identifier', 'index'}, set()),
        'transform': (graph.set_transform, {'identifier', 'transform'}, {'space', 'expected_revision'}),
        'node-flags': (graph.set_node_flags, {'identifier'}, {'visible', 'selectable', 'locked', 'render_visible',
                                                         'camera_visible', 'cast_shadow', 'receive_shadow'}),
        'memberships': (graph.set_memberships, {'identifier'}, {'collections', 'layers'}),
        'collection': (graph.collection, {'identifier'}, {'name', 'layer', 'visible', 'editor_visible', 'render_visible', 'locked'}),
        'tags': (graph.set_tags, {'identifier', 'tags'}, set()),
        'prefab-create': (lambda **p: library.create(graph, **p), {'root_id', 'identifier', 'version'},
                          {'name', 'parameters', 'parameter_bindings'}),
        'prefab-instance': (lambda **p: library.instantiate(graph, **p), {'reference', 'identifier'}, {'parent', 'transform', 'overrides'}),
        'prefab-override': (lambda **p: library.set_overrides(graph, **p), {'identifier', 'overrides'}, set()),
        'prefab-reset': (lambda **p: library.reset(graph, **p), {'identifier'}, set()),
        'prefab-upgrade': (lambda **p: library.upgrade(graph, **p), {'identifier', 'new_reference'}, {'resolutions'}),
        'prefab-detach': (lambda **p: library.detach(graph, **p), {'identifier'}, set()),
    }
    for command in commands['commands']:
        require(isinstance(command, dict) and set(command) == {'operation', 'parameters'} and
                isinstance(command['operation'], str) and command['operation'] in operations and
                isinstance(command['parameters'], dict), 'Unsupported/malformed command.', 'SCENE3D_COMMAND')
        function, required, optional = operations[command['operation']]
        parameters = command['parameters']
        require(required <= set(parameters) <= required | optional, 'Command parameters do not match its typed scope.',
                'SCENE3D_COMMAND')
        result = function(**parameters)
        if isinstance(result, dict):
            require(result.get('applied') is not False, 'Prefab upgrade has unresolved conflicts: '+str(result.get('unresolved')),
                    'SCENE3D_PREFAB_CONFLICT')
            derived.update(result.get('derived_documents', {}))
        results.append(result)
    for path, payload in library.payloads().items():
        if path not in original_payloads:
            derived[path] = decode_json(payload)
    return dict(results=results, derived_documents=derived)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--scene', required=True, help='Exact project-relative scene manifest')
    parser.add_argument('--commands', type=Path, help='Bounded nonexecutable JSON batch')
    parser.add_argument('--destination', type=Path, help='New output snapshot; never an existing project')
    parser.add_argument('--scene-version')
    parser.add_argument('--project-version')
    parser.add_argument('--evaluate', action='store_true', help='Report base-pose matrices, not rendered animation')
    args = parser.parse_args(argv)
    bundle = load_project(args.project)
    graph = SceneGraph.from_bundle(bundle, args.scene)
    library = PrefabLibrary.from_files(args.project, bundle)
    if args.commands is None:
        require(args.evaluate and args.destination is None, 'Choose --evaluate or an edit batch plus new destination.')
        expansion = library.expand(graph.document())
        output = dict(base_pose_only=True, animation_evaluated=False,
                      nodes=SceneGraph(expansion['scene']).evaluate_base(), mappings=expansion['mappings'])
    else:
        require(args.destination is not None, 'An edit batch requires a new snapshot destination.')
        commands = decode_json(read_source_bytes(args.commands, 1024*1024))
        batch = apply_commands(graph, library, commands)
        output = snapshot_project(args.project, args.destination, graph, scene_path=args.scene,
            scene_version=args.scene_version, project_version=args.project_version,
            derived_documents=batch['derived_documents'])
        output['operations'] = len(batch['results'])
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RigError, OSError, UnicodeError) as exc:
        report = exc.report if isinstance(exc, RigError) else dict(code='SCENE3D_IO', stage='scene3d-edit', message=str(exc))
        print(json.dumps(dict(ok=False, error=report), ensure_ascii=True), file=sys.stderr)
        raise SystemExit(2)
