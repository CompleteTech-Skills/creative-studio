"""Canonical imported-component geometry projection for the saved editor scene.

The native importer owns component membership, original-world transforms and
bind deformation. This adapter only expresses its verified world vertices in the
matching browser node's local coordinates; it never selects meshes by name.
"""
from __future__ import annotations
import base64
import hashlib
import json
import numpy as np
from pathlib import Path

from rig_errors import decode_json, require, safe_path, RigError


def project_components(root, snapshot, *, max_vertices, max_resource_bytes):
    from scene3d_graph import SceneGraph
    from scene3d_import import ImportedScene, evaluate_imported_vertices
    from rig_models import load_rig
    from rig_math import transform_points
    nodes = [node for node in snapshot['scene']['nodes'] if 'source_component' in node.get('metadata', {})]
    projections, cache, used, resource_bytes = {}, {}, 0, 0
    if not nodes:
        return projections, used
    graph = SceneGraph.from_bundle(snapshot, snapshot['scene_path'])
    for node in nodes:
        key = None
        try:
            reference = node['asset']
            key = (reference['path'], reference['sha256'])
            asset = snapshot['documents'][reference['path']]
            require(hashlib.sha256(safe_path(root, reference['path'], exists=True).read_bytes()).hexdigest() == reference['sha256'],
                    'EDITOR3D_VIEW_COMPONENT', 'Imported asset manifest identity differs from the node binding.')
            component = node['metadata']['source_component']
            require(component in asset['components'] and asset['components'][component],
                    'EDITOR3D_VIEW_COMPONENT', 'Imported node has no exact native component mapping.')
            require(not node.get('pose_bindings') and not node.get('pose_binding'),
                    'EDITOR3D_VIEW_COMPONENT', 'Pose-bound components require the canonical timeline geometry adapter.')
            if key not in cache:
                def evidence(suffix):
                    matches = [entry for entry in asset['dependencies'] if entry['path'].endswith(suffix)]
                    require(len(matches) == 1, 'EDITOR3D_VIEW_COMPONENT', 'Missing unique native '+suffix+' evidence.')
                    entry = matches[0]
                    path = safe_path(root, entry['path'], exists=True)
                    payload = path.read_bytes()
                    require(hashlib.sha256(payload).hexdigest() == entry['sha256'],
                            'EDITOR3D_VIEW_COMPONENT', 'Imported native evidence changed.')
                    return path, decode_json(payload.decode('utf-8', errors='strict'))
                manifest_path, manifest = evidence('/native/rig.json')
                _, hierarchy = evidence('/hierarchy.json')
                _, report = evidence('/import-report.json')
                rig = load_rig(manifest_path.parent)
                require(rig.manifest == manifest, 'EDITOR3D_VIEW_COMPONENT', 'Native manifest interpretation differs.')
                require(rig.geometry.vertex_count <= max_vertices, 'EDITOR3D_VIEW_LIMIT', 'Imported geometry exceeds the viewport budget.')
                imported = ImportedScene({}, reference, asset, [], hierarchy['nodes'], report)
                sampled = evaluate_imported_vertices(graph, imported, manifest_path.parent)['nodes']
                images = []
                for texture in rig.geometry.textures:
                    image = rig.geometry.images[texture['source']]
                    images.append('data:'+image.mime_type+';base64,'+base64.b64encode(image.data).decode('ascii'))
                encoded_resources = len(json.dumps(dict(materials=rig.geometry.materials, images=images),
                    separators=(',', ':'), allow_nan=False).encode('utf-8'))
                cache[key] = (rig.geometry, sampled, images, encoded_resources)
            if isinstance(cache[key], Exception):
                raise cache[key]
            geometry, sampled, images, encoded_resources = cache[key]
            require(node['id'] in sampled and set(sampled[node['id']]) == set(asset['components'][component]),
                    'EDITOR3D_VIEW_COMPONENT', 'Native evaluated component differs from its declared mesh membership.')
            require(resource_bytes + encoded_resources <= max_resource_bytes, 'EDITOR3D_VIEW_LIMIT',
                    'Imported component resources exceed the projected payload budget.')
            count = sum(len(points) for points in sampled[node['id']].values())
            require(used + count <= max_vertices, 'EDITOR3D_VIEW_LIMIT', 'Imported instances exceed the viewport budget.')
            matrix = np.asarray(graph.evaluate_base(node['id'])['world_matrix'], dtype=float)
            inverse = np.linalg.inv(matrix)
            require(np.isfinite(inverse).all(), 'EDITOR3D_VIEW_COMPONENT', 'Imported node transform cannot be inverted.')
            meshes = []
            source_meshes = geometry.by_id()
            for identifier, points in sampled[node['id']].items():
                mesh = source_meshes[identifier]
                positions = transform_points(inverse, points)
                require(len(positions) == len(mesh.positions), 'EDITOR3D_VIEW_COMPONENT', 'Native component topology differs.')
                meshes.append(dict(id=identifier, name=mesh.name, positions=positions.tolist(),
                    triangles=mesh.triangles.tolist(), material=mesh.material, normals=None,
                    uv=mesh.attributes['TEXCOORD_0'].tolist() if 'TEXCOORD_0' in mesh.attributes else None))
            projections[node['id']] = dict(format='geometry', meshes=meshes, materials=geometry.materials,
                images=images, asset_reference=dict(reference), source_component=component, mode='authored-bind-pose')
            used += count
            resource_bytes += encoded_resources
        except (RigError, ValueError, OSError, KeyError, TypeError, UnicodeError) as error:
            if key is not None and key not in cache:
                cache[key] = error
            projections[node['id']] = dict(format='unavailable', message=str(error))
    return projections, used
