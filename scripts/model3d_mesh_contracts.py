"""Strict versioned contracts for existing Model3D mesh interchange records.

Pure JSON validation: no file reads, symlink checks, acquisition or second store.
Validators return an independent normalized copy; original serialized evidence is
never modified. JSON Schema integers include finite whole-valued JSON floats.
"""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
import json
import unicodedata
from rig_errors import MAX_JSON_BYTES, RigError, finite_json, portable_name, require

EXPORT_SCHEMA = 'model3d-mesh-export.schema.json'
EDITABLE_SCHEMA = 'model3d-editable-topology.schema.json'
MAX_VERTICES = 1_000_000
MAX_TRIANGLES = 2_000_000
MAX_FILES = 4096
MAX_FILE_BYTES = 256 * 1024**2
MAX_TOTAL_BYTES = 1024**3


def _object(properties, required=None):
    return dict(type='object', properties=properties,
                required=list(properties) if required is None else required,
                additionalProperties=False)


def _array(items, minimum, maximum):
    return dict(type='array', items=items, minItems=minimum, maxItems=maximum)


def _integer(minimum, maximum):
    return dict(type='integer', minimum=minimum, maximum=maximum)


def schemas():
    """Fresh Draft 2020-12 schema values; discovery imports no numerical backend."""
    from model3d_contracts import schemas as base_schemas
    # One portable path spelling contract rather than an independent path policy.
    path = base_schemas()['model3d.schema.json']['$defs']['file']['properties']['path']
    identifier = dict(type='string', minLength=1, maxLength=128,
                      pattern=r'^[^\x00-\x1f\x7f\ud800-\udfff]+(?![\s\S])')
    text = dict(type='string', maxLength=4096, pattern=r'^[^\ud800-\udfff]*(?![\s\S])')
    line = dict(type='string', minLength=1, maxLength=4096,
                pattern=r'^[^\x00-\x1f\x7f\ud800-\udfff]+(?![\s\S])')
    header = dict(schema_version=dict(type='integer', const=1))
    metadata = _object(dict(bytes=_integer(1,MAX_FILE_BYTES), sha256=dict(
        type='string',pattern=r'^[0-9a-f]{64}(?![\s\S])')))
    export = _object(dict(**header, kind=dict(const='hatch-model3d-mesh-export'),
        format=dict(enum=['glb','gltf','obj']), vertices=_integer(3,MAX_VERTICES),
        triangles=_integer(1,MAX_TRIANGLES),
        files=dict(type='object', propertyNames=path, additionalProperties=metadata,
                   minProperties=1,maxProperties=MAX_FILES),
        mesh_identity=_array(_object(dict(id=identifier,name=text,region=text,
                                         source_node=_integer(-1,2_000_000))),1,4096),
        material_names=_array(dict(anyOf=[text,dict(type='null')]),1,65536),
        exported_material_slots=_array(_object(dict(slot=_integer(0,65535),name=line)),1,4096),
        limitations=_array(dict(type='string',minLength=1,maxLength=4096),1,128)))
    editable = _object(dict(**header,kind=dict(const='hatch-model3d-editable-topology'),
        mesh=identifier,positions=_array(_array(dict(type='number',minimum=-1e100,maximum=1e100),3,3),3,MAX_VERTICES),
        faces=_array(_array(_integer(0,MAX_VERTICES-1),3,256),1,MAX_TRIANGLES),
        triangle_faces=_array(_integer(0,MAX_TRIANGLES-1),1,MAX_TRIANGLES),
        normal_mode=dict(enum=['smooth','flat'])))
    for name,schema in ((EXPORT_SCHEMA,export),(EDITABLE_SCHEMA,editable)):
        schema['$schema']='https://json-schema.org/draft/2020-12/schema'
        schema['title']='Hatch Model3D '+name.removesuffix('.schema.json')+' v1'
        schema['description']='Structural validation only; runtime checks references and counts. No filesystem integrity or approval claim.'
    return {EXPORT_SCHEMA:export,EDITABLE_SCHEMA:editable}


def _validate(value, name):
    finite_json(value)
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RigError('MODEL3D_DEPENDENCY','Install requirements-rig.txt for mesh contract validation.') from exc
    error=next(Draft202012Validator(schemas()[name]).iter_errors(value),None)
    if error:
        raise RigError('MODEL3D_MESH_SCHEMA',error.message[:2048],
                       asset='/'+'/'.join(map(str,error.absolute_path)))
    # Bound aggregate serialized metadata as well as individual arrays/fields.
    # iterencode avoids materializing a second complete JSON byte buffer.
    byte_count=0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=True,allow_nan=False,separators=(',', ':')).iterencode(value):
            byte_count+=len(chunk.encode('ascii'))
            require(byte_count<=MAX_JSON_BYTES,'MODEL3D_MESH_LIMIT','Mesh JSON exceeds the shared metadata byte cap.')
    except (TypeError,ValueError,UnicodeError) as exc:
        if isinstance(exc,RigError):raise
        raise RigError('MODEL3D_MESH_SCHEMA','Mesh record must contain serializable JSON values.') from exc
    return deepcopy(value)


def validate_export(record):
    """Validate export metadata/references and return a normalized JSON copy.

    Actual hash/size verification, native decoding and atomic publication remain
    the interchange owner's responsibility. File names cannot attest to symlinks.
    """
    result=_validate(record,EXPORT_SCHEMA)
    for key in ('schema_version','vertices','triangles'):result[key]=int(result[key])
    require(len({m['id'] for m in result['mesh_identity']})==len(result['mesh_identity']),
            'MODEL3D_MESH_REFERENCE','Export mesh IDs must be unique.')
    require(result['vertices']>=3*len(result['mesh_identity']) and result['triangles']>=len(result['mesh_identity']),
            'MODEL3D_MESH_REFERENCE','Counts must accommodate every nonempty native mesh.')
    for mesh in result['mesh_identity']:mesh['source_node']=int(mesh['source_node'])
    slots=result['exported_material_slots']
    for slot in slots:slot['slot']=int(slot['slot'])
    indices=[slot['slot'] for slot in slots]
    require(len(indices)==len(set(indices)) and all(i<len(result['material_names']) for i in indices),
            'MODEL3D_MESH_REFERENCE','Exported material slots must be unique and reference source material names.')
    require(len(slots)<=len(result['mesh_identity']), 'MODEL3D_MESH_REFERENCE',
            'Every exported material slot must be used by a mesh.')
    for slot in slots:
        source=result['material_names'][slot['slot']]
        expected=source or f"material_{slot['slot']}"
        require(slot['name']==expected and slot['name']==slot['name'].strip(),
                'MODEL3D_MESH_REFERENCE','Exported slot name must preserve its source material name or explicit fallback.')
    if result['format']=='obj':
        names=[m['name'] or m['id'] for m in result['mesh_identity']]
        material_names=[s['name'] for s in slots]
        require(all(name==name.strip() and all(ord(c)>=32 and ord(c)!=127 for c in name) for name in names)
                and len(names)==len(set(names)) and len(material_names)==len(set(material_names))
                and 'default' not in material_names, 'MODEL3D_MESH_REFERENCE',
                'OBJ object and material names must be unique; default is reserved.')
    aliases={}
    paths=set(result['files'])
    for path, metadata in result['files'].items():
        canonical=portable_name(path)
        alias=unicodedata.normalize('NFC',canonical).casefold()
        require(path==unicodedata.normalize('NFC',path) and alias not in aliases,
                'MODEL3D_MESH_PATH','Export paths must be NFC and must not case-collide.')
        aliases[alias]=path
        metadata['bytes']=int(metadata['bytes'])
    alias_paths=set(aliases)
    require(not any('/'.join(p.split('/')[:i]) in alias_paths for p in alias_paths
                    for i in range(1,len(p.split('/')))),
            'MODEL3D_MESH_PATH','A payload path cannot also be a directory prefix.')
    require(sum(f['bytes'] for f in result['files'].values())<=MAX_TOTAL_BYTES,
            'MODEL3D_MESH_LIMIT','Export dependency byte limit exceeded.')
    required={'model.'+result['format']}
    if result['format']=='obj':required.add('materials.mtl')
    require(required<=paths and 'manifest.json' not in paths, 'MODEL3D_MESH_REFERENCE',
            'Export requires its primary artifact and cannot self-hash its manifest.')
    return result


def validate_editable(record):
    """Validate indexed polygon/render-face correspondence; return normalized copy.

    Polygon planarity/triangulation and actual render geometry are independently
    validated by the primitive/interchange authorities, not manufactured here.
    """
    result=_validate(record,EDITABLE_SCHEMA)
    result['schema_version']=int(result['schema_version'])
    result['faces']=[[int(index) for index in face] for face in result['faces']]
    result['triangle_faces']=[int(index) for index in result['triangle_faces']]
    vertices=len(result['positions']);faces=result['faces'];owners=result['triangle_faces']
    for face in faces:
        require(len(set(face))==len(face) and max(face)<vertices,
                'MODEL3D_MESH_REFERENCE','Polygon corners must be unique valid source vertex references.')
    require(max(owners)<len(faces),'MODEL3D_MESH_REFERENCE','Triangle owner references missing polygon.')
    expected=sum(len(face)-2 for face in faces)
    require(expected<=MAX_TRIANGLES and len(owners)==expected,'MODEL3D_MESH_REFERENCE',
            'Editable polygons require exactly n-2 triangle ownership entries each.')
    counts=Counter(owners)
    require(all(counts[i]==len(face)-2 for i,face in enumerate(faces)),
            'MODEL3D_MESH_REFERENCE','Every polygon must own exactly n-2 triangles.')
    return result
