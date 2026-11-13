"""Portable dependency graph, lock, requirements and replacement contracts."""
from functools import lru_cache
from asset_library_contracts import definitions as library_definitions, canonical_bytes
from rig_errors import RigError


def obj(properties,required=None):
    return dict(type='object',additionalProperties=False,properties=properties,
                required=list(properties) if required is None else required)


def arr(items,maximum=65536):return dict(type='array',items=items,maxItems=maximum)
def ref(name):return {'$ref':'#/$defs/'+name}


@lru_cache(maxsize=1)
def definitions():
    reference=library_definitions()['AssetRef']
    sha=dict(type='string',pattern=r'^[0-9a-f]{64}$(?![\s\S])')
    text=dict(type='string',minLength=1,maxLength=4096)
    strings=arr(text,128);strings['uniqueItems']=True
    loc=obj(dict(library=dict(type='string',minLength=1,maxLength=128),reference=reference))
    file=obj(dict(path=library_definitions()['AssetFile']['properties']['path'],sha256=sha,
                  bytes=dict(type='integer',minimum=1,maximum=64*1024*1024)))
    node=obj(dict(**loc['properties'],library_id=text,files=arr(file,256)))
    diagnostic=obj(dict(code=text,asset=loc,required=dict(type='boolean'),cause=text),['code','asset','required'])
    edge=obj(dict(source=loc,target=loc,purpose=text,required=dict(type='boolean'),
                  status=dict(enum=['resolved','excluded','unavailable','cycle'])))
    graph=obj(dict(schema_version=dict(type='integer',const=1),kind=dict(const='asset-dependency-graph'),
                   roots=arr(loc,4096),nodes=arr(node,4096),edges=arr(edge),diagnostics=arr(diagnostic),complete=dict(type='boolean')))
    lock=obj(dict(schema_version=dict(type='integer',const=1),kind=dict(const='asset-dependency-lock'),
                  include_optional=dict(type='boolean'),graph=ref('graph'),sha256=sha))
    requirements=obj(dict(coordinate_system=dict(enum=['right-handed-y-up-z-forward','legacy-2d']),
        unit_scale=dict(type='number',exclusiveMinimum=0,maximum=1e6),
        skeleton_sha256=sha,rest_pose_sha256=sha,skin_sha256=sha,morph_sha256=sha,material_sha256=sha,
        rig_type=text,root_motion=strings,material_capabilities=strings,texture_channels=strings,
        animation_channels=strings,renderers=strings,formats=strings,extensions=strings),[])
    native=obj(dict(skeleton_sha256=sha,rest_pose_sha256=sha,skin_sha256=sha,morph_sha256=sha,material_sha256=sha,
                    rig_type=text,root_motion=strings,bone_count=dict(type='integer',minimum=0,maximum=256),
                    mesh_count=dict(type='integer',minimum=1,maximum=4096),
                    coordinate_system=dict(const='right-handed-y-up-z-forward'),unit_scale=dict(type='number',const=1)))
    compatibility=obj(dict(compatible=dict(type='boolean'),mismatches=arr(obj(dict(field=text,expected={},actual={})),128),
                           native={'anyOf':[native,dict(type='null')]},renderer_execution_verified=dict(const=False)))
    change=obj(dict(asset=loc,indices=arr(dict(type='integer',minimum=0,maximum=255),256),
                    purposes=arr(text,256),original_manifest_sha256=sha))
    plan=obj(dict(schema_version=dict(type='integer',const=1),kind=dict(const='asset-replacement-plan'),
                  old=loc,new=loc,changes=arr(change,4096),compatibility=compatibility,
                  invalidated=arr(loc,4096),old_lock=ref('lock'),new_lock=ref('lock'),
                  project_files_modified=dict(const=False),sha256=sha))
    return dict(graph=graph,lock=lock,requirements=requirements,plan=plan)


def schemas():
    defs=definitions()
    return {'asset-dependency-'+name+'.schema.json':dict(
        **{'$schema':'https://json-schema.org/draft/2020-12/schema','$defs':defs,'$ref':'#/$defs/'+name},
        title='Asset dependency '+name+' v1') for name in defs}


def validate(value,kind):
    from jsonschema import Draft202012Validator
    if len(canonical_bytes(value))>16*1024*1024:
        raise RigError('LIBRARY_DEPENDENCY_LIMIT','Dependency document exceeds 16 MiB.')
    error=next(Draft202012Validator({'$defs':definitions(),'$ref':'#/$defs/'+kind}).iter_errors(value),None)
    if error:raise RigError('LIBRARY_DEPENDENCY_SCHEMA',error.message,asset='.'.join(map(str,error.absolute_path)))
    return value
