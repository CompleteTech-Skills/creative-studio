"""Calibrated, occlusion-tested observed color projection onto native vertices.

The source bytes and supplied camera calibration are retained in evidence. This
is vertex color projection, not dense texture reconstruction or invented unseen
appearance. Multiple observations use a reproducible linear-color mean.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import io
import numbers
import numpy as np

import camera3d_math as camera
from rig_contracts import DEFAULT_LIMITS
from rig_errors import require, RigError, object_hash
from rig_models import TextureImage, validate_geometry
from model3d_materials import validate_texture


def _linear(color):
    color=np.asarray(color,dtype=float)
    return np.where(color<=.04045,color/12.92,((color+.055)/1.055)**2.4)


def _camera_snapshot(value, *, rigid=False):
    if isinstance(value,np.ndarray):
        require(value.shape==(4,4) and value.dtype.kind in 'fiu','MODEL3D_PROJECTION_CALIBRATION','Camera matrices must be numeric 4x4 values.')
    else:
        require(isinstance(value,(list,tuple)) and len(value)==4
                and all(isinstance(row,(list,tuple,np.ndarray)) and len(row)==4
                        and all(isinstance(item,numbers.Real) and not isinstance(item,(bool,np.bool_)) for item in row) for row in value),
                'MODEL3D_PROJECTION_CALIBRATION','Camera matrices must be numeric 4x4 values.')
    try:
        result=camera._matrix(value,rigid=rigid).copy()
    except (ValueError,TypeError,OverflowError) as exc:
        raise RigError('MODEL3D_PROJECTION_CALIBRATION','Invalid calibrated camera: '+str(exc)) from exc
    require(np.max(np.abs(result))<=1e8,'MODEL3D_PROJECTION_RANGE','Camera matrices exceed numeric magnitude 1e8.')
    return result


def _geometry_hash(geometry):
    digest=hashlib.sha256()
    for mesh in geometry.meshes:
        digest.update(object_hash(dict(id=mesh.id,name=mesh.name,material=mesh.material,region=mesh.region,
                    source_node=mesh.source_node,morph_defaults=mesh.morph_defaults)).encode('ascii'))
        arrays=[('positions',mesh.positions),('triangles',mesh.triangles)]
        arrays += [('attribute:'+key,value) for key,value in sorted(mesh.attributes.items())]
        arrays += [('morph:'+key+':'+semantic,value) for key,target in sorted(mesh.morphs.items()) for semantic,value in sorted(target.items())]
        for key,value in arrays:
            digest.update(key.encode('utf-8'))
            array=np.ascontiguousarray(value)
            digest.update(str((array.dtype.str,array.shape)).encode('ascii'));digest.update(memoryview(array).cast('B'))
    digest.update(object_hash(dict(materials=geometry.materials,textures=geometry.textures,samplers=geometry.samplers,
                                  extensions_used=geometry.extensions_used)).encode('ascii'))
    for image in geometry.images:
        digest.update(object_hash(dict(name=image.name,mime_type=image.mime_type)).encode('ascii'));digest.update(image.data)
    return digest.hexdigest()


def _occluded(point, near, direction, triangles):
    distance=float(np.dot(point-near,direction))
    tolerance=max(1.,abs(distance),float(np.max(np.abs(point))),float(np.max(np.abs(near))))*np.finfo(float).eps*128
    require(np.isfinite(distance),'MODEL3D_PROJECTION_RANGE','Projection ray exceeds numeric range.')
    unresolved=False
    for offset in range(0,len(triangles),1024):
        group=triangles[offset:offset+1024]
        edge1=group[:,1]-group[:,0];edge2=group[:,2]-group[:,0]
        scale=np.maximum(np.max(np.abs(edge1),axis=1),np.max(np.abs(edge2),axis=1))
        # The target's incident faces end at this exact vertex. Excluding them
        # avoids a coarse world-distance cutoff that would erase thin occluders.
        incident=np.any(np.all(group==point[None,None,:],axis=2),axis=1)
        valid=(scale>0)&~incident
        normalized=np.where(valid,scale,1)
        edge1=edge1/normalized[:,None];edge2=edge2/normalized[:,None]
        h=np.cross(np.broadcast_to(direction,edge2.shape),edge2)
        determinant=np.einsum('ij,ij->i',edge1,h)
        valid &= determinant!=0
        require(not np.any(valid & (np.abs(determinant)<1e-100)),
                'MODEL3D_PROJECTION_RANGE','Ray/triangle frame exceeds supported numerical conditioning.')
        reciprocal=np.zeros(len(group));reciprocal[valid]=1/determinant[valid]
        s=(near-group[:,0])/normalized[:,None]
        u=reciprocal*np.einsum('ij,ij->i',s,h)
        q=np.cross(s,edge1)
        v=reciprocal*np.einsum('ij,j->i',q,direction)
        t=reciprocal*np.einsum('ij,ij->i',edge2,q)*normalized
        require(np.isfinite(u).all() and np.isfinite(v).all() and np.isfinite(t).all(),
                'MODEL3D_PROJECTION_RANGE','Occlusion evaluation exceeds numeric range.')
        hit=valid & (u>=-1e-10) & (v>=-1e-10) & (u+v<=1+1e-10)
        if np.any(hit & (t>=0) & (t<distance-tolerance)):return 'occluded'
        if np.any(hit & (t>=0) & (t<=distance+tolerance)):unresolved=True
    return 'unresolved-depth' if unresolved else None


def project_views(geometry, views, *, fallback_srgb=(.5,.5,.5,1.), replace=False,
                  max_tests=2_000_000, limits=None):
    """Return (copy, evidence) with native linear RGBA COLOR_0 values.

    Each view explicitly supplies image:TextureImage, camera_world:4x4,
    projection:4x4 and calibration:{status:'supplied'|'measured',reference:str}.
    No calibration is inferred. Occlusion tests all triangles as opaque/two-sided.
    """
    active={**DEFAULT_LIMITS,**(limits or {})};validate_geometry(geometry,active)
    require(isinstance(views,(list,tuple)) and 0<len(views)<=8,'MODEL3D_PROJECTION_VIEWS','Choose one to eight calibrated views.')
    require(type(max_tests) is int and 0<max_tests<=10_000_000
            and geometry.vertex_count<=20000 and geometry.vertex_count*geometry.triangle_count*len(views)<=max_tests,
            'MODEL3D_PROJECTION_BUDGET','Visibility tests exceed the explicit vertex/triangle work budget.')
    require(type(replace) is bool and all(replace or 'COLOR_0' not in mesh.attributes for mesh in geometry.meshes),
            'MODEL3D_PROJECTION_EXISTS','Replacing existing vertex colors requires replace=True.')
    require(isinstance(fallback_srgb,(tuple,list)) and len(fallback_srgb)==4
            and all(type(value) in (int,float) and np.isfinite(value) and 0<=value<=1 for value in fallback_srgb),
            'MODEL3D_PROJECTION_COLOR','Inferred fallback must be an explicit normalized sRGB color.')
    require(all(np.max(np.abs(mesh.positions))<=1e8 for mesh in geometry.meshes),
            'MODEL3D_PROJECTION_RANGE','Projection supports coordinate magnitudes up to 1e8 meters.')
    encoded=0;frozen_views=[]
    for view in views:
        require(isinstance(view,dict) and set(view)=={'image','camera_world','projection','calibration'},
                'MODEL3D_PROJECTION_CALIBRATION','Every view needs explicit source and camera calibration.')
        calibration=view['calibration']
        require(isinstance(calibration,dict) and set(calibration)=={'status','reference'}
                and isinstance(calibration['status'],str) and calibration['status'] in ('supplied','measured')
                and isinstance(calibration['reference'],str) and 0<len(calibration['reference'])<=4096,
                'MODEL3D_PROJECTION_CALIBRATION','Unknown or inferred calibration is not usable projection evidence.')
        require(isinstance(view['image'],TextureImage) and isinstance(view['image'].data,bytes)
                and isinstance(view['image'].name,str) and 0<len(view['image'].name)<=128,
                'MODEL3D_PROJECTION_IMAGE','Expected immutable native image bytes.')
        encoded+=len(view['image'].data)
        frozen_world=_camera_snapshot(view['camera_world'],rigid=True)
        frozen_projection=_camera_snapshot(view['projection'])
        frozen_views.append(dict(image=TextureImage(view['image'].name,view['image'].mime_type,view['image'].data),
                                 camera_world=frozen_world,projection=frozen_projection,calibration=deepcopy(calibration)))
    require(encoded<=64*1024**2,'MODEL3D_PROJECTION_BUDGET','Aggregate encoded image bytes exceed 64MiB.')
    geometry_bytes=sum(mesh.positions.nbytes+mesh.triangles.nbytes+
        sum(array.nbytes for array in mesh.attributes.values())+
        sum(array.nbytes for target in mesh.morphs.values() for array in target.values()) for mesh in geometry.meshes)
    geometry_bytes+=sum(len(image.data) for image in geometry.images)
    require(geometry_bytes*4+encoded+geometry.triangle_count*9*8+geometry.vertex_count*64+4_194_304*64<=active['memory_bytes'],
            'MODEL3D_PROJECTION_BUDGET','Projection working memory exceeds its configured budget.')
    geometry=deepcopy(geometry)
    validate_geometry(geometry,active)
    source_geometry_sha256=_geometry_hash(geometry)
    triangles=np.concatenate([mesh.positions[mesh.triangles].astype(float) for mesh in geometry.meshes])
    points=np.concatenate([mesh.positions for mesh in geometry.meshes]).astype(float)
    counts=np.zeros(len(points),dtype=np.int64);colors=np.zeros((len(points),4));reports=[]
    from PIL import Image
    for view in frozen_views:
        image=view['image']
        try:
            with Image.open(io.BytesIO(image.data)) as header:
                require(0<header.width*header.height<=4_194_304,'MODEL3D_PROJECTION_BUDGET','Projection source exceeds four million pixels.')
        except (OSError,ValueError,Image.DecompressionBombError) as exc:
            raise RigError('MODEL3D_PROJECTION_IMAGE','Invalid source image: '+str(exc)) from exc
        source=validate_texture(image,limits=active)
        with Image.open(io.BytesIO(image.data)) as decoded: pixels=np.asarray(decoded.convert('RGBA'))
        height,width=pixels.shape[:2]
        try:
            world=camera._matrix(view['camera_world'],rigid=True)
            projection=camera._matrix(view['projection'])
            require(np.max(np.abs(world))<=1e8 and np.max(np.abs(projection))<=1e8,
                    'MODEL3D_PROJECTION_RANGE','Camera matrices exceed supported numeric magnitude.')
        except (ValueError,TypeError,OverflowError) as exc:
            raise RigError('MODEL3D_PROJECTION_CALIBRATION','Invalid calibrated camera: '+str(exc)) from exc
        observed=[];occluded=0;outside=0;transparent=0;unresolved=0
        for index,point in enumerate(points):
            try:
                projected=camera.project(point,world,projection,width,height)
                if not projected['inside']:outside+=1;continue
                near,direction=camera.screen_ray(projected['pixel'],world,projection,width,height)
            except ValueError as exc:
                raise RigError('MODEL3D_PROJECTION_CALIBRATION','Camera cannot evaluate projection: '+str(exc)) from exc
            visibility=_occluded(point,near,direction,triangles)
            if visibility=='occluded':occluded+=1;continue
            if visibility=='unresolved-depth':unresolved+=1;continue
            x,y=projected['pixel'];sample=pixels[min(height-1,max(0,int(y))),min(width-1,max(0,int(x)))].astype(float)/255
            if sample[3]==0:transparent+=1;continue
            rgba=np.r_[_linear(sample[:3]),sample[3]]
            colors[index]+=rgba;counts[index]+=1;observed.append(index)
        reports.append(dict(source_name=image.name,source_sha256=source['sha256'],source_pixels=width*height,
                            camera_world=world.tolist(),projection=projection.tolist(),calibration=deepcopy(view['calibration']),
                            observed_vertices=observed,occluded_vertices=occluded,outside_vertices=outside,
                            transparent_source_vertices=transparent,unresolved_depth_vertices=unresolved))
    fallback=np.r_[_linear(fallback_srgb[:3]),fallback_srgb[3]]
    observed=counts>0
    colors[observed]/=counts[observed,None];colors[~observed]=fallback
    result=geometry;start=0;parts=[]
    for mesh in result.meshes:
        end=start+len(mesh.positions);mesh.attributes['COLOR_0']=colors[start:end].copy()
        parts.append(dict(mesh=mesh.id,observed=np.flatnonzero(observed[start:end]).tolist(),
                          inferred=np.flatnonzero(~observed[start:end]).tolist(),observation_counts=counts[start:end].tolist()))
        start=end
    validate_geometry(result,active)
    report=dict(schema_version=1,method='calibrated-visible-vertex-color',source_geometry_sha256=source_geometry_sha256,
                result_geometry_sha256=_geometry_hash(result),parts=parts,views=reports,
                color_space='linear-sRGB RGBA',sampling='nearest source pixel; top-left image origin',
                blending='equal mean in linear color space over visible nontransparent observations',
                depth_resolution='Incident target faces excluded; near-coincident nonincident hits are inferred/unresolved.',
                inferred_fallback_srgb=list(fallback_srgb),observed_vertices=int(observed.sum()),
                inferred_vertices=int((~observed).sum()),triangle_tests_budget=max_tests,
                limitations=['Vertex sampling does not reconstruct dense textures.',
                             'Visibility treats all geometry as opaque, two-sided triangle surfaces.',
                             'Unseen vertices are explicit inferred fallback, not observations.'])
    return result,report


def project_image(geometry,image,*,camera_world,projection,calibration,**options):
    return project_views(geometry,[dict(image=image,camera_world=camera_world,projection=projection,calibration=calibration)],**options)
