"""Bounded native UV generation with explicit per-triangle seams and padding.

Geometry is copied and corners are duplicated, retaining every native attribute
and morph delta. SkinWeights live outside Geometry and are not transformed here.
Tangents require recalculation by their native authoring owner and fail closed.
"""
from __future__ import annotations
from copy import deepcopy
import math
import numpy as np

from rig_contracts import DEFAULT_LIMITS
from rig_errors import require
from rig_models import validate_geometry


def _area2(points):
    a=points[1]-points[0];b=points[2]-points[0]
    return float(a[0]*b[1]-a[1]*b[0])/2


def _triangle_uv(points, method, bounds):
    a,b=points[1]-points[0],points[2]-points[0]
    scale=max(np.max(np.abs(a)),np.max(np.abs(b)))
    require(scale>0 and np.isfinite(scale),'MODEL3D_UV_GEOMETRY','Triangle has no measurable extent.')
    cross=np.cross(a/scale,b/scale);length=np.linalg.norm(cross)
    require(np.isfinite(length) and length>0,'MODEL3D_UV_GEOMETRY','UVs require nondegenerate triangles.')
    if method in ('planar','box'):
        axes=[0,1] if method=='planar' else [i for i in range(3) if i!=int(np.argmax(np.abs(cross)))]
        uv=points[:,axes].copy()
    else:
        center=(bounds[0]+bounds[1])/2
        local=points-center
        radius=np.linalg.norm(local,axis=1)
        u=np.arctan2(local[:,2],local[:,0])/(2*np.pi)+.5
        if np.ptp(u)>.5:u=np.where(u<.5,u+1,u)
        if method=='cylindrical':
            if abs(cross[1])/length>.999999:
                uv=local[:,[0,2]].copy()
            else:
                height=bounds[1][1]-bounds[0][1]
                require(height>0,'MODEL3D_UV_GEOMETRY','Cylindrical side mapping requires nonzero height.')
                uv=np.column_stack((u,(points[:,1]-bounds[0][1])/height))
        else:
            require(np.all(radius>0),'MODEL3D_UV_GEOMETRY','Spherical coordinates are undefined at the selected bounds center.')
            v=np.arccos(np.clip(local[:,1]/radius,-1,1))/np.pi
            poles=np.hypot(local[:,0],local[:,2])<=radius*np.finfo(float).eps*16
            if np.any(poles):
                require(np.any(~poles),'MODEL3D_UV_GEOMETRY','A spherical triangle cannot consist only of poles.')
                u[poles]=u[~poles].mean()
            uv=np.column_stack((u,v))
    if _area2(uv)<0:uv[:,0]*=-1
    extent=np.ptp(uv,axis=0);denom=float(extent.max())
    require(np.isfinite(uv).all() and denom>0,'MODEL3D_UV_GEOMETRY','Selected mapping cannot represent this triangle.')
    uv=(uv-uv.min(axis=0))/denom
    area=abs(_area2(uv))
    require(area>0,'MODEL3D_UV_GEOMETRY','Selected mapping collapses a triangle; choose box mapping or native authoring.')
    area_m2=length*scale*scale/2
    require(np.isfinite(area_m2) and area_m2>0,'MODEL3D_UV_GEOMETRY','Triangle area exceeds representable diagnostics.')
    return uv,area_m2


def generate_uv(geometry, *, method='box', texcoord=0, resolution=(512,512), padding=2,
                replace=False, max_relative_uv_error=.001, limits=None):
    """Return (copied Geometry, measured report) using a disjoint padded atlas.

    ``planar`` projects onto XY. Box mapping selects each face's dominant axis.
    Cylinder/revolution axes are +Y; spherical center is the part bounds center.
    Each triangle is an explicit island to preserve seams without welding parts.
    """
    active={**DEFAULT_LIMITS,**(limits or {})};validate_geometry(geometry,active)
    require(method in ('planar','box','cylindrical','spherical'),'MODEL3D_UV_METHOD','Unsupported UV mapping.')
    require(type(texcoord) is int and texcoord in (0,1) and type(replace) is bool,
            'MODEL3D_UV_PARAMETER','Choose UV set zero/one and explicit replacement policy.')
    require(type(max_relative_uv_error) in (int,float) and math.isfinite(max_relative_uv_error)
            and 0<=max_relative_uv_error<=1, 'MODEL3D_UV_PARAMETER','Relative portable UV error must lie in [0,1].')
    require(isinstance(resolution,(list,tuple)) and len(resolution)==2
            and all(type(v) is int and 1<=v<=4096 for v in resolution)
            and resolution[0]*resolution[1]<=4_194_304
            and type(padding) is int and 0<=padding<=64,
            'MODEL3D_UV_BUDGET','Atlas dimensions/padding exceed the supported budget.')
    name='TEXCOORD_'+str(texcoord)
    require(all(replace or name not in mesh.attributes for mesh in geometry.meshes),
            'MODEL3D_UV_EXISTS','Replacing existing UVs requires replace=True.')
    require(all('TANGENT' not in mesh.attributes and not any('TANGENT' in target for target in mesh.morphs.values())
                for mesh in geometry.meshes), 'MODEL3D_UV_TANGENT','UV edits require explicit native tangent regeneration.')
    triangles=geometry.triangle_count;corners=triangles*3
    per_corner=3*8+2*8+8
    for mesh in geometry.meshes:
        per_corner=max(per_corner,3*8+2*8+8+sum(value.shape[1]*value.dtype.itemsize for value in mesh.attributes.values())
                       +sum(value.shape[1]*value.dtype.itemsize for targets in mesh.morphs.values() for value in targets.values()))
    source_bytes=sum(mesh.positions.nbytes+mesh.triangles.nbytes+sum(value.nbytes for value in mesh.attributes.values())
                     +sum(value.nbytes for target in mesh.morphs.values() for value in target.values()) for mesh in geometry.meshes)
    source_bytes+=sum(len(image.data) for image in geometry.images)
    require(corners<=active['vertices'] and (source_bytes+corners*per_corner)*4<=active['memory_bytes'],
            'MODEL3D_UV_BUDGET','Seam duplication exceeds vertex/working-memory budget.')
    columns=math.ceil(math.sqrt(triangles));rows=math.ceil(triangles/columns)
    span=np.array([1/columns,1/rows]);margin=np.array([padding/resolution[0],padding/resolution[1]])
    require(np.all(span>2*margin),'MODEL3D_UV_PADDING','Atlas cannot fit the requested islands and padding.')
    result=deepcopy(geometry);island=0;surface_area=0.;uv_area=0.;distortions=[];parts=[];maximum_portable_error=0.
    for mesh in result.meshes:
        original=deepcopy(mesh);flat=original.triangles.reshape(-1)
        bounds=(original.positions.min(axis=0),original.positions.max(axis=0))
        values=np.empty((len(flat),2),dtype=np.float64)
        for index,triangle in enumerate(original.triangles):
            points=original.positions[triangle].astype(float)
            uv,area=_triangle_uv(points,method,bounds)
            offset=np.array([island%columns/columns,island//columns/rows])
            final=offset+margin+uv*(span-2*margin)
            encoded=final.astype(np.float32).astype(np.float64)
            require(np.isfinite(final).all() and np.isfinite(encoded).all()
                    and _area2(final)>0 and _area2(encoded)>0,
                    'MODEL3D_UV_PRECISION','Packed UV island collapses or flips at portable float32 precision.')
            shortest=min(np.linalg.norm(final[a]-final[b]) for a,b in ((0,1),(1,2),(2,0)))
            relative_error=float(np.max(np.linalg.norm(encoded-final,axis=1))/shortest)
            require(np.isfinite(relative_error) and relative_error<=max_relative_uv_error,
                    'MODEL3D_UV_PRECISION','Packed UV quantization exceeds the configured relative edge error.',
                    expected=max_relative_uv_error,observed=relative_error)
            maximum_portable_error=max(maximum_portable_error,relative_error)
            values[index*3:index*3+3]=final;surface_area+=area
            uv_area+=abs(_area2(final))
            # Edge-length stretch ratios are normalized per triangle; this is a
            # reproducible local distortion diagnostic, not an aesthetic score.
            edge3=np.array([np.linalg.norm(points[a]-points[b]) for a,b in ((0,1),(1,2),(2,0))])
            edge2=np.array([np.linalg.norm(final[a]-final[b]) for a,b in ((0,1),(1,2),(2,0))])
            stretch=edge2/(edge3/edge3.max())
            distortions.append(float(stretch.max()/stretch.min()));island+=1
        mesh.positions=original.positions[flat].copy()
        mesh.triangles=np.arange(len(flat),dtype=np.int64).reshape(-1,3)
        mesh.attributes={key:value[flat].copy() for key,value in original.attributes.items()}
        mesh.attributes[name]=values
        mesh.morphs={key:{semantic:value[flat].copy() for semantic,value in target.items()}
                     for key,target in original.morphs.items()}
        parts.append(dict(mesh=mesh.id,vertices=len(flat),islands=len(original.triangles),material_slot=mesh.material))
    validate_geometry(result,active)
    report=dict(schema_version=1,method=method,texcoord=texcoord,resolution=list(resolution),padding_pixels=padding,
                seam_policy='explicit-per-triangle-corner-duplication',parts=parts,islands=triangles,
                orientation='counterclockwise UV winding in each island',
                uv_area=uv_area,unused_texture_fraction=max(0.,1-uv_area),surface_area_m2=surface_area,
                maximum_normalized_edge_stretch=max(distortions),
                portable_uv_precision=dict(format='float32',maximum_relative_edge_error=maximum_portable_error,
                                           configured_relative_edge_error=max_relative_uv_error,
                                           all_packed_triangles_positive_area=True),
                overlap=dict(interior_pairs=0,method='disjoint-atlas-cell-construction',intentional_shared_uv=False),
                limitations=['No tangent or external SkinWeights regeneration.','Per-triangle islands can waste texture space.'])
    return result,report
