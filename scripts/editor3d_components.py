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


def project_components(root, snapshot, *, max_vertices, max_resource_bytes, max_decoded_vertices=2000000, defer_poses=False):
    from scene3d_graph import SceneGraph
    from scene3d_import import project_imported_vertices
    from scene3d_import_capture import resolve_captured_import
    from rig_skinning import evaluate_deformation
    from rig_math import transform_points
    nodes = [node for node in snapshot['scene']['nodes'] if 'source_component' in node.get('metadata', {})]
    projections, cache, used, resource_bytes = {}, {}, 0, 0
    decoded_vertices, sampled_vertices = 0, 0
    if not nodes:
        return projections, used
    graph = SceneGraph.from_bundle(snapshot, snapshot['scene_path'],source_root=root)
    all_nodes={node['id']:node for node in snapshot['scene']['nodes']}
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
            cursor=node['id'];requires_timeline=False
            while cursor is not None:
                binding=all_nodes[cursor].get('pose_binding')
                requires_timeline |= bool(binding and binding['rig']==asset['skeleton'])
                cursor=all_nodes[cursor]['parent']
            require(defer_poses or not requires_timeline,
                    'EDITOR3D_VIEW_COMPONENT', 'Pose-bound components require the canonical timeline geometry adapter.')
            if key not in cache:
                require(decoded_vertices < max_decoded_vertices, 'EDITOR3D_VIEW_LIMIT', 'Imported decoded-vertex budget exceeded.')
                with resolve_captured_import(snapshot,reference,root) as admitted:
                    rig=admitted['rig']
                    require(decoded_vertices + rig.geometry.vertex_count <= max_decoded_vertices,
                            'EDITOR3D_VIEW_LIMIT', 'Imported decoded-vertex budget exceeded.')
                    decoded_vertices += rig.geometry.vertex_count
                    by_id=rig.geometry.by_id()
                    members=[member for member in nodes if member.get('asset')==reference]
                    planned=0
                    for member in members:
                        binding=member['metadata']['source_component']
                        require(binding in asset['components'], 'EDITOR3D_VIEW_COMPONENT', 'Imported node has no exact native component mapping.')
                        planned += sum(len(by_id[mesh].positions) for mesh in asset['components'][binding])
                    require(sampled_vertices + planned <= max_vertices,
                            'EDITOR3D_VIEW_LIMIT', 'Imported instances exceed the sampled vertex budget.')
                    deformed=(evaluate_deformation(rig) if rig.manifest['skeleton'] and rig.weights else
                              {mesh.id:dict(positions=mesh.positions) for mesh in rig.geometry.meshes})
                    class ComponentView:
                        def document(self):return {'nodes':members}
                        def evaluate_base(self,identifier=None):return graph.evaluate_base(identifier)
                    sampled=project_imported_vertices(ComponentView(),admitted['imported'],rig,deformed)['nodes']
                    sampled_vertices += planned
                images = []
                for texture in rig.geometry.textures:
                    image = rig.geometry.images[texture['source']]
                    images.append('data:'+image.mime_type+';base64,'+base64.b64encode(image.data).decode('ascii'))
                encoded_resources = len(json.dumps(dict(materials=rig.geometry.materials, images=images),
                    separators=(',', ':'), allow_nan=False).encode('utf-8'))
                cache[key] = (rig.geometry, sampled, images, encoded_resources)
            if isinstance(cache[key], dict):
                raise RigError('EDITOR3D_VIEW_COMPONENT', cache[key]['error'])
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
                images=images, asset_reference=dict(reference), source_component=component, mode='requires-timeline-preview' if requires_timeline else 'authored-bind-pose',
                requires_timeline_preview=requires_timeline)
            used += count
            resource_bytes += encoded_resources
        except (RigError, ValueError, OSError, KeyError, TypeError, UnicodeError) as error:
            if key is not None and key not in cache:
                cache[key] = {'error': str(error)}
            projections[node['id']] = dict(format='unavailable', message=str(error))
    return projections, used
