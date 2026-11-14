"""Actual saved skeletal/morph/edit/CLI proof using a labelled native fixture."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
import numpy as np
from animation3d_contracts import validate_contract
from animation3d_evaluator import RigClipEvaluator
from animation3d_clips import events_between, source_time
from rig_test_helpers import fixture
from rig_skinning import evaluate_deformation
from rig_errors import object_hash
from test_animation3d_clips import clip


def run(destination):
    destination=Path(destination).absolute()
    destination.mkdir(parents=True,exist_ok=False)
    source=destination/'source';source.mkdir()
    rig=fixture(source,name='native',animated=False)
    body=clip();body.update(id='body',name='Body motion')
    body['tracks'][0]['target']['bone_id']='b0'
    body['tracks'][0]['keys'][-1]['value']=[1,0,0]
    body['events']=[dict(id='beat',name='Midpoint',frame=12,type='marker',payload={})]
    ref=dict(path='native/rig.json',kind='rig',id=rig.manifest['id'],version=rig.manifest['version'],
             sha256=hashlib.sha256((rig.root/'rig.json').read_bytes()).hexdigest())
    body['rig_binding']=ref
    face=deepcopy(body);face.update(id='face',name='Independent face')
    face['events']=[]
    face['tracks'][0].update(id='blink',target=dict(kind='morph',actor_id='actor',mesh_id=rig.geometry.meshes[0].id,morph_id='blink',property='weight'),
                            value_type='float',default_value=0,keys=[dict(time=dict(frame=0),value=0,interpolation='linear'),dict(time=dict(frame=24),value=1)])
    def write(path,value):path.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n');return path
    body_path=write(source/'body.json',body);face_path=write(source/'face.json',face)
    for value in (body,face):validate_contract(value)
    def inventory():return {str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    before=inventory()
    evaluator=RigClipEvaluator(rig.root,'actor')
    base=evaluate_deformation(rig)
    observed={}
    for body_frame,face_frame in [(0,0),(12,6),(6,18),(24,24),(12,6),(0,0)]:
        state=evaluator.evaluate_sources([(body,dict(frame=body_frame)),(face,dict(frame=face_frame))])
        expected=np.array([body_frame/24,0,.2*face_frame/24])
        error=max(float(np.abs(np.array(state['geometry'][mesh]['positions'])-base[mesh]['positions']-expected).max()) for mesh in base)
        # The persisted GLB encodes the authored 0.2m morph as binary32.
        assert error<1e-8,(body_frame,face_frame,error)
        key=f'{body_frame}:{face_frame}'
        if key in observed:assert observed[key]['signature']==state['dependency_signature']
        observed[key]=dict(signature=state['dependency_signature'],maximum_geometry_error_m=error,geometry=state['geometry'])
    output=destination/'frame12.json'
    command=[sys.executable,str(ROOT/'scripts/animation3d_evaluate.py'),'--root',str(rig.root),'--actor','actor',
             '--clip',str(body_path),'--clip',str(face_path),'--clip-frame','12','--clip-frame','6','--output',str(output)]
    cli=subprocess.run(command,capture_output=True,text=True,timeout=60)
    assert cli.returncode==0,(cli.stdout,cli.stderr)
    decoded=json.loads(output.read_bytes())
    assert decoded['geometry']==observed['12:6']['geometry']
    request=dict(schema_version=1,kind='animation3d-edit-request',expected_document_hash=object_hash(body),
        commands=[dict(op='replace-key',track_id=body['tracks'][0]['id'],time=dict(frame=24),
                       key=dict(time=dict(frame=24),value=[2,0,0]))])
    request_path=write(destination/'edit-request.json',request)
    edit=subprocess.run([sys.executable,str(ROOT/'scripts/animation3d_edit.py'),'--clip',str(body_path),'--request',str(request_path),
                         '--rig-root',str(rig.root),'--actor','actor'],capture_output=True,text=True,timeout=60)
    assert edit.returncode==0,(edit.stdout,edit.stderr)
    edited=json.loads(edit.stdout);assert edited['native_validated']
    edited_path=write(destination/'edited-working-clip.json',edited['clip'])
    reopened=json.loads(edited_path.read_bytes());validate_contract(reopened)
    changed=evaluator.evaluate_sources([(reopened,dict(frame=12)),(face,dict(frame=6))])
    for mesh in base:
        old=np.array(decoded['geometry'][mesh]['positions']);new=np.array(changed['geometry'][mesh]['positions'])
        assert np.max(np.abs(new-old-[.5,0,0]))<1e-10
    assert [e['id'] for e in events_between(body,dict(frame=0),dict(frame=12))]==['beat']
    assert events_between(body,dict(frame=12),dict(frame=12))==[]
    assert source_time(body,dict(frame=15),dict(numerator=30,denominator=1))==dict(frame=12,subframe_ticks=0)
    assert before==inventory()
    artifacts={str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.rglob('*') if p.is_file()}
    result=dict(fixture=True,scope='saved real skeletal/morph geometry, exact CLI clocks and transactional detached edits',
        source_hashes=before,sources_unchanged=True,samples=observed,artifact_sha256=artifacts,
        source_vertices=rig.geometry.vertex_count,body_face_independent=True,cli_command=command,
        edit_document_before_hash=edited['before_document_hash'],edit_document_after_hash=edited['after_document_hash'],
        geometry_tolerance_m=1e-8, tolerance_reason='GLB binary32 morph displacement quantization',
        renderer_executed=False,visual_approval=False)
    write(destination/'proof.json',result)
    return result


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path);args=parser.parse_args()
    result=run(args.destination)
    print(json.dumps(dict(destination=str(args.destination),fixture=True,source_files=len(result['source_hashes']),
                          vertices=result['source_vertices'],sources_unchanged=True,body_face_independent=True)))
