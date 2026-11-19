"""Evidence-backed local Director admission; never invokes a remote provider.

Discovery, initialization and executed smoke tests are different evidence states.
Only explicitly requested local probes may execute trusted, fixed worker code.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
from io import BytesIO
import importlib.metadata
import importlib.util
import json
import math
import os
import re
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

from contextlib import ExitStack, contextmanager
import atomic_publish
from director_contracts import validate_method_request, validate_method_result, validate_document
from director_schema import schemas
from rig_errors import RigError, decode_json, object_hash, require, sha256
import director_inventory as inventory

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ('blender.cpu', 'blender.gpu', 'codec.h264', 'codec.vp9', 'codec.av1',
              'importer.obj', 'importer.glb', 'importer.gltf', 'importer.blend',
              'native.acquisition', 'planning.provider', 'module.scene3d',
              'module.camera3d', 'module.lookdev3d', 'module.animation3d',
              'module.render3d', 'module.editor3d', *inventory.OPERATIONS)
MODULES = {'module.scene3d':'scene3d_contracts', 'module.camera3d':'camera3d_contracts',
           'module.lookdev3d':'lookdev_contracts', 'module.animation3d':'animation3d_contracts',
           'module.render3d':'render3d_contracts', 'module.editor3d':'editor3d_contracts',
           'native.acquisition':'hatch', **inventory.MODULES}
LIMITS = dict(max_provider_calls=0, max_cpu_seconds=3600, max_gpu_seconds=3600,
              max_storage_bytes=1024**3, max_memory_bytes=1536*1024**2,
              currency=None, max_amount=None, policy_ref=None)
REMOTE_OPERATIONS = frozenset(('planning.provider', *inventory.PROVIDERS))


def _need(condition, code, message):
    require(condition, 'DIRECTOR_CAPABILITY_'+code, message, stage='capability')


def _diagnostic(code, operation, message):
    return dict(code=code, severity='error', stage='capability', entity_id=operation,
                field_path='capabilities', expected='executed and verified local capability',
                observed=message, retryable=False, recommended_action='Install/configure the required native owner, then explicitly reprobe.', evidence=[])


def _date(value):
    try:
        parsed=datetime.fromisoformat(value.replace('t','T').replace('z','+00:00').replace('Z','+00:00'))
        _need(parsed.tzinfo is not None,'TIME','Timestamp must include a timezone.')
        return parsed
    except (TypeError,ValueError,AttributeError) as error:
        raise RigError('DIRECTOR_CAPABILITY_TIME','Invalid timezone-aware timestamp.',stage='capability') from error


def _unknown_usage():
    return dict(classification='unavailable', **{k:{'value':None,'explanation':'Not measured by capability discovery.'}
                for k in ('provider_calls','input_tokens','output_tokens','cpu_seconds','gpu_seconds','storage_bytes','amount')},
                currency=None, measurement_source='director-local-probe-v1')


def _valid_provider_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value) is not None


def _valid_currency(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Z]{3}', value) is not None


def _config(configuration):
    value = dict(configuration or {})
    _need(not set(value)-{'blender','ffmpeg','ffprobe','timeout_seconds','planning_provider','production_limits',
                         *inventory.PROVIDERS.values()}, 'CONFIG', 'Unknown configuration field.')
    for name in ('blender','ffmpeg','ffprobe'):
        if name in value:
            _need(value[name] is None or isinstance(value[name],str), 'CONFIG', 'Executable must be a trusted local path or null.')
    timeout=value.get('timeout_seconds',45)
    _need(type(timeout) is int and 1 <= timeout <= 120,'CONFIG','Timeout must be one to 120 seconds.')
    limits=value.get('production_limits',{})
    _need(isinstance(limits,dict) and not set(limits)-{'max_cpu_seconds','max_gpu_seconds','max_storage_bytes','max_memory_bytes'},'CONFIG','Unknown production resource limit.')
    for name,amount in limits.items():
        _need(type(amount) is int and 1<=amount<=2_000_000_000,'CONFIG','Production limits must be positive bounded integers.')
    for field in ('planning_provider', *inventory.PROVIDERS.values()):
        provider=value.get(field)
        if provider is None:
            continue
        _need(isinstance(provider,dict) and not set(provider)-{'id','configured','price_per_call','currency'},'CONFIG','Provider descriptor accepts no credentials or endpoints.')
        _need(_valid_provider_id(provider.get('id')), 'CONFIG', 'Provider descriptor requires a valid public ID.')
        _need(type(provider.get('configured')) is bool,'CONFIG','Provider availability must be explicit.')
        price=provider.get('price_per_call')
        _need(price is None or type(price) in (int,float) and 0<=price<=1e9,'CONFIG','Invalid provider price.')
        _need(provider.get('currency') is None or _valid_currency(provider['currency']),
              'CONFIG', 'Provider descriptor currency must contain three uppercase ASCII letters.')
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


def _native_importer_discovered():
    return all(importlib.util.find_spec(name) is not None for name in ('numpy', 'pygltflib'))


@contextmanager
def _posix_completion(process):
    """Observe exit without reaping the session leader or releasing its PID."""
    if hasattr(os, 'waitid'):
        def finished():
            return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        yield finished
    else:
        # CPython 3.12 on macOS has kqueue but does not expose waitid.
        import select
        with select.kqueue() as queue:
            event = select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)
            try:
                queue.control([event], 0)
            except ProcessLookupError:
                # A fast child exited before registration. It is still waitable.
                yield lambda: True
            else:
                yield lambda: bool(queue.control(None, 1, 0))


def _stop_group(process):
    # No poll()/wait() has reaped the session leader. Its PGID cannot be reused
    # for an unrelated group, including when the leader has already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline=time.monotonic()+5
    process.wait(timeout=5)
    # After reaping, only inspect the PGID. Never signal it again. Orphaned
    # zombies may remain in the group but cannot write to the worker scratch.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    group=str(process.pid).encode()
    while time.monotonic()<deadline:
        states=subprocess.run(['/bin/ps','-A','-o','pgid=,stat='],capture_output=True,
                              env={'LC_ALL':'C'},timeout=max(.01,deadline-time.monotonic()))
        if states.returncode or states.stderr:
            raise OSError('Could not inspect the stopped worker group.')
        rows=[line.split() for line in states.stdout.splitlines()]
        if not rows or any(len(row)!=2 or not row[0].isdigit() for row in rows):
            raise OSError('Invalid worker group status.')
        if not any(pgid==group and not state.startswith(b'Z') for pgid,state in rows):
            return
        time.sleep(.01)
    raise OSError('Worker group termination did not complete.')


def _worker(argv, directory, timeout):
    """Bound output/runtime; do not persist subprocess text or inherited secrets."""
    env={key:value for key,value in os.environ.items() if key in
         ('PATH','SYSTEMROOT','WINDIR','LD_LIBRARY_PATH','DISPLAY','CUDA_VISIBLE_DEVICES')}
    env.update(PYTHONPATH=str(ROOT/'scripts'),
               PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    def limits():
        import resource
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024**2,16*1024**2))
        resource.setrlimit(resource.RLIMIT_CPU,(timeout,timeout+1))
    process=None;reason=None
    deadline=time.monotonic()+timeout
    try:
        with tempfile.TemporaryDirectory(prefix='director-worker-env-') as scratch, \
                tempfile.TemporaryFile(dir=scratch) as stream, ExitStack() as cleanup:
            # Keep native caches and transient files outside the fixed output layout.
            env.update(HOME=scratch, TMPDIR=scratch, TEMP=scratch, TMP=scratch)
            if os.name=='nt':
                env.update(USERPROFILE=scratch, APPDATA=scratch, LOCALAPPDATA=scratch)
            if os.name=='nt':
                from director_worker_windows import WindowsJob
                try:
                    job=cleanup.enter_context(WindowsJob())
                    process=job.start([str(a) for a in argv],directory,env,stream,deadline)
                    finished=lambda: process.poll() is not None
                except OSError:
                    reason='worker-containment-failed'
            elif os.name=='posix':
                process=subprocess.Popen([str(a) for a in argv],cwd=directory,env=env,stdin=subprocess.DEVNULL,
                                         stdout=stream,stderr=subprocess.STDOUT,start_new_session=True,
                                         preexec_fn=limits)
                cleanup.callback(_stop_group, process)
                finished=cleanup.enter_context(_posix_completion(process))
            else:
                reason='worker-containment-failed'
            if process is not None:
                while not finished():
                    if time.monotonic()>deadline:reason='timeout'
                    elif os.fstat(stream.fileno()).st_size>1024**2:reason='output-limit'
                    if reason:
                        break
                    time.sleep(.02)
            cleanup.close()
            if reason is None and os.fstat(stream.fileno()).st_size>1024**2:reason='output-limit'
            stream.seek(0)
            raw=stream.read(1024**2)
    except (OSError,subprocess.TimeoutExpired):
        reason='worker-containment-failed';raw=b''
    exit_code=process.returncode if process is not None else None
    if reason or exit_code:
        return {'success':False,'reason':reason or 'worker-failed','exit_code':exit_code},''
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
if not found:
 open('failure.json','w').write(json.dumps({'reason':'gpu-unavailable'}))
 raise RuntimeError('No usable GPU')
s.cycles.device='GPU'
''' if gpu else "s.cycles.device='CPU'\n") + '''s.render.filepath='probe.png'
bpy.ops.render.render(write_still=True)
print('DIRECTOR_PROBE='+json.dumps({'version':bpy.app.version_string,'rendered':True,'device':s.cycles.device}))
''',encoding='utf-8')
    outcome,text=_worker([executable,'--background','--factory-startup','--disable-autoexec',
                          '--threads','1','--python-exit-code','1','--python',script],directory,timeout)
    if outcome['success']:
        from PIL import Image
        payload=_worker_bytes(Path(directory)/'probe.png')
        with Image.open(BytesIO(payload)) as image:
            _need(image.format=='PNG' and image.size==(8,8),'DECODE','Probe raster format or dimensions differ.')
            image.load()
        marker=_marker(text, {'version', 'rendered', 'device'})
        _need(_release_version(marker['version']) and marker['rendered'] is True and
              marker['device']==('GPU' if gpu else 'CPU'),'WORKER','Invalid Blender render result.')
        outcome.update(version=marker['version'],device=marker['device'],
                       render_sha256=hashlib.sha256(payload).hexdigest(),resolution=[8,8])
    elif gpu and outcome['reason']=='worker-failed' and (Path(directory)/'failure.json').exists():
        failure=decode_json(_worker_bytes(Path(directory)/'failure.json', limit=1024))
        if failure=={'reason':'gpu-unavailable'}:outcome['reason']='gpu-unavailable'
    return outcome


def _codec_probe(ffmpeg, ffprobe, operation, directory, timeout):
    codec={'codec.h264':('libx264','mp4'),'codec.vp9':('libvpx-vp9','webm'),'codec.av1':('libaom-av1','webm')}[operation]
    output=Path(directory)/('probe.'+codec[1])
    result,_=_worker([ffmpeg,'-v','error','-nostdin','-f','lavfi','-i','color=red:s=16x16:r=24',
        '-frames:v','1','-an','-c:v',codec[0],'-threads','1','-pix_fmt','yuv420p','-y',output],directory,timeout)
    if result['success']:
        payload=_worker_bytes(output)
        verified,text=_worker([ffprobe,'-v','error','-count_frames','-show_streams','-of','json',output],directory,timeout)
        if not verified['success']:return verified
        report=decode_json(text)
        streams=report.get('streams') if isinstance(report,dict) else None
        expected_codec=operation.split('.')[1]
        _need(isinstance(streams,list) and len(streams)==1 and isinstance(streams[0],dict)
              and streams[0].get('codec_type')=='video' and streams[0].get('codec_name')==expected_codec
              and str(streams[0].get('nb_read_frames'))=='1',
              'DECODE','Codec probe did not independently decode one silent frame of the selected codec.')
        result.update(codec=codec[0],decoded_frames=1,audio_streams=0,output_sha256=hashlib.sha256(payload).hexdigest())
    return result


def _release_version(value):
    return isinstance(value,str) and re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}(?: LTS)?',value) is not None


def _marker(text, fields):
    markers=[line.removeprefix('DIRECTOR_PROBE=') for line in text.splitlines()
             if line.startswith('DIRECTOR_PROBE=')]
    _need(len(markers)==1,'WORKER','Expected exactly one worker result.')
    value=decode_json(markers[0],max_bytes=4096)
    _need(isinstance(value,dict) and set(value)==fields,'WORKER','Unexpected worker result fields.')
    return value


def _blend_import_probe(executable, directory, timeout):
    """Blender authors one triangle; the native importer decodes it via rig_blender."""
    directory=Path(directory)
    author=directory/'author.py';author.write_text(inventory.BLEND_AUTHOR,encoding='utf-8')
    outcome,text=_worker([executable,'--background','--factory-startup','--disable-autoexec','--threads','1',
                          '--python-exit-code','1','--python',author],directory,timeout)
    if not outcome['success']:return _phase_failure(outcome,'blend-authoring-failed')
    _check_outputs(directory, {'author.py','triangle.blend'})
    author_version=_marker(text,{'version'})['version']
    _need(_release_version(author_version),'WORKER','Invalid Blender author version.')
    source=directory/'triangle.blend'
    source_hash=hashlib.sha256(_worker_bytes(source)).hexdigest()
    reader=directory/'probe.py'
    with reader.open('x',encoding='utf-8') as script:
        script.write(inventory.blend_reader(executable))
    outcome,text=_worker([sys.executable,reader],directory,timeout)
    if not outcome['success']:return _phase_failure(outcome,'native-blend-import-failed')
    value=_marker(text,{'vertices','triangles','importing_blender_version'})
    _triangle(value)
    importer_version=value.get('importing_blender_version')
    _need(_release_version(importer_version),'WORKER','Invalid Blender importer version.')
    outcome.update(vertices=3,triangles=1,authoring_blender_version=author_version,
                   importing_blender_version=importer_version,
                   source_sha256=source_hash,importer='rig_import.load_character_model',formats=['BLEND'])
    return outcome


def _phase_failure(outcome, reason):
    if outcome['reason']=='worker-containment-failed':
        return outcome
    return {**outcome,'reason':reason}


def _init_probe(operation, directory, timeout):
    """Fresh-interpreter import plus one dry constructor; never execution evidence."""
    script=Path(directory)/'probe.py'
    script.write_text(inventory.initializer_script(operation,MODULES[operation]),encoding='utf-8')
    outcome,text=_worker([sys.executable,script],directory,timeout)
    if not outcome['success']:
        return _phase_failure(outcome,'initialization-timeout' if outcome['reason']=='timeout' else 'initialization-failed')
    acquisition=operation=='native.acquisition'
    fields={'model_call','generation_backend'} if acquisition else {'schemas_checked'}
    value=_marker(text,fields|{'success','initialization_only','executed','version','call'})
    _need(value['success'] is True and value['initialization_only'] is True and value['executed'] is False
          and (value['version'] is None or _release_version(value['version'])),'WORKER','Invalid initialization result.')
    if acquisition:
        valid=(value['call']=='asset_contracts.versions' and value['model_call'] is None
               and value['generation_backend']=='installed-imagegen-not-invoked')
    else:
        valid=(value['call']==MODULES[operation]+'.schemas' and type(value['schemas_checked']) is int
               and 0<value['schemas_checked']<=4096)
    _need(valid,'WORKER','Invalid initialization details.')
    return {**outcome,**value}


def _triangle(value):
    _need(type(value['vertices']) is int and value['vertices']==3 and
          type(value['triangles']) is int and value['triangles']==1,
          'GEOMETRY','Importer did not decode triangle geometry.')


def _import_probe(operation,directory,timeout):
    suffix=operation.split('.')[1]
    _need(suffix!='blend','INPUT','Blender project import uses its own probe.')
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
    source_hash=sha256(source)
    script=Path(directory)/'probe.py'
    script.write_text("from rig_import import load_character_model\nimport json\na=load_character_model("+repr(str(source))+ ")\nprint(json.dumps({'vertices':a.geometry.vertex_count,'triangles':a.geometry.triangle_count}))\n",encoding='utf-8')
    outcome,text=_worker([sys.executable,script],directory,timeout)
    if outcome['success']:
        value=decode_json(text,max_bytes=1024)
        _need(isinstance(value,dict) and set(value)=={'vertices','triangles'},'WORKER','Unexpected importer result fields.')
        _triangle(value)
        outcome.update(vertices=3,triangles=1,version='native-rig-v1',source_sha256=source_hash)
    return outcome


def _inventory_probe(operation, executable, directory, timeout, usage):
    if operation in ('runtime.python', 'resource.memory', 'resource.storage'):
        return inventory.measure(operation, usage)
    script = inventory.worker_script(operation)
    if script is None:
        return {'success': False, 'reason': 'native-execution-adapter-unavailable'}
    path = Path(directory)/'probe.py'
    path.write_text(script, encoding='utf-8')
    argv = ([executable, '--background', '--factory-startup', '--disable-autoexec',
             '--threads', '1', '--python-exit-code', '1', '--python', path] if operation == 'blender.engines'
            else [sys.executable, path])
    outcome, text = _worker(argv, directory, timeout)
    if not outcome['success']:
        return outcome
    fields={'blender.engines':{'engines','rendered'},'format.image':{'formats','decoded_pixels'},
            'procedural.native':{'vertices','triangles','formats','scope'}}
    value=_marker(text,fields[operation]|{'success','version'})
    _need(value['success'] is True,'WORKER','Inventory worker did not confirm success.')
    if operation=='blender.engines':
        engines=value['engines']
        valid=(_release_version(value['version']) and value['rendered'] is False and
               isinstance(engines,list) and 0<len(engines)<=32 and
               all(isinstance(name,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',name) for name in engines)
               and engines==sorted(set(engines)))
    elif operation=='format.image':
        valid=(_release_version(value['version']) and value['formats']==['PNG'] and
               type(value['decoded_pixels']) is int and value['decoded_pixels']==4)
    else:
        valid=(value['version']=='native-rig-v1' and value['formats']==['GLB'] and
               value['scope']=='procedural-robotic-fixture-only' and
               all(type(value[key]) is int and 0<value[key]<=100000 for key in ('vertices','triangles')))
    _need(valid,'WORKER','Invalid operation-specific inventory result.')
    if operation=='procedural.native':
        _check_outputs(Path(directory), {'probe.py','probe.glb'})
        # Decode with the native importer inside the same process/time bounds as
        # other probes. The verifier reports the digest of the bytes it decoded.
        check,report=_worker([sys.executable,'-P','-c',
                              'from director_capabilities import _verify_robot; _verify_robot()'],
                             directory,timeout)
        if not check['success']:
            return check
        geometry=_marker(report,{'vertices','triangles','output_sha256'})
        _need(all(type(geometry[k]) is int and geometry[k]==value[k] for k in ('vertices','triangles'))
              and isinstance(geometry['output_sha256'],str) and
              re.fullmatch('[0-9a-f]{64}',geometry['output_sha256']),
              'GEOMETRY','Procedural GLB geometry differs from worker evidence.')
        value['output_sha256']=geometry['output_sha256']
    return {**outcome, **value}


PAYLOAD_TYPES={'.png':'image/png','.mp4':'video/mp4','.webm':'video/webm','.obj':'text/plain',
               '.glb':'model/gltf-binary','.gltf':'model/gltf+json','.blend':'application/x-blender'}
WORKER_PAYLOADS={'blender.cpu':'probe.png','blender.gpu':'probe.png','format.image':'probe.png',
                 'procedural.native':'probe.glb','codec.h264':'probe.mp4','codec.vp9':'probe.webm',
                 'codec.av1':'probe.webm',**{'importer.'+ext:'triangle.'+ext for ext in ('glb','gltf','obj','blend')}}


def _worker_file(path):
    info=path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and
          not getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT,
          'WORKER','Worker output must be an ordinary file owned by the probe.')


def _worker_bytes(path, limit=16*1024**2):
    from continuity_contracts import _read_regular_bytes
    _worker_file(path)
    data=_read_regular_bytes(path,limit=limit)
    _need(data,'WORKER','Worker output is empty.')
    return data


def _verify_robot():
    from rig_import import load_character_model
    path=Path.cwd()/'probe.glb'
    data=_worker_bytes(path)
    def read_source(source, limit):
        _need(source==path and len(data)<=limit,'GEOMETRY','Expected a self-contained procedural GLB.')
        return data
    geometry=load_character_model(path,source_reader=read_source).geometry
    print('DIRECTOR_PROBE='+json.dumps(dict(vertices=geometry.vertex_count,triangles=geometry.triangle_count,
                                            output_sha256=hashlib.sha256(data).hexdigest())))


def _check_outputs(root, expected):
    for path in root.iterdir():
        _need(path.name in expected,'WORKER','Unexpected worker output.')
        expected.remove(path.name)
        _worker_file(path)
    _need(not expected,'WORKER','Required worker output is missing.')


def _retain_outputs(operation, worker, outcome, stage):
    """Validate the complete successful scratch layout before retaining any bytes."""
    name=WORKER_PAYLOADS.get(operation)
    expected={name} if name else set()
    if operation in inventory.INITIALIZED or operation in ('format.image','procedural.native','blender.engines',
                                                           'blender.cpu','blender.gpu') or operation.startswith('importer.'):
        expected.add('probe.py')
    if operation=='importer.blend':
        expected.add('author.py')
    root=Path(worker)
    _check_outputs(root, expected)
    if name is None:
        return []
    path=root/name
    data=_worker_bytes(path)
    digest=hashlib.sha256(data).hexdigest()
    if operation=='format.image':
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            _need(image.format=='PNG' and image.size==(2,2) and image.mode=='RGBA' and
                  image.tobytes()==bytes((12,34,56,78))*4,'DECODE','Image fixture pixels differ.')
    else:
        hash_field=('source_sha256' if operation.startswith('importer.') else
                    'render_sha256' if operation.startswith('blender.') else 'output_sha256')
        _need(digest==outcome[hash_field],'HASH','Verified worker output changed.')
    target=stage/'payloads'/(digest+path.suffix)
    target.write_bytes(data)
    return [_reference(target,stage)]


def _reference(path, stage, dependencies=()):
    return dict(artifact_id='probe-'+path.stem,kind='capability-probe-evidence',schema_version=1,
                semantic_version='1.0.0',path=path.relative_to(stage).as_posix(),sha256=sha256(path),
                byte_size=path.stat().st_size,media_type=PAYLOAD_TYPES.get(path.suffix,'application/json'),provenance_ref=None,fixture=True,dependencies=list(dependencies))


class _Stage:
    """Evidence written at ``path``; ``publish_to`` exclusively renames it."""
    def __init__(self, path, usage, publish):
        self.path=path;self.usage=usage;self._publish=publish

    def publish_to(self, destination):
        self._publish(Path(destination))


def _rename_no_replace(stage, destination):
    """Exclusive publication of a pinned stage; never replaces a racing entry."""
    stage.publish_to(destination)


def _same_parent(parent, parent_fd, pin):
    """Refuse when the public parent pathname no longer names the pinned directory."""
    try:
        with pin(parent) as current:
            same=os.path.samestat(os.fstat(current),os.fstat(parent_fd))
    except (OSError,ValueError) as error:
        raise RigError('DIRECTOR_CAPABILITY_DESTINATION','Evidence parent no longer named the pinned directory at the prepublication check.',stage='capability') from error
    _need(same,'DESTINATION','Evidence parent no longer named the pinned directory at the prepublication check.')


def _copy_tree_fd(source, parent_fd, name, depth=0):
    """Copy the bounded build tree into a pinned private stage by descriptor."""
    _need(depth<=2,'DESTINATION','Evidence tree is deeper than its fixed layout.')
    os.mkdir(name,0o777,dir_fd=parent_fd)
    descriptor=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent_fd)
    try:
        for child in sorted(Path(source).iterdir()):
            if child.is_dir() and not child.is_symlink():
                _copy_tree_fd(child,descriptor,child.name,depth+1);continue
            _need(child.is_file() and not child.is_symlink(),'DESTINATION','Evidence build tree holds an unsupported entry.')
            data=child.read_bytes()
            with os.fdopen(os.open(child.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o666,dir_fd=descriptor),'wb') as stream:
                stream.write(data)
            with os.fdopen(os.open(child.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=descriptor),'rb') as stream:
                _need(stream.read(len(data)+1)==data,'HASH','Staged evidence bytes changed before publication.')
    finally:
        os.close(descriptor)


def _disk(path):
    usage=shutil.disk_usage(path);return usage.free,usage.total


def _destination_parent(parent):
    message = 'Evidence parent must be an existing canonical directory without symlinks.'
    try:
        identity = parent.stat(follow_symlinks=False)
        canonical = parent.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise RigError('DIRECTOR_CAPABILITY_DESTINATION', message, stage='capability') from error
    _need(stat.S_ISDIR(identity.st_mode) and canonical == parent, 'DESTINATION', message)
    return identity


@contextmanager
def _checked_parent(parent, expected, pin, inspect_pin):
    message = 'Evidence parent changed before staging.'
    with ExitStack() as stack:
        try:
            authority = stack.enter_context(pin(parent))
            pinned_identity = inspect_pin(authority)
        except (OSError, ValueError) as error:
            raise RigError('DIRECTOR_CAPABILITY_DESTINATION', message, stage='capability') from error
        _need(os.path.samestat(expected, pinned_identity), 'DESTINATION', message)
        yield authority


@contextmanager
def _evidence_stage(destination, expected_parent):
    """Pin the evidence parent before any write and publish only into that directory.

    Linux uses the procfd private stage, other descriptor-capable POSIX hosts a
    descriptor-only stage, and Windows the handle-pinned adapter. Hosts without
    one of these fail closed rather than fall back to an unpinned pathname.
    """
    parent=destination.parent
    if os.name=='nt':
        _need(atomic_publish.windows_private_stage_supported(),'PLATFORM','Pinned Windows evidence publication is unavailable.')
        from atomic_windows import pinned_directory as windows_pin
        with _checked_parent(parent, expected_parent, windows_pin,
                             lambda pin: pin.path.stat(follow_symlinks=False)) as parent_pin, \
                atomic_publish.windows_private_stage(parent_pin.path, prefix='.director-probe-') as stage:
            yield _Stage(stage.path,lambda:_disk(stage.path),stage.publish_to)
        return
    if atomic_publish.private_stage_supported():
        pin=atomic_publish.pinned_directory
        with _checked_parent(parent, expected_parent, pin, os.fstat) as parent_fd, \
                atomic_publish.private_stage(parent_fd, prefix='.director-probe-') as (wrapper, _):
            pinned=Path('/proc/self/fd')/str(parent_fd);payload=wrapper/'published';payload.mkdir()
            def publish(target):
                _same_parent(parent,parent_fd,pin)
                atomic_publish._rename_no_replace(payload,pinned/target.name)
            yield _Stage(payload,lambda:_disk(pinned),publish)
        return
    _need(atomic_publish.private_stage_fd_supported(),'PLATFORM','Pinned evidence publication is unavailable on this platform.')
    pin=atomic_publish.pinned_directory_fd
    with _checked_parent(parent, expected_parent, pin, os.fstat) as parent_fd, \
            atomic_publish.private_stage_fd(parent_fd, prefix='.director-probe-') as stage_fd, \
            tempfile.TemporaryDirectory(prefix='director-build-') as build:
        payload=Path(build)/'published';payload.mkdir()
        def publish(target):
            _copy_tree_fd(payload,stage_fd,'published')
            _same_parent(parent,parent_fd,pin)
            atomic_publish._rename_no_replace('published',target.name,source_dir_fd=stage_fd,destination_dir_fd=parent_fd)
        def usage():
            state=os.statvfs(parent_fd);return state.f_bavail*state.f_frsize,state.f_blocks*state.f_frsize
        yield _Stage(payload,usage,publish)


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
    expected_parent = _destination_parent(destination.parent)
    _need(not destination.exists() and not destination.is_symlink(),'DESTINATION','Evidence destination must be new.')
    stamp=datetime.now(timezone.utc)
    _need(_date(request['context']['deadline'])>stamp,'DEADLINE','Operation deadline has expired.')
    records=[];diagnostics=[];refs=[]
    with _evidence_stage(destination, expected_parent) as pinned_stage:
        stage=pinned_stage.path;(stage/'receipts').mkdir();(stage/'payloads').mkdir()
        for operation in sorted(ops):
            _need(_date(request['context']['deadline'])>datetime.now(timezone.utc),'DEADLINE','Operation deadline expired during probing.')
            payloads=[]
            record={'schema_version':1,'operation':operation,'discovered':False,'initialized':False,'execution_verified':False,
                    'status':'unavailable','reason':'unsupported-operation','dependencies':{'probe_source':sha256(Path(__file__))},'fallback':None}
            record.update(inventory.metadata(operation))
            record['dependencies']['inventory_source'] = sha256(Path(inventory.__file__))
            version=None;executable=None
            try:
                if operation in ('runtime.python', 'resource.memory', 'resource.storage'):
                    record['discovered'] = True
                    record['reason'] = 'measurement-not-requested'
                    record['dependencies']['python'] = sha256(Path(sys.executable).resolve())
                    record['dependencies']['python_version'] = sys.version
                elif operation == 'format.image':
                    record['discovered'] = importlib.util.find_spec('PIL') is not None
                    record['reason'] = 'execution-not-requested' if record['discovered'] else 'missing-pillow'
                elif operation.startswith('blender.') or operation=='importer.blend':
                    executable=_executable('blender',config)
                    record['discovered']=executable is not None;record['reason']='missing-blender' if not executable else 'execution-not-requested'
                    if executable and operation=='importer.blend' and not _native_importer_discovered():
                        record['discovered']=False;record['reason']='missing-native-importer-dependency'
                elif operation.startswith('codec.'):
                    executable=_executable('ffmpeg',config);verifier=_executable('ffprobe',config)
                    record['discovered']=executable is not None and verifier is not None
                    record['reason']='missing-ffmpeg-or-ffprobe' if not record['discovered'] else 'execution-not-requested'
                    if verifier:record['dependencies']['ffprobe']=sha256(verifier)
                elif operation.startswith('importer.'):
                    record['discovered']=_native_importer_discovered()
                    record['reason']='missing-native-importer-dependency' if not record['discovered'] else 'execution-not-requested'
                elif operation in MODULES:
                    module=ROOT/'scripts'/(MODULES[operation]+'.py');record['discovered']=module.is_file()
                    record['reason']='missing-native-module' if not record['discovered'] else (
                        'initialization-not-requested' if operation in inventory.INITIALIZED else 'execution-not-requested')
                    if module.is_file():record['dependencies']['native_module']=sha256(module)
                    if operation in inventory.INITIALIZED:
                        for package in ('jsonschema','Pillow'):
                            try:record['dependencies'][package]=importlib.metadata.version(package)
                            except importlib.metadata.PackageNotFoundError:pass
                elif operation=='planning.provider' or operation in inventory.PROVIDERS:
                    provider=config.get(inventory.PROVIDERS.get(operation, 'planning_provider'))
                    record['discovered']=bool(provider and provider['configured'])
                    missing = 'missing-planning-credentials' if operation=='planning.provider' else 'provider-not-configured'
                    record['reason']=missing if not record['discovered'] else 'remote-execution-not-probed'
                    record['price_availability']='known' if provider and provider.get('price_per_call') is not None else 'unknown'
                    if provider:
                        record.update(provider_id=provider['id'],price_per_call=provider.get('price_per_call'),
                                      currency=provider.get('currency'))
                    record['fallback']='structured-local-plan' if operation=='planning.provider' else None
                if operation in ('format.image', 'procedural.native'):
                    record['dependencies']['python'] = sha256(Path(sys.executable).resolve())
                    record['dependencies']['python_version'] = sys.version
                if operation == 'format.image' and record['discovered']:
                    try:record['dependencies']['Pillow'] = importlib.metadata.version('Pillow')
                    except importlib.metadata.PackageNotFoundError:pass
                if executable:record['dependencies']['executable']=sha256(executable)
                if operation.startswith('importer.') or operation in inventory.MODULES:
                    record['dependencies'].update({p.name:sha256(p) for p in sorted((ROOT/'scripts').glob('rig_*.py'))})
                    for package in ('numpy','pygltflib'):
                        try:record['dependencies'][package]=importlib.metadata.version(package)
                        except importlib.metadata.PackageNotFoundError:pass
                if record['discovered']:
                    record['status']='untested'
                    if operation in allowed:
                        # Worker scratch stays outside the pinned stage: subprocesses
                        # never receive a procfd pathname or the stage descriptor.
                        timeout=config.get('timeout_seconds',45)
                        with tempfile.TemporaryDirectory(prefix='director-worker-') as worker:
                            if operation in inventory.INITIALIZED:outcome=_init_probe(operation,worker,timeout)
                            elif operation in inventory.OPERATIONS:outcome=_inventory_probe(operation,executable,worker,timeout,pinned_stage.usage)
                            elif operation in ('blender.cpu','blender.gpu'):outcome=_blender_probe(executable,operation,worker,timeout)
                            elif operation in ('codec.h264','codec.vp9','codec.av1'):outcome=_codec_probe(executable,verifier,operation,worker,timeout)
                            elif operation=='importer.blend':outcome=_blend_import_probe(executable,worker,timeout)
                            elif operation.startswith('importer.'):outcome=_import_probe(operation,worker,timeout)
                            else:outcome={'success':False,'reason':'native-execution-adapter-unavailable'}
                            initialized=bool(outcome['success'])
                            executed=initialized and not outcome.get('initialization_only',False)
                            if initialized:
                                payloads=_retain_outputs(operation,worker,outcome,stage)
                        # Initialization is reported separately; it never admits work.
                        record['initialized']=initialized
                        record['execution_verified']=executed
                        record['status']='available' if executed else 'untested' if initialized else 'unavailable'
                        record['reason']='executed' if executed else 'initialized-not-executed' if initialized else outcome['reason']
                        record['execution']=outcome
                        version=outcome.get('version')
                        if executed:
                            record['supported_formats'] = outcome.get('formats', record['supported_formats'])
            except (OSError,ValueError,ImportError) as error:
                record.update(status='unavailable',reason='local-probe-failed')
                record['failure_type']=type(error).__name__ # no raw error/path/credentials
            if operation in REMOTE_OPERATIONS and record.get('price_availability')=='unknown':
                diagnostics.append(_diagnostic('CAPABILITY_PRICE_UNKNOWN',operation,'Provider price is unknown; admission requires explicit allowance and reservation.'))
            if operation=='blender.gpu' and not record['execution_verified']:record['fallback']='blender.cpu-requires-separate-probe-and-explicit-policy'
            record['dependency_fingerprint']=object_hash(record['dependencies'])
            record['dependency_scope']='separately-observed-selected-dependencies'
            record['fixture']=True
            record['version']=version
            if version is None:
                record['version_scope']='unknown'
            elif operation in inventory.INITIALIZED:
                record['version_scope']='initialized-component'
            elif operation in ('resource.memory', 'resource.storage', 'procedural.native') or operation.startswith('importer.'):
                record['version_scope']='probe-protocol'
            else:
                record['version_scope']='reported-during-probe'
            record['policy_limits']={**deepcopy(LIMITS),**config.get('production_limits',{})}
            record['payloads']=payloads
            path=stage/'receipts'/(object_hash(record)+'.json');path.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n',encoding='utf-8')
            reference=_reference(path,stage,payloads);refs.append(reference)
            records.append(dict(operation=operation,version=version,status=record['status'],limits=deepcopy(record['policy_limits']),
                                probe_evidence=[reference],execution_verified=record['execution_verified']))
            if record['status']!='available':diagnostics.append(_diagnostic('CAPABILITY_'+record['reason'].upper().replace('-','_'),operation,record['reason']))
        _need(_date(request['context']['deadline'])>datetime.now(timezone.utc),'DEADLINE','Operation deadline expired before publication.')
        value=dict(capabilities=records,expires_at=(stamp+timedelta(hours=1)).isoformat(),digest='0'*64)
        value['digest']=object_hash({k:v for k,v in value.items() if k!='digest'})
        result=dict(schema_version=1,kind='director-result',method='CapabilityRegistry.probe',status='succeeded',
                    operation_id=request['context']['request_id'],input_digest=object_hash(request),value=value,
                    artifacts=refs,diagnostics=diagnostics,usage=_unknown_usage(),provenance_receipts=refs)
        validate_method_result('CapabilityRegistry.probe',result,request)
        (stage/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        _rename_no_replace(pinned_stage,destination)
    return result


def _provider_receipt_price(evidence, limits):
    """Read public pricing only after the caller verifies the execution receipt."""
    if evidence.get('price_availability')!='known':
        _need(evidence.get('price_per_call') is None and evidence.get('currency') is None,
              'PRICE','Unknown provider pricing cannot declare a price or currency.')
        return None
    price=evidence.get('price_per_call');currency=evidence.get('currency')
    _need(type(price) in (int,float) and 0<=price<=1e9 and math.isfinite(price),
          'PRICE','Retained provider price must be finite and nonnegative.')
    _need(_valid_currency(currency) and currency==limits['currency'],
          'PRICE','Retained provider currency differs from policy.')
    return price,currency


def assert_admission(snapshot, required_operations, *, evidence_root=None, now=None,
                     resources=None, network_policy='deny', price_per_call=None,
                     budget_reservation_id=None, allow_unknown_price=False,
                     budget_currency=None, budget_amount=None, paid_policy='deny', allowed_providers=(),
                     verified_provider_costs=None):
    """Verify retained smoke-test evidence and policy on a supported reader.

    The caller supplies the original trusted snapshot. Its expiry bounds use
    of that snapshot; it does not date independent receipts or identify the
    current dependencies, executing process, or execution host.
    """
    _need(isinstance(snapshot,dict),'SCHEMA','Capability snapshot must be an object.')
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
    _need(_date(value['expires_at'])>current,'EXPIRED','Capability evidence expired.')
    by_op={item['operation']:item for item in value['capabilities']}
    _need(len(by_op)==len(value['capabilities']),'DUPLICATE','Duplicate capability entries.')
    operations=list(required_operations)
    for key, measurement in (('max_memory_bytes','resource.memory'),('max_storage_bytes','resource.storage')):
        if key in (resources or {}) and measurement in by_op and measurement not in operations:
            operations.append(measurement)
    for operation in operations:
        item=by_op.get(operation)
        _need(item is not None and item['status']=='available' and item['execution_verified'],'UNAVAILABLE','Required capability is unverified: '+operation)
        _need(bool(item['probe_evidence']),'EVIDENCE','Execution verification requires nonempty retained evidence.')
        _need(evidence_root is not None,'EVIDENCE_ROOT','Admission requires the exact retained evidence root.')
        receipt_prices=[];receipt_providers=[]
        if evidence_root is not None:
            from director_contracts import _read_artifact
            for reference in item['probe_evidence']:
                evidence=decode_json(_read_artifact(evidence_root,reference))
                _need(isinstance(evidence,dict) and evidence.get('operation')==operation and evidence.get('execution_verified') is True and evidence.get('status')=='available','EVIDENCE','Probe evidence does not verify capability.')
                _need(evidence.get('policy_limits')==item['limits'] and evidence.get('version')==item['version'],'EVIDENCE','Capability policy/version differs from execution receipt.')
                payloads=evidence.get('payloads')
                _need(isinstance(payloads,list) and payloads==reference['dependencies'],
                      'EVIDENCE','Retained receipt payloads differ from the capability dependency references.')
                for payload in payloads:_read_artifact(evidence_root,payload)
                if operation in ('resource.memory', 'resource.storage'):
                    key = 'max_memory_bytes' if operation == 'resource.memory' else 'max_storage_bytes'
                    measurement = evidence.get('execution')
                    _need(isinstance(measurement, dict), 'EVIDENCE', 'Resource measurement is missing.')
                    observed = measurement.get('available_bytes')
                    _need(type(observed) is int and observed >= 0, 'EVIDENCE', 'Resource measurement is missing.')
                    amount = (resources or {}).get(key, 0)
                    _need(type(amount) in (int, float) and 0 <= amount <= observed,
                          'RESOURCE', 'Resource estimate exceeds the measured available capacity: '+key)
                if operation in REMOTE_OPERATIONS:
                    provider=evidence.get('provider_id')
                    _need(_valid_provider_id(provider),
                          'POLICY','Executed provider receipt requires a valid provider identity.')
                    receipt_providers.append(provider)
                    receipt_prices.append(_provider_receipt_price(evidence,item['limits']))
        for resource,amount in (resources or {}).items():
            _need(resource in LIMITS and type(amount) in (int,float) and amount>=0,'RESOURCE','Unknown or invalid resource estimate.')
            maximum=item['limits'][resource]
            _need(maximum is not None and amount<=maximum,'RESOURCE','Resource estimate exceeds capability bound: '+resource)
        if operation in REMOTE_OPERATIONS:
            _need(isinstance(allowed_providers,(list,tuple)) and
                  all(_valid_provider_id(p) for p in allowed_providers),
                  'POLICY','Provider allowlist must contain exact provider identities.')
            _need(len(set(receipt_providers))==1 and receipt_providers[0] in allowed_providers,
                  'POLICY','Executed provider identity is not permitted by the operation policy.')
            _need(network_policy=='allow-configured' and budget_reservation_id is not None,'POLICY','Remote provider work requires configured network authority and a reservation.')
            _need(bool(receipt_prices) and all(p==receipt_prices[0] for p in receipt_prices),
                  'EVIDENCE','Provider execution receipts disagree about pricing.')
            known=receipt_prices[0]
            _need(price_per_call is None or known is not None and type(price_per_call) in (int,float)
                  and 0<=price_per_call<=1e9 and math.isfinite(price_per_call) and price_per_call==known[0],
                  'PRICE','Explicit provider price differs from retained execution evidence.')
            _need(known is not None or allow_unknown_price,'PRICE','Unknown provider price requires explicit authority.')
            if known is not None:
                price,currency=known
                _need(price==0 or paid_policy in ('bounded','allow-unknown-price'),
                      'POLICY','Nonzero provider pricing requires explicit paid authority.')
                _need(budget_currency==currency and type(budget_amount) in (int,float)
                      and 0<=budget_amount<=1e9 and math.isfinite(budget_amount),
                      'PRICE','Known provider price requires a matching currency and bounded amount.')
                calls=(resources or {}).get('max_provider_calls',1)
                _need(type(calls) is int and calls>=0,'RESOURCE','Provider call estimate must be a nonnegative integer.')
                _need(Fraction(str(price))*calls<=Fraction(str(budget_amount)),
                      'RESOURCE','Estimated provider amount exceeds budget.')
                policy_amount=item['limits']['max_amount']
                _need(type(policy_amount) in (int,float) and 0<=policy_amount<=1e9 and math.isfinite(policy_amount),
                      'PRICE','Known provider price requires a finite bounded capability amount.')
                _need(Fraction(str(price))*calls<=Fraction(str(policy_amount)),
                      'RESOURCE','Estimated provider amount exceeds capability policy.')
                if verified_provider_costs is not None:
                    verified_provider_costs.append(Fraction(str(price))*calls)
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
