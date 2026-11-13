"""Optional verified Blender planar decimation with UV/material/feature checks.

This is an explicit delimiter-aware DISSOLVE profile, not arbitrary textured
collapse. It preserves original native geometry and publishes only after a
separate Blender reopen/export and native reimport satisfy measured thresholds.
"""
from __future__ import annotations
import copy
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import numpy as np
from PIL import Image
from atomic_publish import _rename_no_replace
from rig_blender import find_blender
from rig_errors import RigError,require,atomic_json,read_json,sha256
from rig_models import validate_geometry,save_geometry
from rig_gltf import export_geometry
from rig_import import load_character_model, _decode_texture
from model3d_topology import _limits,topology_report
from model3d_optimization import compare_meshes,mesh_hash


def _number(value,lower,upper,code):
    require(type(value) in (int,float) and lower<=value<=upper and math.isfinite(value),code,'Numeric policy is outside its finite supported range.')
    return float(value)


def _run(job,root,*,executable,timeout):
    job_path=root/(job['mode']+'-job.json');atomic_json(job_path,job)
    command=[find_blender(executable),'--background','--factory-startup','--disable-autoexec','--threads','2','--python-exit-code','1',
             '--python',str(Path(__file__).with_name('model3d_optimization_blender_worker.py')),'--',str(job_path)]
    try:result=subprocess.run(command,capture_output=True,text=True,timeout=timeout,check=False)
    except subprocess.TimeoutExpired as error:raise RigError('MODEL3D_DECIMATE_TIMEOUT','Blender exceeded its configured deadline.') from error
    require(result.returncode==0 and Path(job['report']).is_file(),'MODEL3D_DECIMATE_BLENDER','Trusted Blender operation failed.',observed=(result.stdout+result.stderr)[-8000:])
    return read_json(job['report'])


def _protected_points(mesh,indices,sharp_angle):
    """Protect explicit features and vertices on geometric hard/boundary edges."""
    require(isinstance(indices,(list,tuple)) and len(indices)<=len(mesh.positions) and all(type(i) is int and 0<=i<len(mesh.positions) for i in indices),
            'MODEL3D_DECIMATE_FEATURE','Protected vertices must be native source indices.')
    points=mesh.positions[mesh.triangles].astype(np.float64);cross=np.cross(points[:,1]-points[:,0],points[:,2]-points[:,0])
    length=np.linalg.norm(cross,axis=1)
    require(np.all(length>1e-14),'MODEL3D_DECIMATE_NUMERIC','Planar decimation needs stable nondegenerate source triangles.')
    normals=cross/length[:,None];edges={}
    for face,tri in enumerate(mesh.triangles):
        for a,b in zip(tri,np.roll(tri,-1)):
            key=tuple(sorted((tuple(mesh.positions[int(a)]),tuple(mesh.positions[int(b)]))))
            edges.setdefault(key,[]).append(face)
    protected={tuple(mesh.positions[i]) for i in indices};hard_edges=[];cosine=math.cos(math.radians(sharp_angle))
    for edge,faces in edges.items():
        require(len(faces)<=2,'MODEL3D_DECIMATE_TOPOLOGY','Geometric nonmanifold edges are unsupported.')
        if len(faces)==1 or np.dot(normals[faces[0]],normals[faces[1]])<cosine:
            protected.update(edge);hard_edges.append(edge)
    require(len(protected)<=4096,'MODEL3D_DECIMATE_FEATURE','Feature-point budget exceeds 4096 per part.')
    return dict(points=np.array(sorted(protected),dtype=float).reshape(-1,3),edges=np.array(hard_edges,dtype=float).reshape(-1,2,3))


def _uv_error(source,target,*,samples=512,pair_budget=2_000_000,distance_tolerance=1e-5):
    """Sample source-vertex UVs against projected target triangle interpolation."""
    if 'TEXCOORD_0' not in source.attributes:return dict(samples=0,max_error=0.,available=False)
    require('TEXCOORD_0' in target.attributes,'MODEL3D_DECIMATE_UV','Target lost UV0.')
    indices=np.unique(np.linspace(0,len(source.positions)-1,min(samples,len(source.positions)),dtype=int))
    require(len(indices)*len(target.triangles)<=pair_budget,'MODEL3D_DECIMATE_METRIC_BUDGET','UV sample pair budget exceeded.')
    tri=target.positions[target.triangles].astype(np.float64);a=tri[:,0];ab=tri[:,1]-a;ac=tri[:,2]-a
    d00=np.einsum('ij,ij->i',ab,ab);d01=np.einsum('ij,ij->i',ab,ac);d11=np.einsum('ij,ij->i',ac,ac)
    den=d00*d11-d01*d01
    require(np.all(den>1e-24),'MODEL3D_DECIMATE_NUMERIC','UV metric requires stable target triangles.')
    uv=target.attributes['TEXCOORD_0'][target.triangles];errors=[];surface_distances=[]
    coordinate_scale=max(1e-12,float(np.max(np.abs(target.positions))),float(np.max(np.abs(source.positions))))
    tie_tolerance=coordinate_scale*np.finfo(np.float64).eps*128
    for i in indices:
        point=source.positions[i];ap=point-a
        d20=np.einsum('ij,ij->i',ap,ab);d21=np.einsum('ij,ij->i',ap,ac)
        v=(d11*d20-d01*d21)/den;w=(d00*d21-d01*d20)/den;u=1-v-w
        projected=a+v[:,None]*ab+w[:,None]*ac
        inside=(u>=0)&(v>=0)&(w>=0)
        distances=np.where(inside,np.sum((projected-point)**2,axis=1),np.inf)
        mapped=u[:,None]*uv[:,0]+v[:,None]*uv[:,1]+w[:,None]*uv[:,2]
        # Outside a triangle, its nearest surface point lies on an edge. Keep
        # that edge's interpolated UV so vertex/edge samples are also exact.
        for left,right in ((0,1),(1,2),(2,0)):
            x=tri[:,left];edge=tri[:,right]-x
            t=np.clip(np.einsum('ij,ij->i',point-x,edge)/np.einsum('ij,ij->i',edge,edge),0,1)
            closest=x+t[:,None]*edge;edge_distance=np.sum((closest-point)**2,axis=1)
            better=edge_distance<distances
            mapped[better]=((1-t)[:,None]*uv[:,left]+t[:,None]*uv[:,right])[better]
            distances=np.minimum(distances,edge_distance)
        distances=np.sqrt(distances);nearest=float(distances.min())
        require(nearest<=distance_tolerance,'MODEL3D_DECIMATE_UV','Source UV sample no longer lies on a corresponding target surface.')
        candidate=distances<=nearest+tie_tolerance
        errors.append(float(np.linalg.norm(mapped[candidate]-source.attributes['TEXCOORD_0'][i],axis=1).min()))
        surface_distances.append(nearest)
    return dict(samples=len(indices),max_error=max(errors),surface_distance_max=max(surface_distances),
                geometric_tie_tolerance_m=tie_tolerance,available=True)


def _material_signature(geometry,index):
    material=geometry.materials[index];pbr=material.get('pbrMetallicRoughness',{})
    result=dict(name=material.get('name'),color=pbr.get('baseColorFactor',[1.,1.,1.,1.]),metallic=pbr.get('metallicFactor',1.),roughness=pbr.get('roughnessFactor',1.),
                alpha=material.get('alphaMode','OPAQUE'),double_sided=material.get('doubleSided',False))
    texture=pbr.get('baseColorTexture')
    if texture is not None:
        image=geometry.images[geometry.textures[texture['index']]['source']]
        with Image.open(io.BytesIO(image.data)) as decoded:
            decoded.load();pixels=decoded.convert('RGBA');result['texture']=dict(size=list(pixels.size),rgba_sha256=hashlib.sha256(pixels.tobytes()).hexdigest())
    return result


def _preflight(geometry,limits):
    validate_geometry(geometry,limits)
    require(geometry.vertex_count*2048+geometry.triangle_count*4096+sum(len(i.data)*8 for i in geometry.images)<=limits['memory_bytes'],
            'MODEL3D_DECIMATE_BUDGET','Conservative Blender working estimate exceeds configured memory.')
    require(not geometry.extensions_used,'MODEL3D_DECIMATE_PROFILE','glTF extensions need a dedicated preservation profile.')
    pixels=0
    for image in geometry.images:
        mime,count=_decode_texture(image.data,limits,pixels)
        require(mime==image.mime_type,'MODEL3D_DECIMATE_PROFILE','Texture MIME does not match strict decoded source.')
        pixels+=count
    names=[m.name or m.id for m in geometry.meshes]
    require(len(set(names))==len(names) and all(isinstance(n,str) and 0<len(n)<=63 and '\x00' not in n for n in names+[m.id for m in geometry.meshes]),
            'MODEL3D_DECIMATE_NAME','Parts need unique nonempty names at most 63 characters.')
    for m in geometry.meshes:
        require(not m.morphs and not m.morph_defaults and set(m.attributes)<={'NORMAL','TEXCOORD_0'},
                'MODEL3D_DECIMATE_PROFILE','Only static normals/UV0 attributes are supported; no morph/color/tangent loss is allowed.')
        require(np.max(np.abs(m.positions))<=10000 and np.min(np.ptp(m.positions,axis=0))>=1e-5,
                'MODEL3D_DECIMATE_NUMERIC','Profile requires nonflat geometry within 10000 canonical meters.')
        report=topology_report(m,limits)
        require(not report['degenerate_faces'] and not report['duplicate_faces'] and not report['nonmanifold_edges'] and not report['inconsistent_edges'],
                'MODEL3D_DECIMATE_TOPOLOGY','Clean source topology before decimation.')
    for material in geometry.materials:
        require(set(material)<={'name','pbrMetallicRoughness','alphaMode','doubleSided'},'MODEL3D_DECIMATE_PROFILE','Only base-color PBR material fields are supported.')
        pbr=material.get('pbrMetallicRoughness',{})
        require(set(pbr)<={'baseColorFactor','metallicFactor','roughnessFactor','baseColorTexture'},'MODEL3D_DECIMATE_PROFILE','Unsupported PBR texture/shader profile.')
        texture=pbr.get('baseColorTexture')
        if texture is not None:
            require(set(texture)<={'index','texCoord'} and texture.get('texCoord',0)==0,'MODEL3D_DECIMATE_PROFILE','Texture transforms and alternate UV sets are unsupported.')
            tex=geometry.textures[texture['index']]
            require(set(tex)=={'source'},'MODEL3D_DECIMATE_PROFILE','Custom texture samplers are unsupported.')
            require(geometry.images[tex['source']].mime_type=='image/png','MODEL3D_DECIMATE_PROFILE','Lossless texture verification currently requires PNG.')
    return names,pixels


def decimate_blender(geometry,destination,*,angle_degrees=1.,sharp_angle_degrees=30.,protected_vertices=None,
                     max_distance=.001,min_silhouette_iou=.99,uv_tolerance=1e-4,point_tolerance=1e-5,
                     budgets=None,limits=None,executable=None,recompute_normals=False,source_snapshot=None):
    """Publish native source, edited/reopened .blend, verified GLB and metrics.

    DISSOLVE preserves delimiter edges and interpolated UVs under this bounded
    profile. It is useful for coplanar/near-planar tessellation, not arbitrary
    target-ratio simplification. Normals may change only under explicit policy.
    """
    limits=_limits(limits);names,texture_pixels=_preflight(geometry,limits)
    angle_degrees=_number(angle_degrees,.001,45.,'MODEL3D_DECIMATE_POLICY')
    sharp_angle_degrees=_number(sharp_angle_degrees,1.,179.,'MODEL3D_DECIMATE_POLICY')
    max_distance=_number(max_distance,0.,100.,'MODEL3D_DECIMATE_POLICY')
    min_silhouette_iou=_number(min_silhouette_iou,0.,1.,'MODEL3D_DECIMATE_POLICY')
    uv_tolerance=_number(uv_tolerance,0.,.1,'MODEL3D_DECIMATE_POLICY');point_tolerance=_number(point_tolerance,1e-7,.001,'MODEL3D_DECIMATE_POLICY')
    require(type(recompute_normals) is bool and (recompute_normals or not any('NORMAL' in m.attributes for m in geometry.meshes)),
            'MODEL3D_DECIMATE_NORMAL','Explicit recompute_normals=True is required to replace source normals.')
    require(protected_vertices is None or isinstance(protected_vertices,dict),'MODEL3D_DECIMATE_FEATURE','Expected per-mesh protected vertex lists.')
    protected_vertices=protected_vertices or {};require(set(protected_vertices)<=set(geometry.by_id()),'MODEL3D_DECIMATE_FEATURE','Unknown protected mesh.')
    require(budgets is None or isinstance(budgets,dict),'MODEL3D_DECIMATE_BUDGET','Budgets must be a mapping.')
    budgets=budgets or {};require(set(budgets)<={'vertices','triangles','materials','texture_pixels'},'MODEL3D_DECIMATE_BUDGET','Unsupported runtime budget.')
    require(all(type(v) is int and 0<v<=2**53 for v in budgets.values()),'MODEL3D_DECIMATE_BUDGET','Runtime budgets must be positive bounded integers.')
    require(texture_pixels<=budgets.get('texture_pixels',16_777_216),'MODEL3D_DECIMATE_BUDGET','Texture pixel budget exceeded.')
    features={m.id:_protected_points(m,protected_vertices.get(m.id,()),sharp_angle_degrees) for m in geometry.meshes}
    points={key:value['points'] for key,value in features.items()}
    require(sum(len(m.positions)*len(points[m.id]) for m in geometry.meshes)<=2_000_000,'MODEL3D_DECIMATE_METRIC_BUDGET','Protected-point worker matching exceeds bounded work budget.')
    target=Path(destination).absolute();target.parent.mkdir(parents=True,exist_ok=True)
    require(not target.exists() and not target.is_symlink(),'MODEL3D_DECIMATE_EXISTS','Use a new destination.')
    root=Path(tempfile.mkdtemp(prefix='.model3d-native-lod-',dir=target.parent))
    try:
        source=root/'source.glb';export_geometry(geometry,source)
        # Reuse strict native admission before Blender sees any generated payload.
        load_character_model(source,limits=limits)
        if source_snapshot is not None:
            from model3d_interchange import export_mesh
            export_mesh(geometry,root/'original',source_snapshot=source_snapshot,limits=limits)
        original=save_geometry(root,geometry);atomic_json(root/'source.json',original)
        common=dict(blend=str(root/'editable.blend'),point_tolerance=point_tolerance)
        create=_run(dict(common,mode='create',input=str(source),angle_degrees=angle_degrees,
                    parts={m.id:dict(protected=points[m.id].tolist()) for m in geometry.meshes},report=str(root/'created.json')),
                    root,executable=executable,timeout=limits['timeout_seconds'])
        reopened=_run(dict(common,mode='reopen',output=str(root/'optimized.glb'),report=str(root/'reopened.json')),
                    root,executable=executable,timeout=limits['timeout_seconds'])
        imported=load_character_model(root/'optimized.glb',limits=limits)
        require(not imported.skeleton and not imported.weights and not imported.clips,'MODEL3D_DECIMATE_PROFILE','Unexpected rig data in static output.')
        output=imported.geometry;actual={m.name:m for m in output.meshes}
        require(len(actual)==len(output.meshes) and set(actual)==set(names),'MODEL3D_DECIMATE_IDENTITY','Named mesh parts changed during native roundtrip.')
        require(output.triangle_count<geometry.triangle_count,'MODEL3D_DECIMATE_REDUCTION','Planar optimization did not reduce actual triangle count.')
        require(output.vertex_count<=budgets.get('vertices',limits['vertices']) and output.triangle_count<=budgets.get('triangles',limits['triangles'])
                and len({m.material for m in output.meshes})<=budgets.get('materials',4096),'MODEL3D_DECIMATE_BUDGET','Optimized output exceeds hard runtime budget.')
        parts=[]
        for name,m in zip(names,geometry.meshes):
            reduced=actual[name];comparison=compare_meshes(m,reduced)
            require(comparison['sampled_surface_distance_max']<=max_distance and all(v['iou']>=min_silhouette_iou for v in comparison['silhouettes'].values()),
                    'MODEL3D_DECIMATE_QUALITY','Reimported geometry exceeds measured distance/silhouette thresholds.',asset=m.id)
            uv=_uv_error(m,reduced,distance_tolerance=max(point_tolerance,max_distance))
            require(uv['max_error']<=uv_tolerance,'MODEL3D_DECIMATE_UV','Reimported UV interpolation exceeds selected tolerance.',asset=m.id)
            maximum=0.
            for point in points[m.id]:maximum=max(maximum,float(np.linalg.norm(reduced.positions-point,axis=1).min()))
            require(maximum<=point_tolerance,'MODEL3D_DECIMATE_FEATURE','Protected hard-edge/attachment point moved or disappeared.',asset=m.id)
            source_edges=features[m.id]['edges']
            require(len(source_edges)*len(reduced.triangles)*3<=4_000_000,'MODEL3D_DECIMATE_METRIC_BUDGET','Hard-edge comparison exceeds bounded work.')
            target_edges=np.concatenate([reduced.triangles[:,[0,1]],reduced.triangles[:,[1,2]],reduced.triangles[:,[2,0]]])
            target_edges=reduced.positions[target_edges]
            for edge in source_edges:
                forward=(np.linalg.norm(target_edges[:,0]-edge[0],axis=1)<=point_tolerance)&(np.linalg.norm(target_edges[:,1]-edge[1],axis=1)<=point_tolerance)
                reverse=(np.linalg.norm(target_edges[:,1]-edge[0],axis=1)<=point_tolerance)&(np.linalg.norm(target_edges[:,0]-edge[1],axis=1)<=point_tolerance)
                require(np.any(forward|reverse),'MODEL3D_DECIMATE_FEATURE','Protected geometric hard-edge connectivity changed.',asset=m.id)
            expected=_material_signature(geometry,m.material);observed=_material_signature(output,reduced.material)
            for key in ('alpha','double_sided','texture'):
                require(expected.get(key)==observed.get(key),'MODEL3D_DECIMATE_MATERIAL','Material mode or exact texture pixels changed.',asset=m.id)
            require(expected['name'] is None or expected['name']==observed['name'],'MODEL3D_DECIMATE_MATERIAL','Named material identity changed.',asset=m.id)
            require(np.allclose(expected['color'],observed['color'],atol=1e-6,rtol=0)
                    and abs(expected['metallic']-observed['metallic'])<=1e-6 and abs(expected['roughness']-observed['roughness'])<=1e-6,
                    'MODEL3D_DECIMATE_MATERIAL','PBR factors changed during decimation.',asset=m.id)
            parts.append(dict(id=m.id,name=name,source_sha256=mesh_hash(m),reimport_sha256=mesh_hash(reduced),comparison=comparison,uv=uv,
                              protected_points=len(points[m.id]),protected_hard_edges=len(source_edges),hard_edges_preserved=True,protected_max_error_m=maximum,material=observed))
        report=dict(schema_version=1,kind='hatch-model3d-blender-planar-optimization',algorithm='Blender DISSOLVE',
            delimiters=['NORMAL','MATERIAL','SEAM','SHARP','UV'],blender_version=reopened['blender_version'],save_reopen=True,
            source_triangles=geometry.triangle_count,texture_pixels=texture_pixels,triangles=output.triangle_count,vertices=output.vertex_count,
            angle_degrees=angle_degrees,sharp_angle_degrees=sharp_angle_degrees,budgets=dict(budgets),parts=parts,
            policy=dict(max_distance_m=max_distance,min_silhouette_iou=min_silhouette_iou,uv_tolerance=uv_tolerance,
                        point_tolerance_m=point_tolerance,recompute_normals=recompute_normals,
                        protected_vertices=copy.deepcopy(protected_vertices)),
            source_descriptor=original,limits=limits,artistic_approval=False,
            limitations=['Planar/angle-limited DISSOLVE, not general textured target-ratio collapse.',
                'Finite source-vertex UV/surface samples and three orthographic silhouettes are not continuous global bounds.',
                'Geometric hard edges and explicit feature vertices only; authored normals require explicit replacement policy.',
                'Static base-color PNG UV0 PBR profile only; skins, morphs, custom samplers/shaders and other attributes reject.',
                'Robust self-intersection and renderer/aesthetic approval remain separate.'])
        report['files']={str(p.relative_to(root)):sha256(p) for p in root.rglob('*') if p.is_file() and not p.name.endswith('-job.json')}
        # Jobs contain private absolute staging paths and are operational scratch.
        for p in root.glob('*-job.json'):p.unlink()
        atomic_json(root/'optimization.json',report);_rename_no_replace(root,target)
        return report
    finally:
        if root.exists():shutil.rmtree(root)
