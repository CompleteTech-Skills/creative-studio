"""Versioned UV, image and visibility-projection edits on semantic assemblies."""
from copy import deepcopy
import hashlib
import json

from model3d_semantic import _finish, _mesh_hash, _resource_hashes, evaluated_geometry, part_signatures, validate_asset
from rig_errors import object_hash, require


def _apply(asset,geometry,report,operation,*,sources=()):
    result=deepcopy(validate_asset(asset));known=asset.geometry.by_id();updated=geometry.by_id()
    require(set(known)==set(updated),'MODEL3D_PART_SURFACES','Surface edits must retain all semantic mesh identities.')
    affected=[]
    for key,part in asset.parts.items():
        changed=any(_mesh_hash(known[mid])!=_mesh_hash(updated[mid]) for mid in part['meshes'])
        old=[asset.geometry.materials[known[mid].material] for mid in part['meshes']]
        new=[geometry.materials[updated[mid].material] for mid in part['meshes']]
        if changed or old!=new or _resource_hashes(asset.geometry,old)!=_resource_hashes(geometry,new):affected.append(key)
    result.geometry=geometry
    envelope=dict(schema_version=1,kind='model3d-semantic-surface-operation',operation=operation,
                  source_parts_sha256=object_hash(part_signatures(asset)),report=report,
                  result_parts_sha256=object_hash(part_signatures(result)))
    data=json.dumps(envelope,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    digest=hashlib.sha256(data).hexdigest();result.evidence[digest]=data
    for source in sources:
        require(isinstance(source,bytes),'MODEL3D_PART_SURFACES','Preserved source images must be exact bytes.')
        result.evidence[hashlib.sha256(source).hexdigest()]=source
    for key in affected:result.parts[key]['evidence'].append(dict(kind='measured-surface-operation',operation=operation,report_sha256=digest))
    return _finish(asset,result,operation)


def generate_asset_uv(asset,**options):
    from model3d_uv import generate_uv
    snapshot=deepcopy(validate_asset(asset));geometry,report=generate_uv(snapshot.geometry,**options)
    return _apply(snapshot,geometry,report,'generate-uv')


def texture_asset(asset,material_slot,image,**options):
    from model3d_materials import bind_texture,validate_texture
    snapshot=deepcopy(validate_asset(asset));source=deepcopy(image)
    geometry=bind_texture(snapshot.geometry,material_slot,source,**options)
    report=dict(material_slot=material_slot,source_sha256=hashlib.sha256(source.data).hexdigest(),
                texture=validate_texture(source),binding=options)
    return _apply(snapshot,geometry,report,'bind-portable-image',sources=[source.data])


def project_asset(asset,views,**options):
    from model3d_projection import project_views
    snapshot=deepcopy(validate_asset(asset));views=deepcopy(views)
    world=evaluated_geometry(snapshot);projected,report=project_views(world,views,**options)
    geometry=deepcopy(snapshot.geometry);known=geometry.by_id()
    for mesh in projected.meshes:known[mesh.id].attributes['COLOR_0']=mesh.attributes['COLOR_0'].copy()
    report=dict(report,semantic_frame='Canonical evaluated world geometry; native local positions and hierarchy retained.')
    return _apply(snapshot,geometry,report,'project-calibrated-visible-colors',sources=[view['image'].data for view in views])
