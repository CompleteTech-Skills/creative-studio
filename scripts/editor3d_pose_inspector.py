"""Immutable Pose authoring over exact captured native rigs.

This service authors Pose documents; it never modifies skeleton rest data or rig
configuration. Preview timing remains owned by the selected-shot scene evaluator.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json

from rig_errors import RigError,decode_json,finite_json,object_hash
from scene3d_contracts import validate_document


def _fail(message):
    raise RigError('EDITOR3D_POSE',message,stage='editor3d')


def _nodes(snapshot):
    scene=validate_document(snapshot['scene'],'scene')
    return {node['id']:node for node in scene['nodes']}


def _scope(nodes,target):
    selected={target};changed=True
    while changed:
        changed=False
        for node in nodes.values():
            if node['parent'] in selected and node['id'] not in selected:
                selected.add(node['id']);changed=True
    return selected


@contextmanager
def _authoring_context(repository,snapshot,identifier):
    from scene3d_import_capture import resolve_captured_import
    from rig_contracts import validate_contract
    from rig_skeleton import neutral_pose
    nodes=_nodes(snapshot)
    if not isinstance(identifier,str) or identifier not in nodes:_fail('Select an existing native character or import group.')
    owner=identifier;cursor=identifier;bindings=[]
    while cursor is not None:
        if 'pose_binding' in nodes[cursor]:bindings.append(cursor)
        cursor=nodes[cursor]['parent']
    if len(bindings)>1:_fail('Overlapping authored poses must be resolved before editing.')
    if bindings:owner=bindings[0]
    selected=_scope(nodes,owner)
    if any('pose_binding' in nodes[key] for key in selected if key!=owner):
        _fail('Select the existing pose owner instead of creating overlapping authored poses.')
    candidates={}
    for key in selected:
        reference=nodes[key].get('asset')
        if not reference:continue
        asset=snapshot['documents'].get(reference['path'])
        if not asset or not asset.get('skeleton'):continue
        pin=asset['skeleton'];identity=tuple(pin[name] for name in ('id','version','path','sha256'))
        candidates.setdefault(identity,(reference,pin))
    if len(candidates)!=1:_fail('Select a subtree containing exactly one native rig identity.')
    reference,rig_reference=next(iter(candidates.values()))
    binding=nodes[owner].get('pose_binding')
    if binding is not None and binding['rig']!=rig_reference:
        _fail('The authored pose targets another exact native rig.')
    source=repository.native_snapshot(snapshot['revision'],snapshot['sha256'])
    bundle={key:snapshot[key] for key in ('project','documents','inventory')}
    with resolve_captured_import(bundle,reference,source['root']) as captured:
        rig=captured['rig']
        if rig.skeleton is None:_fail('This asset has no native skeleton.')
        if rig.geometry.vertex_count>100000:_fail('Interactive native posing supports at most 100,000 rig vertices.')
        if binding is None:
            pose=neutral_pose(rig.skeleton,'editor-neutral')
        else:
            from scene3d_contracts import portable_path
            file=binding['pose']['file'];payload=(captured['root']/portable_path(file['path'])).read_bytes()
            if hashlib.sha256(payload).hexdigest()!=file['sha256']:_fail('Pose bytes differ from their exact saved pin.')
            pose=validate_contract(decode_json(payload),'Pose')
            if pose['id']!=binding['pose']['id'] or pose['rig_version']!=binding['pose']['rig_version']:
                _fail('Saved pose identity differs from its binding.')
        if pose['rig_version']!=rig.skeleton['version']:_fail('Pose targets another native skeleton version.')
        from scene3d_graph import SceneGraph
        graph=SceneGraph.from_bundle(captured['bundle'],snapshot['scene_path'],source_root=captured['root'])
        editable=not graph.evaluate_base(owner)['locked']
        yield dict(owner=nodes[owner],rig=rig,rig_reference=deepcopy(rig_reference),pose=deepcopy(pose),
                   editable=editable)


def describe_pose(repository,snapshot,node_id):
    """Copy-only native authoring state, with no absolute filesystem locations."""
    with _authoring_context(repository,snapshot,node_id) as context:
        rig=context['rig']
        return dict(owner_id=context['owner']['id'],editable=context['editable'],read_only_reason=None if context['editable'] else 'Node or inherited collection/layer membership is locked.',
            rig_reference=context['rig_reference'],skeleton=deepcopy(rig.skeleton),
            authored_pose=context['pose'],
            morph_targets={mesh.id:sorted(mesh.morphs) for mesh in rig.geometry.meshes if mesh.morphs},
            scope='Selected pose owner and its matching native rig descendants.',
            preview_requirement='Select a saved shot and frame for canonical pose preview.',
            unavailable=['rest-skeleton-editing','constraint-configuration','pose-clip-blending'])


def apply_pose_edit(repository,request):
    """Publish a schema-valid immutable authored Pose in one editor transaction."""
    from rig_contracts import validate_contract
    from rig_math import quaternion
    from rig_operations import pose_operation
    from rig_skeleton import mirror_pose,neutral_pose
    from rig_skinning import evaluate_deformation
    finite_json(request)
    request=deepcopy(request)
    base={'node_id','operation','expected_revision','expected_sha256','transaction_id','timeline'}
    if not isinstance(request,dict) or not base<=set(request):_fail('Malformed native pose transaction.')
    operation=request['operation']
    if not isinstance(operation,str):_fail('Choose a named native pose operation.')
    allowed=base|({'bones','morphs'} if operation=='set' else {'symmetry_approved'} if operation=='mirror' else set())
    if operation not in {'set','reset','mirror'} or set(request)-allowed:
        _fail('Choose a supported native pose operation.')
    def builder(source_root,destination,scene_path):
        snapshot=repository.snapshot()
        timeline=request['timeline']
        if not isinstance(timeline,dict) or set(timeline)!={'shot_path','frame'} or not isinstance(timeline['shot_path'],str):
            _fail('Select an exact saved shot and frame for native pose authoring.')
        shot=snapshot['documents'].get(timeline['shot_path'])
        if not shot or shot.get('kind')!='scene3d-shot' or shot['scene']['path']!=snapshot['scene_path']:
            _fail('Selected shot must belong to the active saved scene.')
        from editor3d_timeline import evaluate_timeline
        previous_evaluation=evaluate_timeline(repository,dict(revision=snapshot['revision'],sha256=snapshot['sha256'],
            shot=timeline['shot_path'],frame=timeline['frame']))
        with _authoring_context(repository,snapshot,request['node_id']) as context:
            if not context['editable']:_fail('Unlock the pose owner or its inherited collection/layer membership before editing it.')
            rig=context['rig'];pose=deepcopy(context['pose'])
            if operation=='reset':pose=neutral_pose(rig.skeleton,pose['id'])
            elif operation=='mirror':
                if request.get('symmetry_approved') is not True:
                    _fail('Approve this rig symmetry before mirroring a pose.')
                pose=mirror_pose(rig.skeleton,pose,approved=True)
            else:
                if not request.get('bones') and not request.get('morphs'):_fail('Choose an explicit bone or morph edit.')
                for field in ('bones','morphs'):
                    values=request.get(field,{})
                    if not isinstance(values,dict):_fail('Bone and morph edits must be keyed maps.')
                    for key,change in values.items():
                        if not isinstance(change,dict):_fail('Each native bone or mesh edit must be a map.')
                        if field=='bones' and 'rotation' in change:quaternion(change['rotation'],strict=True)
                        pose[field].setdefault(key,{}).update(deepcopy(change))
            pose['id']='editor-pose-'+object_hash(request)[:32]
            validate_contract(pose,'Pose')
            # Validation evaluates constraints and real morph/bone deformation once;
            # the unsolved authored values are saved so canonical playback applies
            # native constraints exactly once when the frame is evaluated.
            evaluate_deformation(rig,pose_operation(rig,{'pose':pose}))
            raw=(json.dumps(pose,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
            digest=hashlib.sha256(raw).hexdigest()
            path='poses/editor/'+pose['id']+'-'+digest+'.json'
            binding=dict(schema_version=1,rig=context['rig_reference'],pose=dict(id=pose['id'],
                rig_version=pose['rig_version'],file=dict(path=path,sha256=digest)))
            owner_id=context['owner']['id']
        from scene3d_contracts import load_project
        from scene3d_graph import SceneGraph
        from scene3d_snapshot import snapshot_project
        graph=SceneGraph.from_bundle(load_project(source_root),scene_path,source_root=source_root)
        graph.configure_node(owner_id,{'pose_binding':binding})
        result=snapshot_project(source_root,destination,graph,scene_path=scene_path,extra_payloads={path:raw})
        staged=load_project(destination)
        selected=[ref['path'] for ref in staged['project']['shots'] if ref['id']==shot['id'] and
            staged['documents'][ref['path']]['scene']['path']==result['active_scene']['path']]
        if len(selected)!=1:_fail('Selected shot could not be rebound to the new saved pose scene.')
        from fractions import Fraction
        from scene3d_evaluate import evaluate_scene
        evaluate_scene(staged,result['active_scene']['path'],selected[0],Fraction(**previous_evaluation['frame']),
            source_root=destination,sample_geometry=True,max_geometry_vertices=100000)
        return result['active_scene']['path']
    return repository.transact_native(request['expected_revision'],request['transaction_id'],request,builder,
        expected_sha256=request['expected_sha256'],label='Edit authored native pose')


def pose_routes():
    def describe(server,body):
        if not isinstance(body,dict) or set(body)!={'node_id'}:_fail('Select one pose owner.')
        return describe_pose(server.repository,server.repository.snapshot(),body['node_id'])
    def edit(server,body):
        apply_pose_edit(server.repository,body)
        return server.snapshot()
    return {'inspectors/pose/describe':describe,'inspectors/pose/edit':edit}
