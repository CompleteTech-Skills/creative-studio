"""Source-bound actual environment, identity and animation experiments."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from copy import deepcopy
from camera3d_math import look_at
from lookdev_contracts import Registry, canonical_bytes
from rig_errors import require
ROOT=Path(__file__).resolve().parents[1]



def canonical_fixture_triangles(polygons):
    """Freeze cyclic face roots and triangle order without reversing winding."""
    triangles=[]
    for polygon in polygons:
        indices=list(polygon)
        require(len(indices)>=3 and all(type(index) is int and index>=0 for index in indices)
                and len(set(indices))==len(indices), 'LOOKDEV_LAB_GEOMETRY', 'Expected simple indexed fixture faces.')
        root=indices.index(min(indices))
        indices=indices[root:]+indices[:root]
        triangles.extend((indices[0],indices[index],indices[index+1])
                         for index in range(1,len(indices)-1))
    return sorted(triangles)


def frame_parameters(frame):
    require(type(frame) is int and 0 <= frame <= 7, 'LOOKDEV_LAB_FRAME', 'Choose an integer frame 0–7.')
    t=frame/7
    return {'frame':frame,'translation_meters':[-2+4*t,3,2],
            'watts':100+150*t,'temperature_kelvin':2800+6200*t,
            'emission_watts_per_square_meter':0 if frame<4 else 3,
            'occluder_x':-.7+1.4*t}


def material_document(identifier, **changes):
    value=json.loads((ROOT/'examples/lookdev-contracts/ceramic.json').read_text())
    value['id']=identifier
    value['surface'].update(base_color=[.08,.2,.42,1],roughness=.4,double_sided=True)
    value['surface'].update(deepcopy(changes))
    return value


def light_document(identifier='studio-key', **changes):
    value={'schema_version':1,'id':identifier,'version':'1.0.0','kind':'light3d',
        'dependencies':[],'provenance':material_document('provenance')['provenance'],
        'validation_status':'unvalidated','capabilities':[],'type':'area',
        'transform':{'translation_meters':[-2,3,2],
                     'rotation_xyzw':look_at([-2,3,2],[0,.75,0]).tolist()},
        'color':{'encoding':'linear-srgb','value':[1,1,1]},
        'intensity':{'value':150,'unit':'watt'},'area_size_meters':[1,1],
        'casts_shadow':True,'render_policy':{'strict':True,'allow_approximations':False,
        'required_features':[],'fallback':'reject'}}
    value.update(deepcopy(changes));return value


def frame_documents(frame):
    state=frame_parameters(frame)
    light=light_document(f'animated-key-frame-{frame:04d}',temperature_kelvin=state['temperature_kelvin'])
    light['transform']={'translation_meters':state['translation_meters'],
        'rotation_xyzw':look_at(state['translation_meters'],[0,.75,0]).tolist()}
    light['intensity']['value']=state['watts']
    body=material_document(f'animated-body-frame-{frame:04d}',emission={'color':[.08,.2,.42],
        'strength_watts_per_square_meter':state['emission_watts_per_square_meter']})
    return state,body,light


def inspect_mesh_assignment(obj,expected_plan):
    """Inspect actual polygon-selected material datablocks, not object labels."""
    require(obj.type=='MESH' and len(obj.data.polygons)>0,'LOOKDEV_LAB_ASSIGNMENT','Expected a nonempty actual mesh.')
    counts={}
    for face in obj.data.polygons:
        index=face.material_index
        require(type(index) is int and 0<=index<len(obj.data.materials),
                'LOOKDEV_LAB_ASSIGNMENT','Polygon references a missing material slot.')
        material=obj.data.materials[index]
        require(material is not None and material.get('hatch_plan_sha256')==expected_plan['sha256']
                and material.get('hatch_effective_sha256')==expected_plan['effective_sha256'],
                'LOOKDEV_LAB_ASSIGNMENT','Actual polygon material differs from the expected canonical plan.')
        counts[index]=counts.get(index,0)+1
    return {'polygons':len(obj.data.polygons),'actual_slots':[
        {'index':index,'faces':count,'material':obj.data.materials[index].name,
         'plan_sha256':obj.data.materials[index]['hatch_plan_sha256'],
         'effective_sha256':obj.data.materials[index]['hatch_effective_sha256']} for index,count in sorted(counts.items())]}


def run(destination,blender,python_site=None,temporary_root=None):
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=False)
    inputs=sorted((ROOT/'scripts').glob('*.py'))+sorted((ROOT/'profiles').glob('*.json'))+[
        ROOT/'examples/lookdev-contracts/ceramic.json',ROOT/'tests/fixtures/lookdev_laboratory_sequence.py']
    payloads={str(p.relative_to(ROOT)):p.read_bytes() for p in inputs}
    hashes={name:hashlib.sha256(payload).hexdigest() for name,payload in payloads.items()}
    snapshot=destination/'source'
    for name,payload in payloads.items():
        target=snapshot/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
    job={'destination':str(destination),'scripts':str(snapshot/'scripts')}
    (destination/'job.json').write_text(json.dumps(job,indent=2)+'\n')
    command=[str(Path(blender).resolve()),'--background','--factory-startup','--disable-autoexec',
        '--python-use-system-env','--threads','1','--python-exit-code','1','--python',
        str(snapshot/'tests/fixtures/lookdev_laboratory_sequence.py'),'--',str(destination/'job.json')]
    environment=dict(os.environ)
    temporary=Path(temporary_root or destination.parent/'.lookdev-lab-tmp')/destination.name
    temporary.mkdir(parents=True,exist_ok=False);environment['TMPDIR']=str(temporary)
    if python_site:environment['PYTHONPATH']=str(Path(python_site).resolve())
    start=time.monotonic()
    try:
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=environment,timeout=1200)
    except subprocess.TimeoutExpired as exc:
        (destination/'render.log').write_bytes(exc.stdout or b'')
        require(False,'LOOKDEV_LAB_TIMEOUT','Trusted sequence exceeded 1200 seconds; retained render.log.')
    (destination/'render.log').write_bytes(result.stdout)
    require(result.returncode==0,'LOOKDEV_LAB_RENDER','Sequence failed; inspect retained render.log.')
    require(all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items()),
        'LOOKDEV_LAB_SOURCE_CHANGED','Source changed during execution; diagnostic evidence only.')
    report=json.loads((destination/'report.json').read_text())
    report.update(source_sha256=hashes,command=command,elapsed_seconds=time.monotonic()-start)
    (destination/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    outputs={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(destination.iterdir()) if p.is_file()}
    (destination/'sha256.json').write_text(json.dumps(outputs,indent=2)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--blender',required=True,type=Path);p.add_argument('--python-site',type=Path)
    p.add_argument('--temporary-root',type=Path);a=p.parse_args()
    run(a.output,a.blender,a.python_site,a.temporary_root);print(a.output/'report.json')
if __name__=='__main__':main()
