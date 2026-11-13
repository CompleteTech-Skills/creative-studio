"""Trusted, bounded Blender rendering over owner-verified Render3D snapshots.

This module is an execution adapter, not a scheduler or an asset authority.
Untrusted source files are decoded by the existing rig import owners and only
sanitized GLB derivatives are passed into the fixed Blender worker.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from atomic_publish import _rename_no_replace
from rig_errors import RigError, atomic_json, atomic_bytes, read_json, safe_path, sha256, object_hash
from render3d_contracts import (Render3DError, require, validate, make_frame_spec,
                                frame_times, VERSION)
from render3d_snapshot import NativeRigAdapter, Scene3DAdapter, verify_snapshot, create_snapshot

WORKER = Path(__file__).with_name('render3d_blender_worker.py')
MAX_LOG_BYTES = 2 * 1024**2


def _error(category, message):
    return Render3DError(category, message)


def _stop(process):
    if process.poll() is not None:
        return
    if os.name == 'posix':
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)


def _rss(pid):
    """Linux process-tree RSS, or None where the OS has no procfs."""
    if not Path('/proc').is_dir():
        return None
    pending=[pid]; total=0
    while pending:
        current=pending.pop()
        try:
            for line in Path(f'/proc/{current}/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    total+=int(line.split()[1])*1024
            pending.extend(map(int,Path(f'/proc/{current}/task/{current}/children').read_text().split()))
        except (OSError,ValueError):
            continue
    return total


def _supervise(command, *, cwd, timeout, memory_bytes, storage_bytes, cancelled=None):
    """Drain bounded logs while supervising time, memory, storage and cancellation."""
    env=dict(os.environ,OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2',MKL_NUM_THREADS='2')
    start=time.monotonic(); log=bytearray(); lock=threading.Lock(); peak=0
    process=subprocess.Popen(command,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                             start_new_session=os.name=='posix')
    def drain():
        while True:
            chunk=process.stdout.read(16384)
            if not chunk:break
            with lock:
                log.extend(chunk)
                if len(log)>MAX_LOG_BYTES:del log[:-MAX_LOG_BYTES]
    reader=threading.Thread(target=drain,daemon=True);reader.start()
    error=None
    try:
        while process.poll() is None:
            elapsed=time.monotonic()-start
            usage=_rss(process.pid)
            peak=max(peak,usage or 0)
            if cancelled is not None and cancelled():error=_error('cancelled','Render cancellation requested.')
            elif elapsed>timeout:error=_error('timeout','Blender exceeded the requested runtime budget.')
            elif usage is not None and usage>memory_bytes:error=_error('system-memory','Blender exceeded the requested memory budget.')
            else:
                size=sum(p.stat().st_size for p in Path(cwd).rglob('*') if p.is_file() and not p.is_symlink())
                if size>storage_bytes:error=_error('storage-exhaustion','Blender exceeded the requested storage budget.')
            if error:
                _stop(process);break
            time.sleep(.1)
        reader.join(timeout=5)
        payload=bytes(log)
        if error:raise error
        require(process.returncode==0,'worker-crash',payload.decode('utf-8','replace')[-2048:] or 'Blender worker failed.')
        return payload,dict(elapsed_seconds=time.monotonic()-start,peak_memory_bytes=peak or None)
    finally:
        _stop(process)
        if process.stdout:process.stdout.close()
        try:(Path(cwd)/'blender.log').write_bytes(bytes(log))
        except OSError:pass


def _plain(value):
    if hasattr(value,'tolist'):return value.tolist()
    if isinstance(value,dict):return {k:_plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [_plain(v) for v in value]
    return value


def _prepare_asset(root, asset, workspace, index, *, native=False):
    from rig_gltf import export_geometry, export_gltf
    from rig_models import load_rig
    from rig_jobs import create_run
    destination=workspace/f'asset-{index}.glb'
    if native:
        rig=load_rig(root)
    elif asset.get('skeleton'):
        path=safe_path(root,asset['skeleton']['path'],exists=True)
        require(path.name=='rig.json','unsupported-feature','Native rig entrypoint must be rig.json.')
        rig=load_rig(path.parent)
    else:
        source=safe_path(root,asset['content']['path'],exists=True)
        require(source.suffix.lower() in ('.obj','.glb','.gltf'),'unsupported-feature','Render inputs must be owner-decoded OBJ/glTF/GLB or native rigs.')
        rig=create_run(source,workspace/f'import-{index}',identifier=f'render-{index}',fixture=asset['provenance']['fixture'])
    if rig.manifest['skeleton'] is not None:
        export_gltf(rig,destination)
    else:
        require(not rig.clips and not rig.weights,'unsupported-feature','Unrigged animated geometry is not supported by this adapter.')
        export_geometry(rig.geometry,destination)
    return dict(path=destination.name,sha256=sha256(destination),vertices=rig.geometry.vertex_count,
                triangles=rig.geometry.triangle_count,rigged=rig.manifest['skeleton'] is not None,
                clips={name:dict(duration=clip['duration']) for name,clip in rig.clips.items()})


def _static_state(bundle,scene,shot):
    from scene3d_math import evaluate_transforms
    require(not shot['tracks'] and not shot['bindings'] and not shot['temporal_constraints']
            and not shot['material_overrides'],'unsupported-feature','Dynamic Scene3D shots require the temporal owner adapter.')
    require(not shot['camera_overrides'],'unsupported-feature','Camera overrides require the camera owner adapter.')
    nodes=evaluate_transforms(scene)
    collections={item['id']:item['visible'] for item in scene['collections']}
    layers={item['id']:item['visible'] for item in scene['layers']}
    for node in scene['nodes']:
        nodes[node['id']]['visible'] &= node['render']['visible'] and all(collections[c] for c in node['collections']) and all(layers[l] for l in node['layers'])
    return dict(nodes=nodes,world=scene['world'],camera=shot['camera'])


def _prepare_scene(request,snapshot_root,workspace):
    """Convert source geometry and reuse existing canonical evaluation owners."""
    from scene3d_math import to_blender
    from rig_math import BLENDER_FROM_CANONICAL
    import numpy as np
    snapshot=verify_snapshot(snapshot_root,Scene3DAdapter())
    from scene3d_contracts import load_project
    bundle=load_project(snapshot_root); project=bundle['project']
    scene_ref=next(r for r in project['scenes'] if r['id']==request['scene']['id'] and r['version']==request['scene']['version'])
    shot_ref=next(r for r in project['shots'] if r['id']==request['shot']['id'] and r['version']==request['shot']['version'])
    scene=bundle['documents'][scene_ref['path']]; shot=bundle['documents'][shot_ref['path']]
    plan_bytes=len(scene['nodes'])*(request['frame_range']['end']-request['frame_range']['start'])*4096
    require(plan_bytes+snapshot['total_bytes']*8<=request['settings']['resources']['memory_bytes'],
            'system-memory','Evaluated frame plan exceeds memory budget.')
    require(not scene['world']['environment'],'unsupported-feature','HDRI environment maps require the look-development adapter.')
    require(not any(node['material_overrides'] for node in scene['nodes']),'unsupported-feature','Material overrides require the look-development adapter.')
    assets={};nodes=[]
    allowed={'root','group','mesh_instance','skinned_character','static_prop','animated_prop','terrain','camera','light','socket','constraint_target','locator'}
    for node in scene['nodes']:
        require(node['type'] in allowed,'unsupported-feature','Unsupported Scene3D node type: '+node['type'])
        require(node['render']['receive_shadow'],'unsupported-feature','Disabling shadow reception requires a supported material mapping.')
        entry=dict(id=node['id'],type=node['type'],camera=node.get('camera'),light=node.get('light'),
                   cast_shadow=node['render']['cast_shadow'],frames=[])
        if 'asset' in node:
            asset=bundle['documents'][node['asset']['path']]
            key=node['asset']['path']
            if key not in assets:assets[key]=_prepare_asset(snapshot_root,asset,workspace,len(assets))
            entry['asset']=assets[key]
        nodes.append(entry)
    frames=[]
    for index in range(request['frame_range']['start'],request['frame_range']['end']):
        spec=make_frame_spec(request,index,Path(snapshot_root)/'snapshot.json')
        _,source=frame_times(request,index)
        shot_frame=source*Fraction(**shot['fps'])
        try:
            from scene3d_evaluate import evaluate_scene
        except ImportError:
            state=_static_state(bundle,scene,shot)
        else:
            state=evaluate_scene(bundle,scene_ref['path'],shot_ref['path'],shot_frame,source_root=snapshot_root)
        for entry in nodes:
            node_state=state['nodes'][entry['id']]
            matrix=np.asarray(node_state['world_matrix'])
            converted=BLENDER_FROM_CANONICAL @ matrix if entry['type'] in ('camera','light') else to_blender(matrix)
            clip=node_state.get('native_clip')
            if clip:
                clip=dict(id=clip['reference']['id'],time=float(Fraction(**clip['time'])))
            entry['frames'].append(dict(index=index,matrix=_plain(converted),
                visible=bool(node_state['visible']),clip=clip,light=node_state.get('light'),camera=node_state.get('camera')))
        frames.append(dict(index=index,source_time=dict(numerator=source.numerator,denominator=source.denominator),
                           world=state.get('world',scene['world']),spec=spec))
    return dict(nodes=nodes,frames=frames,camera=request['camera'],native_preview=False,
                geometry_vertices=sum(a['vertices'] for a in assets.values()),
                geometry_triangles=sum(a['triangles'] for a in assets.values()))


def _prepare_native(request,snapshot_root,workspace):
    from rig_models import load_rig
    from rig_math import BLENDER_FROM_CANONICAL
    import numpy as np
    asset=_prepare_asset(snapshot_root,{},workspace,0,native=True)
    require(request['camera'] in ('front','side','back','three-quarter'),'unsupported-feature','Native preview camera must name a supported rig preview direction.')
    rig=load_rig(snapshot_root)
    clip=request['shot']['id'] if request['shot']['id'] in rig.clips else next(iter(rig.clips),None) if len(rig.clips)<=1 else None
    require(not rig.clips or clip is not None,'unsupported-feature','Native preview shot must name an available clip.')
    frames=[];node_frames=[]
    for index in range(request['frame_range']['start'],request['frame_range']['end']):
        spec=make_frame_spec(request,index,Path(snapshot_root)/'snapshot.json')
        _,source=frame_times(request,index)
        if clip:require(source<=Fraction(str(rig.clips[clip]['duration'])),'invalid-input','Native clip sample exceeds its duration.')
        frames.append(dict(index=index,source_time=dict(numerator=source.numerator,denominator=source.denominator),
                           world=dict(background=[.08,.08,.08],exposure=0),spec=spec))
        node_frames.append(dict(index=index,matrix=np.eye(4).tolist(),visible=True,
                                clip=dict(id=clip,time=float(source)) if clip else None))
    return dict(nodes=[dict(id='native',type='skinned_character' if asset['rigged'] else 'mesh_instance',asset=asset,cast_shadow=True,frames=node_frames)],
                frames=frames,camera=request['camera'],native_preview=True,
                geometry_vertices=asset['vertices'],geometry_triangles=asset['triangles'])


class BlenderBackend:
    def __init__(self,executable=None):
        from rig_blender import find_blender
        try:self.executable=find_blender(executable)
        except RigError as exc:raise Render3DError('unsupported-backend',str(exc)) from exc

    def inspect_version(self):
        result=subprocess.run([self.executable,'--version'],capture_output=True,text=True,timeout=15,check=False)
        require(result.returncode==0,'unsupported-backend','Unable to inspect Blender version.')
        first=result.stdout.splitlines()[0]
        require(first.startswith('Blender 4.5.'),'unsupported-backend','Supported Blender release is 4.5 LTS.')
        return dict(id='blender',adapter_version=VERSION,application_version=first,
                    executable_sha256=sha256(self.executable),worker_sha256=sha256(WORKER),
                    platform=platform.platform(),engine='cycles',device='cpu')

    def cinematics_probe(self):
        """Query actual native properties in a bounded, factory-startup worker."""
        from cinematics3d_blender import validate_probe
        from cinematics3d_blender_runtime import __file__ as runtime_file
        runtime=Path(runtime_file); backend=self.inspect_version(); runtime_hash=sha256(runtime)
        with tempfile.TemporaryDirectory(prefix='render3d-cinematic-probe-') as temporary:
            root=Path(temporary); atomic_json(root/'job.json',dict(schema_version=1,mode='cinematics-probe'))
            command=[self.executable,'--background','--factory-startup','--disable-autoexec',
                     '--python-exit-code','1','--python',str(WORKER),'--',str(root/'job.json')]
            _supervise(command,cwd=root,timeout=60,memory_bytes=2*1024**3,storage_bytes=16*1024**2)
            require(sha256(self.executable)==backend['executable_sha256'] and sha256(WORKER)==backend['worker_sha256']
                    and sha256(runtime)==runtime_hash,'stale-asset','Probe code changed during execution.')
            probe=validate_probe(read_json(root/'cinematics-probe.json'))
            require(probe['application_version']==backend['application_version'],
                    'verification-failed','Probe version differs from admitted executable.')
            return probe

    def capabilities(self):
        return dict(schema_version=1,kind='render3d-capabilities',adapter=dict(id='blender',version=VERSION),
                    backend=self.inspect_version(),discovery='available',initialization='not-run',execution='not-run',
                    evidence=[],engines=['cycles'],features=['png','exr','native-rig','scene3d'],diagnostics=[])

    def probe(self,configuration):
        # Hardware owner implements real GPU/device initialization separately.
        from render3d_hardware import probe_hardware
        return probe_hardware(executable=self.executable,**configuration)

    def validate(self,snapshot,settings):
        validate(settings,'RenderSettings')
        require(settings['engine']=='cycles','unsupported-backend','This adapter currently supports Cycles.')
        require(settings['device_policy']=='cpu','unsupported-device','GPU selection requires the hardware-policy adapter.')
        require(settings['output']['passes']==['combined'],'unsupported-feature','Auxiliary render passes require the pass adapter.')
        require(snapshot['scope'] in ('scene3d','native-rig-preview'),'unsupported-feature','Unsupported source snapshot scope.')
        return dict(valid=True)

    def render_frames(self,request,snapshot_root,destination,*,cancelled=None,progress=None,cinematics=None):
        validate(request,'RenderRequest')
        cinematic_plan=None; cinematic_files={}; cinematic_code={}
        if cinematics is not None:
            from cinematics3d_blender import validate_compiled, ADAPTER_SOURCES, __file__ as compiler_file
            from cinematics3d_blender_runtime import __file__ as runtime_file
            cinematic_plan,cinematic_files=validate_compiled(cinematics,request)
            cinematic_code={name:sha256(Path(compiler_file).with_name(name)) for name in ADAPTER_SOURCES}
        require(('cinematics_sha256' in request['settings'])==(cinematic_plan is not None),
                'unsupported-feature','Cinematic settings require their matching compiled native plan.')
        root=Path(snapshot_root).absolute();target=Path(destination).absolute()
        require(not target.exists() and not target.is_symlink(),'ownership','Render destination already exists.')
        require(not target.parent.resolve().joinpath(target.name).is_relative_to(root.resolve()),'ownership','Output cannot be nested in source snapshot.')
        snapshot=read_json(root/'snapshot.json');validate(snapshot,'RenderSnapshot');self.validate(snapshot,request['settings'])
        adapter=Scene3DAdapter() if snapshot['scope']=='scene3d' else NativeRigAdapter()
        verify_snapshot(root,adapter)
        make_frame_spec(request,request['frame_range']['start'],root/'snapshot.json')
        # Admission precedes creating output paths and importing expensive assets.
        settings=request['settings'];resources=settings['resources'];width,height=settings['resolution']
        frame_count=request['frame_range']['end']-request['frame_range']['start']
        estimate=width*height*64+snapshot['total_bytes']*8+frame_count*16384
        require(estimate<=resources['memory_bytes'],'system-memory','Scene/image admission exceeds memory budget.')
        require(width*height*16*(request['frame_range']['end']-request['frame_range']['start'])<=resources['storage_bytes'],
                'storage-exhaustion','Conservative uncompressed output estimate exceeds storage budget.')
        target.parent.mkdir(parents=True,exist_ok=True)
        stage=Path(tempfile.mkdtemp(prefix='.render3d-',dir=target.parent))
        try:
            frozen=stage/'snapshot'
            captured=create_snapshot(root,frozen,adapter)
            require(captured==snapshot,'stale-asset','Snapshot changed while capturing render source.')
            original=(root/'snapshot.json').read_bytes()
            require(len(original)==request['snapshot']['bytes'] and hashlib.sha256(original).hexdigest()==request['snapshot']['sha256'],
                    'stale-asset','Snapshot metadata differs from request-bound bytes.')
            atomic_bytes(frozen/'snapshot.json',original)
            source=stage/'source';source.mkdir()
            scene=(_prepare_scene if snapshot['scope']=='scene3d' else _prepare_native)(request,frozen,source)
            require(scene['geometry_vertices']>0 and scene['geometry_triangles']>0,'missing-asset','Rendering requires actual nonempty geometry.')
            backend=self.inspect_version()
            job=dict(schema_version=1,mode='render',settings=settings,scene=scene,output='../frames',report='../worker-report.json')
            if cinematic_plan is not None:
                job['cinematics']=cinematic_plan
                for name,payload in cinematic_files.items():
                    path=safe_path(stage/'cinematics',name); path.parent.mkdir(parents=True,exist_ok=True)
                    atomic_bytes(path,payload)
                atomic_json(stage/'cinematics'/'plan.json',cinematic_plan)
                atomic_json(stage/'cinematics'/'probe.json',cinematics['probe'])
            atomic_json(source/'job.json',job)
            command=[self.executable,'--background','--factory-startup','--disable-autoexec','--python-exit-code','1','--python',str(WORKER),'--',str(source/'job.json')]
            log,usage=_supervise(command,cwd=stage,timeout=resources['timeout_seconds'],memory_bytes=resources['memory_bytes'],storage_bytes=resources['storage_bytes'],cancelled=cancelled)
            (stage/'blender.log').write_bytes(log)
            require(sha256(self.executable)==backend['executable_sha256'] and sha256(WORKER)==backend['worker_sha256'],
                    'stale-asset','Executed backend code changed during rendering.')
            if cinematic_plan is not None:
                require(all(sha256(Path(__file__).with_name(name))==digest for name,digest in cinematic_code.items()),
                        'stale-asset','Cinematic adapter changed during rendering.')
            report=read_json(stage/'worker-report.json')
            require(report['version']==backend['application_version'],'verification-failed','Worker version changed after admission.')
            require(report['rendered']==[f['index'] for f in scene['frames']],'incomplete-output','Worker did not report every requested frame.')
            images=[]
            extension=settings['output']['format']
            for frame in scene['frames']:
                relative=f'frames/{frame["index"]:06d}.{extension}';path=stage/relative
                images.append(dict(frame=frame['index'],file=dict(path=relative,sha256=sha256(path),bytes=path.stat().st_size),
                    format=extension,bit_depth=settings['output']['bit_depth'],pass_name='combined',resolution=[width,height]))
                if progress:progress(frame['index'])
            manifest=dict(schema_version=1,request_sha256=object_hash(request),snapshot_sha256=snapshot['snapshot_sha256'],backend=backend,
                          frames=[frame['spec'] for frame in scene['frames']],worker=report,fixture=snapshot['fixture'])
            if cinematic_plan is not None:
                require(report.get('cinematics',{}).get('plan')==cinematic_plan,
                        'verification-failed','Worker cinematic plan differs from admitted settings.')
                manifest['cinematics']=dict(plan=cinematic_plan,code=cinematic_code,
                    files={name:dict(sha256=hashlib.sha256(payload).hexdigest(),bytes=len(payload))
                           for name,payload in cinematic_files.items()})
            atomic_json(stage/'manifest.json',manifest)
            artifact=lambda name:dict(path=name,sha256=sha256(stage/name),bytes=(stage/name).stat().st_size)
            indices=[frame['index'] for frame in scene['frames']]
            result=dict(schema_version=1,kind='render3d-result',job=request['id'],request_sha256=object_hash(request),snapshot_sha256=snapshot['snapshot_sha256'],
                backend=backend,status='complete',requested_frames=indices,successful=indices,failed=[],skipped=[],reused=[],images=images,diagnostics=[],
                usage=dict(**usage,output_bytes=sum(p.stat().st_size for p in stage.rglob('*') if p.is_file()),measurement='measured' if usage['peak_memory_bytes'] is not None else 'estimated'),
                logs=[artifact('blender.log')],manifest=artifact('manifest.json'),verification=dict(status='not-run',verifier=None,evidence=None))
            from render3d_verify import verify_result
            verification=verify_result(request,result,stage,snapshot_root=frozen,expected_backend=backend)
            atomic_json(stage/'verification.json',verification)
            result['verification']=dict(status='passed',verifier=verification['verifier'],evidence=artifact('verification.json'))
            validate(result,'RenderResult');atomic_json(stage/'result.json',result)
            require(sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())<=resources['storage_bytes'],
                    'storage-exhaustion','Verified delivery exceeds storage budget.')
            _rename_no_replace(stage,target)
            return result
        except Exception as exc:
            error=exc if isinstance(exc,Render3DError) else Render3DError(
                'invalid-input' if isinstance(exc,RigError) else 'storage-exhaustion' if isinstance(exc,OSError) and exc.errno in (28,122) else 'external-tool',str(exc))
            error.report['path']=str(stage)
            try:atomic_json(stage/'failure.json',error.report)
            except OSError:pass
            # Private failed staging is retained for diagnostics; it is never a
            # published result and the requested destination remains untouched.
            raise error from exc

    def prepare(self,snapshot,settings):
        return self.validate(snapshot,settings)

    def cancel(self,worker_id):
        raise Render3DError('unsupported-feature','Use the render cancellation callback; process identifiers are never accepted from untrusted input.')


def render_frames(request,snapshot_root,destination,*,executable=None,cancelled=None,progress=None):
    return BlenderBackend(executable).render_frames(request,snapshot_root,destination,cancelled=cancelled,progress=progress)
