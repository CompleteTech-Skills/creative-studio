"""Real semantic hierarchy, PBR/UV/texture, pose and exact identity workflow."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np

from model3d_materials import material,procedural_texture
from model3d_primitives import primitive
from model3d_semantic import (apply_pose,create_asset,define_pose,evaluated_geometry,label_part,
                              group_parts,part_signatures,reparent_part,transform_part)
from model3d_semantic_evidence import bind_identity,bind_style,identity_contract,map_states,validate_identity
from model3d_semantic_io import export_asset,load_asset,save_asset
from model3d_semantic_surfaces import generate_asset_uv,texture_asset
from rig_errors import atomic_json,object_hash,require,sha256
from rig_models import Geometry


def _translation(x=0,y=0,z=0):
    result=np.eye(4);result[:3,3]=[x,y,z];return result


def demonstrate(destination):
    root=Path(destination).absolute()
    require(not root.exists() and not root.is_symlink(),'MODEL3D_DEMO_EXISTS','Choose a new demo directory.')
    root.mkdir(parents=True)
    body_color=[.08,.16,.32,1.];head_srgb=np.array([204,77,51])/255
    head_color=[float(v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4) for v in head_srgb]+[1.]
    meshes=[primitive('box',dict(width=1,height=1.8,depth=.6),name='body').mesh,
            primitive('box',dict(width=.5,height=.5,depth=.5),name='head').mesh,
            primitive('box',dict(width=.25,height=1.2,depth=.25),name='left-arm').mesh,
            primitive('box',dict(width=.25,height=1.2,depth=.25),name='right-arm').mesh]
    meshes[1].material=1
    asset=create_asset(Geometry(meshes,[material('plastic',color=body_color,name='body-paint'),
                                      material('ceramic',color=head_color,name='head-ceramic')]),asset_id='semantic-robot',fixture=True)
    for key,semantic,side,position in [('body','torso','center',(0,0,0)),('head','head','center',(0,1.4,0)),
            ('left-arm','arm','left',(-.8,.3,0)),('right-arm','arm','right',(.8,.3,0))]:
        asset=label_part(asset,key,semantic=semantic,side=side,pivot=[0.,.6,0.] if 'arm' in key else [0.,0.,0.],
                         attachments=[dict(id='tool-mount',point=[0.,-.6,0.],target=None)] if 'arm' in key else [])
        if key!='body':asset=reparent_part(asset,key,'body',preserve_world=False)
        asset=transform_part(asset,key,_translation(*position))
    asset=group_parts(asset,'character-root',['body']);asset=label_part(asset,'character-root',semantic='character-root',side='center')
    identity_path=root/'inputs/identity.json'
    atomic_json(identity_path,dict(schema_version=1,palette=dict(head='#cc4d33'),
                                  proportions=dict(head_width_to_body=.5),forbid_mirror=True,
                                  materials=['Painted body and ceramic head; engineering fixture.']))
    contract,data=identity_contract(identity_path,required_parts=['body','head','left-arm','right-arm'],forbidden_parts=['wings'],
        ratios=[dict(numerator='head',denominator='body',axis=0,minimum=.3,maximum=.5)],
        colors=[dict(part='head',rgba=head_color,maximum_error=1e-8)])
    asset=bind_identity(asset,contract,data)
    asset=bind_style(asset,'3d-toy',adaptations=dict(geometry='Explicit box assembly with authored mechanical arms.',
        material='Portable painted plastic and ceramic PBR factors; no learned texture.',
        texture='Actual seeded checker PNG applied to body; head palette remains explicit.',
        render='Same-model native authoring owner supplies lighting and human review; unexecuted here.'))
    asset=generate_asset_uv(asset,method='box',resolution=(512,512),padding=2)
    asset=texture_asset(asset,0,procedural_texture('checker',width=32,height=32,seed=7,
            colors=((.2,.4,.8,1.),(.6,.7,.9,1.)),name='body-checker'))
    neutral=np.asarray(asset.parts['right-arm']['matrix']);rotation=np.eye(4)
    angle=np.pi/3;rotation[:2,:2]=[[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]]
    wave=neutral@_translation(y=.6)@rotation@_translation(y=-.6)
    asset=define_pose(asset,'neutral',{'right-arm':neutral});asset=define_pose(asset,'wave',{'right-arm':wave})
    asset=map_states(asset,{'idle':'neutral','running':None})
    before=object_hash(part_signatures(asset));save_asset(asset,root/'revisions/base')
    base_export=export_asset(asset,root/'base-export',format='glb')
    posed=apply_pose(asset,'wave');save_asset(posed,root/'revisions/wave')
    posed_export=export_asset(posed,root/'wave-export',format='gltf')
    base_positions=evaluated_geometry(asset).by_id()['right-arm'].positions
    pose_positions=evaluated_geometry(posed).by_id()['right-arm'].positions
    displacement=float(np.linalg.norm(pose_positions-base_positions,axis=1).max())
    require(displacement>.1 and object_hash(part_signatures(asset))==before,
            'MODEL3D_DEMO_POSE','Rigid pose failed to move actual geometry or changed the base asset.')
    restored=load_asset(root/'revisions/base')
    require(part_signatures(restored)==part_signatures(asset),'MODEL3D_DEMO_RELOAD','Restored native semantic revision differs.')
    identity=validate_identity(asset);require(identity['ok'],'MODEL3D_DEMO_IDENTITY','Explicit identity invariants failed.')
    report=dict(schema_version=1,fixture=True,review='visually-unreviewed',provider_calls=0,
                base_export=base_export,posed_export=posed_export,identity=identity,
                right_arm_maximum_displacement_m=displacement,original_identity_sha256=hashlib.sha256(data).hexdigest(),
                same_base_geometry_preserved=True,source_texture_sha256=hashlib.sha256(asset.geometry.images[0].data).hexdigest(),
                files={p.relative_to(root).as_posix():sha256(p) for p in root.rglob('*') if p.is_file()},
                unexecuted=['Blender edit/save/reopen','multi-angle identity renders','browser review','human artistic approval'])
    atomic_json(root/'acceptance.json',report);return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--destination',required=True)
    arguments=parser.parse_args();demonstrate(arguments.destination);print(Path(arguments.destination)/'acceptance.json')
