"""Retain genuine bounded procedural inspection evidence, never art approval."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).resolve().parent)]
from continuity_inspection import inspect_asset,compare_assets,classify_rigs,NativeInspectionOwner
from continuity_contracts import canonical_bytes,digest
from test_rig_continuity_inspection import gltf,pointer,native_request
from rig_test_helpers import fixture
from rig_errors import RigError
from PIL import Image


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path);args=parser.parse_args()
    root=args.output;root.mkdir(parents=True,exist_ok=False)
    base,_=gltf(root,'baseline.glb');changed,_=gltf(root,'changed-geometry.glb',width=1.5)
    texture,_=gltf(root,'changed-texture.glb',color=(200,20,10))
    rig=fixture(root,name='native-character',animated=False);native=native_request(root,rig)
    protected={r['path']:r['sha256'] for r in [native['source'],*native['dependencies']]}
    for path in (base,changed,texture):protected[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    _,capacity=gltf(root,'capacity-unused.glb')
    (root/'capacity-unused.glb').unlink()
    image=io.BytesIO();Image.new('RGBA',(512,512),(20,40,60,255)).save(image,format='PNG')
    capacity.json['images']=[dict(mimeType='image/png',bufferView=capacity.view(image.getvalue()))]
    one=root/'capacity-one.glb';capacity.write(one)
    capacity.json['images'].append(dict(mimeType='image/png',bufferView=capacity.view(image.getvalue())))
    two=root/'capacity-two.glb';capacity.write(two)
    for path in (one,two):protected[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    def write(name,value):(root/name).write_bytes(canonical_bytes(value)+b'\n')
    sources=[dict(source=pointer(root,path)) for path in (base,changed,texture)]
    measured=inspect_asset(root,**sources[0],landmarks={'bottom':dict(mesh_id='mesh-1-0',vertex_index=0),
        'top':dict(mesh_id='mesh-1-0',vertex_index=2)},distances={'height':['bottom','top']})
    inspection=inspect_asset(root,**native)
    geometry=compare_assets(root,sources[0],sources[1]);materials=compare_assets(root,sources[0],sources[2])
    classification=classify_rigs(root,native,native)
    image_ok=inspect_asset(root,pointer(root,one),limits=dict(memory_bytes=8*1024**2))
    try:
        inspect_asset(root,pointer(root,two),limits=dict(memory_bytes=8*1024**2))
    except RigError as exc:
        image_rejection=exc.report
    else:
        raise AssertionError('combined image capacity was incorrectly admitted')
    assert image_rejection['code']=='RIG_LIMIT_MEMORY'
    assert image_ok['inspection']['native_image_admission']['images'][0]['width']==512
    assert not geometry['geometry_identity'] and not geometry['visual_approval']
    assert materials['geometry_identity'] and not materials['material_identity'] and materials['needs_visual_review']
    assert measured['inspection']['measurements']['distances']['height']['distance_m']==2
    assert len(inspection['inspection']['skeleton']['bones'])==3
    assert inspection['inspection']['skins']['strip']['weights']['shape']==[4,4]
    assert classification['status']=='unverified' and classification['issues'][0]['code']=='APPROVAL_REQUIRED'
    for name,value in [('measured.json',measured),('native-inspection.json',inspection),('geometry-comparison.json',geometry),
                       ('texture-comparison.json',materials),('unreviewed-classification.json',classification)]:write(name,value)
    write('capacity-one-inspection.json',image_ok);write('capacity-two-rejection.json',image_rejection)
    (root/'evidence').mkdir();owner=NativeInspectionOwner(root,sources[0]['source'],'evidence')
    write('capability-snapshot.json',owner.snapshot())
    assert protected=={path:hashlib.sha256((root/path).read_bytes()).hexdigest() for path in protected}
    source_root=Path(__file__).resolve().parents[1]
    report=dict(status='passed-synthetic-native-inspection',fixture=True,production_artwork_approved=False,
        software=measured['inspection']['software'],source_files_unchanged=protected,
        hierarchy_nodes=len(measured['inspection']['hierarchy']),measured_height_m=2,
        native_vertices=inspection['inspection']['analysis']['vertices'],native_bones=3,
        real_texture_bytes=measured['inspection']['textures'][0]['bytes'],
        geometry_difference_detected=True,texture_difference_detected=True,
        combined_image_capacity=dict(memory_budget_bytes=8*1024**2,single_512_image_accepted=True,
            two_512_images_rejected=image_rejection),
        normalized_geometry_sha256=measured['inspection']['geometry_sha256'],
        receipt_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.json')},
        implementation_sha256={name:hashlib.sha256((source_root/name).read_bytes()).hexdigest() for name in
            ('scripts/continuity_inspection.py','tests/test_rig_continuity_inspection.py','tests/continuity_inspection_probe.py')},
        limitations=['Engineering triangle/strip fixtures, not production artwork.','No Blender-native provider or visual review executed.',
                     'Unreviewed equal rigs intentionally remain unverified.','Retarget approved-decision classification is a separately labeled simulated fixture gate in unit tests.'])
    write('report.json',report);print(json.dumps(report,sort_keys=True))


if __name__=='__main__':main()
