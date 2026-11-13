"""Evidence-backed local Director admission; never invokes a remote provider.

Discovery, initialization and executed smoke tests are different evidence states.
Only explicitly requested local probes may execute trusted, fixed worker code.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from atomic_publish import _rename_no_replace
from director_contracts import validate_method_request, validate_method_result, validate_document
from director_schema import schemas
from rig_errors import RigError, decode_json, object_hash, require, sha256

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ('blender.cpu', 'blender.gpu', 'codec.h264', 'codec.vp9', 'codec.av1',
              'importer.obj', 'importer.glb', 'importer.gltf', 'importer.blend',
              'native.acquisition', 'planning.provider', 'module.scene3d',
              'module.camera3d', 'module.lookdev3d', 'module.animation3d',
              'module.render3d', 'module.editor3d')
MODULES = {'module.scene3d':'scene3d_contracts', 'module.camera3d':'camera3d_contracts',
           'module.lookdev3d':'lookdev_contracts', 'module.animation3d':'animation3d_contracts',
           'module.render3d':'render3d_contracts', 'module.editor3d':'editor3d_contracts',
           'native.acquisition':'hatch'}
LIMITS = dict(max_provider_calls=0, max_cpu_seconds=60, max_gpu_seconds=60,
              max_storage_bytes=64*1024**2, max_memory_bytes=1024**3,
              currency=None, max_amount=None, policy_ref=None)


def _need(condition, code, message):
    require(condition, 'DIRECTOR_CAPABILITY_'+code, message, stage='capability')


def _diagnostic(code, operation, message):
    return dict(code=code, severity='error', stage='capability', entity_id=operation,
                field_path='capabilities', expected='executed and verified local capability',
                observed=message, retryable=False, recommended_action='Install/configure the required native owner, then explicitly reprobe.', evidence=[])


def _unknown_usage():
    return dict(classification='unavailable', **{k:{'value':None,'explanation':'Not measured by capability discovery.'}
                for k in ('provider_calls','input_tokens','output_tokens','cpu_seconds','gpu_seconds','storage_bytes','amount')},
                currency=None, measurement_source='director-local-probe-v1')


def _config(configuration):
    value = dict(configuration or {})
    _need(not set(value)-{'blender','ffmpeg','ffprobe','timeout_seconds','planning_provider'}, 'CONFIG', 'Unknown configuration field.')
    for name in ('blender','ffmpeg','ffprobe'):
        if name in value:
            _need(value[name] is None or isinstance(value[name],str), 'CONFIG', 'Executable must be a trusted local path or null.')
    timeout=value.get('timeout_seconds',45)
    _need(type(timeout) is int and 1 <= timeout <= 120,'CONFIG','Timeout must be one to 120 seconds.')
    provider=value.get('planning_provider')
    if provider is not None:
        _need(isinstance(provider,dict) and not set(provider)-{'id','configured','price_per_call','currency'},'CONFIG','Provider descriptor accepts no credentials or endpoints.')
        _need(isinstance(provider.get('id'),str) and provider['id'] and len(provider['id'])<=128,'CONFIG','Provider ID is required.')
        _need(type(provider.get('configured')) is bool,'CONFIG','Provider availability must be explicit.')
        price=provider.get('price_per_call')
        _need(price is None or type(price) in (int,float) and 0<=price<=1e9,'CONFIG','Invalid provider price.')
        _need(provider.get('currency') is None or isinstance(provider['currency'],str) and len(provider['currency'])==3,'CONFIG','Currency must be a three-letter code.')
        _need((price is None)==(provider.get('currency') is None),'CONFIG','Known price and currency must be supplied together.')
    return value


def _executable(name, config):
    selected=config.get(name, os.environ.get('HATCH_BLENDER') if name=='blender' else None)
    if name in config and selected is None:return None
    path=Path(selected) if selected else Path(shutil.which(name) or '/nonexistent-director-tool')
    try:
        resolved=path.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved,os.X_OK):return None
        return resolved
    except OSError:return None


def _worker(argv, directory, timeout):
    """Bound output/runtime; do not persist subprocess text or inherited secrets."""
    output=Path(directory)/'worker-output.txt'
    env={key:value for key,value in os.environ.items() if key in
         ('PATH','SYSTEMROOT','WINDIR','LD_LIBRARY_PATH','DISPLAY','CUDA_VISIBLE_DEVICES')}
    env.update(HOME=str(directory), TMPDIR=str(directory), PYTHONPATH=str(ROOT/'scripts'),
               PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    def limits():
        import resource
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024**2,16*1024**2))
        resource.setrlimit(resource.RLIMIT_CPU,(timeout,timeout+1))
    with output.open('wb') as stream:
        process=subprocess.Popen([str(a) for a in argv],cwd=directory,env=env,stdin=subprocess.DEVNULL,
                                 stdout=stream,stderr=subprocess.STDOUT,start_new_session=os.name!='nt',
                                 preexec_fn=limits if os.name=='posix' else None)
        started=time.monotonic();reason=None
        while process.poll() is None:
            if time.monotonic()-started>timeout:reason='timeout'
            elif output.stat().st_size>1024**2:reason='output-limit'
            if reason:
                if os.name=='posix':os.killpg(process.pid,signal.SIGKILL)
                else:process.kill()
                break
            time.sleep(.02)
        process.wait()
    raw=output.read_bytes()[:1024**2];output.unlink()
    if reason or process.returncode:
        return {'success':False,'reason':reason or 'worker-failed','exit_code':process.returncode},''
    return {'success':True,'reason':None,'exit_code':0},raw.decode('utf-8','replace')


def _blender_probe(executable, operation, directory, timeout):
    script=Path(directory)/'probe.py'
    gpu=operation=='blender.gpu'
    script.write_text('''import bpy,json
bpy.ops.wm.read_factory_settings(use_empty=True)
s=bpy.context.scene
s.render.engine='CYCLES'
s.cycles.samples=1
s.render.resolution_x=8;s.render.resolution_y=8;s.render.resolution_percentage=100
s.render.threads_mode='FIXED';s.render.threads=1
bpy.ops.mesh.primitive_cube_add()
bpy.ops.object.camera_add(location=(0,-5,0),rotation=(1.5707963267948966,0,0))
s.camera=bpy.context.object
bpy.ops.object.light_add(type='POINT',location=(0,-3,3));bpy.context.object.data.energy=100
''' + ('''prefs=bpy.context.preferences.addons['cycles'].preferences
found=False
for backend in ('CUDA','OPTIX','HIP','ONEAPI','METAL'):
 try:
  prefs.compute_device_type=backend;prefs.get_devices()
  devices=[d for d in prefs.devices if d.type!='CPU']
  if devices:
   for d in prefs.devices:d.use=d.type!='CPU'
   found=True;break
 except Exception:pass
if not found:raise RuntimeError('No usable GPU')
s.cycles.device='GPU'
''' if gpu else "s.cycles.device='CPU'\n") + '''s.render.filepath='probe.png'
bpy.ops.render.render(write_still=True)
print('DIRECTOR_PROBE='+json.dumps({'version':bpy.app.version_string,'rendered':True}))
''',encoding='utf-8')
    outcome,text=_worker([executable,'--background','--factory-startup','--disable-autoexec','--threads','1','--python',script],directory,timeout)
    if outcome['success']:
        from PIL import Image
        with Image.open(Path(directory)/'probe.png') as image:
            image.load();_need(image.size==(8,8),'DECODE','Probe raster dimensions differ.')
        marker=next((line.removeprefix('DIRECTOR_PROBE=') for line in text.splitlines() if line.startswith('DIRECTOR_PROBE=')),None)
        _need(marker is not None,'WORKER','Missing Blender result.')
        outcome.update(version=decode_json(marker)['version'],render_sha256=sha256(Path(directory)/'probe.png'),resolution=[8,8])
    return outcome


def _codec_probe(ffmpeg, ffprobe, operation, directory, timeout):
    codec={'codec.h264':('libx264','mp4'),'codec.vp9':('libvpx-vp9','webm'),'codec.av1':('libaom-av1','webm')}[operation]
    output=Path(directory)/('probe.'+codec[1])
    result,_=_worker([ffmpeg,'-v','error','-nostdin','-f','lavfi','-i','color=red:s=16x16:r=24',
        '-frames:v','1','-an','-c:v',codec[0],'-threads','1','-pix_fmt','yuv420p','-y',output],directory,timeout)
    if result['success']:
        verified,text=_worker([ffprobe,'-v','error','-count_frames','-show_streams','-of','json',output],directory,timeout)
        if not verified['success']:return verified
        streams=decode_json(text)['streams'];video=[s for s in streams if s['codec_type']=='video']
        _need(len(streams)==len(video)==1 and int(video[0]['nb_read_frames'])==1,'DECODE','Codec probe did not independently decode one silent frame.')
        result.update(codec=codec[0],decoded_frames=1,audio_streams=0,output_sha256=sha256(output))
    return result


def _import_probe(operation,directory,timeout):
    suffix=operation.split('.')[1]
    if suffix=='blend':return {'success':False,'reason':'native-blend-roundtrip-not-exercised'}
    # A real triangle in each supported interchange format, decoded by the native owner.
    source=Path(directory)/('triangle.'+suffix)
    if suffix=='obj':source.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n',encoding='ascii')
    else:
        import base64,struct
        binary=struct.pack('<9f3H',0,0,0,1,0,0,0,1,0,0,1,2)
        doc={'asset':{'version':'2.0'},'buffers':[{'byteLength':len(binary)}],
             'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':36},{'buffer':0,'byteOffset':36,'byteLength':6}],
             'accessors':[{'bufferView':0,'componentType':5126,'count':3,'type':'VEC3','min':[0,0,0],'max':[1,1,0]},
                          {'bufferView':1,'componentType':5123,'count':3,'type':'SCALAR'}],
             'meshes':[{'primitives':[{'attributes':{'POSITION':0},'indices':1}]}],
             'nodes':[{'mesh':0}],'scenes':[{'nodes':[0]}],'scene':0}
        if suffix=='gltf':
            doc['buffers'][0]['uri']='data:application/octet-stream;base64,'+base64.b64encode(binary).decode()
            source.write_text(json.dumps(doc),encoding='utf-8')
        else:
            data=json.dumps(doc).encode();data+=b' '*((-len(data))%4);binary+=b'\x00'*((-len(binary))%4)
            source.write_bytes(struct.pack('<4sII',b'glTF',2,12+8+len(data)+8+len(binary))+struct.pack('<II',len(data),0x4E4F534A)+data+struct.pack('<II',len(binary),0x004E4942)+binary)
    script=Path(directory)/'probe.py'
    script.write_text("from rig_import import load_character_model\nimport json\na=load_character_model("+repr(str(source))+ ")\nprint(json.dumps({'vertices':a.geometry.vertex_count,'triangles':a.geometry.triangle_count}))\n",encoding='utf-8')
    outcome,text=_worker([sys.executable,script],directory,timeout)
    if outcome['success']:
        value=decode_json(text)
        _need(value['vertices']==3 and value['triangles']==1,'GEOMETRY','Importer did not decode triangle geometry.')
        outcome.update(value,source_sha256=sha256(source))
    return outcome


def _reference(path, stage, dependencies=()):
    return dict(artifact_id='probe-'+path.stem,kind='director-capability-evidence',schema_version=1,
                semantic_version='1.0.0',path=path.relative_to(stage).as_posix(),sha256=sha256(path),
                byte_size=path.stat().st_size,media_type='application/json',provenance_ref=None,fixture=True,dependencies=list(dependencies))


def probe(request, evidence_root, *, configuration=None):
    """Run requested local smoke tests and atomically publish immutable evidence.

    configuration contains trusted executable paths and a public provider descriptor,
    never secrets. evidence_root must be a new directory. Provider calls are never made.
    """
    validate_method_request('CapabilityRegistry.probe',request)
    config=_config(configuration);ops=request['input']['operations'];allowed=request['input']['local_probes']
    _need(len(ops)==len(set(ops)) and len(allowed)==len(set(allowed)),'INPUT','Duplicate operations/probes.')
    _need(set(allowed)<=set(ops),'INPUT','Local probes must name requested operations.')
    destination=Path(evidence_root).absolute()
    _need(not destination.exists() and not destination.is_symlink(),'DESTINATION','Evidence destination must be new.')
    _need(destination.parent.is_dir() and destination.parent.resolve()==destination.parent,'DESTINATION','Evidence parent must be an existing canonical directory without symlinks.')
    stamp=datetime.now(timezone.utc)
    _need(datetime.fromisoformat(request['context']['deadline'].replace('Z','+00:00'))>stamp,'DEADLINE','Operation deadline has expired.')
    records=[];diagnostics=[];refs=[]
    with tempfile.TemporaryDirectory(prefix='.director-probe-',dir=destination.parent) as tmp:
        stage=Path(tmp)/'published';stage.mkdir();(stage/'receipts').mkdir();(stage/'payloads').mkdir()
        for operation in sorted(ops):
            _need(datetime.fromisoformat(request['context']['deadline'].replace('Z','+00:00'))>datetime.now(timezone.utc),'DEADLINE','Operation deadline expired during probing.')
            payloads=[]
            record={'schema_version':1,'operation':operation,'discovered':False,'initialized':False,'execution_verified':False,
                    'status':'unavailable','reason':'unsupported-operation','dependencies':{},'fallback':None}
            version=None;executable=None
            try:
                if operation.startswith('blender.') or operation=='importer.blend':
                    executable=_executable('blender',config)
                    record['discovered']=executable is not None;record['reason']='missing-blender' if not executable else 'execution-not-requested'
                elif operation.startswith('codec.'):
                    executable=_executable('ffmpeg',config);verifier=_executable('ffprobe',config)
                    record['discovered']=executable is not None and verifier is not None
                    record['reason']='missing-ffmpeg-or-ffprobe' if not record['discovered'] else 'execution-not-requested'
                    if verifier:record['dependencies']['ffprobe']=sha256(verifier)
                elif operation.startswith('importer.'):
                    record['discovered']=all(importlib.util.find_spec(name) is not None for name in ('numpy','pygltflib'))
                    record['reason']='missing-native-importer-dependency' if not record['discovered'] else 'execution-not-requested'
                elif operation in MODULES:
                    module=ROOT/'scripts'/(MODULES[operation]+'.py');record['discovered']=module.is_file()
                    record['reason']='missing-native-module' if not record['discovered'] else 'contract-module-only'
                    if module.is_file():record['dependencies']['native_module']=sha256(module)
                elif operation=='planning.provider':
                    provider=config.get('planning_provider')
                    record['discovered']=bool(provider and provider['configured'])
                    record['reason']='missing-planning-credentials' if not record['discovered'] else 'remote-execution-not-probed'
                    record['price_availability']='known' if provider and provider.get('price_per_call') is not None else 'unknown'
                    if provider:record['provider_id']=provider['id']
                    record['fallback']='structured-local-plan'  # This changes planning, never substitutes 2D for 3D.
                if executable:record['dependencies']['executable']=sha256(executable)
                if operation.startswith('importer.'):
                    record['dependencies'].update({p.name:sha256(p) for p in sorted((ROOT/'scripts').glob('rig_*.py'))})
                    for package in ('numpy','pygltflib'):
                        try:record['dependencies'][package]=importlib.metadata.version(package)
                        except importlib.metadata.PackageNotFoundError:pass
                if record['discovered']:
                    record['status']='untested'
                    if operation in allowed:
                        with tempfile.TemporaryDirectory(prefix='worker-',dir=tmp) as worker:
                            if operation in ('blender.cpu','blender.gpu'):outcome=_blender_probe(executable,operation,worker,config.get('timeout_seconds',45))
                            elif operation in ('codec.h264','codec.vp9','codec.av1'):outcome=_codec_probe(executable,verifier,operation,worker,config.get('timeout_seconds',45))
                            elif operation.startswith('importer.'):outcome=_import_probe(operation,worker,config.get('timeout_seconds',45))
                            else:outcome={'success':False,'reason':'native-execution-adapter-unavailable'}
                            if outcome['success']:
                                for original in sorted(Path(worker).glob('*')):
                                    if original.is_file() and original.suffix in ('.png','.mp4','.webm','.obj','.glb','.gltf'):
                                        target=stage/'payloads'/(sha256(original)+original.suffix)
                                        shutil.copyfile(original,target)
                                        payloads.append(_reference(target,stage))
                        record['initialized']=bool(outcome['success'])
                        record['execution_verified']=bool(outcome['success'])
                        record['status']='available' if outcome['success'] else 'unavailable'
                        record['reason']='executed' if outcome['success'] else outcome['reason'];record['execution']=outcome
                        version=outcome.get('version')
            except (OSError,ValueError,ImportError) as error:
                record.update(status='unavailable',reason='local-probe-failed')
                record['failure_type']=type(error).__name__ # no raw error/path/credentials
            if operation=='blender.gpu' and not record['execution_verified']:record['fallback']='blender.cpu-requires-separate-probe-and-explicit-policy'
            record['dependency_fingerprint']=object_hash(record['dependencies'])
            record['fixture']=True
            record['payloads']=payloads
            path=stage/'receipts'/(object_hash(record)+'.json');path.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n',encoding='utf-8')
            reference=_reference(path,stage,payloads);refs.append(reference)
            records.append(dict(operation=operation,version=version,status=record['status'],limits=deepcopy(LIMITS),
                                probe_evidence=[reference],execution_verified=record['execution_verified']))
            if record['status']!='available':diagnostics.append(_diagnostic('CAPABILITY_'+record['reason'].upper().replace('-','_'),operation,record['reason']))
        value=dict(capabilities=records,expires_at=(stamp+timedelta(hours=1)).isoformat(),digest='0'*64)
        value['digest']=object_hash({k:v for k,v in value.items() if k!='digest'})
        result=dict(schema_version=1,kind='director-result',method='CapabilityRegistry.probe',status='succeeded',
                    operation_id=request['context']['request_id'],input_digest=object_hash(request),value=value,
                    artifacts=refs,diagnostics=diagnostics,usage=_unknown_usage(),provenance_receipts=refs)
        validate_method_result('CapabilityRegistry.probe',result,request)
        (stage/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        _rename_no_replace(stage,destination)
    return result


def assert_admission(snapshot, required_operations, *, evidence_root=None, now=None,
                     resources=None, network_policy='deny', price_per_call=None,
                     budget_reservation_id=None, allow_unknown_price=False):
    """Fail closed for missing/stale/unexecuted native capabilities or resources."""
    if snapshot.get('kind')=='director-result':
        validate_method_result('CapabilityRegistry.probe',snapshot)
        value=snapshot['value'];_need(value is not None,'SNAPSHOT','Probe failed.')
    else:value=snapshot
    from jsonschema import Draft202012Validator
    schema=schemas()['director.schema.json']
    errors=list(Draft202012Validator({'$defs':schema['$defs'],'$ref':'#/$defs/output-capability-snapshot'}).iter_errors(value))
    _need(not errors,'SCHEMA','Invalid capability snapshot.')
    _need(isinstance(required_operations,(list,tuple)) and all(isinstance(x,str) for x in required_operations),'INPUT','Required operations must be a list of names.')
    _need(resources is None or isinstance(resources,dict),'RESOURCE','Resource estimates must be a map.')
    _need(value['digest']==object_hash({k:v for k,v in value.items() if k!='digest'}),'HASH','Capability snapshot digest changed.')
    current=now or datetime.now(timezone.utc)
    _need(datetime.fromisoformat(value['expires_at'].replace('Z','+00:00'))>current,'EXPIRED','Capability evidence expired.')
    by_op={item['operation']:item for item in value['capabilities']}
    _need(len(by_op)==len(value['capabilities']),'DUPLICATE','Duplicate capability entries.')
    for operation in required_operations:
        item=by_op.get(operation)
        _need(item is not None and item['status']=='available' and item['execution_verified'],'UNAVAILABLE','Required capability is unverified: '+operation)
        if evidence_root is not None:
            from director_contracts import _read_artifact
            for reference in item['probe_evidence']:
                evidence=decode_json(_read_artifact(evidence_root,reference))
                _need(evidence['operation']==operation and evidence['execution_verified'] and evidence['status']=='available','EVIDENCE','Probe evidence does not verify capability.')
                for payload in reference['dependencies']:_read_artifact(evidence_root,payload)
        for resource,amount in (resources or {}).items():
            _need(resource in LIMITS and type(amount) in (int,float) and amount>=0,'RESOURCE','Unknown or invalid resource estimate.')
            maximum=item['limits'][resource]
            _need(maximum is not None and amount<=maximum,'RESOURCE','Resource estimate exceeds capability bound: '+resource)
        if operation=='planning.provider':
            _need(network_policy=='allow-configured' and budget_reservation_id is not None,'POLICY','Remote planning requires configured network authority and a reservation.')
            _need(price_per_call is not None or allow_unknown_price,'PRICE','Unknown provider price requires explicit authority.')
    return deepcopy(value)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',type=Path,required=True);parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--blender');parser.add_argument('--ffmpeg');parser.add_argument('--ffprobe')
    args=parser.parse_args()
    try:
        request=decode_json(args.request.read_bytes())
        config={k:getattr(args,k) for k in ('blender','ffmpeg','ffprobe') if getattr(args,k) is not None}
        result=probe(request,args.destination,configuration=config)
        print(json.dumps(result));return 0
    except (RigError,OSError) as exc:
        print(json.dumps({'status':'failed','diagnostic':exc.report if isinstance(exc,RigError) else {'code':'DIRECTOR_CAPABILITY_IO'}}));return 1


if __name__=='__main__':raise SystemExit(main())
