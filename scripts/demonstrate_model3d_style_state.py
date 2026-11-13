"""Execute supported registry appearance and an existing catalog rigid state."""
import argparse
import hashlib
from pathlib import Path
import numpy as np

from model3d_semantic import evaluated_geometry,part_signatures
from model3d_semantic_evidence import map_states
from model3d_semantic_io import export_asset,load_asset,save_asset
from model3d_semantic_style import apply_catalog_state,apply_style_materials
from rig_errors import atomic_json,object_hash,require,sha256


def demonstrate(source,destination):
    root=Path(destination).absolute()
    require(not root.exists() and not root.is_symlink(),'MODEL3D_STYLE_DEMO_EXISTS','Choose a new proof destination.')
    asset=load_asset(source);before=object_hash(part_signatures(asset));root.mkdir(parents=True)
    require(asset.fixture is True and {'body','left-arm','right-arm'}<=set(asset.parts) and 'wave' in asset.poses,
            'MODEL3D_STYLE_DEMO_SOURCE','Use the retained semantic robot with its actual wave pose.')
    styled=apply_style_materials(asset,'clay',parts=['body','left-arm','right-arm'])
    style_export=export_asset(styled,root/'style-export');save_asset(styled,root/'styled-native')
    mapped=map_states(styled,{'waving':'wave'});posed=apply_catalog_state(mapped,'waving')
    state_export=export_asset(posed,root/'state-export');save_asset(posed,root/'state-native')
    original_geometry=evaluated_geometry(asset).by_id();style_geometry=evaluated_geometry(styled).by_id()
    require(all(np.array_equal(original_geometry[key].positions,style_geometry[key].positions) for key in original_geometry),
            'MODEL3D_STYLE_DEMO_GEOMETRY','Style execution changed geometry instead of selected PBR appearance.')
    old_slot=asset.geometry.by_id()['body'].material;new_slot=styled.geometry.by_id()['body'].material
    old=asset.geometry.materials[old_slot]['pbrMetallicRoughness'];new=styled.geometry.materials[new_slot]['pbrMetallicRoughness']
    require((old.get('metallicFactor',1),old.get('roughnessFactor',1))!=(new['metallicFactor'],new['roughnessFactor']),
            'MODEL3D_STYLE_DEMO_APPEARANCE','Selected source already has identical factors; choose a distinct baseline for this proof.')
    displacement=float(np.linalg.norm(evaluated_geometry(posed).by_id()['right-arm'].positions-style_geometry['right-arm'].positions,axis=1).max())
    require(displacement>.1 and object_hash(part_signatures(asset))==before,
            'MODEL3D_STYLE_DEMO_STATE','Catalog state failed to move geometry or altered its source.')
    report=dict(schema_version=1,fixture=True,source_snapshot=str(Path(source).absolute()),source_unchanged=True,
        registry_style='clay',body_before=old,body_after=new,unchanged_style_geometry=True,
        catalog_state='waving',represented_pose='wave',maximum_right_arm_displacement_m=displacement,
        style_export=style_export,state_export=state_export,
        unchanged_image_bytes=[],
        review='visually-unreviewed',unexecuted=['Rendered style/identity appearance comparison','Human artistic approval'],
        files={p.relative_to(root).as_posix():sha256(p) for p in root.rglob('*') if p.is_file()})
    # Actual native images remain byte-identical across source/style revisions.
    require([i.data for i in asset.geometry.images]==[i.data for i in styled.geometry.images],
            'MODEL3D_STYLE_DEMO_IMAGES','Style changed original image bytes.')
    report['unchanged_image_bytes']=[hashlib.sha256(i.data).hexdigest() for i in asset.geometry.images]
    atomic_json(root/'acceptance.json',report);return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',required=True);parser.add_argument('--destination',required=True)
    args=parser.parse_args();demonstrate(args.source,args.destination);print(Path(args.destination)/'acceptance.json')
