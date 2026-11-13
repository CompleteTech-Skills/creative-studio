"""Optional pinned Blender adapter with timeout, reopen and independent samples."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import hashlib
import numpy as np
from rig_contracts import validate_contract
from rig_errors import RigError, atomic_json, read_json, require, sha256


def find_blender(executable=None):
    value=executable or os.environ.get('HATCH_BLENDER') or shutil.which('blender')
    require(value and Path(value).is_file(),'RIG_BLENDER_MISSING','Install Blender 4.5 LTS and set HATCH_BLENDER or --blender.')
    return str(Path(value).absolute())


def _run(job,*,executable=None,timeout=300):
    blender=find_blender(executable)
    with tempfile.TemporaryDirectory(prefix='hatch-blender-') as temporary:
        job_path=Path(temporary)/'job.json';atomic_json(job_path,job)
        command=[blender,'--background','--factory-startup','--disable-autoexec','--python-exit-code','1',
                 '--python',str(Path(__file__).with_name('rig_blender_worker.py')),'--',str(job_path)]
        try:result=subprocess.run(command,capture_output=True,text=True,timeout=timeout,check=False)
        except subprocess.TimeoutExpired as exc:raise RigError('RIG_BLENDER_TIMEOUT','Blender exceeded the configured deadline.',retryable=True,preserved=job.get('input')) from exc
        require(result.returncode==0 and Path(job['report']).is_file(),'RIG_BLENDER_FAILED','Blender failed before producing its inspected output.',
                observed=(result.stderr+result.stdout)[-8000:],preserved=job.get('input'))
        report=read_json(job['report']);report['command']=[blender,*command[1:-1],'<job.json>'];return report



def _new_output(path, code):
    path = Path(path).absolute()
    # The caller chooses the output parent; canonical aliases such as macOS
    # /var are valid. The new output itself must never follow a symlink.
    path = path.parent.resolve() / path.name
    require(not path.is_symlink(),
            'RIG_SYMLINK', 'Export destinations must not be symbolic links.')
    require(not path.exists(), code, 'Use a new export destination.', preserved=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.parent.resolve() / path.name


def _publish_outputs(outputs):
    """Publish complete files exclusively, retaining evidence on partial failure.

    For a two-file export, the primary .blend file is published last. A failed
    final promotion may leave a complete verified sidecar; never unlink a public
    path during rollback because another writer may have replaced that path.
    """
    from atomic_publish import _rename_no_replace
    try:
        for source, destination in outputs:
            _rename_no_replace(source, destination)
    except OSError as exc:
        raise RigError('RIG_EXPORT_PUBLICATION', 'Export destination changed or exclusive publication is unavailable; any verified sidecar is retained.',
                       preserved=str(destination), observed=str(exc)) from exc


def export_blender(rig,destination,*,executable=None,timeout=None,verify=True):
    from rig_gltf import export_gltf
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from rig_attachments import socket_world
    from rig_skeleton import neutral_pose
    destination=_new_output(destination,'RIG_EXPORT_EXISTS')
    final_roundtrip=_new_output(destination.with_suffix('.roundtrip.glb'),'RIG_EXPORT_EXISTS') if verify else None
    timeout=timeout or rig.manifest['limits']['timeout_seconds']
    with tempfile.TemporaryDirectory(prefix='.hatch-blender-export-',dir=destination.parent) as temporary:
        root=Path(temporary);staged=root/'editable.blend';source=root/'character.glb';export_gltf(rig,source)
        atomic_json(root/'rig.json',rig.manifest)
        samples=[];indices={m.id:np.linspace(0,len(m.positions)-1,min(24,len(m.positions)),dtype=int).tolist() for m in rig.geometry.meshes}
        for name,clip in rig.clips.items():
            for fraction in (0.,.37,.73):samples.append(dict(clip=name,time=clip['duration']*fraction,vertices=indices,bones=[b['id'] for b in rig.skeleton['bones']],sockets=[s['id'] for s in rig.manifest['attachments']]))
        if not samples:
            samples=[dict(clip='rest',time=0.,rest=True,vertices=indices,bones=[b['id'] for b in rig.skeleton['bones']],
                          sockets=[s['id'] for s in rig.manifest['attachments']])]
        socket_rest={s['id']:socket_world(rig,neutral_pose(rig.skeleton),s['id']).tolist() for s in rig.manifest['attachments']}
        base=dict(fps=24,duration=max((c['duration'] for c in rig.clips.values()),default=2.),samples=samples,socket_rest=socket_rest,
                  morph_defaults={mesh.id:{name:mesh.morph_defaults.get(name,0.) for name in mesh.morphs}
                                  for mesh in rig.geometry.meshes})
        report=_run(dict(base,mode='create',input=str(source),manifest=str(root/'rig.json'),output=str(staged),report=str(root/'create-report.json')),
                    executable=executable,timeout=timeout)
        require(report['ok'],'RIG_BLENDER_EDITABLE','Blender output lacks armature, skin modifier or weights.')
        if verify:
            exported=root/'roundtrip.glb'
            report=_run(dict(base,mode='reopen',input=str(staged),export=str(exported),report=str(root/'reopen-report.json')),
                        executable=executable,timeout=timeout)
            maximum=0.;joint_max=0.;socket_max=0.;compared=0
            require(len(report['samples']) == len(samples),'RIG_BLENDER_SAMPLES','Independent Blender sample inventory is incomplete.')
            from rig_skeleton import evaluate_fk
            for sample in report['samples']:
                require(sample['matched'] or not rig.clips,'RIG_BLENDER_ANIMATION','Blender could not activate a named NLA clip.',asset=sample['clip'])
                expected_inventory=dict(meshes=set(indices),bones={b['id'] for b in rig.skeleton['bones']},sockets=set(socket_rest))
                differences={kind:dict(missing=sorted(expected-set(sample[kind])),extra=sorted(set(sample[kind])-expected))
                             for kind,expected in expected_inventory.items()}
                complete=not any(item['missing'] or item['extra'] for item in differences.values())
                require(complete,'RIG_BLENDER_SAMPLES',
                        'Independent Blender mesh, bone or socket inventory is incomplete: '+str(differences),observed=differences)
                pose=evaluate_animation(rig,rig.clips[sample['clip']],sample['time'])['pose'] if rig.clips else neutral_pose(rig.skeleton);native=evaluate_deformation(rig,pose);world=evaluate_fk(rig.skeleton,pose)
                for name,points in sample['meshes'].items():
                    require(np.shape(points)==(len(indices[name]),3),'RIG_BLENDER_SAMPLES','Independent Blender vertex sample count differs.')
                    reference=native[name]['positions'][indices[name]]
                    maximum=max(maximum,float(np.linalg.norm(reference-np.array(points),axis=1).max()));compared+=len(points)
                for name,point in sample['bones'].items():joint_max=max(joint_max,float(np.linalg.norm(world[name][:3,3]-point)))
                for name,matrix in sample['sockets'].items():
                    socket_max=max(socket_max,float(np.max(np.abs(socket_world(rig,pose,name)-np.asarray(matrix)))))
            require(compared>0,'RIG_BLENDER_SAMPLES','Independent Blender mesh samples are missing.')
            report.update(max_vertex_error_m=maximum,max_joint_error_m=joint_max,compared_vertices=compared,
                          max_socket_transform_error=socket_max,tolerance_m=.01,roundtrip_path=str(final_roundtrip),roundtrip_sha256=sha256(exported))
            report['ok']=report['ok'] and maximum<=.01 and joint_max<=.01 and socket_max<=.01
        from rig_models import rig_signature
        report.update(schema_version=1,format='blend',path=str(destination),sha256=sha256(staged),independent=True,reader='Blender',reopened=verify,
                      rig_sha256=rig_signature(rig.manifest),source_glb_sha256=sha256(source))
        validate_contract(report, 'RigExportReceipt')
        require(report['ok'],'RIG_BLENDER_VERIFICATION','Independent Blender samples differ from the native rig; output was not published.')
        publications=([(exported,final_roundtrip)] if verify else [])+[(staged,destination)]
        _publish_outputs(publications)
        return report


def import_blend(path,*,scale=1.,limits=None,executable=None):
    from rig_import import _gltf_model
    from rig_contracts import DEFAULT_LIMITS
    limits=limits or DEFAULT_LIMITS;path=Path(path).absolute()
    require(path.stat().st_size<=limits['source_bytes'],'RIG_LIMIT_SOURCE','Blender input exceeds its byte budget.')
    with tempfile.TemporaryDirectory(prefix='hatch-blend-import-') as temporary:
        root=Path(temporary);output=root/'derived.glb'
        report=_run(dict(mode='import',input=str(path),export=str(output),report=str(root/'report.json'),samples=[]),executable=executable,timeout=limits['timeout_seconds'])
        imported=_gltf_model(output,scale=scale,limits=limits)
        imported.source_files=[path];imported.report.update(import_format='.blend',source_sha256=sha256(path),blender=report,
            warnings=imported.report.get('warnings',[])+['External Blender linked libraries must be made local and textures packed before portable import.'])
        return imported


def render_blender_preview(rig,destination,*,clip=None,times=None,pose=None,blend_path=None,blend_sha256=None,
                           width=256,height=256,camera='three-quarter',executable=None):
    """Render real Cycles CPU PNGs with one shared camera and exact source provenance."""
    import math
    from copy import deepcopy
    from rig_animation import clip_from_poses
    from rig_skeleton import neutral_pose
    from rig_models import rig_signature
    require(type(width) is int and type(height) is int and 64<=width<=1024 and 64<=height<=1024,
            'RIG_RENDER_SIZE','Blender preview dimensions must be 64 to 1024 pixels.')
    require(camera in ('front','side','back','three-quarter'),'RIG_CAMERA','Unknown preview camera.')
    require(width*height*64+rig.geometry.vertex_count*128+rig.geometry.triangle_count*96<=rig.manifest['limits']['memory_bytes'],
            'RIG_RENDER_BUDGET','Blender preview exceeds its conservative scene-buffer estimate.')
    times=[0.] if times is None else list(times)
    require(0<len(times)<=120 and all(math.isfinite(t) and t>=0 for t in times),
            'RIG_RENDER_BUDGET','A preview needs 1 to 120 finite nonnegative sample times.')
    target=_new_output(destination,'RIG_RENDER_EXISTS')
    require(len(times)==1 or target.suffix.lower()!='.png','RIG_RENDER_DESTINATION','Use a folder for multiple frames.')
    single_file=len(times)==1 and target.suffix.lower()=='.png'
    outputs=[target] if single_file else [target/f'frame-{i:04d}.png' for i in range(len(times))]
    with tempfile.TemporaryDirectory(prefix='.hatch-blender-render-',dir=target.parent) as temporary:
        root=Path(temporary);staged=root/'frames';staged.mkdir()
        staged_outputs=[staged/p.name for p in outputs];working=rig
        if pose is not None or clip is None:
            require(blend_path is None,'RIG_RENDER_POSE','Explicit poses require a fresh derived Blender scene.')
            working=deepcopy(rig);pose=pose or neutral_pose(rig.skeleton)
            authored=clip_from_poses(working,'preview-pose',[0.,1.],[pose,pose]);working.clips={authored['id']:authored};clip=authored['id']
        require(clip in working.clips and max(times)<=working.clips[clip]['duration'],
                'RIG_RENDER_TIME','Render samples must lie inside an available clip.')
        if blend_path is None:
            blend_path=root/'render.blend';created=export_blender(working,blend_path,executable=executable,verify=False)
            blend_sha256=created['sha256']
        require(isinstance(blend_sha256,str) and len(blend_sha256)==64 and all(c in '0123456789abcdef' for c in blend_sha256),
                'RIG_BLENDER_REFERENCE','A supplied Blender project requires the exact SHA-256 from its export receipt.')
        blend_path=Path(blend_path).absolute()
        from rig_errors import open_payload
        snapshot=root/'input.blend';digest=hashlib.sha256();written=0
        with open_payload(blend_path.parent,blend_path.name) as (source,info):
            require(info.st_size<=rig.manifest['limits']['source_bytes'],'RIG_LIMIT_SOURCE','Blender input exceeds its byte budget.')
            with snapshot.open('xb') as output:
                while True:
                    block=source.read(1024*1024)
                    if not block:break
                    written+=len(block)
                    require(written<=rig.manifest['limits']['source_bytes'],'RIG_LIMIT_SOURCE','Blender input exceeds its byte budget.')
                    output.write(block);digest.update(block)
        blend_digest=digest.hexdigest()
        require(blend_digest==blend_sha256,'RIG_BLENDER_CHANGED','Blender project differs from its immutable export reference.')
        requests=[dict(time=float(t),path=str(p)) for t,p in zip(times,staged_outputs)]
        report=_run(dict(mode='render',input=str(snapshot),clip=clip,frames=requests,width=width,height=height,camera=camera,
                         report=str(root/'report.json'),samples=[],rig_sha256=rig_signature(working.manifest)),executable=executable,timeout=rig.manifest['limits']['timeout_seconds'])
        require(all(p.is_file() and not p.is_symlink() and p.stat().st_size>100 for p in staged_outputs),'RIG_RENDER_OUTPUT','Blender did not create every requested PNG.')
        from PIL import Image
        for path in staged_outputs:
            try:
                with Image.open(path) as frame:
                    frame.load()
                    require(frame.format=='PNG' and frame.size==(width,height) and frame.mode=='RGBA',
                            'RIG_RENDER_OUTPUT','Blender output must be a decodable RGBA PNG at the requested dimensions.')
            except (OSError, ValueError) as exc:
                if isinstance(exc,RigError):raise
                raise RigError('RIG_RENDER_OUTPUT','Blender output is not a complete decodable PNG.') from exc
        result=dict(schema_version=1,blender_version=report['blender_version'],engine='Cycles CPU',samples=16,
                    rig_sha256=rig_signature(rig.manifest),source_sha256=rig.manifest['source']['sha256'],
                    blend_sha256=blend_digest,fixture=rig.manifest['source']['fixture'],clip=clip,camera=camera,
                    width=width,height=height,transparent=True,review_status='pending',
                    frames=[dict(path=str(p),time=float(t),sha256=sha256(source)) for t,p,source in zip(times,outputs,staged_outputs)])
        if not single_file:atomic_json(staged/'frames.json',result)
        _publish_outputs([(staged_outputs[0],target)] if single_file else [(staged,target)])
        return result
