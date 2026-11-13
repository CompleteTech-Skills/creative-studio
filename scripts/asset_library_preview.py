"""Verified asset inspection images through the canonical Render3D backend.

Inspection geometry is derived from actual native vertices, edges, UVs and bones.
It is not a new renderer and never changes an immutable library asset or its review.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile

from asset_library import AssetLibrary, read_source_bytes
from atomic_publish import _rename_no_replace
from rig_errors import RigError, object_hash, decode_json

VERSION = '1.0.0'
MODES = ('perspective', 'orthographic', 'idle', 'motion', 'material', 'environment',
         'wireframe', 'skeleton', 'uv')
MAX_FILES = 1024
MAX_GEOMETRY_BYTES = 128 * 1024**2


def require(condition, code, message):
    if not condition:
        raise RigError('LIBRARY_PREVIEW_' + code, message, stage='asset-preview')


def _cache_bytes(path):
    """Include empty owned runtime-lock artifacts without following a link."""
    path=Path(path)
    if path.lstat().st_size:
        return read_source_bytes(path)
    require(not any(p.is_symlink() for p in (path,*path.parents)), 'PATH', 'Cache contains a symlink.')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    try:
        before=os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size==0 and not os.read(fd,1),
                'CACHE','Empty cache artifact changed while reading.')
        return b''
    finally:
        os.close(fd)


def _cache_path(value):
    from asset_library_contracts import portable_path
    # Native run creation leaves its owned sibling lock in the cache. Keep the
    # ordinary portable-name rules, allowing only that specific hidden basename.
    require(isinstance(value, str), 'PATH', 'Cache paths must be strings.')
    parts=value.split('/')
    if parts[-1]=='.native.rig.lock':
        parts[-1]='native.rig.lock'
    portable_path('/'.join(parts))
    return value


def _check_geometry_budget(geometry, samples):
    size=sum(mesh.positions.nbytes+mesh.triangles.nbytes+
             sum(array.nbytes for array in mesh.attributes.values())+
             sum(array.nbytes for target in mesh.morphs.values() for array in target.values())
             for mesh in geometry.meshes)
    require(size*(samples+2)<=MAX_GEOMETRY_BYTES,
            'BUDGET','Sampled geometry exceeds the preview memory estimate.')


def settings(value=None):
    result = dict(mode='perspective', width=256, height=256, samples=8,
                  direction='three-quarter', times=['0'], clip=None,
                  background=[.08, .08, .08], material=None)
    if value is not None:
        require(isinstance(value, dict) and set(value) <= set(result), 'SETTINGS', 'Unknown preview setting.')
        result.update(deepcopy(value))
    require(result['mode'] in MODES, 'MODE', 'Unsupported inspection mode.')
    require(result['direction'] in ('front', 'side', 'back', 'three-quarter'), 'CAMERA', 'Unsupported direction.')
    for key, low, high in [('width',64,1024), ('height',64,1024), ('samples',1,64)]:
        require(type(result[key]) is int and low <= result[key] <= high, 'BUDGET', 'Invalid '+key+' budget.')
    require(isinstance(result['times'], list) and 1 <= len(result['times']) <= 32,
            'TIME', 'Use one to thirty-two exact source times.')
    times=[]
    for value in result['times']:
        require(type(value) is str and len(value) <= 64, 'TIME', 'Times must be bounded rational strings.')
        try:
            number=Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise RigError('LIBRARY_PREVIEW_TIME', 'Invalid exact source time.') from exc
        require(0 <= number <= 600 and number.denominator <= 1000000, 'TIME', 'Source time exceeds bounds.')
        times.append(str(number))
    result['times']=times
    require(result['mode'] == 'motion' or len(times) == 1, 'TIME', 'Multiple samples require motion mode.')
    require(result['clip'] is None or type(result['clip']) is str and 0 < len(result['clip']) <= 128,
            'CLIP', 'Clip must be an explicit native identifier.')
    require(result['mode'] != 'motion' or result['clip'] is not None, 'CLIP', 'Motion requires an existing clip.')
    require(isinstance(result['background'], list) and len(result['background']) == 3 and
            all(type(v) in (int,float) and math.isfinite(v) and 0 <= v <= 1 for v in result['background']),
            'COLOR', 'Background must be three finite linear color values.')
    material=result['material']
    if material is not None:
        require(isinstance(material,dict) and set(material)=={'base_color','metallic','roughness'},
                'MATERIAL', 'Explicit material comparison requires base_color, metallic and roughness.')
        require(isinstance(material['base_color'],list) and len(material['base_color'])==4,
                'MATERIAL','Base color requires four values.')
        require(all(type(v) in (int,float) and math.isfinite(v) and 0 <= v <= 1
                    for v in [*material['base_color'],material['metallic'],material['roughness']]),
                'MATERIAL','Material values must be finite in zero to one.')
    return result


def _closure(library, reference):
    pending=[reference]; found={}
    while pending:
        ref=pending.pop(); key=object_hash(ref)
        if key in found:continue
        require(len(found)<256,'BUDGET','Preview dependency closure exceeds 256 versions.')
        manifest=library.resolve(ref)
        found[key]=dict(reference=deepcopy(ref), files=deepcopy(manifest['files']))
        pending.extend(edge['target'] for edge in manifest['dependencies'])
        pending.extend(manifest['provenance']['parents'])
        pending.extend(manifest['metadata']['style_refs'])
    return [found[key] for key in sorted(found)]


def _geometry(rig, config, source_time):
    import numpy as np
    from rig_models import Geometry, Mesh, validate_geometry
    from rig_geometry import mesh_edges, vertex_normals
    from rig_demo import tube
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from rig_skeleton import evaluate_fk, neutral_pose
    mode=config['mode']; geometry=deepcopy(rig.geometry); pose=None
    if config['clip'] is not None:
        require(config['clip'] in rig.clips,'CLIP','Requested clip is not present in the exact asset.')
        require(source_time <= Fraction(str(rig.clips[config['clip']]['duration'])), 'TIME', 'Sample exceeds clip duration.')
        require(rig.skeleton is not None and rig.weights, 'CAPABILITY','Native clip preview requires a bound skeleton.')
        pose=evaluate_animation(rig,config['clip'],float(source_time))['pose']
        evaluated=evaluate_deformation(rig,pose,normals=True)
        for mesh in geometry.meshes:
            mesh.positions=evaluated[mesh.id]['positions'];mesh.attributes['NORMAL']=evaluated[mesh.id]['normals']
            mesh.morphs={};mesh.morph_defaults={}
    if config['material'] is not None:
        material=config['material']
        geometry.materials=[dict(name='explicit-preview-comparison',pbrMetallicRoughness=dict(
            baseColorFactor=material['base_color'],metallicFactor=material['metallic'],roughnessFactor=material['roughness']))]
        for mesh in geometry.meshes:mesh.material=0
        geometry.images=[];geometry.textures=[];geometry.samplers=[];geometry.extensions_used=[]
    if mode in ('wireframe','skeleton','uv'):
        material=dict(name='inspection-lines',pbrMetallicRoughness=dict(baseColorFactor=[.95,.55,.08,1],metallicFactor=0,roughnessFactor=.65))
        points=np.concatenate([mesh.positions for mesh in geometry.meshes]);span=max(float(np.ptp(points,axis=0).max()),.01)
        lines=[]
        if mode=='skeleton':
            require(rig.skeleton is not None,'CAPABILITY','Skeleton inspection requires a real skeleton.')
            world=evaluate_fk(rig.skeleton,pose or neutral_pose(rig.skeleton))
            for bone in rig.skeleton['bones']:
                if bone['parent']:
                    lines.append((world[bone['parent']][:3,3],world[bone['id']][:3,3]))
        elif mode=='wireframe':
            for mesh in geometry.meshes:
                edges,_=mesh_edges(mesh.triangles)
                require(len(lines)+len(edges)<=4096,'BUDGET','Inspection exceeds 4096 edges.')
                lines.extend((mesh.positions[a],mesh.positions[b]) for a,b in edges)
        else:
            for mesh in geometry.meshes:
                require('TEXCOORD_0' in mesh.attributes,'CAPABILITY','UV inspection requires actual TEXCOORD_0 data on every mesh.')
                uv=mesh.attributes['TEXCOORD_0'];edges,_=mesh_edges(mesh.triangles)
                require(len(lines)+len(edges)<=4096,'BUDGET','Inspection exceeds 4096 edges.')
                lines.extend((np.array([uv[a,0],uv[a,1],0]),np.array([uv[b,0],uv[b,1],0])) for a,b in edges)
            span=1.
        require(0 < len(lines) <= 4096,'BUDGET','Inspection line count must be between one and 4096.')
        meshes=[]
        for a,b in lines:
            if np.linalg.norm(b-a)>1e-10:
                meshes.append(tube('line-'+str(len(meshes)),[a,b],span*.0025,0,sides=4,subdivisions=1))
        require(bool(meshes),'CAPABILITY','No nondegenerate inspection lines are available.')
        geometry=Geometry(meshes,[material])
    validate_geometry(geometry)
    return geometry


def _scene(root, geometry, config, provenance, framing_bounds=None):
    """Create ordinary owner-validated Scene3D documents, never a renderer job bypass."""
    import numpy as np
    from rig_gltf import export_geometry
    from rig_math import matrix_quaternion
    from scene3d_contracts import COORDINATES, canonical_bytes, load_project
    from scene3d_example import node
    root.mkdir(); model=root/'model.glb';export_geometry(geometry,model)
    refs=[]
    def write(kind,identifier,**fields):
        value=dict(schema_version=1,kind='scene3d-'+kind,id=identifier,version='1.0.0',**fields)
        path=identifier+'.json';data=canonical_bytes(value);(root/path).write_bytes(data)
        ref=dict(id=identifier,version='1.0.0',path=path,sha256=hashlib.sha256(data).hexdigest(),kind=value['kind'])
        refs.append(ref);return ref
    points=np.concatenate([mesh.positions for mesh in geometry.meshes]);low=points.min(axis=0);high=points.max(axis=0)
    frame_low,frame_high=framing_bounds if framing_bounds is not None else (low,high)
    center=(frame_low+frame_high)/2;span=max(float(np.linalg.norm(frame_high-frame_low)),.1)
    file=dict(path='model.glb',sha256=hashlib.sha256(model.read_bytes()).hexdigest())
    asset=write('asset','asset',name='Exact library inspection derivative',asset_type='mesh',content=file,source=file,
        provenance=provenance,coordinate_system=COORDINATES,original_coordinate_system='canonical',pivot=[0,0,0],
        meshes=[file],materials=[],bounds=dict(minimum=low.tolist(),maximum=high.tolist()),collision=None,sockets=[],
        components={},skeleton=None,animations=[],lods=[],dependencies=[],resources=dict(vertices=geometry.vertex_count,
        triangles=geometry.triangle_count,bytes=model.stat().st_size),validation=[],review=dict(status='unreviewed',receipts=[]))
    mesh=node('mesh','mesh_instance','root');mesh['asset']=asset
    camera=node('camera','camera','root')
    direction='front' if config['mode']=='uv' else config['direction']
    offset=np.asarray({'front':[0,0,1],'back':[0,0,-1],'side':[1,0,0],'three-quarter':[2.3,1.1,3.8]}[direction],float)
    offset=offset/np.linalg.norm(offset);location=center+offset*span*2
    forward=(center-location)/np.linalg.norm(center-location);right=np.cross(forward,[0,1,0]);right/=np.linalg.norm(right)
    up=np.cross(right,forward);rotation=np.column_stack((right,up,-forward))
    camera['transform'].update(translation=location.tolist(),rotation=matrix_quaternion(rotation).tolist())
    camera['camera']=dict(projection='perspective' if config['mode']=='perspective' else 'orthographic',
        near=max(.001,span*.001),far=max(100,span*20),vertical_fov=.8,orthographic_height=span*1.2)
    nodes=[node('root'),mesh,camera];lights=[]
    for name,position,intensity in [('key',[-1,2,3],500),('fill',[2,1,-1],150)]:
        light=node(name,'light','root');light['transform']['translation']=(center+np.asarray(position)*span).tolist()
        light['light']=dict(type='point',color=[1,1,1],intensity=intensity*span*span)
        nodes.append(light);lights.append(name)
    legacy_settings=dict(backend='blender-cycles',width=config['width'],height=config['height'],samples=config['samples'],
        transparent=False,color_space='srgb',output='png',device='cpu')
    scene=write('scene','scene',name='Library inspection',description='Exact native inspection geometry',revision=0,
        coordinate_system=COORDINATES,roots=['root'],nodes=nodes,world=dict(coordinate_system=COORDINATES,
        background=config['background'],exposure=0,environment=None,metadata={}),collections=[],layers=[],
        cameras=['camera'],lights=lights,recipes=[],dependencies=[asset],bounds=None,shots=['shot'],
        continuity=[],metadata={},provenance=provenance)
    shot=write('shot','shot',name='Inspection sample',scene=scene,duration_frames=1,fps=dict(numerator=24,denominator=1),
        camera='camera',camera_overrides={},tracks=[],bindings=[],material_overrides=[],temporal_constraints=[],
        render=legacy_settings,continuity=[],dependencies=[asset],metadata={})
    write('project','project',name='Asset inspection',status='fixture' if provenance['fixture'] else 'development',
        coordinate_system=COORDINATES,asset_catalog=None,assets=[asset],characters=[],scenes=[scene],shots=[shot],
        prefabs=[],animations=[],render_defaults=legacy_settings,budgets=dict(max_nodes=100,max_files=64,
        max_bytes=64*1024**2,max_frames=32),created_at='2026-11-13T00:00:00Z',modified_at='2026-11-13T00:00:00Z',
        linked_2d_project=None,dependency_lock=deepcopy(refs),validation=[],provenance=provenance,metadata={})
    load_project(root)


def _request(snapshot, config):
    from rig_errors import sha256
    return dict(schema_version=1,kind='render3d-request',id='asset-preview',project=dict(id='project',version='1.0.0'),
        scene=dict(id='scene',version='1.0.0'),shot=dict(id='shot',version='1.0.0'),mode='3d',
        snapshot=dict(path='snapshot.json',sha256=sha256(snapshot/'snapshot.json'),bytes=(snapshot/'snapshot.json').stat().st_size),
        fps=dict(numerator=24,denominator=1),frame_range=dict(start=0,end=1),source_start=dict(numerator=0,denominator=1),
        playback_rate=dict(numerator=1,denominator=1),camera='camera',settings=dict(engine='cycles',device_policy='cpu',
        device=None,allow_cpu_fallback=False,resolution=[config['width'],config['height']],samples=config['samples'],seed=17,
        denoise=False,output=dict(format='png',bit_depth=8,passes=['combined'],alpha='opaque',color_space='srgb'),
        exposure=0,view_transform='standard',resources=dict(memory_bytes=2*1024**3,storage_bytes=512*1024**2,
        timeout_seconds=120,workers=1,max_frames=32)),priority=0,max_attempts=1,cache_policy='verify-reuse',
        destination=dict(path='render',owner_token='asset-preview'))


def verify_preview(path, expected_key=None):
    from PIL import Image
    root=Path(path);record=decode_json(read_source_bytes(root/'preview.json'))
    require(isinstance(record,dict) and record.get('kind')=='asset-library-preview' and record.get('schema_version')==1,
            'CACHE','Invalid preview record.')
    require(expected_key is None or record['key']==expected_key,'CACHE','Preview key does not match current source/settings.')
    require(object_hash(record.get('identity'))==record['key'],'CACHE','Preview identity does not match its immutable key.')
    require(record['identity']['settings']=={k:v for k,v in record['settings'].items() if k!='resolution'},
            'CACHE','Cached settings changed independently of identity.')
    require(isinstance(record.get('files'),list) and len(record['files'])<=MAX_FILES,'CACHE','Invalid cache inventory.')
    require(isinstance(record.get('images'),list) and 1<=len(record['images'])<=32,'CACHE','Invalid image inventory.')
    expected={'preview.json'}; inventory={}
    for item in record['files']:
        _cache_path(item['path']);require(item['path'] not in expected,'CACHE','Duplicate cached file.')
        expected.add(item['path']);inventory[item['path']]=item;payload=_cache_bytes(root/item['path'])
        require(len(payload)==item['bytes'] and hashlib.sha256(payload).hexdigest()==item['sha256'],'CACHE','Cached output changed.')
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    require(actual==expected,'CACHE','Unexpected or missing cache output.')
    for item in record['images']:
        require(item['path'] in inventory,'CACHE','Image is outside the verified inventory.')
        require(item['sha256']==inventory[item['path']]['sha256'] and item['bytes']==inventory[item['path']]['bytes'],
                'CACHE','Image reference changed independently of inventory.')
        with Image.open(io.BytesIO(read_source_bytes(root/item['path']))) as picture:
            require(list(picture.size)==record['settings']['resolution'],'CACHE','Cached image dimensions changed.')
            picture.load()
    return record


def create_preview(library, reference, cache_root, *, config=None, logical_path='runtime/asset.glb', executable=None, cancelled=None):
    require(isinstance(library,AssetLibrary),'LIBRARY','Use the canonical AssetLibrary owner.')
    config=settings(config);closure=_closure(library,reference);manifest=library.resolve(reference)
    from render3d_blender import BlenderBackend
    from render3d_snapshot import Scene3DAdapter,create_snapshot
    from rig_jobs import create_run
    backend=BlenderBackend(executable);identity=backend.inspect_version()
    source=library.read_file(reference,logical_path)
    require(source.startswith(b'glTF'),'FORMAT','Preview requires a verified GLB payload.')
    source_hash=hashlib.sha256(source).hexdigest()
    require(len(source)*(len(config['times'])*16+2) <= 512*1024**2,'BUDGET','Source/frame disk estimate exceeds preview budget.')
    key_input=dict(version=VERSION,closure=closure,settings=config,source=source_hash,backend=identity,
                   code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    key=object_hash(key_input)
    root=Path(cache_root).absolute()
    require(not any(p.is_symlink() for p in (root,*root.parents)),'ROOT','Cache paths cannot contain symlinks.')
    if not root.exists():root.mkdir(mode=0o700,parents=False)
    require(root.is_dir() and not root.is_symlink(),'ROOT','Preview cache must be a real private directory.')
    if os.name=='posix':require(root.stat().st_uid==os.getuid() and root.stat().st_mode&0o077==0,'ROOT','Preview cache must be private.')
    target=root/key
    if target.exists() or target.is_symlink():return verify_preview(target,key)
    stage=Path(tempfile.mkdtemp(prefix='.preview-',dir=root))
    try:
        (stage/'source.glb').write_bytes(source)
        rig=create_run(stage/'source.glb',stage/'native',fixture=manifest['provenance']['fixture'],limits={'influences':8})
        _check_geometry_budget(rig.geometry,len(config['times']))
        images=[];usage=[]
        provenance=dict(origin='procedural-fixture' if manifest['provenance']['fixture'] else 'import',
            fixture=manifest['provenance']['fixture'],tool='asset-library-preview-'+VERSION,
            inputs=[],attribution=manifest['metadata']['author'],license=manifest['metadata']['license'])
        import numpy as np
        geometries=[_geometry(rig,config,Fraction(value)) for value in config['times']]
        framing=np.concatenate([mesh.positions for geometry in geometries for mesh in geometry.meshes])
        framing_bounds=(framing.min(axis=0),framing.max(axis=0))
        for index,(value,geometry) in enumerate(zip(config['times'],geometries)):
            require(cancelled is None or not cancelled(),'CANCELLED','Preview cancelled before publication.')
            sample=stage/('sample-'+str(index));sample.mkdir()
            _scene(sample/'scene',geometry,config,provenance,framing_bounds)
            create_snapshot(sample/'scene',sample/'snapshot',Scene3DAdapter())
            request=_request(sample/'snapshot',config)
            (sample/'request.json').write_text(json.dumps(request,sort_keys=True))
            result=backend.render_frames(request,sample/'snapshot',sample/'render',cancelled=cancelled)
            require(result['verification']['status']=='passed','VERIFY','Renderer output verification did not pass.')
            image=result['images'][0]['file'];images.append(dict(path=f'sample-{index}/render/'+image['path'],time=value,
                sha256=image['sha256'],bytes=image['bytes']))
            usage.append(result['usage'])
        require(_closure(library,reference)==closure,'STALE','Library source changed during preview.')
        files=[]
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                require(not path.is_symlink(),'PATH','Unexpected preview symlink.')
                payload=_cache_bytes(path);files.append(dict(path=path.relative_to(stage).as_posix(),bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest()))
        require(len(files)<=MAX_FILES,'BUDGET','Preview inventory exceeded file budget.')
        record=dict(schema_version=1,kind='asset-library-preview',key=key,identity=key_input,asset=reference,closure=closure,
            settings=dict(config,resolution=[config['width'],config['height']]),backend=identity,images=images,files=files,
            usage=usage,verification='decoded-and-hash-verified',visual_review='not-reviewed',asset_readiness='unchanged')
        (stage/'preview.json').write_text(json.dumps(record,sort_keys=True))
        verify_preview(stage,key)
        _rename_no_replace(stage,target)
        return record
    finally:
        if stage.exists():shutil.rmtree(stage)


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create');create.add_argument('--library',type=Path,required=True)
    create.add_argument('--reference',type=Path,required=True);create.add_argument('--settings',type=Path)
    create.add_argument('--cache',type=Path,required=True);create.add_argument('--source-path',default='runtime/asset.glb')
    create.add_argument('--blender');verify=sub.add_parser('verify');verify.add_argument('path',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=='verify':result=verify_preview(args.path)
        else:
            reference=decode_json(read_source_bytes(args.reference,1024**2))
            config=decode_json(read_source_bytes(args.settings,1024**2)) if args.settings else None
            result=create_preview(AssetLibrary(args.library),reference,args.cache,config=config,
                                  logical_path=args.source_path,executable=args.blender)
        print(json.dumps(result,sort_keys=True));return 0
    except (RigError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps(dict(status='failed',diagnostic=exc.report if isinstance(exc,RigError) else dict(message=str(exc)))))
        return 2


if __name__=='__main__':raise SystemExit(main())
