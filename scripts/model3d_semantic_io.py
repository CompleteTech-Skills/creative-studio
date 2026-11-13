"""Immutable semantic snapshots and real hierarchical native GLB/glTF export."""
from __future__ import annotations
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unicodedata
import struct
import zipfile
import numpy as np

from atomic_publish import _rename_no_replace
from rig_contracts import DEFAULT_LIMITS,validate_contract
from rig_errors import (RigError,atomic_bytes,atomic_json,decode_json,object_hash,open_payload,
                        portable_name,read_json,require,safe_path,sha256)
from rig_models import load_geometry, save_geometry
from model3d_semantic import SemanticAsset, evaluated_geometry, part_signatures, validate_asset


def _manifest(asset,geometry,evidence):
    return dict(schema_version=1,kind='model3d-semantic-asset',id=asset.id,version=asset.version,
                geometry=geometry,parts=asset.parts,history=asset.history,evidence=evidence,
                identity=asset.identity,style=asset.style,poses=asset.poses,states=asset.states,
                derivatives=asset.derivatives,fixture=asset.fixture)


def _write(asset,stage):
    validate_asset(asset)
    geometry=save_geometry(stage,asset.geometry);records={}
    for key,data in asset.evidence.items():
        require(key==hashlib.sha256(data).hexdigest(),'MODEL3D_SEMANTIC_EVIDENCE','Evidence key must be its byte hash.')
        path='evidence/'+key+'.bin';atomic_bytes(stage/path,data);records[key]=dict(path=path,bytes=len(data),sha256=key)
    manifest=_manifest(asset,geometry,records);manifest['sha256']=object_hash(manifest)
    atomic_json(stage/'semantic.json',manifest)
    loaded=load_asset(stage)
    require(part_signatures(loaded)==part_signatures(asset),'MODEL3D_SEMANTIC_ROUNDTRIP','Native snapshot changed component geometry or metadata.')
    return manifest


def save_asset(asset,destination):
    """Save a new revision directory; every pre-existing destination is preserved."""
    asset=deepcopy(validate_asset(asset));destination=Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(),'MODEL3D_SEMANTIC_EXISTS','Snapshot destination must be new.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.semantic-',dir=destination.parent) as temp:
        stage=Path(temp)/'asset';stage.mkdir();manifest=_write(asset,stage)
        _rename_no_replace(stage,destination)
    return dict(path=str(destination),id=asset.id,version=asset.version,sha256=manifest['sha256'])


def _admit_manifest(manifest):
    keys={'schema_version','kind','id','version','geometry','parts','history','evidence','identity','style','poses','states','derivatives','fixture','sha256'}
    require(isinstance(manifest,dict) and set(manifest)==keys and type(manifest['schema_version']) is int
            and manifest['schema_version']==1 and manifest['kind']=='model3d-semantic-asset',
            'MODEL3D_SEMANTIC_SCHEMA','Unsupported semantic snapshot.')
    require(manifest['sha256']==object_hash({k:v for k,v in manifest.items() if k!='sha256'}),
            'MODEL3D_SEMANTIC_HASH','Semantic metadata hash differs.')
    validate_contract(manifest['geometry'],'GeometryAsset')
    require(isinstance(manifest['parts'],dict) and isinstance(manifest['history'],list)
            and isinstance(manifest['poses'],dict) and isinstance(manifest['states'],dict)
            and isinstance(manifest['derivatives'],dict),'MODEL3D_SEMANTIC_SCHEMA','Invalid nested semantic metadata containers.')
    records=manifest['evidence'];require(isinstance(records,dict) and len(records)<=4096,'MODEL3D_SEMANTIC_SCHEMA','Invalid evidence inventory.')
    total=0
    for key,record in records.items():
        require(isinstance(key,str) and len(key)==64 and all(c in '0123456789abcdef' for c in key)
                and isinstance(record,dict) and set(record)=={'path','bytes','sha256'} and type(record['bytes']) is int
                and 0<record['bytes']<=32*1024**2 and record['sha256']==key,
                'MODEL3D_SEMANTIC_SCHEMA','Invalid source evidence record.')
        total+=record['bytes'];require(total<=256*1024**2,'MODEL3D_SEMANTIC_LIMIT','Evidence bytes exceed their limit.')
    names=[manifest['geometry']['data']['path']]+[r['path'] for r in records.values()]
    aliases=[]
    for name in names:
        portable_name(name);alias=unicodedata.normalize('NFC',name).casefold()
        require(alias not in aliases and all(not alias.startswith(other+'/') and not other.startswith(alias+'/') for other in aliases),
                'MODEL3D_SEMANTIC_PATH','Declared payload paths have aliases or file/directory conflicts.')
        aliases.append(alias)
    return manifest


def _bundle_budget(path,evidence_bytes):
    # Native bundles are ordinary ZIPs written by rig_models. Bound central
    # directory allocation and the tighter semantic copy profile before NumPy
    # reads any member. ZIP64/split bundles are outside this bounded profile.
    size=path.stat().st_size
    with path.open('rb') as stream:
        require(size>=22,'MODEL3D_SEMANTIC_GEOMETRY','Native bundle is truncated.')
        stream.seek(max(0,size-65557));tail=stream.read(min(size,65557));end=tail.rfind(b'PK\x05\x06')
        require(end>=0 and len(tail)-end>=22,'MODEL3D_SEMANTIC_GEOMETRY','Native bundle has no bounded ZIP directory.')
        _,disk,cd_disk,local_count,count,central_bytes,offset,comment=struct.unpack_from('<4s4H2LH',tail,end)
        require(end+22+comment==len(tail) and disk==cd_disk==0 and local_count==count
                and 0<count<=DEFAULT_LIMITS['archive_files'] and central_bytes<=8*1024**2
                and offset+central_bytes<=size,'MODEL3D_SEMANTIC_LIMIT','Native bundle directory exceeds the semantic profile.')
    try:
        with zipfile.ZipFile(path) as bundle:
            require((sum(info.file_size for info in bundle.infolist())+evidence_bytes)*12<=DEFAULT_LIMITS['memory_bytes'],
                    'MODEL3D_SEMANTIC_LIMIT','Decoded native bundle exceeds the semantic edit working set.')
    except zipfile.BadZipFile as exc:
        raise RigError('MODEL3D_SEMANTIC_GEOMETRY','Invalid native geometry bundle.') from exc


def load_asset(root):
    """Parse only a bounded, no-follow snapshot of the admitted source closure."""
    root=Path(root).absolute();require(not root.is_symlink(),'MODEL3D_SEMANTIC_PATH','Snapshot root cannot be a symbolic link.')
    root=root.resolve();info=root.stat();root_identity=(info.st_dev,info.st_ino)
    with open_payload(root,'semantic.json',root_identity=root_identity) as (stream,info):
        require(0<info.st_size<=32*1024**2,'MODEL3D_SEMANTIC_LIMIT','Semantic JSON exceeds its bounded size.')
        raw=stream.read(32*1024**2+1)
        require(len(raw)==info.st_size,'MODEL3D_SEMANTIC_HASH','Semantic metadata changed during capture.')
    manifest=_admit_manifest(decode_json(raw));evidence={};total=len(raw)
    with tempfile.TemporaryDirectory(prefix='semantic-read-') as temporary:
        stage=Path(temporary)
        def capture(name,digest,maximum,expected_size=None):
            nonlocal total
            destination=safe_path(stage,name);destination.parent.mkdir(parents=True,exist_ok=True)
            with open_payload(root,name,root_identity=root_identity) as (stream,info):
                require(0<info.st_size<=maximum and (expected_size is None or expected_size==info.st_size),
                        'MODEL3D_SEMANTIC_LIMIT','Captured payload exceeds its byte budget.')
                total+=info.st_size
                require(total<=DEFAULT_LIMITS['memory_bytes']//3,'MODEL3D_SEMANTIC_LIMIT','Snapshot closure exceeds its working budget.')
                hasher=hashlib.sha256();count=0
                with destination.open('xb') as out:
                    for chunk in iter(lambda:stream.read(1024**2),b''):
                        count+=len(chunk);require(count<=info.st_size,'MODEL3D_SEMANTIC_HASH','Payload grew during capture.')
                        hasher.update(chunk);out.write(chunk)
                require(count==info.st_size and hasher.hexdigest()==digest,'MODEL3D_SEMANTIC_HASH','Captured payload differs from its admitted hash.')
            return destination
        descriptor=manifest['geometry'];capture(descriptor['data']['path'],descriptor['data']['sha256'],DEFAULT_LIMITS['source_bytes'])
        for key,record in manifest['evidence'].items():
            path=capture(record['path'],key,32*1024**2,record['bytes']);evidence[key]=path.read_bytes()
        _bundle_budget(stage/descriptor['data']['path'],sum(map(len,evidence.values())))
        try:geometry=load_geometry(stage,descriptor)
        except (KeyError,TypeError,AttributeError,IndexError,zipfile.BadZipFile) as exc:
            raise RigError('MODEL3D_SEMANTIC_GEOMETRY','Malformed native geometry payload.') from exc
    result=SemanticAsset(geometry,manifest['parts'],id=manifest['id'],version=manifest['version'],history=manifest['history'],
                         evidence=evidence,identity=manifest['identity'],style=manifest['style'],poses=manifest['poses'],
                         states=manifest['states'],derivatives=manifest['derivatives'],fixture=manifest['fixture'])
    validate_asset(result)
    if result.identity is not None:
        from model3d_semantic_evidence import validate_identity
        report=validate_identity(result)
        require(not result.identity['strict'] or report['ok'],'MODEL3D_IDENTITY','Restored identity constraints no longer hold.')
    return result


def export_asset(asset,destination,*,format='glb'):
    """Export actual part hierarchy plus immutable native semantic source."""
    from rig_gltf import GltfBuilder,_export_geometry
    from rig_import import load_character_model
    from model3d_topology import _face_vectors
    require(format in ('glb','gltf'),'MODEL3D_SEMANTIC_FORMAT','Semantic hierarchy export supports GLB/glTF only.')
    asset=deepcopy(validate_asset(asset));destination=Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(),'MODEL3D_SEMANTIC_EXISTS','Export destination must be new.')
    expected=evaluated_geometry(asset);expected_by=expected.by_id();active=set()
    for key,part in asset.parts.items():
        parent=key;visible=True
        while parent is not None:
            visible=visible and asset.parts[parent]['visible'];parent=asset.parts[parent]['parent']
        if visible:active.add(key)
    local=deepcopy(asset.geometry);local.meshes=[m for m in local.meshes if m.id in expected_by]
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.semantic-export-',dir=destination.parent) as temp:
        stage=Path(temp)/'export';stage.mkdir();_write(asset,stage/'native')
        builder=GltfBuilder();mesh_nodes=_export_geometry(builder,local);nodes={}
        for key in asset.parts:
            if key not in active:continue
            part=asset.parts[key];nodes[key]=len(builder.json['nodes'])
            builder.json['nodes'].append(dict(name='part:'+key,matrix=np.asarray(part['matrix']).T.reshape(-1).tolist(),
                children=[mesh_nodes[m] for m in part['meshes']],extras=dict(hatchSemantic=dict(id=key,semantic=part['semantic'],
                    side=part['side'],pivot=part['pivot'],attachments=part['attachments'],review='unreviewed'))))
        roots=[]
        for key,index in nodes.items():
            parent=asset.parts[key]['parent']
            if parent is None:roots.append(index)
            else:builder.json['nodes'][nodes[parent]]['children'].append(index)
        builder.json['scenes']=[dict(nodes=roots)]
        builder.json['asset']['extras']=dict(hatchSemantic=dict(id=asset.id,version=asset.version,
            source='native/semantic.json',review='visually-unreviewed',fixture=asset.fixture,
            rig='rigid-part-assembly; no skin or animation claim'),
            hatchSource=dict(fixture=asset.fixture is True,classification='unknown' if asset.fixture is None else 'fixture' if asset.fixture else 'supplied-nonfixture'))
        target=stage/('model.'+format);builder.write(target)
        imported=load_character_model(target).geometry;actual=imported.by_id()
        require(set(actual)==set(expected_by),'MODEL3D_SEMANTIC_ROUNDTRIP','Export changed native component inventory.')
        for key,source in expected_by.items():
            decoded=actual[key]
            require(np.array_equal(source.triangles,decoded.triangles) and source.positions.shape==decoded.positions.shape,
                    'MODEL3D_SEMANTIC_ROUNDTRIP','Export changed component topology.')
            error=float(np.max(np.abs(source.positions-decoded.positions)))
            scale=max(float(np.max(np.abs(source.positions))),1e-12)
            require(error<=scale*2e-6,'MODEL3D_SEMANTIC_ROUNDTRIP','Export position error exceeds float32 tolerance.')
            before,_=_face_vectors(source);after,_=_face_vectors(decoded)
            require(np.all(np.linalg.norm(after,axis=1)>0) and np.all(np.einsum('ij,ij->i',before,after)>0),
                    'MODEL3D_SEMANTIC_ROUNDTRIP','Float32 export collapsed or reversed a supported face.')
            require(source.material==decoded.material,'MODEL3D_SEMANTIC_ROUNDTRIP','Export changed material assignment.')
            for name,values in source.attributes.items():
                require(name in decoded.attributes and np.allclose(values,decoded.attributes[name],rtol=2e-5,atol=2e-6),
                        'MODEL3D_SEMANTIC_ROUNDTRIP','Export changed native vertex attributes.')
        require([i.data for i in expected.images]==[i.data for i in imported.images],
                'MODEL3D_SEMANTIC_ROUNDTRIP','Export changed original image bytes.')
        files={p.relative_to(stage).as_posix():sha256(p) for p in stage.rglob('*') if p.is_file()}
        receipt=dict(schema_version=1,id=asset.id,version=asset.version,format=format,files=files,
                     parts=sorted(active),hidden_parts=sorted(set(asset.parts)-active),
                     vertices=expected.vertex_count,triangles=expected.triangle_count,independent_reimport=True,
                     review='visually-unreviewed',limitations=['Rigid parts are not a skinned rig or functioning animation.'])
        atomic_json(stage/'export.json',receipt);_rename_no_replace(stage,destination)
    return receipt
