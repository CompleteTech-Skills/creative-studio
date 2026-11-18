"""Executed supported style aspects and existing catalog rigid-pose states."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json

from model3d_semantic import _finish,apply_pose,validate_asset
from model3d_semantic_evidence import bind_style
from model3d_semantic_io import load_asset,save_asset
from rig_errors import RigError,object_hash,require

# These explicitly supported registry choices affect real portable PBR factors.
# They do not imply automatic geometry, strands, learned maps or arbitrary shaders.
STYLE_RECIPES={
    'clay':dict(family='rough-ceramic-approximation',metallic=0.,roughness=.92),
    '3d-toy':dict(family='painted-plastic-approximation',metallic=0.,roughness=.3),
    'plush':dict(family='matte-cloth-approximation',metallic=0.,roughness=1.),
}


def apply_style_materials(asset,style_id,*,parts=None,notes=''):
    """Resolve the real style registry, then change supported selected PBR slots.

    Existing colors, texture bytes/UVs, alpha, emission and geometry remain exact.
    An unsupported style or shader extension fails rather than recording a fake
    style execution. The resulting metadata describes the precise approximation.
    """
    from style_registry import resolve_style
    require(isinstance(style_id,str) and 0<len(style_id)<=128,'MODEL3D_STYLE_UNSUPPORTED','Style selection must be a bounded registry identifier.')
    validate_asset(asset);snapshot=resolve_style(style_id,notes=notes);resolved=snapshot['resolved_id']
    require(resolved in STYLE_RECIPES,'MODEL3D_STYLE_UNSUPPORTED',
            'Executable PBR adaptations currently support clay, 3d-toy and plush only.')
    if parts is None:parts=[key for key,part in asset.parts.items() if part['meshes']]
    require(isinstance(parts,list) and bool(parts) and all(isinstance(key,str) for key in parts)
            and len(set(parts))==len(parts)
            and all(key in asset.parts and bool(asset.parts[key]['meshes']) for key in parts),
            'MODEL3D_STYLE_PARTS','Choose known mesh-bearing parts explicitly.')
    known=asset.geometry.by_id();mesh_ids={mid for key in parts for mid in asset.parts[key]['meshes']}
    slots=sorted({known[mid].material for mid in mesh_ids})
    require(len(asset.geometry.materials)+len(slots)<=4096,'MODEL3D_STYLE_LIMIT','Style material revisions exceed their slot budget.')
    require(all(not asset.geometry.materials[index].get('extensions') for index in slots),
            'MODEL3D_STYLE_UNSUPPORTED','Material shader extensions require their native authoring owner; core PBR factors cannot certify this adaptation.')
    recipe=deepcopy(STYLE_RECIPES[resolved])
    styled=bind_style(asset,style_id,notes=notes,adaptations=dict(
        geometry='Existing native geometry preserved; no generated topology is implied.',
        material=f"Execute {recipe['family']}: metallic={recipe['metallic']}, roughness={recipe['roughness']} on selected parts.",
        texture='Keep exact existing colors, image resources and UV assignments.',
        render='Portable core PBR factors are exported and reimported; actual lighting/appearance review remains separate.'))
    result=deepcopy(styled);mapping={};changes=[]
    for index in slots:
        material=deepcopy(asset.geometry.materials[index]);material['name']=f'style-{resolved}-{index}'
        pbr=material.setdefault('pbrMetallicRoughness',{})
        before=dict(metallic=pbr.get('metallicFactor',1.),roughness=pbr.get('roughnessFactor',1.))
        pbr.update(metallicFactor=recipe['metallic'],roughnessFactor=recipe['roughness'])
        changes.append(dict(source_slot=index,before=before,after=deepcopy(recipe),
                            source_material_sha256=object_hash(asset.geometry.materials[index])))
        mapping[index]=len(result.geometry.materials);result.geometry.materials.append(material)
    for mesh in result.geometry.meshes:
        if mesh.id in mesh_ids:mesh.material=mapping[mesh.material]
    for key in parts:result.parts[key]['evidence'].append(dict(kind='executed-supported-registry-style',
        registry_id=resolved,registry_snapshot_sha256=snapshot['sha256'],material_changes=deepcopy(changes),
        limitation='Supported portable PBR aspects only; geometry, strands and learned textures are not generated.'))
    return _finish(styled,result,'execute-supported-style:'+resolved)


def apply_catalog_state(asset,state_id):
    """Evaluate the real persisted rigid pose selected by an existing state ID."""
    from state_catalog import load_library
    validate_asset(asset);library=load_library()
    require(isinstance(state_id,str) and state_id in library.states and state_id in asset.states,
            'MODEL3D_STATE_UNAVAILABLE','State must exist in the native catalog and have an explicit asset mapping.')
    binding=asset.states[state_id]
    require(binding['catalog_sha256']==library.sha256,'MODEL3D_STATE_STALE','State mapping belongs to a different catalog snapshot.')
    require(binding['status']=='rigid-pose' and binding['pose'] in asset.poses,
            'MODEL3D_STATE_UNAVAILABLE','This asset has no represented rigid pose for the selected state.')
    result=apply_pose(asset,binding['pose'])
    result.history[-1]['operation']='apply-existing-catalog-state:'+state_id
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    style=commands.add_parser('style');style.add_argument('--source',required=True);style.add_argument('--style',required=True)
    style.add_argument('--part',action='append');style.add_argument('--notes',default='');style.add_argument('--destination',required=True)
    state=commands.add_parser('state');state.add_argument('--source',required=True);state.add_argument('--state',required=True);state.add_argument('--destination',required=True)
    args=parser.parse_args(argv)
    try:
        asset=load_asset(args.source)
        result=apply_style_materials(asset,args.style,parts=args.part,notes=args.notes) if args.command=='style' else apply_catalog_state(asset,args.state)
        print(json.dumps(save_asset(result,args.destination),indent=2,allow_nan=False));return 0
    except (RigError,OSError,ValueError) as exc:
        print(json.dumps(dict(ok=False,error=exc.report if isinstance(exc,RigError) else dict(code='MODEL3D_STYLE_IO',message=str(exc)))),file=__import__('sys').stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
