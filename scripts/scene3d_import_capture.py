"""Resolve one registered native import inside its complete private capture.

Consumers must keep the context alive while passing ``native_root`` or ``rig``
to legacy loaders. No path into the caller's mutable source tree is returned.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path

from rig_errors import decode_json, finite_json, object_hash
from scene3d_contracts import load_project, portable_path, require, validate_document


@contextmanager
def resolve_captured_import(bundle, asset_reference, source_root, *, verified_inventory=None):
    """Yield exact ImportedScene, loaded RigProject and private native directory.

    ``bundle`` is the full owner-verified project from ``load_project``. This
    deliberately captures its complete inventory before invoking a native loader;
    static imports also retain a native rig manifest behind their evidence refs.
    """
    from scene3d_capture import capture_verified_files
    from scene3d_import import ImportedScene
    from rig_models import load_rig
    import numpy as np
    asset_reference=deepcopy(asset_reference)
    require(isinstance(bundle, dict) and {'project','documents','inventory'} <= set(bundle),
            'Captured import requires a complete verified project bundle.')
    require(isinstance(asset_reference, dict) and set(asset_reference)=={'kind','id','version','path','sha256'}
            and asset_reference['kind']=='scene3d-asset','Captured import requires an exact native asset reference.')
    portable_path(asset_reference['path'])
    inventory=bundle['inventory'] if verified_inventory is None else verified_inventory
    require(inventory==bundle['inventory'],'Captured import must retain the complete verified project inventory.')
    with capture_verified_files(source_root,inventory) as captured:
        admitted=load_project(captured)
        require(admitted['inventory']==bundle['inventory'],
                'Captured project inventory differs from its complete registered closure.')
        asset=admitted['documents'].get(asset_reference['path'])
        require(asset is not None,'Captured import asset is absent from the registered project.')
        asset=validate_document(asset,'asset')
        raw=(captured/asset_reference['path']).read_bytes()
        require(asset['id']==asset_reference['id'] and asset['version']==asset_reference['version'] and
                hashlib.sha256(raw).hexdigest()==asset_reference['sha256'],
                'Captured import asset differs from its exact reference.','SCENE3D_SOURCE_CHANGED')
        require(bundle['documents'].get(asset_reference['path'])==asset,
                'Captured import differs from its supplied verified asset.')
        def original(suffix):
            entries=[entry for entry in asset['dependencies'] if entry['path'].endswith(suffix)]
            require(len(entries)==1,'Imported asset must declare exactly one '+suffix+' record.')
            entry=entries[0];path=captured/portable_path(entry['path']);payload=path.read_bytes()
            require(hashlib.sha256(payload).hexdigest()==entry['sha256'],
                    'Captured imported authoring source differs from its pin.','SCENE3D_SOURCE_CHANGED')
            return entry,path,payload
        _,_,report_raw=original('/import-report.json')
        _,_,hierarchy_raw=original('/hierarchy.json')
        report,hierarchy=decode_json(report_raw),decode_json(hierarchy_raw)
        require(isinstance(report,dict) and report.get('schema_version')==1 and
                type(report.get('fixture')) is bool and report['fixture']==asset['provenance']['fixture'],
                'Imported provenance report is malformed or disagrees with its asset.')
        require(isinstance(hierarchy,dict) and isinstance(hierarchy.get('nodes'),list) and
                0<len(hierarchy['nodes'])<=10000,'Imported hierarchy report is malformed.')
        finite_json(hierarchy)
        seen=set()
        for node in hierarchy['nodes']:
            require(isinstance(node,dict) and type(node.get('source_node')) is int and
                    node['source_node']>=0 and node['source_node'] not in seen,
                    'Imported hierarchy has a missing or duplicate source node.')
            seen.add(node['source_node'])
            try:matrix=np.asarray(node.get('world_matrix'),dtype=float)
            except (TypeError,ValueError,OverflowError):require(False,'Imported hierarchy has an invalid world matrix.')
            require(matrix.shape==(4,4) and np.isfinite(matrix).all(),
                    'Imported hierarchy needs finite four-by-four source matrices.')
        manifest_ref,manifest_path,manifest_raw=original('/native/rig.json')
        require(manifest_path.name=='rig.json','Imported native manifest must use its canonical filename.')
        native_root=manifest_path.parent
        rig=load_rig(native_root)
        require(object_hash(decode_json(manifest_raw))==object_hash(rig.manifest),
                'Native loader did not evaluate the captured exact rig manifest.','SCENE3D_SOURCE_CHANGED')
        if asset['skeleton'] is not None:
            pin=asset['skeleton']
            require(pin['kind']=='hatch-3d-rig' and pin['id']==rig.manifest['id'] and
                    pin['version']==rig.manifest['version'] and pin['path']==manifest_ref['path'] and
                    pin['sha256']==manifest_ref['sha256'],'Imported skeleton pin differs from its captured native rig.')
        require((asset['skeleton'] is not None)==(rig.manifest['skeleton'] is not None),
                'Imported asset skeleton capability differs from its native rig.')
        require(report.get('source_sha256')==rig.manifest['source']['sha256'],
                'Imported provenance source differs from the actual native rig.')
        require(asset['source']['sha256']==rig.manifest['source']['sha256'],
                'Imported original source pin differs from the actual native source.')
        require(rig.manifest['source']['fixture']==asset['provenance']['fixture']==report['fixture'],
                'Imported fixture provenance differs from the actual native source.')
        aliases=report.get('native_clip_map')
        require(isinstance(aliases,dict) and len(aliases)<=1024,
                'Imported native clip aliases must be a bounded map.')
        for alias,target in aliases.items():
            require(isinstance(alias,str) and 0<len(alias)<=4096 and
                    isinstance(target,str) and 0<len(target)<=128 and target in rig.clips,
                    'Imported native clip alias must name an existing bounded native clip.')
        require(all(set(meshes)<=set(rig.geometry.by_id()) for meshes in asset['components'].values()),
                'Imported component names an absent native mesh.')
        imported=ImportedScene({},deepcopy(asset_reference),asset,[],hierarchy['nodes'],report)
        yield dict(imported=imported,rig=rig,native_root=native_root,root=captured,
                   bundle=admitted,asset_reference=deepcopy(asset_reference))
