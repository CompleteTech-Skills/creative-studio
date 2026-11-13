"""Explicit bounded LODs for smooth unrigged geometry; source meshes stay untouched.

Uses the preserved component-local remesher. Rich vertex attributes require an
appropriate backend and are rejected, never silently discarded. Measurements are
sampled surface distances and pixel silhouettes, not Hausdorff or artistic proof.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import tempfile
import shutil
import numpy as np
from atomic_publish import _rename_no_replace
from rig_contracts import DEFAULT_LIMITS
from rig_errors import require
from rig_models import Geometry, validate_geometry, save_geometry
from model3d_topology import topology_report, clean_mesh, orient_mesh, _limits
from model3d_optimization_remesh import vertex_cluster_remesh


def mesh_hash(mesh):
    """Geometry/identity dependency fingerprint, including native vertex data."""
    h=hashlib.sha256()
    h.update(json.dumps([mesh.id,mesh.name,mesh.region,mesh.material,mesh.source_node,
                         mesh.morph_defaults],sort_keys=True,allow_nan=False).encode())
    arrays=[('positions',mesh.positions),('triangles',mesh.triangles)]
    arrays += [('attribute:'+k,v) for k,v in sorted(mesh.attributes.items())]
    arrays += [('morph:'+n+':'+k,v) for n,t in sorted(mesh.morphs.items()) for k,v in sorted(t.items())]
    for key,a in arrays:
        h.update(json.dumps([key,a.dtype.str,a.shape]).encode());h.update(a.tobytes(order='C'))
    return h.hexdigest()


def invalidated_derivatives(before, after, dependencies):
    """Return names bound to changed/removed mesh fingerprints; never mutate records.

    Dependencies map derivative IDs to {mesh_id: expected_mesh_hash}. Unknown
    referenced meshes invalidate. Nonmesh material/source dependencies remain
    the caller's responsibility and must not be represented as mesh hashes.
    """
    require(isinstance(dependencies,dict) and all(isinstance(k,str) and isinstance(v,dict)
        and all(isinstance(m,str) and isinstance(h,str) and len(h)==64 for m,h in v.items())
        for k,v in dependencies.items()),'MODEL3D_LOD_DEPENDENCY','Expected hash-bound mesh dependencies.')
    old={m.id:mesh_hash(m) for m in before.meshes};new={m.id:mesh_hash(m) for m in after.meshes}
    return sorted(k for k,refs in dependencies.items() if any(old.get(m)!=h or new.get(m)!=h for m,h in refs.items()))


def repair_mesh(mesh, *, policy, limits=None):
    """Copy-based native cleanup/orientation, controlled by explicit boolean policy."""
    allowed={'remove_degenerate','remove_duplicate_faces','remove_unused_vertices','weld_exact_vertices','orient','outward'}
    require(isinstance(policy,dict) and set(policy)<=allowed and all(type(v) is bool for v in policy.values()),
            'MODEL3D_REPAIR_POLICY','Repair flags must be explicitly selected booleans.')
    require(not policy.get('outward') or policy.get('orient'),'MODEL3D_REPAIR_POLICY','Outward repair requires orientation.')
    before=topology_report(mesh,limits)
    result=clean_mesh(mesh,**{k:v for k,v in policy.items() if k not in ('orient','outward')},limits=limits)
    if policy.get('orient'):result=orient_mesh(result,outward=policy.get('outward',False),limits=limits)
    return result,dict(before=before,after=topology_report(result,limits),policy=dict(policy),
                       source_sha256=mesh_hash(mesh),result_sha256=mesh_hash(result))


def _sample_distance(source,target,samples,pair_budget):
    # Closest point on triangles, including interiors and clamped edge segments.
    idx=np.unique(np.linspace(0,len(source.positions)-1,min(samples,len(source.positions)),dtype=int))
    require(len(idx)*len(target.triangles)<=pair_budget,'MODEL3D_LOD_METRIC_BUDGET','Distance pair budget exceeded.')
    tri=target.positions[target.triangles].astype(float);a,b,c=tri[:,0],tri[:,1],tri[:,2]
    ab=b-a;ac=c-a;n=np.cross(ab,ac);den=np.einsum('ij,ij->i',n,n)
    require(np.all(den>0) and np.isfinite(den).all(),'MODEL3D_LOD_NUMERIC','Distance metric requires stable triangles.')
    distances=[]
    for p in source.positions[idx]:
        ap=p-a;plane=p-(np.einsum('ij,ij->i',ap,n)/den)[:,None]*n
        u=np.cross(b-a,plane-a);v=np.cross(c-b,plane-b);w=np.cross(a-c,plane-c)
        inside=(np.einsum('ij,ij->i',u,n)>=0)&(np.einsum('ij,ij->i',v,n)>=0)&(np.einsum('ij,ij->i',w,n)>=0)
        best=np.where(inside,np.sum((plane-p)**2,axis=1),np.inf)
        for x,y in ((a,b),(b,c),(c,a)):
            edge=y-x;d=np.einsum('ij,ij->i',edge,edge)
            t=np.clip(np.einsum('ij,ij->i',p-x,edge)/d,0,1)
            best=np.minimum(best,np.sum((x+t[:,None]*edge-p)**2,axis=1))
        distances.append(float(np.sqrt(best.min())))
    return max(distances),len(idx)


def _silhouette(mesh,axes,bounds,resolution,pixel_budget):
    minimum,maximum=bounds;extent=maximum-minimum
    require(np.all(extent>0),'MODEL3D_LOD_METRIC','Silhouette planes need nonzero extents.')
    uv=(mesh.positions[:,axes]-minimum)/extent*(resolution-2)+.5
    mask=np.zeros((resolution,resolution),dtype=bool);operations=0
    for t in uv[mesh.triangles]:
        low=np.maximum(0,np.floor(t.min(axis=0)).astype(int));high=np.minimum(resolution-1,np.ceil(t.max(axis=0)).astype(int))
        operations+=int(np.prod(high-low+1))
        require(operations<=pixel_budget,'MODEL3D_LOD_METRIC_BUDGET','Silhouette raster budget exceeded.')
        x,y=np.meshgrid(np.arange(low[0],high[0]+1)+.5,np.arange(low[1],high[1]+1)+.5)
        signs=[]
        for a,b in zip(t,np.roll(t,-1,axis=0)):signs.append((b[0]-a[0])*(y-a[1])-(b[1]-a[1])*(x-a[0]))
        signs=np.asarray(signs);inside=np.all(signs>=0,axis=0)|np.all(signs<=0,axis=0)
        mask[low[1]:high[1]+1,low[0]:high[0]+1]|=inside
    return mask


def compare_meshes(source,target,*,samples=128,resolution=64,pair_budget=2_000_000,pixel_budget=4_000_000):
    """Symmetric sampled vertex-to-surface distance; three orthographic pixel IoUs."""
    require(type(samples) is int and 1<=samples<=1024 and type(resolution) is int and 8<=resolution<=256,
            'MODEL3D_LOD_METRIC','Metric samples/resolution exceed bounded ranges.')
    for v in (pair_budget,pixel_budget):require(type(v) is int and 1<=v<=20_000_000,'MODEL3D_LOD_METRIC_BUDGET','Invalid metric work budget.')
    # Canonical geometry bounds ensure numerically stable metric units and raster indices.
    for m in (source,target):
        require(np.max(np.abs(m.positions))<=1e10 and np.min(np.ptp(m.positions,axis=0))>=1e-10,
                'MODEL3D_LOD_NUMERIC','Metrics support finite nonflat geometry in bounded canonical coordinates.')
    forward,n=_sample_distance(source,target,samples,pair_budget);reverse,k=_sample_distance(target,source,samples,pair_budget)
    views={}
    for label,axes in [('front',(0,1)),('side',(2,1)),('top',(0,2))]:
        points=np.concatenate([source.positions[:,axes],target.positions[:,axes]])
        bounds=(points.min(axis=0),points.max(axis=0))
        a=_silhouette(source,axes,bounds,resolution,pixel_budget);b=_silhouette(target,axes,bounds,resolution,pixel_budget)
        union=int(np.count_nonzero(a|b));require(union>0,'MODEL3D_LOD_METRIC','Silhouette contains no sampled pixels.')
        views[label]=dict(iou=float(np.count_nonzero(a&b)/union),source_pixels=int(a.sum()),target_pixels=int(b.sum()))
    return dict(sampled_surface_distance_max=max(forward,reverse),samples=[n,k],silhouettes=views,
                resolution=resolution,limitations=['Finite vertex samples are not a Hausdorff bound.','Three orthographic pixel views do not establish all-view silhouette or visual approval.'])


@dataclass(frozen=True)
class LODResult:
    levels: tuple[Geometry,...]
    report: dict


def generate_lods(geometry,*,cell_sizes,source_version,regenerate_normals=False,max_distance=.1,min_silhouette_iou=.85,
                  budgets=None,per_part=None,protected_vertices=None,limits=None):
    """Generate LOD0 plus explicit increasing grid sizes. Reject unmet budgets/quality.

    Mesh/material identities, independent parts and all texture payloads remain.
    Simplification supports smooth untextured position-only geometry; NORMAL may
    be replaced only under regenerate_normals=True. UV/hard-edge/morph data reject.
    """
    limits=_limits(limits);validate_geometry(geometry,limits)
    require(type(regenerate_normals) is bool,'MODEL3D_LOD_POLICY','Normal policy must be boolean.')
    require(isinstance(cell_sizes,(list,tuple)) and 2<=len(cell_sizes)<=5 and all(type(v) in (int,float) and 0<v<=1e100 and math.isfinite(v) for v in cell_sizes)
            and all(a<b for a,b in zip(cell_sizes,cell_sizes[1:])), 'MODEL3D_LOD_POLICY','Supply 2–5 strictly increasing positive cell sizes.')
    require(type(max_distance) in (int,float) and 0<=max_distance<=1e100 and math.isfinite(max_distance)
            and type(min_silhouette_iou) in (int,float) and 0<=min_silhouette_iou<=1,'MODEL3D_LOD_POLICY','Invalid distance/silhouette thresholds.')
    require(isinstance(budgets or {},dict) and set(budgets or {})<= {'vertices','triangles','materials','texture_pixels'},'MODEL3D_LOD_BUDGET','Unknown budget.')
    require(per_part is None or isinstance(per_part,dict),'MODEL3D_LOD_BUDGET','Part budgets must be a mapping.')
    require(protected_vertices is None or isinstance(protected_vertices,dict),'MODEL3D_LOD_POLICY','Protected vertices must be a mesh mapping.')
    protected_vertices=dict(protected_vertices or {})
    require(set(protected_vertices)<=set(geometry.by_id()),'MODEL3D_LOD_POLICY','Unknown protected mesh ID.')
    budgets=dict(budgets or {});per_part=dict(per_part or {})
    require(set(per_part)<=set(geometry.by_id()) and all(isinstance(v,dict) and set(v)<={'vertices','triangles'} for v in per_part.values()),'MODEL3D_LOD_BUDGET','Invalid per-part budget.')
    for value in [*budgets.values(),*(v for p in per_part.values() for v in p.values())]:
        require(type(value) is int and value>0,'MODEL3D_LOD_BUDGET','Budgets must be positive integers.')
    require(not geometry.images and not geometry.textures,'MODEL3D_LOD_ATTRIBUTE','Textured simplification needs a UV-aware backend; retain source and choose another policy.')
    require(len(geometry.materials)<=budgets.get('materials',4096),'MODEL3D_LOD_BUDGET','Material budget exceeded.')
    estimated=sum(m.positions.nbytes+m.triangles.nbytes+sum(a.nbytes for a in m.attributes.values())
                  for m in geometry.meshes)*(len(cell_sizes)+2)*4
    require(estimated<=limits['memory_bytes'],'MODEL3D_LOD_BUDGET','Retained LOD working memory exceeds budget before copies.')
    source=copy.deepcopy(geometry);levels=[source];reports=[]
    for size in cell_sizes:
        result=copy.deepcopy(geometry);parts=[]
        for i,m in enumerate(geometry.meshes):
            candidate=copy.deepcopy(m)
            if regenerate_normals and set(candidate.attributes)<={'NORMAL'}:candidate.attributes={}
            reduced=vertex_cluster_remesh(candidate,cell_size=size,source_version=source_version,limits=limits,protected_vertices=protected_vertices.get(m.id,()))
            comparison=compare_meshes(m,reduced.mesh)
            require(comparison['sampled_surface_distance_max']<=max_distance and all(v['iou']>=min_silhouette_iou for v in comparison['silhouettes'].values()),'MODEL3D_LOD_QUALITY','LOD exceeds explicit measured quality thresholds.',asset=m.id)
            part_budget=per_part.get(m.id,{})
            require(len(reduced.mesh.positions)<=part_budget.get('vertices',limits['vertices']) and len(reduced.mesh.triangles)<=part_budget.get('triangles',limits['triangles']),'MODEL3D_LOD_BUDGET','Part exceeds its budget.',asset=m.id)
            result.meshes[i]=reduced.mesh;parts.append(dict(mesh=m.id,source_sha256=mesh_hash(m),sha256=mesh_hash(reduced.mesh),remesh=reduced.report,comparison=comparison))
        validate_geometry(result,limits)
        require(result.vertex_count<=budgets.get('vertices',limits['vertices']) and result.triangle_count<=budgets.get('triangles',limits['triangles']),'MODEL3D_LOD_BUDGET','LOD exceeds hard asset budget.')
        require(result.triangle_count<levels[-1].triangle_count,'MODEL3D_LOD_REDUCTION','Each requested LOD must actually reduce triangle count; choose different explicit cells.')
        levels.append(result);reports.append(dict(level=len(levels)-1,cell_size=size,vertices=result.vertex_count,triangles=result.triangle_count,parts=parts))
    return LODResult(tuple(levels),dict(schema_version=1,kind='hatch-model3d-lods',source_version=source_version,source_meshes={m.id:mesh_hash(m) for m in geometry.meshes},materials_sha256=hashlib.sha256(json.dumps(geometry.materials,sort_keys=True,allow_nan=False).encode()).hexdigest(),levels=reports,source_preserved=True,limitations=['Smooth position-only remeshing; no texture/UV/hard-edge/morph simplification.','Self-intersection measurements unavailable; source orientation/manifold/boundary gates execute.','Registry publication and nonmesh dependency invalidation remain with owning lifecycle service.']))


def export_lods(result,destination,*,format='glb',source_snapshot=None):
    """Exclusively publish all actual LOD exports, sourceLOD0 and reports together."""
    from model3d_interchange import export_mesh
    require(isinstance(result,LODResult),'MODEL3D_LOD_INPUT','Expected generated LOD result.')
    require(len(result.levels)==len(result.report['levels'])+1 and result.report['source_meshes']=={m.id:mesh_hash(m) for m in result.levels[0].meshes},'MODEL3D_LOD_CHANGED','Source LOD changed after measurement.')
    for level,record in zip(result.levels[1:],result.report['levels']):
        require({p['mesh']:p['sha256'] for p in record['parts']}=={m.id:mesh_hash(m) for m in level.meshes},'MODEL3D_LOD_CHANGED','Measured LOD changed before export.')
    for level in result.levels:
        require(hashlib.sha256(json.dumps(level.materials,sort_keys=True,allow_nan=False).encode()).hexdigest()==result.report['materials_sha256'],'MODEL3D_LOD_CHANGED','Measured materials changed before export.')
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.model3d-lod-',dir=destination.parent))
    try:
        if source_snapshot is not None:
            require(result.report['source_meshes']=={m.id:mesh_hash(m) for m in source_snapshot.model.geometry.meshes}
                    and source_snapshot.model.geometry.materials==result.levels[0].materials,'MODEL3D_LOD_CHANGED','Original snapshot differs from measured source.')
        for i,g in enumerate(result.levels):
            original=source_snapshot if i==0 else None
            export_mesh(original.model.geometry if original else g,stage/f'lod{i}',format=format,source_snapshot=original)
        descriptor=save_geometry(stage,result.levels[0])
        (stage/'source.json').write_text(json.dumps(descriptor,sort_keys=True,indent=2)+'\n')
        (stage/'lods.json').write_text(json.dumps(result.report,sort_keys=True,indent=2,allow_nan=False)+'\n')
        _rename_no_replace(stage,destination)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return destination


def main():
    """Standalone additive command; central Model3D CLI remains separately owned."""
    import argparse
    from rig_source_snapshot import import_snapshot
    from rig_errors import RigError
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True,type=Path);p.add_argument('--destination',required=True,type=Path)
    p.add_argument('--cells',required=True,nargs='+',type=float);p.add_argument('--source-version',required=True)
    p.add_argument('--regenerate-normals',action='store_true');p.add_argument('--max-distance',type=float,default=.1)
    p.add_argument('--min-silhouette-iou',type=float,default=.85);p.add_argument('--format',choices=['glb','gltf','obj'],default='glb')
    a=p.parse_args()
    try:
        with import_snapshot(a.input) as snapshot:
            model=snapshot.model
            require(not model.skeleton and not model.weights and not model.clips,'MODEL3D_LOD_ATTRIBUTE','Rigged/animated input requires a skin-aware simplifier.')
            result=generate_lods(model.geometry,cell_sizes=a.cells,source_version=a.source_version,
                regenerate_normals=a.regenerate_normals,max_distance=a.max_distance,min_silhouette_iou=a.min_silhouette_iou)
            export_lods(result,a.destination,format=a.format,source_snapshot=snapshot)
        print(json.dumps(result.report,sort_keys=True));return 0
    except RigError as error:
        print(json.dumps(error.report,sort_keys=True));return 1
    except OSError as error:
        print(json.dumps(dict(code='MODEL3D_LOD_IO',message=str(error))));return 1

if __name__=='__main__':raise SystemExit(main())
