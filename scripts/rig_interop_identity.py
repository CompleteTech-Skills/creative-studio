"""Source-bound structural identity for the native rig round-trip diagnostic."""
from copy import deepcopy
import hashlib
import io
import math
import numpy as np
from rig_errors import RigError, require
from rig_models import _MATERIAL_TEXTURE_SLOTS
from rig_surface_compare import match_rows


def _same_value(a,b,*,tolerance=1e-6):
    if isinstance(a,bool) or isinstance(b,bool):return type(a) is type(b) and a==b
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):
        return math.isfinite(a) and math.isfinite(b) and abs(a-b)<=tolerance
    if isinstance(a,dict) and isinstance(b,dict):
        return a.keys()==b.keys() and all(_same_value(a[key],b[key],tolerance=tolerance) for key in a)
    if isinstance(a,list) and isinstance(b,list):
        return len(a)==len(b) and all(_same_value(x,y,tolerance=tolerance) for x,y in zip(a,b))
    return type(a) is type(b) and a==b


def _material(geometry,index,*,memory_bytes,images):
    value=deepcopy(geometry.materials[index]);evidence={}
    pbr=value.setdefault('pbrMetallicRoughness',{})
    for key,default in (('baseColorFactor',[1.,1.,1.,1.]),('metallicFactor',1.),('roughnessFactor',1.)):
        pbr.setdefault(key,default)
    for key,default in (('emissiveFactor',[0.,0.,0.]),('alphaMode','OPAQUE'),('alphaCutoff',.5),('doubleSided',False)):
        value.setdefault(key,default)
    def binding(owner,key,label):
        if key not in owner:return
        info=owner[key];info.setdefault('texCoord',0)
        if key in ('normalTexture','clearcoatNormalTexture'):info.setdefault('scale',1.)
        if key=='occlusionTexture':info.setdefault('strength',1.)
        texture=deepcopy(geometry.textures[info.pop('index')]);image_index=texture.pop('source')
        sampler=deepcopy(geometry.samplers[texture.pop('sampler')]) if 'sampler' in texture else {}
        sampler.setdefault('wrapS',10497);sampler.setdefault('wrapT',10497)
        image=geometry.images[image_index]
        if image_index not in images:
            from PIL import Image
            try:
                with Image.open(io.BytesIO(image.data)) as decoded:
                    require(decoded.width*decoded.height*12<=memory_bytes,
                            'RIG_ROUNDTRIP_COMPARE_LIMIT','Decoded texture exceeds the comparison-memory budget.')
                    rgba=decoded.convert('RGBA')
                    images[image_index]=dict(size=list(rgba.size),rgba_sha256=hashlib.sha256(rgba.tobytes()).hexdigest())
            except RigError:
                raise
            except (OSError,ValueError,Image.DecompressionBombError) as exc:
                raise RigError('RIG_ROUNDTRIP_MATERIAL','Texture cannot be decoded for identity comparison.') from exc
        info['resource']=dict(texture=texture,sampler=sampler,image=images[image_index])
        evidence[label]=dict(encoded_sha256=hashlib.sha256(image.data).hexdigest(),mime_type=image.mime_type,
                             **images[image_index])
    for key in ('baseColorTexture','metallicRoughnessTexture'):binding(pbr,key,'pbr.'+key)
    for key in ('normalTexture','occlusionTexture','emissiveTexture'):binding(value,key,key)
    for name,fields in _MATERIAL_TEXTURE_SLOTS.items():
        extension=value.get('extensions',{}).get(name)
        if extension is not None:
            for key in fields:binding(extension,key,name+'.'+key)
    if value.get('extensions')=={}:value.pop('extensions')
    return value,evidence


def _vertex_fields(mesh,positions,attributes):
    fields=[mesh.positions if positions is None else positions]
    if attributes:
        fields.extend(mesh.attributes[name] for name in sorted(mesh.attributes))
        fields.extend(mesh.morphs[name][semantic] for name in sorted(mesh.morphs)
                      for semantic in sorted(mesh.morphs[name]))
    return fields


def loose_records(mesh,*,positions=None,memory_bytes=2*1024**3,attributes=True):
    fields=_vertex_fields(mesh,positions,attributes);width=sum(field.shape[1] for field in fields)
    require(len(mesh.positions)<=memory_bytes//12,'RIG_ROUNDTRIP_COMPARE_LIMIT','Loose-vertex inventory mask exceeds the comparison-memory budget.')
    unused=np.ones(len(mesh.positions),dtype=bool);unused[mesh.triangles]=False
    require(int(unused.sum())*width*8*12<=memory_bytes,'RIG_ROUNDTRIP_COMPARE_LIMIT','Loose-vertex records exceed the pre-allocation comparison-memory budget.')
    return np.concatenate([field[unused].astype(np.float64,copy=False) for field in fields],axis=1)


def corner_records(mesh,*,positions=None,memory_bytes=2*1024**3,attributes=True):
    """One XYZ-first row per oriented triangle; independent of vertex indices."""
    fields=_vertex_fields(mesh,positions,attributes);width=sum(field.shape[1] for field in fields)
    required=(len(mesh.positions)*width+len(mesh.triangles)*3*width)*8
    require(required*12<=memory_bytes,'RIG_ROUNDTRIP_COMPARE_LIMIT',
            'Oriented corner records exceed the pre-allocation comparison-memory budget.')
    vertices=np.concatenate(fields,axis=1,dtype=np.float64) if len(fields)>1 else np.asarray(fields[0],dtype=np.float64)
    return vertices[mesh.triangles].reshape(len(mesh.triangles),3*width),width


def compare_identity(source,target,*,memory_bytes,maximum_work):
    from rig_models import validate_geometry
    validate_geometry(source);validate_geometry(target)
    a=source.by_id();b=target.by_id()
    require(a.keys()==b.keys(),'RIG_ROUNDTRIP_IDENTITY','Round trip changed the named mesh-part inventory.')
    require(sorted(source.extensions_used)==sorted(target.extensions_used),
            'RIG_ROUNDTRIP_IDENTITY','Round trip changed required material/geometry extension capabilities.')
    meshes={};image_caches=({},{});remaining_work=maximum_work
    for name,mesh in a.items():
        other=b[name]
        require(mesh.region==other.region and mesh.attributes.keys()==other.attributes.keys()
                and mesh.morphs.keys()==other.morphs.keys()
                and all(mesh.morphs[key].keys()==other.morphs[key].keys() for key in mesh.morphs),
                'RIG_ROUNDTRIP_IDENTITY','Round trip changed part semantics, vertex attributes or named morph targets.',asset=name)
        defaults={key:mesh.morph_defaults.get(key,0.) for key in mesh.morphs}
        target_defaults={key:other.morph_defaults.get(key,0.) for key in other.morphs}
        require(_same_value(defaults,target_defaults),'RIG_ROUNDTRIP_IDENTITY','Round trip changed default morph weights.',asset=name)
        material,source_images=_material(source,mesh.material,memory_bytes=memory_bytes,images=image_caches[0])
        target_material,target_images=_material(target,other.material,memory_bytes=memory_bytes,images=image_caches[1])
        require(_same_value(material,target_material),'RIG_ROUNDTRIP_MATERIAL',
                'Round trip changed a mesh material binding, PBR value, sampler or decoded texture.',asset=name)
        expected,width=corner_records(mesh,memory_bytes=memory_bytes)
        actual,target_width=corner_records(other,memory_bytes=memory_bytes)
        require(width==target_width,'RIG_ROUNDTRIP_IDENTITY','Round trip changed corner attribute widths.',asset=name)
        surface=match_rows(expected,actual,cyclic_width=width,memory_bytes=memory_bytes,maximum_work=remaining_work)
        remaining_work-=surface['work']
        loose=match_rows(loose_records(mesh,memory_bytes=memory_bytes),loose_records(other,memory_bytes=memory_bytes),
                         memory_bytes=memory_bytes,maximum_work=remaining_work)
        remaining_work-=loose['work']
        meshes[name]=dict(loose_vertices=loose['records'],oriented_faces=surface['records'],degenerate_faces=surface['degenerate_faces'],
                          max_corner_component_error=surface['max_component_error'],material_binding=True,
                          source_images=source_images,imported_images=target_images)
    return dict(meshes=meshes,rest_geometry=True,oriented_triangle_multiplicity=True,corner_attributes=True,loose_vertices=True,
                default_morphs=True,material_bindings=True,texture_comparison='exact decoded RGBA; encoded hashes retained',
                geometry_component_tolerance_m=2e-5,attribute_component_tolerance=2e-5,
                material_numeric_tolerance=1e-6,work=maximum_work-remaining_work)
