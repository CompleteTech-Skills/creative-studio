"""Create a small hash-bound, explicitly synthetic Scene3D contract fixture.

The OBJ contains genuine triangle geometry. This is a contract/loading example,
not an implemented scene builder, renderer or production-art approval.
"""
from copy import deepcopy
import argparse
import hashlib
from pathlib import Path

from scene3d_contracts import COORDINATES, canonical_bytes, load_project


SETTINGS = dict(backend='unavailable', width=128, height=128, samples=16,
                transparent=True, color_space='srgb', output='png', device='cpu')
TRANSFORM = dict(translation=[0, 0, 0], rotation=[0, 0, 0, 1], scale=[1, 1, 1])
PROVENANCE = dict(origin='procedural-fixture', fixture=True, tool='scene3d-contract-example-v1',
                  inputs=[], attribution='Hatch Pet engineering fixture', license='CC0-1.0')


def node(identifier, kind='group', parent=None):
    return dict(id=identifier, name=identifier, type=kind, parent=parent,
                transform=deepcopy(TRANSFORM), visible=True, collections=[], layers=[],
                material_overrides=[], animation_bindings=[], constraints=[], bounds=None,
                locked=False, selectable=True,
                render=dict(visible=True, cast_shadow=True, receive_shadow=True),
                source=None, metadata={})


def build_example(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    obj = b'# Explicit engineering fixture: one genuine triangle\nv 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 2 3\n'
    (destination / 'triangle.obj').write_bytes(obj)
    file = dict(path='triangle.obj', sha256=hashlib.sha256(obj).hexdigest())
    references = []
    def write(kind, identifier, **fields):
        value = dict(schema_version=1, kind='scene3d-' + kind, id=identifier, version='1.0.0', **fields)
        path = identifier + '.json'
        payload = canonical_bytes(value)
        (destination / path).write_bytes(payload)
        reference = dict(id=identifier, version='1.0.0', path=path,
                         sha256=hashlib.sha256(payload).hexdigest(), kind=value['kind'])
        references.append(reference)
        return reference
    asset = write('asset', 'triangle', name='Triangle fixture', asset_type='mesh',
        content=file, source=file, provenance=deepcopy(PROVENANCE),
        coordinate_system=COORDINATES, original_coordinate_system='right-handed Y-up meters',
        pivot=[0, 0, 0], meshes=[file], materials=[], bounds=dict(minimum=[0, 0, 0], maximum=[1, 0, 1]),
        collision=None, sockets=[], components={}, skeleton=None, animations=[], lods=[], dependencies=[],
        resources=dict(vertices=3, triangles=1, bytes=len(obj)), validation=[],
        review=dict(status='unreviewed', receipts=[]))
    mesh = node('mesh', 'mesh_instance', 'root')
    mesh['asset'] = asset
    mesh['transform']['translation'] = [2, 0, 0]
    camera = node('camera', 'camera', 'root')
    camera['transform']['translation'] = [0, 2, 4]
    camera['camera'] = dict(projection='perspective', near=0.1, far=100,
                            vertical_fov=0.8, orthographic_height=4)
    scene = write('scene', 'example-scene', name='Contract fixture', description='A real triangle and a camera.',
        revision=0, coordinate_system=COORDINATES, roots=['root'], nodes=[node('root'), mesh, camera],
        world=dict(coordinate_system=COORDINATES, background=[0.1, 0.1, 0.1], exposure=0, environment=None, metadata={}),
        collections=[], layers=[], cameras=['camera'], lights=[], recipes=[], dependencies=[asset],
        bounds=None, shots=['example-shot'], continuity=[], metadata={}, provenance=deepcopy(PROVENANCE))
    prefab_mesh = node('prefab-mesh', 'mesh_instance', 'prefab-root')
    prefab_mesh['asset'] = asset
    write('prefab', 'example-prefab', name='Triangle subtree', roots=['prefab-root'],
          nodes=[node('prefab-root'), prefab_mesh], parameters={}, material_slots=[], sockets=[],
          overrides=[], dependencies=[asset], provenance=deepcopy(PROVENANCE))
    write('animation', 'example-animation', duration_frames=24, fps=dict(numerator=24, denominator=1),
          tracks=[dict(target='mesh', property='translation', interpolation='linear',
                       keys=[dict(frame=0, value=[2, 0, 0]), dict(frame=24, value=[3, 0, 0])])],
          bindings=[], dependencies=[], provenance=deepcopy(PROVENANCE))
    shot = write('shot', 'example-shot', name='Static contract fixture shot', scene=scene,
        duration_frames=24, fps=dict(numerator=24, denominator=1), camera='camera', camera_overrides={},
        tracks=[], bindings=[], material_overrides=[], temporal_constraints=[],
        render=SETTINGS, continuity=[], dependencies=[asset], metadata={})
    write('render', 'example-render', shot=shot, start_frame=0, end_frame=24, settings=SETTINGS,
          output_directory='renders/example', dependencies=[], provenance=deepcopy(PROVENANCE))
    write('project', 'project', name='Scene3D engineering contract example', status='fixture',
        coordinate_system=COORDINATES, asset_catalog=None, assets=[asset], characters=[],
        scenes=[scene], shots=[shot],
        prefabs=[r for r in references if r['kind'] == 'scene3d-prefab'],
        animations=[r for r in references if r['kind'] == 'scene3d-animation'],
        render_defaults=SETTINGS,
        budgets=dict(max_nodes=100, max_files=64, max_bytes=1024*1024, max_frames=1000),
        created_at='2026-10-23T00:00:00Z', modified_at='2026-10-23T00:00:00Z', linked_2d_project=None,
        dependency_lock=deepcopy(references), validation=[], provenance=deepcopy(PROVENANCE), metadata={})
    return load_project(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='New destination directory')
    args = parser.parse_args()
    result = build_example(args.destination)
    print(result['status'], len(result['documents']), 'documents,', len(result['inventory']), 'files')
