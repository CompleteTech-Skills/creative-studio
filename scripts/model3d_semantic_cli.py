"""Additive native semantic Model3D import/edit/inspect/export workflow."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

from model3d_semantic import (_finish,apply_pose,change_material,combine_parts,create_asset,define_pose,duplicate_part,group_parts,
    evaluated_geometry,label_part,reparent_part,replace_part,select,set_visibility,split_part,transform_part)
from model3d_semantic_evidence import bind_identity,bind_style,identity_contract,map_decomposition,map_states,validate_identity
from model3d_semantic_io import export_asset,load_asset,save_asset
from rig_errors import RigError,read_json,require,safe_path


def import_asset(path,*,asset_id='asset'):
    from model3d_mesh_import import import_mesh
    with import_mesh(path) as source:
        require(source.model.skeleton is None and not source.model.weights and not source.model.clips,
                'MODEL3D_SEMANTIC_RIG','Keep imported skins/animation in their native rig owner; semantic mesh editing does not silently drop them.')
        from rig_contracts import DEFAULT_LIMITS
        require(sum(record['bytes'] for record in source.source_records)*12<=DEFAULT_LIMITS['memory_bytes'],
                'MODEL3D_SEMANTIC_LIMIT','Retained original source working set exceeds its memory budget.')
        asset=create_asset(source.model.geometry,asset_id=asset_id,fixture=True if source.model.report.get('fixture') else None)
        records=[]
        for record in source.source_records:
            data=safe_path(source.root,record['path'],exists=True).read_bytes()
            require(hashlib.sha256(data).hexdigest()==record['sha256'],'MODEL3D_SEMANTIC_SOURCE','Captured source bytes changed.')
            asset.evidence[record['sha256']]=data;records.append(record)
        for part in asset.parts.values():part['evidence'].append(dict(kind='exact-native-source-snapshot',files=records,
            provenance=source.provenance,hierarchy='Native world-space mesh import; original authored source hierarchy retained in source bytes.'))
        return asset


def edit(asset,operation,spec):
    require(isinstance(spec,dict),'MODEL3D_SEMANTIC_EDIT','Edit specification must be a JSON object.')
    require(isinstance(operation,str) and 0<len(operation)<=128,'MODEL3D_SEMANTIC_EDIT','Choose a documented edit operation.')
    handlers={'transform':transform_part,'reparent':reparent_part,'visibility':set_visibility,'label':label_part,
              'duplicate':duplicate_part,'split':split_part,'combine':combine_parts,'material':change_material,
              'pose-define':define_pose,'pose-apply':apply_pose,'states':map_states,'style':bind_style,
              'decomposition':map_decomposition,'group':group_parts}
    if operation=='identity':
        require('source_identity' in spec and isinstance(spec['source_identity'],(str,Path))
                and set(spec)<=set(('source_identity','required_parts','forbidden_parts','ratios','colors','strict')),
                'MODEL3D_IDENTITY','Identity binding requires original source metadata and explicit supported invariants.')
        contract,data=identity_contract(**spec);return bind_identity(asset,contract,data)
    if operation=='replace':
        from copy import deepcopy
        require(set(spec)=={'part_id','source','material_slot'} and isinstance(spec['source'],(str,Path))
                and type(spec['material_slot']) is int
                and 0<=spec['material_slot']<len(asset.geometry.materials),
                'MODEL3D_SEMANTIC_EDIT','Replacement requires part_id, source and explicit existing material_slot.')
        part=select(asset,spec['part_id']);replacement=import_asset(spec['source'])
        meshes=deepcopy(replacement.geometry.meshes)
        for mesh in meshes:mesh.material=spec['material_slot']
        # The explicit material policy keeps target resources. Original source
        # appearance remains preserved as evidence, never silently relabeled.
        result=deepcopy(asset);removed=set(part['meshes'])
        result.geometry.meshes=[m for m in result.geometry.meshes if m.id not in removed]+meshes
        result.parts[spec['part_id']]['meshes']=[m.id for m in meshes]
        result.evidence.update(replacement.evidence)
        result.fixture=True if asset.fixture is True or replacement.fixture is True else False if asset.fixture is False and replacement.fixture is False else None
        result.parts[spec['part_id']]['evidence'].append(dict(kind='explicit-source-geometry-replacement',
            source_evidence=sorted(replacement.evidence),material_policy='adopt-explicit-target-slot',material_slot=spec['material_slot'],
            original_fixture=asset.fixture,replacement_fixture=replacement.fixture))
        return _finish(asset,result,'replace-source-geometry:'+spec['part_id'])
    require(operation in handlers,'MODEL3D_SEMANTIC_EDIT','Unsupported semantic operation.')
    try:return handlers[operation](asset,**spec)
    except TypeError as exc:require(False,'MODEL3D_SEMANTIC_EDIT','Invalid edit arguments: '+str(exc)[:1024])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    imported=commands.add_parser('import');imported.add_argument('--source',required=True);imported.add_argument('--id',default='asset');imported.add_argument('--destination',required=True)
    inspect=commands.add_parser('inspect');inspect.add_argument('--source',required=True)
    edited=commands.add_parser('edit');edited.add_argument('--source',required=True);edited.add_argument('--operation',required=True);edited.add_argument('--spec',required=True);edited.add_argument('--destination',required=True)
    exported=commands.add_parser('export');exported.add_argument('--source',required=True);exported.add_argument('--destination',required=True);exported.add_argument('--format',choices=['glb','gltf'],default='glb')
    args=parser.parse_args(argv)
    try:
        if args.command=='import':result=save_asset(import_asset(args.source,asset_id=args.id),args.destination)
        else:
            asset=load_asset(args.source)
            if args.command=='inspect':
                geometry=evaluated_geometry(asset,include_hidden=True)
                result=dict(id=asset.id,version=asset.version,fixture=asset.fixture,parts=asset.parts,
                            vertices=geometry.vertex_count,triangles=geometry.triangle_count,
                            identity=validate_identity(asset),states=asset.states,derivatives=asset.derivatives)
            elif args.command=='edit':result=save_asset(edit(asset,args.operation,read_json(args.spec)),args.destination)
            else:result=export_asset(asset,args.destination,format=args.format)
        print(json.dumps(result,indent=2,allow_nan=False));return 0
    except (RigError,OSError,ValueError) as exc:
        print(json.dumps(dict(ok=False,error=exc.report if isinstance(exc,RigError) else dict(code='MODEL3D_SEMANTIC_IO',message=str(exc)))),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
