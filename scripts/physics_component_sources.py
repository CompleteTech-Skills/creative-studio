"""Canonical, exact-byte component mesh admission shared by physics adapters.

The caller supplies the already verified Scene3D closure. Geometry decoding and
world transforms remain owned by rig_import and scene3d_math, respectively.
Category-specific topology or solver suitability is the adapter's responsibility.
"""
from pathlib import Path
import tempfile


def _admit_component_meshes(root, world, bundle):
    import numpy as np
    from physics_backends import BackendError, _Reader, digest, require, MAX_INPUT_BYTES
    from rig_contracts import DEFAULT_LIMITS
    from rig_import import load_character_model
    from rig_math import transform_points
    from scene3d_math import evaluate_transforms

    root = Path(root)
    scene = bundle['documents'].get(world['scene']['path'])
    require(scene is not None and scene['kind'] == 'scene3d-scene' and
            (scene['id'], scene['version']) == (world['scene']['id'], world['scene']['version']) and
            bundle['inventory'][world['scene']['path']]['sha256'] == world['scene']['sha256'],
            'PHYSICS_SOURCE_IDENTITY', 'Components require the exact admitted Scene3D scene.')
    nodes = {node['id']: node for node in scene['nodes']}
    transforms = evaluate_transforms(scene)
    assets = {item['id']: item for item in world['assets']}
    result = {}; vertex_count = triangle_count = 0
    with _Reader(root) as reader:
        for component in world['components']:
            if 'geometry' not in component:
                continue
            node = nodes.get(component['id'])
            require(node is not None and node['type'] in ('mesh_instance', 'static_prop') and node.get('asset'),
                    'PHYSICS_SOURCE_IDENTITY', 'Each mesh component ID must bind its native scene mesh node.')
            asset = bundle['documents'].get(node['asset']['path'])
            reference = assets.get(component['geometry'])
            require(asset is not None and asset['kind'] == 'scene3d-asset' and reference is not None and
                    reference['kind'] == 'geometry' and
                    (reference['id'], reference['version'], reference['path'], reference['sha256']) ==
                    (asset['id'], asset['version'], asset['content']['path'], asset['content']['sha256']),
                    'PHYSICS_SOURCE_IDENTITY', 'Component geometry differs from its exact native asset.')
            require(asset['coordinate_system'] == world['coordinate_system'] and asset['pivot'] == [0, 0, 0] and
                    not asset['skeleton'] and not asset['animations'] and not asset['materials'] and not asset['lods'],
                    'PHYSICS_CAPABILITY_UNAVAILABLE', 'Component geometry requires canonical unrigged, untextured meshes.')
            require(asset['provenance']['fixture'] == scene['provenance']['fixture'] == bundle['project']['provenance']['fixture'],
                    'PHYSICS_PROVENANCE', 'Component, scene and project fixture flags disagree.')
            name = reference['path']
            require(Path(name).suffix.lower() == '.obj', 'PHYSICS_FORMAT_UNSUPPORTED',
                    'Component admission currently supports actual OBJ geometry.')
            payload = reader.read(name, MAX_INPUT_BYTES)
            expected = bundle['inventory'].get(name)
            require(expected is not None and len(payload) == expected['bytes'] and digest(payload) == reference['sha256'] == expected['sha256'],
                    'PHYSICS_INPUT_CHANGED', 'Component source bytes changed after Scene3D admission.')
            require(not any(line.split(maxsplit=1)[0] in ('mtllib', 'usemtl') for line in payload.decode('utf-8').splitlines()
                            if line.strip() and not line.lstrip().startswith('#')),
                    'PHYSICS_FORMAT_UNSUPPORTED', 'External OBJ materials are not supported by component admission.')
            # Decode a detached exact-byte file, preventing an adjacent external
            # resource or a mutation of the source path from altering the import.
            with tempfile.TemporaryDirectory(prefix='physics-component-decode-') as directory:
                path = Path(directory) / 'source.obj'; path.write_bytes(payload)
                imported = load_character_model(path, limits={**DEFAULT_LIMITS,
                    'vertices': min(world['budgets']['vertices'], 100000),
                    'triangles': min(world['budgets']['triangles'], 200000), 'source_bytes': MAX_INPUT_BYTES})
            require(len(imported.source_files) == 1 and len(imported.geometry.meshes) == 1,
                    'PHYSICS_GEOMETRY_UNSUPPORTED', 'Each component requires one independently decoded mesh.')
            mesh = imported.geometry.meshes[0]
            local = np.asarray(mesh.positions, dtype=np.float64)
            faces = np.asarray(mesh.triangles)
            require(asset['resources']['vertices'] == len(local) and asset['resources']['triangles'] == len(faces),
                    'PHYSICS_GEOMETRY', 'Component vertex/triangle declarations differ from decoded geometry.')
            require('vertex_count' not in component or component['vertex_count'] == len(local),
                    'PHYSICS_GEOMETRY', 'Component vertex count differs from its real source.')
            bounds = [local.min(axis=0), local.max(axis=0)]
            require(asset['bounds'] is not None and np.allclose(bounds,
                    [asset['bounds']['minimum'], asset['bounds']['maximum']], rtol=1e-6, atol=1e-8),
                    'PHYSICS_GEOMETRY', 'Component source bounds differ from the native asset.')
            matrix = np.asarray(transforms[component['id']]['world_matrix'])
            points = transform_points(matrix, local)
            require(np.isfinite(points).all(), 'PHYSICS_GEOMETRY', 'Component world geometry is nonfinite.')
            result[component['id']] = dict(positions=points.tolist(), local_positions=local.tolist(),
                triangles=faces.tolist(), world_matrix=matrix.tolist(),
                source=dict(asset_id=asset['id'], asset_version=asset['version'], path=name,
                            sha256=reference['sha256'], bytes=len(payload)))
            vertex_count += len(local); triangle_count += len(faces)
            require(vertex_count <= world['budgets']['vertices'] and triangle_count <= world['budgets']['triangles'],
                    'PHYSICS_RESOURCE_LIMIT', 'Component meshes exceed aggregate world geometry budgets.')
    return dict(components=result, vertices=vertex_count, triangles=triangle_count,
                fixture=bool(scene['provenance']['fixture']))


def admit_component_meshes(root, world, bundle):
    from physics_backends import BackendError
    from rig_errors import RigError
    try:
        return _admit_component_meshes(root, world, bundle)
    except BackendError:
        raise
    except (RigError, KeyError, ValueError, TypeError, UnicodeError, OSError, RecursionError) as exc:
        raise BackendError('PHYSICS_SOURCE_INVALID', 'Component source admission failed: ' + str(exc),
                           category='invalid-input') from exc
