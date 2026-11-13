"""Exact immutable Scene3D pose admission over canonical rig operations."""
from copy import deepcopy
import hashlib
from pathlib import Path

from asset_library import read_source_bytes
from rig_errors import decode_json
from scene3d_contracts import portable_path, require, validate_document


def evaluate_poses(graph, evaluated, documents, native_clips, root, *, sample_geometry, max_vertices):
    """Resolve explicit authored poses without mutating rig rest state.

    Native state exposes canonical pose, bone hierarchy and mesh-qualified morphs.
    Bone matrices are in rig bind space; instance/component world mapping remains
    explicit in ``world_from_rig``. Geometry is emitted only when requested.
    """
    import numpy as np
    from rig_models import load_rig
    from rig_contracts import validate_contract
    from rig_operations import pose_operation
    from rig_skeleton import evaluate_fk
    from rig_skinning import evaluate_deformation
    from rig_math import transform_points
    nodes = {node['id']:node for node in graph.document()['nodes']}
    authored = [(node['id'],node['pose_binding']) for node in nodes.values() if 'pose_binding' in node]
    if not authored:
        return {}
    require(root is not None, 'Pose evaluation requires the exact source root.')
    assignments = {}
    for target, binding in authored:
        matches = []
        for identifier, node in nodes.items():
            cursor = identifier
            while cursor is not None and cursor != target:
                cursor = nodes[cursor]['parent']
            if cursor != target or 'asset' not in node:
                continue
            asset = documents.get(node['asset']['path'])
            if asset is not None and asset.get('skeleton') == binding['rig']:
                matches.append(identifier)
        require(matches, 'Pose binding has no matching exact rig asset in its subtree.')
        for identifier in matches:
            require(identifier not in assignments and identifier not in native_clips,
                    'Overlapping native poses or clips have no declared blend policy.')
            assignments[identifier] = binding
        evaluated[target]['native_pose'] = deepcopy(binding)
        evaluated[target]['native_pose_components'] = matches
    geometry, cache, count, state_entries = {}, {}, 0, 0
    for identifier,binding in assignments.items():
        node = nodes[identifier]
        asset = validate_document(documents[node['asset']['path']], 'asset')
        require(asset['skeleton'] == binding['rig'], 'Pose rig differs from its asset skeleton pin.')
        rig_ref, pose_ref = binding['rig'], binding['pose']
        def read(reference):
            path = Path(root)/portable_path(reference['path'])
            payload = read_source_bytes(path,16*1024*1024)
            require(hashlib.sha256(payload).hexdigest() == reference['sha256'], 'Pose or rig source changed.')
            return path,payload
        rig_path,_ = read(rig_ref)
        require(rig_path.name == 'rig.json', 'Pose rig pin must name the canonical rig manifest.')
        _,payload = read(pose_ref['file'])
        key = (tuple(rig_ref[k] for k in ('kind','id','version','path','sha256')),
               pose_ref['id'],pose_ref['rig_version'],pose_ref['file']['path'],pose_ref['file']['sha256'])
        if key not in cache:
            rig = load_rig(rig_path.parent)
            require(rig.manifest['id'] == rig_ref['id'] and rig.manifest['version'] == rig_ref['version'],
                    'Pose rig identity/version differs from actual source.')
            require(rig.manifest['skeleton'] is not None, 'Pose requires an actual native skeleton.')
            source_pose = validate_contract(decode_json(payload),'Pose')
            require(source_pose.get('id') == pose_ref['id'] and
                    source_pose.get('rig_version') == pose_ref['rig_version'] == rig.skeleton['version'],
                    'Pose identity or skeleton version differs from its pin.')
            pose = pose_operation(rig, {'pose':source_pose})
            # Always validate mesh-qualified morph targets, even when geometry is
            # not requested by an inspector or browser state consumer.
            meshes = rig.geometry.by_id()
            require(set(pose['morphs']) <= set(meshes), 'Pose targets an unknown mesh.')
            for mesh,weights in pose['morphs'].items():
                require(set(weights) <= set(meshes[mesh].morphs), 'Pose targets an unknown morph.')
            bones = {bone:matrix.tolist() for bone,matrix in evaluate_fk(rig.skeleton,pose).items()}
            deformed = evaluate_deformation(rig,pose) if sample_geometry else None
            cache[key] = rig,pose,bones,deformed
        rig,pose,bones,deformed = cache[key]
        state_entries += len(bones) + sum(len(mesh.morphs) for mesh in rig.geometry.meshes)
        require(state_entries <= 250000, 'Native pose state budget exceeded.','SCENE3D_EVALUATION_LIMIT')
        world_from_rig = np.asarray(evaluated[identifier]['world_matrix'])
        imported = None
        if 'source_component' in node.get('metadata',{}):
            from scene3d_import import ImportedScene
            def original(suffix):
                entries = [entry for entry in asset['dependencies'] if entry['path'].endswith(suffix)]
                require(len(entries) == 1, 'Imported pose source record is missing.')
                return decode_json(read(entries[0])[1])
            report,hierarchy = original('/import-report.json'),original('/hierarchy.json')
            imported = ImportedScene({},node['asset'],asset,[],hierarchy['nodes'],report)
            source_node = node['metadata']['source_node']
            original_world = next((h['world_matrix'] for h in hierarchy['nodes'] if h['source_node']==source_node),None)
            require(original_world is not None, 'Imported pose component has no source transform.')
            world_from_rig = world_from_rig @ np.linalg.inv(original_world)
        state = evaluated[identifier]
        state['native_pose'] = deepcopy(binding)
        state['native_pose_evaluated'] = True
        state['native_state'] = dict(pose=deepcopy(pose),skeleton=deepcopy(rig.skeleton),
            bone_matrices=deepcopy(bones),bone_matrix_space='rig-bind',world_from_rig=world_from_rig.tolist(),
            morph_targets={mesh.id:sorted(mesh.morphs) for mesh in rig.geometry.meshes})
        if sample_geometry:
            if imported is not None:
                from scene3d_import import evaluate_imported_vertices
                class ComponentView:
                    def document(self):return {'nodes':[node]}
                    def evaluate_base(self, selected):return graph.evaluate_base(selected)
                positions = evaluate_imported_vertices(ComponentView(),imported,rig_path.parent,
                    pose=decode_json(payload))['nodes'][identifier]
            else:
                positions = {mesh:transform_points(world_from_rig,value['positions']).tolist()
                             for mesh,value in deformed.items()}
            count += sum(len(points) for points in positions.values())
            require(count <= max_vertices, 'Native output vertex budget exceeded.','SCENE3D_EVALUATION_LIMIT')
            geometry[identifier] = positions
    return geometry
