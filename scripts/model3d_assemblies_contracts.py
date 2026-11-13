"""Version-one procedural assembly IR; discovery needs no numerical backend."""
from __future__ import annotations
from copy import deepcopy
import math
import re
from rig_errors import require, finite_json

VERSION='1.0.0'
LIMITS=dict(parts=256,vertices=100_000,triangles=200_000,memory_bytes=256*1024**2)
FORMS={'box','plane','sphere','cylinder','cone','torus','ellipsoid','capsule','tube','curve','extrusion'}
FAMILIES={'plastic','metal','rubber','cloth','wood','stone','glass','ceramic','painted'}
IDENTIFIER=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?![\s\S])'


def schema():
    def array(items,n):return dict(type='array',items=items,minItems=n,maxItems=n)
    def obj(properties):return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)
    number=dict(type='number',minimum=-10000,maximum=10000)
    vector=array(number,3);identifier=dict(type='string',pattern=IDENTIFIER,maxLength=64)
    material=obj(dict(id=identifier,family=dict(enum=sorted(FAMILIES)),
        color=array(dict(type='number',minimum=0,maximum=1),4),
        metallic=dict(type='number',minimum=0,maximum=1),roughness=dict(type='number',minimum=.04,maximum=1)))
    part=obj(dict(id=identifier,parent=dict(anyOf=[identifier,dict(type='null')]),
        semantic=dict(type='string',minLength=1,maxLength=128),
        primitive=dict(enum=['box','plane','sphere','cylinder','cone','torus','ellipsoid','capsule','tube','curve','extrusion']),
        parameters=dict(type='object',maxProperties=8),
        transform=obj(dict(translation=vector,rotation=array(dict(type='number',minimum=-1,maximum=1),4),
                           scale=array(dict(type='number',minimum=.0001,maximum=10000),3))),
        material=identifier,attachments=dict(type='object',propertyNames=identifier,additionalProperties=vector,maxProperties=32)))
    result=obj(dict(schema_version=dict(type='integer',const=1),kind=dict(const='hatch-model3d-assembly'),
        id=identifier,seed=dict(type='integer',minimum=0,maximum=2**32-1),
        description=dict(type='string',minLength=1,maxLength=8192),fixture=dict(type='boolean'),
        materials=dict(type='array',items=material,minItems=1,maxItems=32),
        parts=dict(type='array',items=part,minItems=1,maxItems=LIMITS['parts'])))
    result.update({'$schema':'https://json-schema.org/draft/2020-12/schema','title':'Hatch procedural assembly IR v1'})
    return result


def validate_ir(value, *, limits=None):
    """Return a normalized independent IR, checking graph and preallocation budgets."""
    finite_json(value)
    from jsonschema import Draft202012Validator
    error=next(Draft202012Validator(schema()).iter_errors(value),None)
    require(error is None,'MODEL3D_ASSEMBLY_SCHEMA',error.message[:512] if error else '',
            asset='/'.join(map(str,error.absolute_path)) if error else None)
    require(type(value['schema_version']) is int and type(value['seed']) is int,
            'MODEL3D_ASSEMBLY_SCHEMA','Version and seed must be actual JSON integers.')
    selected=dict(LIMITS)
    require(limits is None or isinstance(limits,dict) and not set(limits)-set(LIMITS),
            'MODEL3D_ASSEMBLY_BUDGET','Unknown assembly budget.')
    for key,amount in (limits or {}).items():
        require(type(amount) is int and 0<amount<=LIMITS[key], 'MODEL3D_ASSEMBLY_BUDGET','Budgets must be bounded positive integers.')
        selected[key]=amount
    # Inspect parameters and allocation bounds before copying caller arrays.
    result=value
    normalized={}
    materials={item['id'] for item in result['materials']}
    parts={item['id']:item for item in result['parts']}
    require(len(materials)==len(result['materials']) and len(parts)==len(result['parts']),
            'MODEL3D_ASSEMBLY_REFERENCE','Material and part IDs must be unique.')
    require(len(parts)<=selected['parts'],'MODEL3D_ASSEMBLY_BUDGET','Part budget exceeded.')
    require(any(part['parent'] is None for part in parts.values()),'MODEL3D_ASSEMBLY_REFERENCE','Assembly needs a root part.')
    from model3d_assemblies_shapes import parameters
    vertices=triangles=0
    for part in parts.values():
        require(part['material'] in materials and (part['parent'] is None or part['parent'] in parts),
                'MODEL3D_ASSEMBLY_REFERENCE','Missing part parent or material.',asset=part['id'])
        seen=set();current=part
        while current is not None:
            require(current['id'] not in seen and len(seen)<32,'MODEL3D_ASSEMBLY_HIERARCHY','Part hierarchy cycles or exceeds depth32.')
            seen.add(current['id']);current=parts.get(current['parent'])
        q=part['transform']['rotation']
        require(abs(sum(v*v for v in q)-1)<1e-6,'MODEL3D_ASSEMBLY_TRANSFORM','Rotation must be a unit XYZW quaternion.')
        normalized[part['id']],(v,t)=parameters(part['primitive'],part['parameters'])
        vertices+=v;triangles+=t
        require(vertices<=selected['vertices'] and triangles<=selected['triangles']
                and vertices*2000+triangles*500<=selected['memory_bytes'],
                'MODEL3D_ASSEMBLY_BUDGET','Assembly working-set estimate exceeds its budget before mesh allocation.')
    result=deepcopy(value)
    for part in result['parts']:part['parameters']=normalized[part['id']]
    return result
