"""Exact source bindings and measurable identity checks for semantic 3D parts."""
from __future__ import annotations
from copy import deepcopy
import hashlib
from pathlib import Path
import numpy as np

from rig_errors import decode_json, finite_json, object_hash, open_payload, require
from model3d_semantic import (SemanticAsset, _finish, evaluated_geometry, identifier,
                              create_asset,part_signatures, validate_asset, world_matrices)


def _capture(path,maximum=32*1024**2):
    path=Path(path).absolute()
    with open_payload(path.parent,path.name) as (stream,info):
        require(0<info.st_size<=maximum,'MODEL3D_IDENTITY_SOURCE','Source evidence exceeds its bounded size.')
        data=stream.read(maximum+1)
        require(len(data)==info.st_size,'MODEL3D_IDENTITY_SOURCE','Source evidence changed during capture.')
    return data


def identity_contract(source_identity,*,required_parts=(),forbidden_parts=(),ratios=(),colors=(),strict=True):
    """Freeze supplied invariants; no anatomy or subjective confidence is inferred."""
    data=_capture(source_identity);payload=decode_json(data)
    require(isinstance(payload,dict),'MODEL3D_IDENTITY_SOURCE','Expected original identity JSON object.')
    _source_identity(payload)
    require(type(strict) is bool,'MODEL3D_IDENTITY','strict must be an explicit boolean.')
    contract=dict(schema_version=1,kind='model3d-identity',source_sha256=hashlib.sha256(data).hexdigest(),
                  required_parts=list(required_parts),forbidden_parts=list(forbidden_parts),
                  ratios=deepcopy(list(ratios)),colors=deepcopy(list(colors)),strict=strict)
    for rule in contract['ratios']:
        if isinstance(rule,dict):rule.setdefault('scope','owned-meshes')
    _contract(contract)
    return contract,data


def _source_identity(payload):
    from asset_contracts import validate_identity as native_identity
    require(isinstance(payload,dict) and isinstance(payload.get('directions',{}),dict),
            'MODEL3D_IDENTITY_SOURCE','Expected existing Hatch identity metadata.')
    try:
        return native_identity(payload,{'states':[{'id':key} for key in payload.get('directions',{})]})
    except (ValueError,TypeError) as exc:
        require(False,'MODEL3D_IDENTITY_SOURCE','Existing identity contract is invalid: '+str(exc)[:1024])


def _contract(value):
    require(isinstance(value,dict) and set(value)=={'schema_version','kind','source_sha256','required_parts',
            'forbidden_parts','ratios','colors','strict'} and type(value['schema_version']) is int
            and value['schema_version']==1 and value['kind']=='model3d-identity' and type(value['strict']) is bool,
            'MODEL3D_IDENTITY','Unsupported identity contract.')
    require(isinstance(value['source_sha256'],str) and len(value['source_sha256'])==64
            and all(c in '0123456789abcdef' for c in value['source_sha256']),
            'MODEL3D_IDENTITY','Identity source requires an exact hash.')
    for field in ('required_parts','forbidden_parts'):
        require(isinstance(value[field],list) and len(value[field])<=4096,'MODEL3D_IDENTITY','Identity part list exceeds its budget.')
        for key in value[field]:identifier(key)
        require(len(set(value[field]))==len(value[field]),'MODEL3D_IDENTITY','Duplicate identity part.')
    require(not set(value['required_parts']) & set(value['forbidden_parts']),'MODEL3D_IDENTITY','Identity parts conflict.')
    require(isinstance(value['ratios'],list) and len(value['ratios'])<=256
            and isinstance(value['colors'],list) and len(value['colors'])<=256,
            'MODEL3D_IDENTITY','Identity measurements exceed their budget.')
    for record in value['ratios']:
        require(isinstance(record,dict) and set(record)=={'numerator','denominator','axis','minimum','maximum','scope'}
                and record['scope'] in ('owned-meshes','subtree'),
                'MODEL3D_IDENTITY','Ratio requires explicit parts, axis and thresholds.')
        identifier(record['numerator']);identifier(record['denominator'])
        require(type(record['axis']) is int and 0<=record['axis']<=2
                and type(record['minimum']) in (int,float) and type(record['maximum']) in (int,float)
                and 0<=record['minimum']<=record['maximum']<=1e6,
                'MODEL3D_IDENTITY','Invalid geometric ratio thresholds.')
    for record in value['colors']:
        require(isinstance(record,dict) and set(record)=={'part','rgba','maximum_error'},
                'MODEL3D_IDENTITY','Color invariant requires a part, rgba and tolerance.')
        identifier(record['part'])
        require(isinstance(record['rgba'],list) and len(record['rgba'])==4
                and all(type(v) in (int,float) and 0<=v<=1 for v in record['rgba'])
                and type(record['maximum_error']) in (int,float) and 0<=record['maximum_error']<=1,
                'MODEL3D_IDENTITY','Invalid explicit color tolerance.')
    finite_json(value)


def validate_identity(asset):
    validate_asset(asset)
    if asset.identity is None:return dict(ok=True,status='unbound',checks=[],unmeasured=['No identity invariants supplied.'])
    contract=asset.identity;_contract(contract)
    require(contract['source_sha256'] in asset.evidence
            and hashlib.sha256(asset.evidence[contract['source_sha256']]).hexdigest()==contract['source_sha256'],
            'MODEL3D_IDENTITY_SOURCE','Original identity bytes are absent or changed.')
    source_identity=_source_identity(decode_json(asset.evidence[contract['source_sha256']]))
    checks=[];world=world_matrices(asset);known=asset.geometry.by_id()
    if source_identity['forbid_mirror']:
        reflected=[key for key,m in world.items() if np.linalg.det(m[:3,:3]/np.max(np.abs(m[:3,:3]),axis=0))<0]
        checks.append(dict(check='source-forbids-reflected-part-transforms',parts=reflected,ok=not reflected))
    for key in contract['required_parts']:checks.append(dict(check='required-part',part=key,ok=key in asset.parts))
    for key in contract['forbidden_parts']:checks.append(dict(check='forbidden-part',part=key,ok=key not in asset.parts))
    extent_cache={}
    def extent(key,axis,scope):
        cache_key=(key,axis,scope)
        if cache_key in extent_cache:return extent_cache[cache_key]
        if key not in asset.parts:return None
        points=[]
        subtree={key};last=-1
        while scope=='subtree' and last!=len(subtree):
            last=len(subtree);subtree|={k for k,p in asset.parts.items() if p['parent'] in subtree}
        for child in subtree:
            for mid in asset.parts[child]['meshes']:
                with np.errstate(over='ignore',invalid='ignore'):
                    transformed=known[mid].positions@world[child][:3,:3].T+world[child][:3,3]
                require(np.isfinite(transformed).all(),'MODEL3D_IDENTITY','Identity measurement exceeds the represented numeric range.')
                points.append(transformed)
        if not points:return None
        extent_cache[cache_key]=float(max(p[:,axis].max() for p in points)-min(p[:,axis].min() for p in points))
        return extent_cache[cache_key]
    for record in contract['ratios']:
        a=extent(record['numerator'],record['axis'],record['scope']);b=extent(record['denominator'],record['axis'],record['scope'])
        ratio=a/b if a is not None and b is not None and b>0 else None
        checks.append(dict(check='extent-ratio',rule=record,measured=ratio,
                           ok=bool(ratio is not None and np.isfinite(ratio) and record['minimum']<=ratio<=record['maximum'])))
    for record in contract['colors']:
        values=[]
        if record['part'] in asset.parts:
            for mid in asset.parts[record['part']]['meshes']:
                material=asset.geometry.materials[known[mid].material]
                pbr=material.get('pbrMetallicRoughness',{})
                # A color factor cannot certify the appearance of a texture.
                if 'baseColorTexture' in pbr:values.append(None)
                else:
                    factor=np.asarray(pbr.get('baseColorFactor',[1,1,1,1]))
                    colors=known[mid].attributes.get('COLOR_0',np.ones((1,4)))
                    values.append(float(np.max(np.abs(colors*factor-record['rgba']))))
        checks.append(dict(check='linear-base-color-with-vertex-modulation',rule=record,measured=values,
                           ok=bool(values) and all(v is not None and v<=record['maximum_error'] for v in values)))
    return dict(schema_version=1,ok=all(c['ok'] for c in checks),status='explicit-measurable-invariants-only',source_sha256=contract['source_sha256'],
                checks=checks,complete_identity_validation=False,
                retained_source_constraints={key:value for key,value in source_identity.items() if value and key!='schema_version'},
                unmeasured=['Unmapped source markings/proportions/palette/anatomy, rendered silhouette correspondence, texture appearance and artistic approval require explicit mapping and actual review.'])


def bind_identity(asset,contract,source_bytes):
    _contract(contract)
    require(isinstance(source_bytes,bytes) and hashlib.sha256(source_bytes).hexdigest()==contract['source_sha256'],
            'MODEL3D_IDENTITY_SOURCE','Contract must bind exact original identity bytes.')
    result=deepcopy(asset);result.identity=deepcopy(contract);result.evidence[contract['source_sha256']]=source_bytes
    report=validate_identity(result)
    require(not contract['strict'] or report['ok'],'MODEL3D_IDENTITY','Supplied identity invariants do not hold.')
    return _finish(asset,result,'bind-exact-identity')


def map_decomposition(asset,root,mapping):
    """Bind explicit 2D-to-native-part correspondences without asserting 3D recovery."""
    from character_decomposition import load_decomposition
    manifest=load_decomposition(root,verify=True)
    require(isinstance(mapping,dict) and bool(mapping) and len(mapping)<=4096
            and set(mapping)<=set(asset.parts),'MODEL3D_DECOMPOSITION','Map known native parts explicitly.')
    components={c['id']:c for c in manifest['components']};result=deepcopy(asset)
    original_manifest=_capture(Path(root)/'decomposition.json')
    require(decode_json(original_manifest)==manifest,'MODEL3D_DECOMPOSITION','Decomposition changed during evidence capture.')
    manifest_hash=hashlib.sha256(original_manifest).hexdigest();result.evidence[manifest_hash]=original_manifest
    # The authoritative native manifest filename is discovered by its loader;
    # retain exact component/source/annotation/identity evidence from that record.
    for part_id,component_id in mapping.items():
        require(component_id in components,'MODEL3D_DECOMPOSITION','Unknown source component.')
        component=components[component_id]
        record=dict(kind='explicit-decomposition-correspondence',component_id=component_id,
                    manifest_sha256=manifest_hash,
                    source_sha256=manifest['source']['sha256'],component=deepcopy(component),
                    canvas=manifest['canvas'],source_semantic_review=component['review']['status'],
                    spatial_mapping='2D evidence only; depth and 3D pivot require authored geometry.',
                    native_meshes=list(result.parts[part_id]['meshes']))
        for ref in [component['asset'],component['mask'],manifest['annotation'],manifest['identity']]:
            data=_capture(Path(root)/ref['path'])
            require(hashlib.sha256(data).hexdigest()==ref['sha256'],'MODEL3D_DECOMPOSITION','Source evidence hash changed.')
            result.evidence[ref['sha256']]=data
        data=_capture(Path(root)/manifest['source']['path'])
        require(hashlib.sha256(data).hexdigest()==manifest['source']['sha256'],'MODEL3D_DECOMPOSITION','Original source changed.')
        result.evidence[manifest['source']['sha256']]=data
        result.parts[part_id]['evidence'].append(record)
    return _finish(asset,result,'map-explicit-decomposition')


def bind_style(asset,style_id,*,notes='',adaptations=None):
    from style_registry import resolve_style
    snapshot=resolve_style(style_id,notes=notes)
    require(isinstance(adaptations,dict) and set(adaptations)=={'geometry','material','texture','render'}
            and all(isinstance(v,str) and 0<len(v)<=4096 for v in adaptations.values()),
            'MODEL3D_STYLE','Supply explicit geometry/material/texture/render adaptations; styles do not generate meshes.')
    result=deepcopy(asset);result.style=dict(snapshot=snapshot,adaptations=deepcopy(adaptations),
                                          source_sha256=object_hash(snapshot),geometry_generated=False)
    return _finish(asset,result,'bind-style-snapshot')


def map_states(asset,mapping):
    from state_catalog import load_library
    library=load_library()
    require(isinstance(mapping,dict) and len(mapping)<=416,'MODEL3D_STATE','Expected bounded explicit state mapping.')
    result=deepcopy(asset)
    for state,pose in mapping.items():
        require(state in library.states,'MODEL3D_STATE','State identifier is not in the existing catalog.')
        require(pose is None or pose in asset.poses,'MODEL3D_STATE','State must select an existing real rigid pose or be unavailable.')
        result.states[state]=dict(pose=pose,status='rigid-pose' if pose is not None else 'unavailable',
                                 catalog_sha256=library.sha256,animation=False,review='pending')
    return _finish(asset,result,'map-existing-state-definitions')


def from_assembly_ir(value):
    """Adapt the authoritative #45 local meshes and hierarchy when installed."""
    import json
    from rig_models import Geometry
    from rig_math import compose
    try:from model3d_assemblies import generate
    except ImportError:
        require(False,'MODEL3D_ASSEMBLY_UNAVAILABLE','Install the existing procedural assembly owner before adapting its IR.')
    assembly=generate(value)
    geometry=Geometry([deepcopy(assembly.editable[part['id']].mesh) for part in assembly.ir['parts']],
                      deepcopy(assembly.geometry.materials))
    asset=create_asset(geometry,asset_id=assembly.ir['id'],fixture=assembly.ir['fixture'])
    original=deepcopy(asset)
    data=json.dumps(assembly.ir,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    digest=hashlib.sha256(data).hexdigest();asset.evidence[digest]=data
    for item in assembly.ir['parts']:
        part=asset.parts[item['id']];tr=item['transform']
        part.update(parent=item['parent'],semantic=item['semantic'],
                    matrix=compose(tr['translation'],tr['rotation'],tr['scale']).tolist(),
                    attachments=[dict(id=key,point=point,target=None) for key,point in item['attachments'].items()])
        part['evidence'].append(dict(kind='validated-procedural-assembly-ir',source_sha256=digest,
            original_file=False,method='Authoritative assembly owner generated local native geometry and normalized IR.'))
    return _finish(original,asset,'adapt-existing-procedural-assembly')
