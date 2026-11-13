"""Execute #60 with genuine procedural geometry and no production approvals."""
from __future__ import annotations

from dataclasses import asdict
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np

from asset_library import AssetLibrary
from rig_demo import create_demo
from rig_errors import decode_json
from scene3d_contracts import canonical_bytes, load_project
from scene3d_example import build_example, TRANSFORM
from scene3d_graph import SceneGraph
from scene3d_import import add_import, evaluate_imported_vertices, import_gltf, materialize
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project


def demonstrate(destination):
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    source_rig, numeric = create_demo(destination/'source-rig', 'robotic', identifier='scene60-robot')
    source = source_rig.root/'exports/character.glb'
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    license_file = destination/'source-code-license.txt'
    shutil.copyfile(Path(__file__).resolve().parents[1]/'LICENSE.txt', license_file)
    library = AssetLibrary(destination/'library', create=True)
    imported = import_gltf(source, library, asset_id='robot', attribution='Hatch Pet procedural engineering fixture',
                           license_file=license_file, fixture=True)
    materialize(library, imported.library_reference, destination/'imported')
    native = destination/'imported/imports/robot/1.0.0/native'
    source_project = destination/'project'
    build_example(source_project)
    project = decode_json((source_project/'project.json').read_bytes())
    project['budgets'].update(max_nodes=1024, max_files=512, max_bytes=128*1024*1024)
    (source_project/'project.json').write_bytes(canonical_bytes(project))
    bundle = load_project(source_project)
    graph = SceneGraph.from_bundle(bundle, 'example-scene.json')
    add_import(graph, imported, 'robot-source')
    prefabs = PrefabLibrary.from_files(source_project, bundle)
    created = prefabs.create(graph, 'robot-source', 'robot-assembly', '1.0.0')
    transform = deepcopy(TRANSFORM)
    transform['translation'] = [3, 0, 0]
    prefabs.instantiate(graph, created['reference'], 'robot-copy', transform=transform)
    expanded = prefabs.expand(graph.document())
    expanded_graph = SceneGraph(expanded['scene'], external_references={})
    first_clip = next(iter(imported.report['native_clip_map']))
    evaluated = evaluate_imported_vertices(expanded_graph, imported, native, clip=first_clip, time=.37)
    # Both copies share actual native geometry/skin/clip bytes. Their component
    # edit delta is exactly a three-meter X translation.
    direct = {n['metadata']['source_component']: n['id'] for n in graph.document()['nodes'] if 'asset' in n
              and n['asset'] == imported.asset_reference}
    mapping = expanded['mappings']['robot-copy']
    differences = []
    for component, node_id in direct.items():
        copied = mapping[node_id]
        for mesh, points in evaluated['nodes'][node_id].items():
            delta = np.asarray(evaluated['nodes'][copied][mesh])-np.asarray(points)
            differences.append(float(np.max(np.abs(delta-[3, 0, 0]))))
    manifest = library.resolve(imported.library_reference)
    payloads = {r['path']: library.read_file(imported.library_reference, r['path']) for r in manifest['files']
                if r['path'] != imported.asset_reference['path']}
    derived = {path: decode_json(payload) for path, payload in prefabs.payloads().items() if path not in bundle['documents']}
    derived.update(expanded['derived_documents'])
    derived[imported.asset_reference['path']] = imported.asset
    portable = snapshot_project(source_project, destination/'portable', expanded_graph,
        scene_path='example-scene.json', derived_documents=derived, extra_payloads=payloads)
    reopened = load_project(destination/'portable')
    receipt = dict(schema_version=1, fixture=True, source='procedural-engineering-geometry',
        original_source_sha256=original_hash, source_unchanged=hashlib.sha256(source.read_bytes()).hexdigest() == original_hash,
        native_numeric_validation=numeric['ok'], imported=asdict(imported),
        actual_native_clip=first_clip, sample_time_seconds=.37,
        shared_instance_max_translation_error_m=max(differences),
        scene_nodes=len(expanded_graph.document()['nodes']),
        actual_geometry_vertices=imported.report['geometry']['vertices'],
        actual_geometry_triangles=imported.report['geometry']['triangles'],
        portable=portable, reopen_status=reopened['status'],
        production_approved=False, visual_approved=False,
        limitations=['Native CPU preview is base-color-only.', 'Source fixtures are not production artwork.',
                     'This proves graph/import/prefab/snapshot integration, not a scene renderer or timeline UI.'])
    (destination/'acceptance.json').write_text(json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False)+'\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True, help='New local demonstration directory')
    args = parser.parse_args()
    report = demonstrate(args.destination)
    print(json.dumps(dict(status=report['reopen_status'], vertices=report['actual_geometry_vertices'],
                         triangles=report['actual_geometry_triangles'], nodes=report['scene_nodes'],
                         instance_error_m=report['shared_instance_max_translation_error_m'], fixture=True)))
