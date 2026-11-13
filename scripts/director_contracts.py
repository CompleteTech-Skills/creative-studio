"""Validate Director v1 contracts without invoking planning or production.

Schema admission is not native asset acceptance. Optional exact-byte closure is
read-only and verifies regular project files; native owners still validate their
formats, rendering and review evidence.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import unicodedata

from director_schema import DOCUMENTS, METHODS, MILESTONES, definitions, schemas
from rig_contracts import COORDINATES
from rig_errors import RigError, decode_json, finite_json, object_hash, portable_name, require

MAX_BYTES = 32 * 1024**2
MAX_FILES = 4096
MAX_TOTAL_BYTES = 1024**3
ARTIFACT_KEYS = frozenset(definitions()['artifact-ref']['properties'])


def _require(condition, code, message):
    require(condition, 'DIRECTOR_' + code, message, stage='director')


def _integer(value, name, minimum=0):
    # JSON Schema integer admits 24.0; normalize only at arithmetic boundaries,
    # retaining the original record and exact-byte digest without rewriting it.
    _require(isinstance(value,(int,float)) and not isinstance(value, bool) and
             minimum <= value <= 2_000_000_000 and math.isfinite(value) and value==int(value),
             'TIME', name + ' must be a bounded integer.')
    return int(value)


def _fps(value):
    _require(isinstance(value, dict) and set(value)=={'numerator','denominator'},'TIME','Invalid rational FPS.')
    a,b=(_integer(value[k],k,1) for k in ('numerator','denominator'))
    _require(a<=1_000_000 and b<=1_000_000,'TIME','FPS exceeds the rational bound.')
    return Fraction(a,b)


def frame_timestamp(frame, fps):
    """Exact seconds at a frame boundary (terminal boundaries are permitted)."""
    return Fraction(_integer(frame,'frame'),1)/_fps(fps)


def duration_frames(seconds, fps):
    """Convert an exact rational duration to integral frames; never round."""
    _require(isinstance(seconds,(int,Fraction,dict)) and not isinstance(seconds,bool),
             'TIME','Use exact integer or rational seconds.')
    try:
        if isinstance(seconds,dict):
            _require(set(seconds)=={'numerator','denominator'},'TIME','Invalid rational duration.')
            seconds=Fraction(_integer(seconds['numerator'],'duration'),_integer(seconds['denominator'],'denominator',1))
        value=Fraction(seconds)*_fps(fps)
    except (ValueError,TypeError,ZeroDivisionError) as exc:
        raise RigError('DIRECTOR_TIME','Invalid rational duration.',stage='director') from exc
    _require(value.denominator==1,'TIME','Duration is not an integral frame count.')
    return _integer(value.numerator,'duration frames',1)


def convert_position(position, source='canonical', target='blender'):
    """Explicit basis conversion from the established rig authority.

    +Y-up canonical (x,y,z) <-> Blender (x,-z,y); units remain meters.
    This position adapter does not replace quaternion/TRS rig math.
    """
    _require(isinstance(position,(list,tuple)) and len(position)==3 and
             all(isinstance(x,(int,float)) and not isinstance(x,bool) and abs(x)<=1e9
                 and math.isfinite(x) for x in position),'COORDINATES','Expected a finite three-vector.')
    _require(source in ('canonical','blender') and target in ('canonical','blender'),
             'COORDINATES','Unknown coordinate basis.')
    x,y,z=position
    if source==target:return [x,y,z]
    return [x,-z,y] if source=='canonical' else [x,z,-y]


def artifact_references(value):
    if isinstance(value,dict):
        if set(value)==ARTIFACT_KEYS:
            yield value
        for child in value.values():yield from artifact_references(child)
    elif isinstance(value,list):
        for child in value:yield from artifact_references(child)


def _walk(value):
    yield value
    if isinstance(value,dict):
        for child in value.values():yield from _walk(child)
    elif isinstance(value,list):
        for child in value:yield from _walk(child)


def _unique(items, name, key='id'):
    ids=[x[key] if isinstance(x,dict) else x for x in items]
    _require(len(ids)==len(set(ids)),'DUPLICATE','Duplicate '+name+'.')
    return set(ids)


def _range(value):
    _require(value['end_frame']>value['start_frame'],'TIME','Frame range must be nonempty and half-open.')


def _semantics(value):
    for x in _walk(value):
        if not isinstance(x,dict):continue
        if set(x)=={'start_frame','end_frame'}:_range(x)
        if set(x)=={'numerator','denominator'}:
            _require(x['denominator']>0,'TIME','Rational denominator must be positive.')
        if set(x)=={'value','explanation'}:
            _require(x['value'] is not None or bool(x['explanation']),'USAGE','Unknown measurements need an explanation.')
        if 'classification' in x and 'measurement_source' in x:
            if x['classification']=='unavailable':
                _require(all(x[k]['value'] is None for k in ('provider_calls','input_tokens','output_tokens',
                    'cpu_seconds','gpu_seconds','storage_bytes','amount')),'USAGE','Unavailable usage cannot declare measurements.')
            _require((x['currency'] is None)==(x['amount']['value'] is None),'USAGE','Currency and amount must be known together.')
            for name in ('provider_calls','input_tokens','output_tokens','storage_bytes'):
                amount=x[name]['value']
                _require(amount is None or float(amount).is_integer(),'USAGE','Count measurements must be integers.')
        if 'policy' in x and 'project_revision_sha256' in x:
            policy=x['policy']
            datetime.fromisoformat(x['deadline'].replace('Z','+00:00'))
            _require(datetime.fromisoformat(x['deadline'].replace('Z','+00:00')).tzinfo is not None,
                     'CONTEXT','Deadline must include a timezone.')
            if policy['network']=='allow-configured' or policy['paid']!='deny':
                _require(policy['policy_ref'] is not None,'AUTHORIZATION','Network/paid operations require an explicit policy reference.')
            if policy['paid']!='deny':
                _require(x['budget_reservation_id'] is not None,'AUTHORIZATION','Paid operations require a budget reservation.')
        if 'capabilities' in x and 'expires_at' in x:
            _unique(x['capabilities'],'capability operations','operation')
            for capability in x['capabilities']:
                _require(not capability['execution_verified'] or
                         (capability['status']=='available' and bool(capability['probe_evidence'])),
                         'EVIDENCE','Execution verification requires available capability and probe evidence.')
        if 'nodes' in x and 'edges' in x and 'critical_path_seconds' in x:
            nodes=_unique(x['nodes'],'graph node IDs')
            graph={node:set() for node in nodes}
            for edge in x['edges']:
                _require(edge['source'] in nodes and edge['target'] in nodes,'REFERENCE','Dangling graph edge.')
                graph[edge['source']].add(edge['target'])
                target=next(n for n in x['nodes'] if n['id']==edge['target'])
                _require(edge['artifact'] in target['inputs'],'REFERENCE','Graph edge must pin an exact target input.')
            _acyclic(graph,'graph')
        if 'state' in x and 'native_workers' in x:
            _require(x['progress'] is None or x['progress']<=1,'JOB','Progress must be within zero and one.')
            _require(x['state']!='completed' or (x['artifacts'] and x['native_workers']),
                     'EVIDENCE','Completed native jobs need output and worker evidence.')
        if 'frames' in x and 'backend' in x and 'frame_range' in x:
            _range(x['frame_range'])
            start,end=x['frame_range']['start_frame'],x['frame_range']['end_frame']
            _require(len(x['frames'])==end-start and
                     all(frame['index']==start+offset for offset,frame in enumerate(x['frames'])),
                     'FRAME','Verified frames must be ordered, complete and unique.')
            for frame in x['frames']:
                actual=Fraction(_integer(frame['timestamp']['numerator'],'timestamp numerator'),
                                _integer(frame['timestamp']['denominator'],'timestamp denominator',1))
                _require(actual==frame_timestamp(frame['index'],x['frame_rate']) and frame['resolution']==x['resolution'],
                         'FRAME','Frame time or resolution does not match the pinned request.')
        if 'verified' in x and 'format_losses' in x:
            _require(not x['verified'] or (x['outputs'] and x['verification']),'EVIDENCE','Verified exports need actual output and verification references.')
            if x['verified'] and x['target'] in ('mp4','webm'):
                _require(x['audio_streams']==0 and x['decoded_frames'] is not None and x['decoded_frames']>0,
                         'VIDEO','Verified video must have decoded frames and zero audio streams.')
        if 'ready' in x and 'missing_capabilities' in x:
            _require(not x['ready'] or (not x['missing_capabilities'] and x['evidence'] and
                     not any(item['severity']=='error' for item in x['diagnostics'])),
                     'EVIDENCE','Ready preflight cannot have missing capabilities/errors and requires probe evidence.')
        if 'name' in x and 'authority' in x and 'scope' in x and x['name'] in MILESTONES:
            _require(x['status']!='passed' or bool(x['evidence']),'EVIDENCE','Passed milestones require evidence.')
            _require(not (x['name']=='visual_qa' and x['status']=='passed' and x['authority']=='automated'),
                     'REVIEW','Automated validation cannot grant visual approval.')
    kind=value['kind']
    if kind=='director-creative-brief':
        _unique(value['characters'],'character IDs')
        _require(duration_frames(value['duration'],value['frame_rate'])>0,'TIME','Empty brief.')
        _require(Fraction(*[int(n) for n in value['resolution']])==_fps(value['aspect_ratio']),
                 'TIME','Aspect ratio differs from resolution.')
    elif kind=='director-story-plan':
        _unique(value['beats'],'beat IDs')
        _require(all(b['actions'] or b['composition'] for b in value['beats']),'STORY','Every beat needs visible action or composition.')
    elif kind=='director-shot-plan':
        _unique(value['shots'],'shot IDs')
        cursor=0
        for shot in value['shots']:
            _range(shot)
            _unique(shot['actors'],'shot actor IDs')
            _unique(shot['props'],'shot prop IDs')
            _require(not set(shot['actors']) & set(shot['props']),'DUPLICATE','Actor and prop identities must be distinct.')
            overlap=shot['transition']['overlap_frames']
            _require(shot['transition']['kind']=='dissolve' or overlap==0,'TIME','Cuts cannot overlap.')
            _require(overlap<shot['duration_frames'] and shot['start_frame']==cursor-overlap and
                     shot['duration_frames']==shot['end_frame']-shot['start_frame'],'TIME','Shot gap, unintended overlap or duration mismatch.')
            _unique(shot['markers'],'marker IDs')
            for marker in shot['markers']:
                _require(shot['start_frame']<=marker['frame']<shot['end_frame'],'TIME','Marker is outside its shot.')
                _require(marker['target'] in shot['actors']+shot['props'],'REFERENCE','Marker target is not an actor or prop.')
            cursor=shot['end_frame']
        _require(cursor==value['duration_frames'],'TIME','Shot coverage differs from duration.')
    elif kind=='director-character-identity':
        _unique(value['proportions'],'proportion names','name')
        _unique(value['bone_mapping'],'bone roles','role')
        _unique(value['bone_mapping'],'native bone identities','bone')
    elif kind=='director-project':
        _require(_unique(value['milestones'],'milestone names','name')==set(MILESTONES),'MILESTONE','All independent milestone states are required.')
        _require(value['status']!='complete' or all(m['status']=='passed' for m in value['milestones']),
                 'MILESTONE','Project completion requires every independent milestone.')
    elif kind=='director-request':
        method=value['method'];data=value['input']
        if method.startswith('AcquisitionGateway.'):
            operation=method.rsplit('.',1)[1]
            if operation!='prepare':_require(data['binding_id'] is not None,'ACQUISITION','Existing operation needs a native binding.')
            if operation in ('ingest','fail','reconcile'):
                _require(data['claim_id'] is not None,'ACQUISITION','Existing operation needs its original claim.')
            if operation=='ingest':_require(data['candidates'],'ACQUISITION','Ingest requires candidate bytes.')
            if operation=='fail':_require(data['diagnostics'],'ACQUISITION','Failure requires preserved diagnostics.')
        if method=='BudgetLedger.settle':
            _require(data['actual_usage']['classification']=='actual','USAGE','Settlement requires actual usage.')
        if method=='QAService.record_review':_require(data['evidence'],'REVIEW','Review needs exact evidence.')
    elif kind=='director-result':
        status=value['status']
        _require((status not in ('succeeded','partial') and value['value'] is None) or
                 (status in ('succeeded','partial') and value['value'] is not None),
                 'RESULT','Only successful or partial operations may carry a typed value.')
        _require(status=='succeeded' or value['diagnostics'],'RESULT','Non-success results require typed diagnostics.')
        if value['method'] in ('ExecutionService.submit','RenderAdapter.submit') and value['value'] is not None:
            _require(value['value']['state'] in ('queued','running'),'JOB','Submission cannot report completed execution.')
        if status=='succeeded':
            output=value['value'];method=value['method']
            if method=='AssetBuildAdapter.build':
                _require(output['meshes'] and output['vertices']>0 and output['triangles']>0,
                         'EVIDENCE','A successful asset build needs actual mesh references and nonzero geometry counts.')
            elif method=='RigMotionAdapter.prepare':
                _require(output['deformation_checks'],'EVIDENCE','Successful rig/motion preparation needs deformation evidence.')
            elif method=='SceneAssembler.assemble':
                _require(output['scenes'],'EVIDENCE','Successful assembly needs an authoritative scene reference.')
            elif method in ('ExportService.export','ExportService.verify'):
                _require(output['verified'] and output['outputs'] and output['verification'],
                         'EVIDENCE','Successful export needs actual outputs and independent verification references.')


def _acyclic(graph,label):
    # Iterative traversal permits the full bounded graph without recursion limits.
    visiting,done=set(),set()
    for origin in graph:
        stack=[(origin,False)]
        while stack:
            node,leaving=stack.pop()
            if leaving:
                visiting.remove(node);done.add(node)
            elif node not in done:
                _require(node not in visiting,'CYCLE','Cyclic '+label+' dependencies.')
                visiting.add(node);stack.append((node,True))
                stack.extend((child,False) for child in graph.get(node,()))


def validate_document(value):
    """Validate structure and semantics; no files are read or effects invoked."""
    finite_json(value)
    error=next(_validator().iter_errors(value),None)
    _require(error is None,'SCHEMA',error.message[:2048] if error else '')
    _semantics(value)
    refs=list(artifact_references(value))
    paths={};identities={};aliases={}
    for item in refs:
        path=portable_name(item['path'])
        _require(path==unicodedata.normalize('NFC',path),'PATH','Paths must use Unicode NFC.')
        alias=path.casefold()
        _require(alias not in aliases or aliases[alias]==path,'PATH','Case-colliding paths.')
        identity=(item['artifact_id'],item['semantic_version'])
        _require(path not in paths or paths[path]==item,'REFERENCE','Conflicting exact references for a path.')
        _require(identity not in identities or identities[identity]==item,'REFERENCE','Conflicting exact artifact identity.')
        paths[path]=item;identities[identity]=item;aliases[alias]=path
    _require(len(paths)<=MAX_FILES and sum(r['byte_size'] for r in paths.values())<=MAX_TOTAL_BYTES,
             'LIMIT','Artifact closure exceeds bounded storage.')
    return value


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(schemas()['director.schema.json'],format_checker=Draft202012Validator.FORMAT_CHECKER)


def load_document(path):
    return validate_document(decode_json(_read_json_file(path)))


def _read_json_file(path):
    """Bound and classify the opened input before reading, including CLI inputs."""
    try:
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
        with os.fdopen(os.open(path,flags),'rb') as stream:
            info=os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode),'PATH','JSON input must be a regular file.')
            _require(info.st_size<=MAX_BYTES,'LIMIT','Document exceeds 32 MiB.')
            data=stream.read(MAX_BYTES+1)
            _require(len(data)<=MAX_BYTES,'LIMIT','Document exceeds 32 MiB.')
            return data
    except OSError as exc:
        raise RigError('DIRECTOR_PATH','JSON input is missing or unsafe.',stage='director') from exc


def _read_artifact(root, artifact):
    """Open every path component relative to the root; reject links and devices."""
    root=Path(root)
    # dir_fd/O_NOFOLLOW protects reads even if an untrusted child is exchanged.
    _require(hasattr(os,'O_NOFOLLOW') and os.open in os.supports_dir_fd,
             'PLATFORM','Secure descriptor-relative artifact reads are unavailable on this platform.')
    flags=os.O_RDONLY|os.O_NOFOLLOW|getattr(os,'O_NONBLOCK',0)
    descriptors=[]
    try:
        fd=os.open(root,flags|getattr(os,'O_DIRECTORY',0));descriptors.append(fd)
        parts=artifact['path'].split('/')
        for part in parts[:-1]:
            fd=os.open(part,flags|getattr(os,'O_DIRECTORY',0),dir_fd=fd);descriptors.append(fd)
            _require(stat.S_ISDIR(os.fstat(fd).st_mode),'PATH','Artifact parent is not a directory.')
        fd=os.open(parts[-1],flags,dir_fd=fd);descriptors.append(fd)
        _require(stat.S_ISREG(os.fstat(fd).st_mode),'PATH','Artifact must be a regular file, never a directory/link/device.')
        with os.fdopen(os.dup(fd),'rb') as stream:data=stream.read(int(artifact['byte_size'])+1)
        _require(len(data)==artifact['byte_size'] and hashlib.sha256(data).hexdigest()==artifact['sha256'],
                 'HASH','Artifact bytes differ from exact reference: '+artifact['path'])
        return data
    except OSError as exc:
        raise RigError('DIRECTOR_PATH','Artifact is missing or unsafe: '+artifact['path'],stage='director') from exc
    finally:
        for fd in reversed(descriptors):os.close(fd)


def validate_bundle(documents, root=None):
    """Validate a bounded document set, links and optionally all exact file bytes.

    Without root this is structural/reference validation, never byte acceptance.
    Native format validation and production execution belong to their owners.
    """
    _require(isinstance(documents,list) and 0<len(documents)<=4096,'LIMIT','Expected one to 4096 documents.')
    for document in documents:validate_document(document)
    core=[d for d in documents if 'id' in d]
    ids=_unique(core,'document IDs')
    by_id={d['id']:d for d in core}
    refs={};paths={};aliases={}
    for document in documents:
        for item in artifact_references(document):
            identity=(item['artifact_id'],item['semantic_version'])
            _require(identity not in refs or refs[identity]==item,'REFERENCE','Conflicting bundle artifact identity.')
            _require(item['path'] not in paths or paths[item['path']]==item,'REFERENCE','Conflicting bundle path.')
            alias=unicodedata.normalize('NFC',item['path']).casefold()
            _require(alias not in aliases or aliases[alias]==item['path'],'PATH','Bundle contains path aliases.')
            refs[identity]=item;paths[item['path']]=item;aliases[alias]=item['path']
            if item['kind'].startswith('director-') and item['kind'] not in ('director-inventory',):
                _require(item['artifact_id'] in ids and by_id[item['artifact_id']]['kind']==item['kind'],
                         'REFERENCE','Dangling Director document reference.')
                target=by_id[item['artifact_id']]
                _require(item['schema_version']==target['schema_version'] and
                         item['semantic_version']==target['semantic_version'],
                         'REFERENCE','Director reference version does not match its document.')
    _require(len(paths)<=MAX_FILES and sum(r['byte_size'] for r in paths.values())<=MAX_TOTAL_BYTES,
             'LIMIT','Bundle payload closure exceeds bounds.')
    graph={key:{(dep['artifact_id'],dep['semantic_version']) for dep in item['dependencies']}
           for key,item in refs.items()}
    _acyclic(graph,'artifact')
    stories={d['id']:{b['id'] for b in d['beats']} for d in core if d['kind']=='director-story-plan'}
    for doc in core:
        if doc['kind']=='director-shot-plan':
            beats=stories.get(doc['story']['artifact_id'])
            _require(beats is not None,'REFERENCE','Shot plan lacks its story document.')
            for shot in doc['shots']:
                _require(shot['beat_ids'] and set(shot['beat_ids'])<=beats,'REFERENCE','Shot references missing story beats.')
    if root is not None:
        for item in paths.values():
            data=_read_artifact(root,item)
            if item['kind'].startswith('director-') and item['kind']!='director-inventory':
                decoded=decode_json(data)
                _require(decoded==by_id[item['artifact_id']],'REFERENCE','Pinned Director bytes differ from the supplied document.')
            if item['kind']=='director-inventory':
                inventory=decode_json(data)
                _require(isinstance(inventory,list),'REFERENCE','Directory artifacts require an inventory manifest.')
                # Inventory members must already be in the bounded exact closure.
                for member in inventory:
                    _require(isinstance(member,dict) and member.get('path') in paths and paths[member['path']]==member,
                             'REFERENCE','Inventory contains an undeclared or conflicting member.')
    return documents


def validate_method_request(method,request):
    validate_document(request)
    _require(method in METHODS and request['kind']=='director-request' and request['method']==method,
             'METHOD','Request does not match the selected method.')
    return request


def validate_method_result(method,result,request=None):
    validate_document(result)
    _require(method in METHODS and result['kind']=='director-result' and result['method']==method,
             'METHOD','Result does not match the selected method.')
    if request is not None:
        validate_method_request(method,request)
        _require(result['input_digest']==object_hash(request),'RESULT','Result input digest is stale or mismatched.')
        if result['value'] is not None:
            data,value=request['input'],result['value']
            if method=='AssetResolver.resolve':
                expected=[object_hash(spec) for spec in data['specs']]
                observed=[object_hash(item['spec']) for item in value['requirements']]
                _require(len(observed)==len(set(observed)) and sorted(expected)==sorted(observed),
                         'RESULT','Resolution must cover each requested specification exactly once.')
            elif method.startswith('AcquisitionGateway.'):
                if data['binding_id'] is not None:
                    _require(value['binding']['artifact_id']==data['binding_id'],
                             'RESULT','Acquisition response changed its native binding.')
                for field in ('claim_id','provider_request_id'):
                    _require(data[field] is None or value[field]==data[field],
                             'RESULT','Acquisition response changed its original '+field+'.')
                _require(value['outcome']!='uncertain' or value['claim_id'] is not None,
                         'RESULT','An uncertain acquisition must retain its original claim.')
            elif method.startswith('BudgetLedger.'):
                operation=method.rsplit('.',1)[1]
                expected={'reserve':'reserved','release':'released','settle':
                          'uncertain' if data.get('outcome')=='uncertain' else 'settled'}[operation]
                _require(value['state']==expected,'RESULT','Budget receipt state does not match the operation outcome.')
                if operation!='reserve':
                    _require(value['reservation_id']==data['reservation_id'],
                             'RESULT','Budget response changed its original reservation.')
            elif method=='PlanningProvider.generate_structured':
                _require(value['content']['kind']==data['output_schema_id'] and
                         value['content']['schema_version']==data['output_schema_version'] and
                         value['template_sha256']==data['template_sha256'],
                         'RESULT','Planning output schema or prompt template differs from the request.')
                _require(len(value['repair_history'])<=data['max_repairs'],
                         'RESULT','Planning response exceeded the bounded repair allowance.')
            elif method=='FrameEvaluator.evaluate':
                _require(value['scene']==data['scene'] and value['frame']==data['frame'],
                         'RESULT','Evaluation differs from the requested scene or exact sample time.')
            elif method in ('ExecutionService.status','ExecutionService.resume','ExecutionService.cancel',
                            'RenderAdapter.status','RenderAdapter.cancel'):
                _require(value['job_id']==data['job_id'],'RESULT','Job response changed the native job identity.')
            elif method=='RenderAdapter.verify':
                _require(all(value[key]==data[key] for key in ('scene_snapshot','frame_range','frame_rate','resolution')),
                         'RESULT','Verified render differs from the pinned scene, frame range, timebase or resolution.')
                _require([item['artifact'] for item in value['frames']]==data['frames'],
                         'RESULT','Verified render does not identify the supplied frames exactly.')
            elif method=='ExportService.export':
                _require(value['target']==data['target'],'RESULT','Export target changed.')
            elif method=='ExportService.verify':
                _require(data['expected_frames'] is None or value['decoded_frames']==data['expected_frames'],
                         'RESULT','Decoded frame count differs from the expected count.')
                _require(not data['zero_audio_streams'] or value['audio_streams']==0,
                         'RESULT','Requested silent export contains audio or lacks verified stream count.')
            elif method=='QAService.record_review':
                _require(all(value[key]==data[key] for key in ('reviewer_id','authority','scope','decisions','evidence')),
                         'RESULT','Review receipt changed reviewer, scope, decisions or exact evidence.')
            elif method=='RevisionService.apply':
                _require(value['parent']['sha256']==data['expected_revision'],
                         'RESULT','Applied revision does not identify the expected parent.')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true');parser.add_argument('--write-schemas',action='store_true')
    parser.add_argument('--validate',type=Path);parser.add_argument('--bundle',type=Path);parser.add_argument('--root',type=Path)
    args=parser.parse_args()
    try:
        if args.check or args.write_schemas:
            for name,schema in schemas().items():
                path=Path(__file__).resolve().parents[1]/'profiles'/name
                text=json.dumps(schema,indent=2)+'\n'
                if args.write_schemas:path.write_text(text,encoding='utf-8')
                else:_require(path.read_text(encoding='utf-8')==text,'SCHEMA_DRIFT','Generated schema differs: '+name)
        if args.validate:load_document(args.validate)
        if args.bundle:
            validate_bundle(decode_json(_read_json_file(args.bundle)),args.root)
        print(json.dumps({'status':'valid','scope':'contracts-and-exact-bytes' if args.root else 'contracts-only',
                          'production_acceptance':False}))
    except (RigError,OSError) as exc:
        print(json.dumps({'status':'failed','diagnostic':exc.report if isinstance(exc,RigError) else str(exc)}))
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
