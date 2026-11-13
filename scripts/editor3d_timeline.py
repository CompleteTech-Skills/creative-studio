"""Editor timeline commands over canonical Scene3D documents and evaluators.

This adapter has no persistence engine, playback clock or animation evaluator.
The editor repository owns transactions and the native Scene3D owner publishes
new immutable document versions. Curves are exactly those supported by that owner.
"""
from __future__ import annotations
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from rig_errors import RigError, finite_json
from scene3d_contracts import validate_document, load_project

MAX_COMMANDS = 256
MAX_COMMAND_BYTES = 1024 * 1024
PROPERTIES = ('translation', 'rotation', 'scale', 'visible', 'material', 'light_intensity', 'world_exposure')


def _require(ok, message):
    if not ok:
        raise RigError('EDITOR3D_TIMELINE', message, stage='editor3d-timeline')


def _integer(value, maximum=1_000_000):
    _require(type(value) is int and 0 <= value <= maximum, 'Frame/count must be an exact bounded integer.')
    return value


def _keys(command, required, optional=()):
    _require(isinstance(command, dict) and set(required) <= set(command) <= set(required) | set(optional),
             'Timeline command has unknown or missing fields.')


def _reference(path, document):
    from scene3d_contracts import canonical_bytes
    return dict(kind=document['kind'], id=document['id'], version=document['version'], path=path,
                sha256=hashlib.sha256(canonical_bytes(document)).hexdigest())


def _created_path(identifier):
    _require(isinstance(identifier,str) and 0 < len(identifier) <= 128, 'Use a bounded resource ID.')
    return 'editor-created/' + hashlib.sha256(identifier.encode()).hexdigest()[:24] + '/1.0.0.json'


def prepare_timeline_commands(bundle, commands):
    """Prepare bounded native document edits, leaving the supplied bundle intact.

    Returned options are inputs to scene3d_snapshot.snapshot_project. They are
    not a successful save receipt; the native publisher must validate full closure.
    """
    finite_json(commands)
    _require(isinstance(commands,list) and 0 < len(commands) <= MAX_COMMANDS, 'Use 1 to 256 timeline commands.')
    _require(len(json.dumps(commands,separators=(',',':')).encode()) <= MAX_COMMAND_BYTES, 'Timeline request exceeds 1 MiB.')
    project=deepcopy(bundle['project']); documents=deepcopy(bundle['documents'])
    original=set(documents); edited=set(); additions={}
    selected={field:[ref['path'] for ref in project[field]] for field in ('scenes','shots','animations')}

    def resource(path, kinds):
        _require(isinstance(path,str) and path in documents and documents[path]['kind'] in kinds,
                 'Select an exact native document path of the required kind.')
        return documents[path]

    def edit(path, kinds):
        value=resource(path,kinds); edited.add(path); return value

    def create(value, field):
        _require(not any(doc['kind']==value['kind'] and doc['id']==value['id'] for doc in documents.values()),
                 'Resource ID already exists; choose a new ID.')
        value['version']='1.0.0'; path=_created_path(value['id'])
        _require(path not in documents, 'New resource path collides with an existing document.')
        validate_document(value);documents[path]=value;additions[path]=value;selected[field].append(path)
        return path

    for command in commands:
        _require(isinstance(command,dict),'Timeline command must be an object.')
        op=command.get('operation')
        if op in ('key-upsert','key-delete','key-move','track-interpolation'):
            required={'operation','document','target','property'}
            if op=='key-upsert':required|={'frame','value'}
            elif op=='key-delete':required|={'frame'}
            elif op=='key-move':required|={'frame','to_frame'}
            else:required|={'interpolation'}
            _keys(command,required,{'interpolation'} if op=='key-upsert' else ())
            value=edit(command['document'],{'scene3d-shot','scene3d-animation'})
            prop=command['property'];target=command['target']
            _require(prop in PROPERTIES and isinstance(target,str), 'Unsupported timeline channel.')
            tracks=value['tracks']; track=next((t for t in tracks if t['target']==target and t['property']==prop),None)
            if op=='key-upsert':
                frame=_integer(command['frame']);_require(frame<=value['duration_frames'],'Key exceeds the clip terminal boundary.')
                if track is None:
                    interpolation=command.get('interpolation', 'slerp' if prop=='rotation' else 'step' if prop in ('visible','material') else 'linear')
                    track=dict(target=target,property=prop,interpolation=interpolation,keys=[]);tracks.append(track)
                elif 'interpolation' in command:
                    track['interpolation']=command['interpolation']
                track['keys']=[key for key in track['keys'] if key['frame']!=frame]
                track['keys'].append(dict(frame=frame,value=deepcopy(command['value'])))
                track['keys'].sort(key=lambda key:key['frame'])
            else:
                _require(track is not None,'The selected track does not exist.')
                if op=='track-interpolation':track['interpolation']=command['interpolation']
                else:
                    frame=_integer(command['frame']);key=next((k for k in track['keys'] if k['frame']==frame),None)
                    _require(key is not None,'The selected key does not exist.')
                    if op=='key-delete':
                        track['keys'].remove(key)
                        if not track['keys']:tracks.remove(track)
                    else:
                        to_frame=_integer(command['to_frame'])
                        _require(to_frame<=value['duration_frames'] and not any(k['frame']==to_frame for k in track['keys'] if k is not key),
                                 'Moved key exceeds duration or collides with an existing key.')
                        key['frame']=to_frame;track['keys'].sort(key=lambda k:k['frame'])
            validate_document(value)
        elif op in ('shot-settings','clip-settings'):
            _keys(command,{'operation','document'},{'duration_frames','fps','camera','name'})
            kind='scene3d-shot' if op=='shot-settings' else 'scene3d-animation'
            value=edit(command['document'],{kind})
            _require(set(command)-{'operation','document'},'Provide at least one setting.')
            _require(kind=='scene3d-shot' or not {'camera','name'}&set(command),'Clip does not carry camera/name fields.')
            for field in ('duration_frames','fps','camera','name'):
                if field in command:value[field]=deepcopy(command[field])
            validate_document(value)
        elif op=='clip-bind':
            _keys(command,{'operation','document','clip','target','start_frame','end_frame','speed','loop'})
            value=edit(command['document'],{'scene3d-shot','scene3d-animation'})
            clip=resource(command['clip'],{'scene3d-animation'})
            binding=dict(target=command['target'],clip=_reference(command['clip'],clip),
                start_frame=command['start_frame'],end_frame=command['end_frame'],speed=command['speed'],loop=command['loop'])
            _require(binding not in value['bindings'],'Clip binding already exists.')
            value['bindings'].append(binding);validate_document(value)
        elif op=='clip-unbind':
            _keys(command,{'operation','document','index'})
            value=edit(command['document'],{'scene3d-shot','scene3d-animation'});index=_integer(command['index'],255)
            _require(index<len(value['bindings']),'Binding index is absent.');value['bindings'].pop(index)
        elif op=='clip-create':
            _keys(command,{'operation','source','id'})
            source=resource(command['source'],{'scene3d-shot','scene3d-animation'})
            clip=dict(schema_version=1,kind='scene3d-animation',id=command['id'],version='1.0.0',
                duration_frames=source['duration_frames'],fps=deepcopy(source['fps']),tracks=deepcopy(source['tracks']),
                bindings=deepcopy(source['bindings']),dependencies=deepcopy(source['dependencies']),
                provenance=deepcopy(bundle['project']['provenance']))
            create(clip,'animations')
        elif op in ('shot-duplicate','shot-create'):
            _keys(command,{'operation','source','id','name'})
            source=resource(command['source'],{'scene3d-shot'});value=deepcopy(source)
            value.update(id=command['id'],name=command['name'])
            if op=='shot-create':value.update(tracks=[],bindings=[],material_overrides=[],temporal_constraints=[])
            create(value,'shots')
            scene=edit(value['scene']['path'],{'scene3d-scene'})
            _require(value['id'] not in scene['shots'],'Scene already has this shot ID.');scene['shots'].append(value['id'])
        elif op=='shot-delete':
            _keys(command,{'operation','document'});path=command['document'];shot=resource(path,{'scene3d-shot'})
            _require(path in selected['shots'],'Shot is not selected by this project.');selected['shots'].remove(path)
            scene=edit(shot['scene']['path'],{'scene3d-scene'})
            if not any(documents[p]['id']==shot['id'] and documents[p]['scene']['path']==shot['scene']['path'] for p in selected['shots']):
                scene['shots'].remove(shot['id'])
        elif op in ('scene-duplicate','scene-create'):
            _keys(command,{'operation','source','id','name'});scene=deepcopy(resource(command['source'],{'scene3d-scene'}))
            scene.update(id=command['id'],name=command['name'],shots=[],revision=0)
            if op=='scene-create':
                from scene3d_example import node
                scene.update(nodes=[node('root')],roots=['root'],cameras=[],lights=[],dependencies=[],collections=[],layers=[])
            create(scene,'scenes')
        elif op=='scene-rename':
            _keys(command,{'operation','document','name'});scene=edit(command['document'],{'scene3d-scene'});scene['name']=command['name'];validate_document(scene)
        elif op=='scene-delete':
            _keys(command,{'operation','document'});path=command['document'];resource(path,{'scene3d-scene'})
            _require(path in selected['scenes'] and len(selected['scenes'])>1,'Cannot delete the last scene.')
            selected['scenes'].remove(path);selected['shots']=[p for p in selected['shots'] if documents[p]['scene']['path']!=path]
        elif op=='project-order':
            _keys(command,{'operation','field','paths'});field=command['field'];paths=command['paths']
            _require(isinstance(field,str) and field in selected and isinstance(paths,list) and all(isinstance(path,str) for path in paths) and len(paths)==len(set(paths)) and set(paths)==set(selected[field]),
                     'Reordering requires an exact permutation of selected document paths.')
            selected[field]=list(paths)
        else:
            _require(False,'Unsupported timeline operation: '+str(op))
    for path in edited:validate_document(documents[path])
    return dict(document_replacements={path:documents[path] for path in edited if path in original},
                derived_documents={path:documents[path] for path in additions},project_selection=selected)


def apply_timeline(repository, body):
    """Apply a fixed set of native operations through the repository transaction."""
    _keys(body,{'expected_revision','expected_sha256','transaction_id','commands'})
    commands=deepcopy(body['commands'])
    # Semantic resolution belongs inside the repository's idempotency boundary.
    # A delivered transaction may name a document promoted by its first save.
    def build(source,destination,scene_path):
        from scene3d_snapshot import snapshot_project
        bundle=load_project(source);options=prepare_timeline_commands(bundle,commands)
        receipt=snapshot_project(source,destination,scene_path=scene_path,**options)
        # The native publisher retains historic documents. Repository revisions
        # retain history themselves and admit only the selected verified closure.
        selected_bundle=load_project(destination)
        for path in set(receipt['inventory'])-set(selected_bundle['inventory']):
            (Path(destination)/path).unlink()
        if receipt['active_scene'] is not None:return receipt['active_scene']['path']
        available=load_project(destination)['project']['scenes']
        return scene_path if any(ref['path']==scene_path for ref in available) else available[0]['path']
    return repository.transact_native(body['expected_revision'],body['transaction_id'],
        dict(adapter='editor3d-timeline-v1',commands=commands),build,
        expected_sha256=body['expected_sha256'],label='Edit timeline')


def evaluate_timeline(repository, body):
    """Evaluate one exact frame in one hash-bound saved native snapshot."""
    _keys(body,{'revision','sha256','shot','frame'})
    frame=body['frame']
    if isinstance(frame,dict):
        _keys(frame,{'numerator','denominator'})
        numerator=_integer(frame['numerator'],1_000_000_000)
        denominator=_integer(frame['denominator'],1_000_000)
        _require(denominator>0,'Frame denominator must be positive.')
        frame=Fraction(numerator,denominator)
    else:
        _integer(frame)
    native=repository.native_snapshot(body['revision'],body['sha256'])
    root=native['root'];bundle=load_project(root)
    _require(any(ref['path']==body['shot'] for ref in bundle['project']['shots']),'Select an exact registered shot path.')
    shot=bundle['documents'][body['shot']]
    from scene3d_evaluate import evaluate_scene
    result=evaluate_scene(bundle,shot['scene']['path'],body['shot'],frame,source_root=root)
    result['revision']=body['revision'];result['snapshot_sha256']=body['sha256']
    return result


def select_scene(repository, body):
    """Persist the editor's active scene without changing native document bytes."""
    _keys(body,{'expected_revision','expected_sha256','transaction_id','scene'})
    selected=body['scene'];_require(isinstance(selected,str),'Select an exact registered scene path.')
    def build(source,destination,previous_scene):
        from asset_library import read_source_bytes
        from scene3d_contracts import portable_path
        bundle=load_project(source)
        _require(any(ref['path']==selected for ref in bundle['project']['scenes']),'Scene is not selected by this project.')
        destination=Path(destination);destination.mkdir()
        for path,record in bundle['inventory'].items():
            payload=read_source_bytes(Path(source)/portable_path(path),record['bytes'])
            _require(len(payload)==record['bytes'] and hashlib.sha256(payload).hexdigest()==record['sha256'],'Native scene source changed while switching.')
            target=destination/portable_path(path);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
        return selected
    return repository.transact_native(body['expected_revision'],body['transaction_id'],
        dict(adapter='editor3d-scene-select-v1',scene=selected),build,
        expected_sha256=body['expected_sha256'],label='Open scene')


def sample_curve(repository, body):
    """Bounded exact samples from the canonical evaluator, never a UI interpolator."""
    _keys(body,{'revision','sha256','shot','target','property','sample_count'})
    count=body['sample_count'];_require(type(count) is int and 2<=count<=33,'Curve samples must be between 2 and 33.')
    prop=body['property'];target=body['target']
    _require(prop in ('translation','rotation','scale','visible','light_intensity','world_exposure') and isinstance(target,str),'Unsupported curve channel.')
    native=repository.native_snapshot(body['revision'],body['sha256']);bundle=load_project(native['root'])
    _require(any(ref['path']==body['shot'] for ref in bundle['project']['shots']),'Select an exact registered shot path.')
    shot=bundle['documents'][body['shot']]
    from scene3d_evaluate import evaluate_scene
    samples=[]
    for index in range(count):
        frame=Fraction(index*(shot['duration_frames']-1),count-1)
        evaluated=evaluate_scene(bundle,shot['scene']['path'],body['shot'],frame,source_root=native['root'],max_track_samples=8192)
        if prop=='world_exposure':
            _require(target=='world','World exposure requires world target.');value=evaluated['world']['exposure']
        else:
            _require(target in evaluated['nodes'],'Curve target is absent from the evaluated scene.')
            state=evaluated['nodes'][target]
            if prop in ('translation','rotation','scale'):value=state['channels'][prop]
            elif prop=='visible':value=state['visible']
            else:
                _require('light' in state,'Light intensity requires a light target.');value=state['light']['intensity']
        samples.append(dict(frame=evaluated['frame'],value=deepcopy(value)))
    return dict(revision=body['revision'],snapshot_sha256=body['sha256'],shot=deepcopy(body['shot']),
                target=target,property=prop,samples=samples,semantics=(
                    'evaluated-local-channels-before-constraints' if prop in ('translation','rotation','scale') else
                    'effective-render-visibility' if prop=='visible' else 'evaluated-world-exposure' if prop=='world_exposure' else 'evaluated-light-intensity'))


def routes():
    """Fixed authenticated server dispatch; no caller-supplied Python callbacks."""
    return {'timeline/edit':lambda server,body: apply_timeline(server.repository,body),
            'timeline/evaluate':lambda server,body:evaluate_timeline(server.repository,body),
            'timeline/curve':lambda server,body:sample_curve(server.repository,body),
            'timeline/select-scene':lambda server,body:select_scene(server.repository,body)}
