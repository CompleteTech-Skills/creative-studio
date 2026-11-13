"""Bounded calibrated silhouette intersection, native voxel surface and reprojection.

Column vectors; canonical +Y up, meters, camera-local -Z forward/+Y up.
A visual hull constrains a volume; it never recovers unseen concavities/anatomy.
"""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import math
import re
import numpy as np
from rig_errors import finite_json, require
from rig_models import Geometry, Mesh, validate_geometry
from rig_contracts import DEFAULT_LIMITS
from camera3d_math import _matrix

DEFAULT_BUDGET = dict(voxels=262144, pixels=1048576, views=16, raster_samples=20000000,
                      vertices=1000000, triangles=2000000, memory_bytes=512*1024**2)


def _budgets(values):
    require(values is None or isinstance(values, dict), 'MODEL3D_HULL_LIMIT', 'Limits must be an object.')
    result = dict(DEFAULT_BUDGET)
    for key, value in (values or {}).items():
        require(key in result and type(value) is int and 0 < value <= DEFAULT_BUDGET[key],
                'MODEL3D_HULL_LIMIT', 'Limits must be bounded positive integers.', asset=str(key))
        result[key] = value
    return result


def _array(value, shape, label):
    # Validate before conversion so boolean/string calibration is never numerical evidence.
    require(isinstance(value, (list, tuple, np.ndarray)), 'MODEL3D_HULL_CAMERA', 'Expected numeric '+label)
    try:raw=np.asarray(value)
    except (TypeError,ValueError):
        require(False,'MODEL3D_HULL_CAMERA','Invalid '+label)
    require(raw.shape == shape and raw.dtype.kind in 'fiu' and np.isfinite(raw).all()
            and np.max(np.abs(raw)) <= 1e100, 'MODEL3D_HULL_CAMERA', 'Invalid '+label)
    return raw.astype(float)


def _evidence(value):
    """Bound the caller's JSON receipt before copying or serialization."""
    require(isinstance(value,dict) and bool(value),'MODEL3D_HULL_EVIDENCE','Explicit source/mask/calibration evidence is required.')
    stack=[(value,0)];count=0
    while stack:
        item,depth=stack.pop();count+=1
        require(depth<=32 and count<=4096,'MODEL3D_HULL_EVIDENCE','Evidence metadata exceeds node/depth budget.')
        if isinstance(item,dict):
            require(len(item)<=1024 and all(isinstance(k,str) and len(k)<=256 for k in item),
                    'MODEL3D_HULL_EVIDENCE','Evidence object keys must be bounded strings.')
            stack.extend((v,depth+1) for v in item.values())
        elif isinstance(item,list):
            require(len(item)<=4096,'MODEL3D_HULL_EVIDENCE','Evidence array is too long.')
            stack.extend((v,depth+1) for v in item)
        else:
            require(item is None or type(item) is bool or isinstance(item,str) and len(item)<=8192
                    or type(item) in (int,float) and -1e100<=item<=1e100 and math.isfinite(item),
                    'MODEL3D_HULL_EVIDENCE','Evidence must contain bounded JSON values.')
    finite_json(value)
    size=0
    for chunk in json.JSONEncoder(ensure_ascii=True,allow_nan=False).iterencode(value):
        size+=len(chunk)
        require(size<=65536,'MODEL3D_HULL_EVIDENCE','Evidence JSON exceeds64KiB.')
    return deepcopy(value)


@dataclass(frozen=True)
class CalibratedView:
    name: str
    mask: np.ndarray
    world: np.ndarray
    projection: np.ndarray
    calibration: str
    evidence: dict
    camera: dict


@dataclass
class HullResult:
    geometry: Geometry
    occupancy: np.ndarray
    report: dict


def calibrated_view(mask, *, name, calibration, evidence, camera_to_world=None,
                    world_to_camera=None, intrinsics=None, orthographic=None,
                    near=.01, far=100.):
    """Snapshot a complete foreground silhouette and explicit calibration.

    Perspective K uses [fx,skew,cx;0,fy,cy;0,0,1], raster top-left pixel edges.
    Orthographic gives (left,right,bottom,top) in camera-local meters.
    Exactly one extrinsic direction and one projection must be supplied. Unknown
    calibration rejects; estimated matrices stay labelled estimated throughout.
    evidence is the caller's explicit source/mask/calibration receipt, never approval.
    """
    require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',name) is not None,
            'MODEL3D_HULL_CAMERA','View name must be a portable identifier.')
    require(calibration in ('known','estimated'), 'MODEL3D_HULL_CALIBRATION',
            'Supply known or explicitly estimated calibration; unknown views cannot carve a precise hull.')
    evidence=_evidence(evidence)
    require(isinstance(mask,np.ndarray) and mask.dtype == np.bool_ and mask.ndim == 2
            and 1 <= mask.shape[0] <= 4096 and 1 <= mask.shape[1] <= 4096
            and mask.size <= DEFAULT_BUDGET['pixels'] and bool(mask.any()),
            'MODEL3D_HULL_MASK','Provide a bounded nonempty boolean silhouette without implicit segmentation.')
    require((camera_to_world is None) != (world_to_camera is None), 'MODEL3D_HULL_CAMERA', 'Supply exactly one extrinsic convention.')
    require((intrinsics is None) != (orthographic is None), 'MODEL3D_HULL_CAMERA', 'Supply intrinsics or orthographic bounds.')
    require(type(near) in (int,float) and type(far) in (int,float) and 0 < near < far <= 1e6,
            'MODEL3D_HULL_CAMERA','Require finite positive near < far <= 1e6 meters.')
    try:
        raw=_array(camera_to_world if camera_to_world is not None else world_to_camera,(4,4),'extrinsics')
        world=_matrix(raw if camera_to_world is not None else np.linalg.inv(raw),rigid=True).copy()
    except (ValueError,np.linalg.LinAlgError) as exc:
        require(False,'MODEL3D_HULL_CAMERA','Extrinsics must be a finite rigid proper camera transform.')
    height,width=mask.shape
    p=np.zeros((4,4))
    if intrinsics is not None:
        k=_array(intrinsics,(3,3),'intrinsics')
        require(np.array_equal(k[2],[0,0,1]) and k[1,0] == 0 and k[0,0] > 0 and k[1,1] > 0,
                'MODEL3D_HULL_CAMERA','Intrinsics require positive fx/fy and upper-triangular pinhole K.')
        p[0]=[2*k[0,0]/width,-2*k[0,1]/width,1-2*k[0,2]/width,0]
        p[1]=[0,2*k[1,1]/height,2*k[1,2]/height-1,0]
        p[2,2]=-(far+near)/(far-near);p[2,3]=-2*far*near/(far-near);p[3,2]=-1
        camera=dict(projection='perspective',intrinsics=k.tolist())
    else:
        rect=_array(orthographic,(4,),'orthographic bounds');left,right,bottom,top=rect
        require(left < right and bottom < top, 'MODEL3D_HULL_CAMERA','Orthographic bounds must have positive spans.')
        p[0,0]=2/(right-left);p[0,3]=-(right+left)/(right-left)
        p[1,1]=2/(top-bottom);p[1,3]=-(top+bottom)/(top-bottom)
        p[2,2]=-2/(far-near);p[2,3]=-(far+near)/(far-near);p[3,3]=1
        camera=dict(projection='orthographic',bounds_m=rect.tolist())
    try:_matrix(p)
    except ValueError:require(False,'MODEL3D_HULL_CAMERA','Calibration produces an invalid projection.')
    frozen=mask.copy();frozen.setflags(write=False);world.setflags(write=False);p.setflags(write=False)
    camera.update(camera_to_world=world.tolist(),near_m=float(near),far_m=float(far),
                  convention='camera-local -Z view, +Y up; top-left raster pixel edges')
    receipt=evidence
    receipt['mask']=dict(width=width,height=height,encoding='row-major bool uint8 0/1',
                         sha256=hashlib.sha256(frozen.tobytes()).hexdigest())
    return CalibratedView(name,frozen,world,p,calibration,receipt,camera)


def _check_view(view):
    require(isinstance(view,CalibratedView) and isinstance(view.camera,dict)
            and isinstance(view.evidence,dict),'MODEL3D_HULL_CAMERA','Expected a calibrated view record.')
    record=view.camera
    require({'projection','camera_to_world','near_m','far_m','convention'}<=set(record),
            'MODEL3D_HULL_CAMERA','Camera calibration record is incomplete.')
    require(record['projection'] in ('perspective','orthographic'),
            'MODEL3D_HULL_CAMERA','Unsupported camera projection.')
    key='intrinsics' if record['projection']=='perspective' else 'bounds_m'
    require(key in record,'MODEL3D_HULL_CAMERA','Camera calibration record is incomplete.')
    candidate=calibrated_view(view.mask,name=view.name,calibration=view.calibration,evidence=view.evidence,
                camera_to_world=record['camera_to_world'],near=record['near_m'],far=record['far_m'],
                **({'intrinsics':record[key]} if key=='intrinsics' else {'orthographic':record[key]}))
    require(isinstance(view.world,np.ndarray) and isinstance(view.projection,np.ndarray)
            and np.array_equal(candidate.world,view.world) and np.array_equal(candidate.projection,view.projection)
            and candidate.evidence['mask']==view.evidence.get('mask'),
            'MODEL3D_HULL_CAMERA','Calibrated view matrix or mask differs from its receipt.')


def _projection(points, view):
    inverse=np.linalg.inv(view.world)
    camera=points@inverse[:3,:3].T+inverse[:3,3]
    clip=np.column_stack((camera,np.ones(len(points))))@view.projection.T
    w=clip[:,3]
    inside=(w>0)&np.all(clip[:,:3]>=-w[:,None],axis=1)&np.all(clip[:,:3]<=w[:,None],axis=1)&(camera[:,2]<0)
    safe=np.where(w>0,w,1)
    pixel=np.column_stack(((clip[:,0]/safe+1)*view.mask.shape[1]/2,
                           (1-clip[:,1]/safe)*view.mask.shape[0]/2))
    return pixel,inside


def _clip_triangle(triangle):
    polygon=list(triangle)
    for axis,sign in ((0,1),(0,-1),(1,1),(1,-1),(2,1),(2,-1)):
        result=[]
        if not polygon:break
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):
            da=a[3]+sign*a[axis];db=b[3]+sign*b[axis]
            if da>=0:result.append(a)
            if (da>=0)!=(db>=0):result.append(a+(b-a)*(da/(da-db)))
        polygon=result
    return polygon


def rasterize_silhouette(geometry, view, *, limits=None):
    """Rasterize actual clipped surface triangles at pixel centers, no depth proxy.

    Union coverage needs no z-buffer. Near/far/frustum clipping occurs in
    homogeneous space before division. Edge-on triangles contribute no pixels.
    """
    budget=_budgets(limits)
    _check_view(view)
    native=dict(DEFAULT_LIMITS,vertices=budget['vertices'],triangles=budget['triangles'],memory_bytes=budget['memory_bytes'])
    validate_geometry(geometry,native)
    require(view.mask.size <= budget['pixels'],'MODEL3D_HULL_LIMIT','Raster pixel budget exceeded.')
    out=np.zeros(view.mask.shape,dtype=bool);samples=0;h,w=out.shape
    pv=view.projection@np.linalg.inv(view.world)
    for mesh in geometry.meshes:
        require(len(mesh.positions)*32 <= budget['memory_bytes'],'MODEL3D_HULL_LIMIT','Projection working memory exceeded.')
        clip=np.column_stack((mesh.positions,np.ones(len(mesh.positions))))@pv.T
        for tri in mesh.triangles:
            poly=_clip_triangle(clip[tri])
            for i in range(1,len(poly)-1):
                corners=np.asarray([poly[0],poly[i],poly[i+1]])
                if np.any(corners[:,3]<=0):continue
                xy=corners[:,:2]/corners[:,3,None]
                xy=np.column_stack(((xy[:,0]+1)*w/2,(1-xy[:,1])*h/2))
                lo=np.maximum(0,np.ceil(xy.min(axis=0)-.5)).astype(int)
                hi=np.minimum([w-1,h-1],np.floor(xy.max(axis=0)-.5)).astype(int)
                if np.any(hi<lo):continue
                a,b=xy[1]-xy[0],xy[2]-xy[0]
                area=float(a[0]*b[1]-a[1]*b[0])
                if abs(area)<1e-14:continue
                samples+=(hi[0]-lo[0]+1)*(hi[1]-lo[1]+1)
                require(samples<=budget['raster_samples'],'MODEL3D_HULL_LIMIT','Triangle raster work budget exceeded.')
                xs=np.arange(lo[0],hi[0]+1)+.5
                for y in range(lo[1],hi[1]+1):
                    px=np.column_stack((xs,np.full(len(xs),y+.5)))
                    signs=[(b[0]-a[0])*(px[:,1]-a[1])-(b[1]-a[1])*(px[:,0]-a[0])
                           for a,b in zip(xy,np.roll(xy,-1,axis=0))]
                    selected=np.all(np.asarray(signs)*(1 if area>0 else -1)>=-1e-10,axis=0)
                    out[y,lo[0]:hi[0]+1]|=selected
    return out


def _manifold(mesh):
    # Every edge needs two opposite incidences; every vertex link one cycle.
    edges={};links=[{} for _ in mesh.positions]
    for triangle in mesh.triangles:
        a,b,c=map(int,triangle)
        for x,y in ((a,b),(b,c),(c,a)):
            edges.setdefault((min(x,y),max(x,y)),[]).append(x<y)
        for center,x,y in ((a,b,c),(b,c,a),(c,a,b)):
            links[center].setdefault(x,set()).add(y);links[center].setdefault(y,set()).add(x)
    require(all(len(refs)==2 and refs[0]!=refs[1] for refs in edges.values()),
            'MODEL3D_HULL_NONMANIFOLD','Voxel contacts create nonmanifold edges; revise resolution or masks.')
    for link in links:
        require(link and all(len(adj)==2 for adj in link.values()),'MODEL3D_HULL_NONMANIFOLD','Surface has a nonmanifold vertex.')
        remaining=set(link);stack=[remaining.pop()]
        while stack:
            for neighbor in link[stack.pop()]:
                if neighbor in remaining:remaining.remove(neighbor);stack.append(neighbor)
        require(not remaining,'MODEL3D_HULL_NONMANIFOLD','Diagonal voxel contacts create disconnected vertex links; revise observations.')


def _surface(occupancy, bounds, budget):
    # Integer grid corners are welded before any world-space conversion.
    quads=[((-1,0,0),[(0,0,0),(0,0,1),(0,1,1),(0,1,0)]),
           ((1,0,0),[(1,0,0),(1,1,0),(1,1,1),(1,0,1)]),
           ((0,-1,0),[(0,0,0),(1,0,0),(1,0,1),(0,0,1)]),
           ((0,1,0),[(0,1,0),(0,1,1),(1,1,1),(1,1,0)]),
           ((0,0,-1),[(0,0,0),(0,1,0),(1,1,0),(1,0,0)]),
           ((0,0,1),[(0,0,1),(1,0,1),(1,1,1),(0,1,1)])]
    size=np.asarray(occupancy.shape);count=0
    for axis in range(3):
        count+=int(np.count_nonzero(np.diff(np.pad(occupancy,[(1,1) if i==axis else (0,0) for i in range(3)]).astype(np.int8),axis=axis)))
    require(count*4<=budget['vertices'] and count*2<=budget['triangles']
            and count*4096<=budget['memory_bytes'],'MODEL3D_HULL_LIMIT','Voxel surface working/output budget exceeded before meshing.')
    vertices=[];triangles=[];index={}
    for cell in np.argwhere(occupancy):
        for offset,corners in quads:
            neighbor=cell+offset
            if np.all(neighbor>=0) and np.all(neighbor<size) and occupancy[tuple(neighbor)]:continue
            face=[]
            for corner in corners:
                key=tuple(map(int,cell+corner))
                if key not in index:index[key]=len(vertices);vertices.append(key)
                face.append(index[key])
            triangles.extend([[face[0],face[1],face[2]],[face[0],face[2],face[3]]])
    positions=bounds[0]+np.asarray(vertices,dtype=float)/size*(bounds[1]-bounds[0])
    mesh=Mesh('visual-hull',positions,np.asarray(triangles,dtype=np.int64),name='Calibrated visual hull')
    _manifold(mesh)
    from model3d_topology import _normals, _degenerate
    require(not np.any(_degenerate(mesh)),'MODEL3D_HULL_NUMERIC',
            'Voxel corners collapsed at this world-space precision; revise bounds or resolution.')
    mesh.attributes['NORMAL']=_normals(mesh)
    return Geometry([mesh],[dict(name='hull-diffuse',pbrMetallicRoughness=dict(metallicFactor=0.,roughnessFactor=.65))])


def carve_visual_hull(views, bounds, resolution, *, limits=None):
    """Intersect complete calibrated silhouettes at voxel centers, then mesh.

    All cells must be visible in every supplied view; out-of-frame/near/far cells
    are excluded. Centers classify entire cells: the boundary uncertainty is up
    to one voxel diagonal. No smoothing, symmetry, calibration inference or
    unknown-view assumptions occur. Inputs and source evidence are unchanged.
    """
    budget=_budgets(limits)
    require(isinstance(views,(list,tuple)) and 2<=len(views)<=budget['views']
            and all(isinstance(v,CalibratedView) for v in views), 'MODEL3D_HULL_VIEWS','Supply 2..bounded calibrated views.')
    require(len({v.name for v in views})==len(views),'MODEL3D_HULL_VIEWS','View names must be unique.')
    for view in views:_check_view(view)
    require(all(v.mask.size<=budget['pixels'] for v in views),'MODEL3D_HULL_LIMIT','View raster budget exceeded.')
    bounds=_array(bounds,(2,3),'world bounds')
    require(np.all(bounds[1]>bounds[0]) and np.max(np.abs(bounds))<=1e6,'MODEL3D_HULL_BOUNDS','Bounds need positive finite meter extents within +/-1e6.')
    require(isinstance(resolution,(list,tuple)) and len(resolution)==3
            and all(type(v) is int and 1<=v<=256 for v in resolution),'MODEL3D_HULL_LIMIT','Resolution requires three integers in1..256.')
    count=math.prod(resolution)
    require(count<=budget['voxels'] and count*32+sum(v.mask.nbytes for v in views)<=budget['memory_bytes'],
            'MODEL3D_HULL_LIMIT','Voxel working set exceeds budget before allocation.')
    occupancy=np.ones(tuple(resolution),dtype=bool);flat=occupancy.ravel();remaining=[]
    for view in views:
        for start in range(0,count,16384):
            indices=np.arange(start,min(start+16384,count))
            cells=np.column_stack(np.unravel_index(indices,resolution))
            points=bounds[0]+(cells+.5)/np.asarray(resolution)*(bounds[1]-bounds[0])
            pixel,valid=_projection(points,view)
            # Out-of-frustum coordinates can be huge near the camera plane;
            # invalid cells never index the mask, so clamp before integer casts.
            xy=np.floor(np.clip(pixel,-1,max(view.mask.shape)+1)).astype(np.int64)
            valid&=(xy[:,0]>=0)&(xy[:,0]<view.mask.shape[1])&(xy[:,1]>=0)&(xy[:,1]<view.mask.shape[0])
            selected=np.zeros(len(indices),dtype=bool)
            selected[valid]=view.mask[xy[valid,1],xy[valid,0]]
            flat[indices]&=selected
        remaining.append(dict(view=view.name,remaining_voxels=int(flat.sum())))
    require(bool(occupancy.any()),'MODEL3D_HULL_CONTRADICTION','No cell satisfies all calibrated observations within the supplied volume.',observed=remaining)
    geometry=_surface(occupancy,bounds,budget)
    metrics=[]
    for view in views:
        rendered=rasterize_silhouette(geometry,view,limits=budget)
        intersection=int(np.count_nonzero(rendered&view.mask));union=int(np.count_nonzero(rendered|view.mask))
        missing=int(np.count_nonzero(view.mask&~rendered));excess=int(np.count_nonzero(rendered&~view.mask))
        target_centroid=np.argwhere(view.mask).mean(axis=0)
        actual=np.argwhere(rendered)
        metrics.append(dict(view=view.name,calibration=view.calibration,iou=intersection/union,
                            foreground_pixels=int(view.mask.sum()),rendered_pixels=int(rendered.sum()),
                            missing_foreground_pixels=missing,excess_background_pixels=excess,
                            centroid_residual_px=float(np.linalg.norm(actual.mean(axis=0)-target_centroid)) if len(actual) else None,
                            contradicts_observation=bool(missing or excess)))
    step=(bounds[1]-bounds[0])/np.asarray(resolution)
    pairs=[];redundant=[]
    for i,left in enumerate(views):
        for right in views[i+1:]:
            cosine=float(np.clip(np.dot(left.world[:3,2],right.world[:3,2]),-1,1))
            same=bool(np.allclose(left.world,right.world,atol=1e-10,rtol=0)
                      and np.allclose(left.projection,right.projection,atol=1e-10,rtol=0)
                      and left.mask.shape==right.mask.shape)
            pairs.append(dict(views=[left.name,right.name],baseline_m=float(np.linalg.norm(left.world[:3,3]-right.world[:3,3])),
                              axis_angle_degrees=math.degrees(math.acos(cosine)),redundant_calibration=same))
            if same:redundant.append([left.name,right.name])
    rank=int(np.linalg.matrix_rank(np.asarray([v.world[:3,2] for v in views]),tol=1e-8))
    uncertainty=['Hidden concavities and internal surfaces remain unknown; no observed anatomy is inferred.',
                 'Voxel faces are derived boundaries of an approximate silhouette intersection, not directly measured surface samples.']
    if redundant:uncertainty.append('Some views share the same calibration and add no independent geometric constraint.')
    if rank==1:uncertainty.append('Viewing axes are collinear; depth constraints depend on projection and camera baselines, and completeness is unproven.')
    report=dict(schema_version=1,kind='model3d-calibrated-visual-hull',coordinate_system='right-handed +Y up +Z forward meters',
                method='voxel-center silhouette intersection; exposed voxel faces',calibration_status='estimated' if any(v.calibration=='estimated' for v in views) else 'known',
                bounds_m=bounds.tolist(),resolution=list(resolution),voxel_size_m=step.tolist(),
                boundary_uncertainty_m=float(np.linalg.norm(step)),occupied_voxels=int(occupancy.sum()),
                occupancy=dict(encoding='C-order XYZ bool uint8 0/1',shape=list(occupancy.shape),
                               sha256=hashlib.sha256(occupancy.tobytes()).hexdigest()),
                watertight_manifold=True,remaining_after_views=remaining,reprojection=metrics,
                source_evidence=[dict(name=v.name,evidence=deepcopy(v.evidence),calibration=v.calibration,camera=deepcopy(v.camera)) for v in views],
                classification='derived-from-multiple-observations',review_status='pending',
                completeness='partial',provider_calls=0,review='pending',
                surface_classification=dict(triangle_range=[0,geometry.triangle_count],
                    classification='derived-from-multiple-observations',directly_observed=False),
                view_geometry=dict(axis_rank=rank,pairs=pairs,redundant_calibration_pairs=redundant),
                uncertainties=uncertainty,
                assumptions=['Caller supplies complete foreground masks and calibrated cameras.',
                             'Each occupied cell represents its whole voxel based on the cell-center observations.'],
                limitations=['Cell-center decisions approximate surface boundaries; reprojection measures actual mesh pixel-center coverage.',
                             'Visual hull cannot recover hidden concavities, internal geometry, textures or anatomy.',
                             'All views are complete observations; outside-frame/clip cells are excluded.',
                             'No automatic calibration, image segmentation, smoothing or artistic approval is inferred.'])
    occupancy.setflags(write=False)
    from model3d_reconstruction import _seal_result
    return _seal_result(HullResult(geometry,occupancy,report))
