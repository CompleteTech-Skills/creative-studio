"""Explicit portable PBR parameters and deterministic image textures.

These are parameterized material recipes, not learned textures or physical
material identification. All operations retain the supplied Geometry unchanged.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import io
import math
import numpy as np

from rig_contracts import DEFAULT_LIMITS,validate_contract
from rig_errors import require
from rig_models import Geometry, TextureImage, validate_geometry


FAMILIES = {
    'plastic':(0.,.35), 'metal':(1.,.25), 'rubber':(0.,.9), 'cloth':(0.,.9),
    'fur-stylized':(0.,1.), 'wood':(0.,.65), 'stone':(0.,.9), 'glass':(0.,.05),
    'ceramic':(0.,.2), 'paper':(0.,1.), 'painted':(0.,.45), 'toon':(0.,1.)}


def _color(value, count=4):
    require(isinstance(value,(list,tuple)) and len(value)==count
            and all(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1 for v in value),
            'MODEL3D_MATERIAL_COLOR','Colors must be explicit finite normalized components.')
    return list(map(float,value))


def material(family='plastic', *, color=(.5,.5,.5,1.), metallic=None, roughness=None,
             emission=(0.,0.,0.), name=None):
    require(isinstance(family,str) and family in FAMILIES, 'MODEL3D_MATERIAL_FAMILY','Unsupported material family.')
    label=family if name is None else name
    require(isinstance(label,str) and 0<len(label)<=128 and '\x00' not in label,
            'MODEL3D_MATERIAL_NAME','Material name must be bounded text.')
    base=_color(color); emitted=_color(emission,3)
    metal,rough=FAMILIES[family]
    for key,value in [('metallic',metallic),('roughness',roughness)]:
        require(value is None or (type(value) in (int,float) and math.isfinite(value) and 0<=value<=1),
                'MODEL3D_MATERIAL_PARAMETER','PBR factors must lie in [0,1].',asset=key)
    result=dict(name=label,pbrMetallicRoughness=dict(baseColorFactor=base,
                metallicFactor=metal if metallic is None else metallic,
                roughnessFactor=rough if roughness is None else roughness),emissiveFactor=emitted,
                extras=dict(model3d_material_family=family,appearance='parameterized-PBR'))
    if base[3]<1 or family=='glass':result['alphaMode']='BLEND'
    # Transmission and a complex cloth/fur/toon shader are not fabricated by a
    # simple portable core material. Their approximation is explicit metadata.
    if family in ('glass','fur-stylized','cloth','toon'):
        result['extras']['limitation']='Core PBR approximation; no transmission, strand geometry or custom shader.'
    return result


def assign_material(geometry, mesh_ids, value):
    validate_geometry(geometry)
    require(isinstance(mesh_ids,(list,tuple)) and bool(mesh_ids)
            and all(isinstance(key,str) and key in geometry.by_id() for key in mesh_ids)
            and len(set(mesh_ids))==len(mesh_ids),
            'MODEL3D_MATERIAL_PART','Choose existing unique mesh identifiers.')
    require(isinstance(value,dict), 'MODEL3D_MATERIAL_PARAMETER','Material must be a native PBR object.')
    result=deepcopy(geometry);slot=len(result.materials);result.materials.append(deepcopy(value))
    for mesh in result.meshes:
        if mesh.id in mesh_ids:mesh.material=slot
    validate_geometry(result)
    return result


def procedural_texture(kind='checker', *, width=128, height=128, seed=0,
                       colors=((.15,.15,.15,1.),(.8,.8,.8,1.)), scale=8,
                       name='procedural', max_pixels=4_194_304):
    """Return a real deterministic PNG consumed by native texture exporters.

    Color recipes produce sRGB RGBA images. ``normal`` is a flat tangent-space
    normal map; ``orm`` packs supplied color components as data channels.
    """
    require(kind in ('checker','stripes','wood','stone','cloth','normal','orm'),
            'MODEL3D_TEXTURE_KIND','Unsupported procedural texture recipe.')
    require(type(width) is int and type(height) is int and 1<=width<=4096 and 1<=height<=4096
            and type(max_pixels) is int and 0<max_pixels<=4_194_304 and width*height<=max_pixels,
            'MODEL3D_TEXTURE_BUDGET','Texture dimensions exceed the explicit pixel budget.')
    require(type(seed) is int and 0<=seed<2**32 and type(scale) is int and 1<=scale<=4096,
            'MODEL3D_TEXTURE_PARAMETER','Seed/scale must be bounded integers.')
    require(isinstance(name,str) and 0<len(name)<=128 and '\x00' not in name,
            'MODEL3D_TEXTURE_PARAMETER','Texture name must be bounded text.')
    require(isinstance(colors,(list,tuple)) and len(colors)==2,'MODEL3D_TEXTURE_PARAMETER','Supply two colors.')
    palette=np.asarray([_color(c) for c in colors],float)
    y,x=np.mgrid[:height,:width]
    if kind=='checker':factor=((x*scale//width+y*scale//height)%2).astype(float)
    elif kind=='stripes':factor=(x*scale//width%2).astype(float)
    elif kind=='wood':factor=(np.sin(x/width*scale*2*np.pi+.25*np.sin(y/height*scale*2*np.pi))+1)/2
    elif kind=='cloth':factor=.5*((x*scale//width)%2)+.5*((y*scale//height)%2)
    elif kind=='stone':factor=np.random.default_rng(seed).random((height,width))
    else:factor=np.zeros((height,width))
    image=np.rint((palette[0]+factor[...,None]*(palette[1]-palette[0]))*255).astype(np.uint8)
    if kind=='normal':image[:]=[128,128,255,255]
    from PIL import Image
    stream=io.BytesIO();Image.fromarray(image,'RGBA').save(stream,format='PNG',compress_level=9)
    return TextureImage(name,'image/png',stream.getvalue())


def validate_texture(image, *, limits=None, total_pixels=0):
    from rig_import import _decode_texture
    require(isinstance(image,TextureImage),
            'MODEL3D_TEXTURE_IMAGE','Expected immutable native image bytes.')
    # TextureImage is a mutable container even though its byte payload is not.
    # Decode, format comparison and receipt must reference the same selected data.
    data,declared_mime=image.data,image.mime_type
    require(isinstance(data,bytes),'MODEL3D_TEXTURE_IMAGE','Expected immutable native image bytes.')
    active={**DEFAULT_LIMITS,**(limits or {})}
    validate_contract(active,'Limits')
    require(type(total_pixels) is int and total_pixels>=0,'MODEL3D_TEXTURE_BUDGET','Decoded inventory pixel count must be nonnegative.')
    mime,pixels=_decode_texture(data,active,total_pixels=total_pixels)
    require(mime==declared_mime,'MODEL3D_TEXTURE_IMAGE','Declared and actual texture format differ.')
    return dict(mime_type=mime,pixels=pixels,sha256=hashlib.sha256(data).hexdigest())


def bind_texture(geometry, material_slot, image, *, role='base-color', texcoord=0, limits=None):
    """Append immutable image resources and bind actual portable native slots."""
    active={**DEFAULT_LIMITS,**(limits or {})};validate_contract(active,'Limits')
    validate_geometry(geometry,active)
    require(type(material_slot) is int and 0<=material_slot<len(geometry.materials),
            'MODEL3D_TEXTURE_BINDING','Unknown material slot.')
    require(role in ('base-color','normal','occlusion','emissive','metallic-roughness','orm')
            and type(texcoord) is int and texcoord in (0,1),
            'MODEL3D_TEXTURE_BINDING','Unsupported map role/UV set.')
    require(isinstance(image,TextureImage) and isinstance(image.data,bytes),
            'MODEL3D_TEXTURE_IMAGE','Expected immutable native image bytes.')
    selected_image=TextureImage(image.name,image.mime_type,image.data)
    retained=sum(mesh.positions.nbytes+mesh.triangles.nbytes+sum(value.nbytes for value in mesh.attributes.values())
                 +sum(value.nbytes for target in mesh.morphs.values() for value in target.values()) for mesh in geometry.meshes)
    retained+=sum(len(item.data) for item in geometry.images)
    require((retained+len(selected_image.data))*4<=active['memory_bytes'],'MODEL3D_TEXTURE_BUDGET','Encoded closure exceeds texture binding working memory.')
    # Snapshot the selected closure before strict decode. Validating the caller's
    # containers then copying them would let an editor race decoded bytes with
    # unrelated replacement bytes. Encoded allocation is preflighted above.
    result=deepcopy(geometry);validate_geometry(result,active)
    total_pixels=0
    for existing in result.images:
        report=validate_texture(existing,limits=active,total_pixels=total_pixels);total_pixels+=report['pixels']
    validate_texture(selected_image,limits=active,total_pixels=total_pixels)
    index=len(result.textures)
    result.images.append(selected_image);result.samplers.append(dict(magFilter=9729,minFilter=9729,wrapS=33071,wrapT=33071))
    result.textures.append(dict(source=len(result.images)-1,sampler=len(result.samplers)-1))
    binding=dict(index=index,texCoord=texcoord);value=result.materials[material_slot]
    if role in ('base-color','metallic-roughness','orm'):
        value.setdefault('pbrMetallicRoughness',{})['baseColorTexture' if role=='base-color' else 'metallicRoughnessTexture']=deepcopy(binding)
    if role in ('normal','occlusion','emissive','orm'):
        value[{'normal':'normalTexture','occlusion':'occlusionTexture','emissive':'emissiveTexture','orm':'occlusionTexture'}[role]]=deepcopy(binding)
    validate_geometry(result,active)
    return result
