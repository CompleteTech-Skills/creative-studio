"""Actual saved native sources; fixture decisions never prove artwork approval."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from continuity_inspection import inspect_asset,compare_assets,classify_rigs,NativeInspectionOwner
from continuity_contracts import canonical_bytes,digest
from continuity_test_helpers import sample
from rig_gltf import GltfBuilder,export_gltf
from rig_jobs import save_rig
from rig_models import load_rig
from rig_retarget import build_retarget_profile
from rig_skinning import generate_skin_weights
from rig_test_helpers import fixture


def pointer(root,path,*,kind='mesh',identity='fixture',version='1.0.0'):
    path=Path(path)
    return dict(id=identity,version=version,path=path.relative_to(root).as_posix(),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),kind=kind)


def native_request(root,rig):
    source=pointer(root,rig.root/'rig.json',kind='hatch-3d-rig',identity=rig.manifest['id'],version=rig.manifest['version'])
    deps=[pointer(root,p,kind='native-dependency') for p in sorted(rig.root.rglob('*')) if p.is_file() and p!=rig.root/'rig.json']
    return dict(source=source,dependencies=deps)


def gltf(root,name='source.glb',*,color=(20,40,60),width=1.,external=False):
    b=GltfBuilder(); image=io.BytesIO();Image.new('RGB',(2,2),color).save(image,format='PNG')
    attrs=dict(POSITION=b.accessor([[0,0,0],[width,0,0],[0,2,0]],'VEC3'),
               NORMAL=b.accessor([[0,0,1]]*3,'VEC3'),TANGENT=b.accessor([[1,0,0,1]]*3,'VEC4'),
               TEXCOORD_0=b.accessor([[0,0],[1,0],[0,1]],'VEC2'))
    primitive=dict(attributes=attrs,material=0,targets=[dict(POSITION=b.accessor([[0,0,.1]]*3,'VEC3'))])
    images=[dict(uri='texture.png')] if external else [dict(mimeType='image/png',bufferView=b.view(image.getvalue()))]
    b.json.update(nodes=[dict(name='Actual parent',translation=[3,0,0],children=[1]),dict(name='Actual mesh',mesh=0)],
                  meshes=[dict(name='same descriptive metadata',extras=dict(targetNames=['smile']),primitives=[primitive])],
                  scenes=[dict(nodes=[0])],images=images,
                  materials=[dict(name='same descriptive material',pbrMetallicRoughness=dict(baseColorTexture=dict(index=0)))],
                  textures=[dict(source=0)])
    b.json['asset']['extras']=dict(hatchSource=dict(fixture=True))
    if external:
        (root/'texture.png').write_bytes(image.getvalue())
    b.write(root/name)
    return root/name,b


class ContinuityInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_actual_hierarchy_geometry_uv_normals_material_texture_morph_and_landmarks(self):
        path,_=gltf(self.root)
        source=pointer(self.root,path)
        original=path.read_bytes()
        r=inspect_asset(self.root,source,landmarks={'a':dict(mesh_id='mesh-1-0',vertex_index=0),
                         'head':dict(mesh_id='mesh-1-0',vertex_index=2)},distances={'height':['a','head']})
        d=r['inspection'];self.assertEqual(r['sha256'],digest(d));self.assertEqual(path.read_bytes(),original)
        self.assertEqual(d['hierarchy'][1]['parent'],0)
        self.assertEqual(d['hierarchy'][1]['world_matrix'][0][3],3)
        self.assertEqual(d['analysis']['vertices'],3);self.assertEqual(d['analysis']['triangles'],1)
        self.assertEqual(d['meshes'][0]['attributes']['TEXCOORD_0']['shape'],[3,2])
        self.assertIn('NORMAL',d['meshes'][0]['attributes']);self.assertIn('TANGENT',d['meshes'][0]['attributes'])
        self.assertIn('smile',d['meshes'][0]['morphs']);self.assertEqual(d['textures'][0]['bytes'],len(self.image_bytes(path)))
        self.assertIn('sRGB',d['texture_color_spaces'][0]['color_space'])
        self.assertEqual(d['materials'][0]['inputs']['pbrMetallicRoughness']['baseColorTexture']['index'],0)
        self.assertEqual(d['measurements']['distances']['height']['distance_m'],2.)
        self.assertEqual(d['measurements']['distances']['height']['relative_to_height'],1.)
        self.assertFalse(d['visual_approval']);self.assertEqual(d['semantic_equivalence'],'unverified')

    def image_bytes(self,path):
        from rig_import import load_character_model
        return load_character_model(path).geometry.images[0].data

    def test_changed_geometry_and_texture_with_identical_names_require_real_review(self):
        a,_=gltf(self.root,'a.glb');b,_=gltf(self.root,'b.glb',width=1.5)
        r=compare_assets(self.root,dict(source=pointer(self.root,a)),dict(source=pointer(self.root,b)))
        self.assertFalse(r['byte_identity']);self.assertFalse(r['geometry_identity']);self.assertTrue(r['needs_visual_review'])
        c,_=gltf(self.root,'c.glb',color=(200,30,10))
        r=compare_assets(self.root,dict(source=pointer(self.root,a)),dict(source=pointer(self.root,c)))
        self.assertTrue(r['geometry_identity']);self.assertFalse(r['material_identity']);self.assertFalse(r['visual_approval'])

    def test_container_and_vertex_serialization_are_separate_from_surface_identity(self):
        a,b=gltf(self.root,'a.glb');b.write(self.root/'b.gltf')
        r=compare_assets(self.root,dict(source=pointer(self.root,a)),dict(source=pointer(self.root,self.root/'b.gltf',kind='gltf')))
        self.assertFalse(r['byte_identity']);self.assertTrue(r['geometry_identity']);self.assertTrue(r['material_identity'])

    def test_external_texture_changes_do_not_inherit_source_file_byte_identity(self):
        requests=[]
        for name,color in [('a',(10,20,30)),('b',(100,200,30))]:
            folder=self.root/name;folder.mkdir();path,_=gltf(folder,'source.gltf',color=color,external=True)
            requests.append(dict(source=pointer(self.root,path),dependencies=[pointer(self.root,folder/'texture.png',kind='texture')]))
        result=compare_assets(self.root,*requests)
        self.assertTrue(result['source_byte_identity']);self.assertFalse(result['byte_identity'])
        self.assertFalse(result['material_identity']);self.assertTrue(result['needs_visual_review'])

    def test_comparison_rechecks_left_source_after_actual_right_inspection(self):
        from unittest.mock import patch
        a,_=gltf(self.root,'a.glb');b,_=gltf(self.root,'b.glb')
        calls=0
        def actual_inspection_then_external_change(*args,**kwargs):
            nonlocal calls
            result=inspect_asset(*args,**kwargs)
            calls+=1
            if calls==2:
                a.write_bytes(a.read_bytes()+b' ')
            return result
        # Both native inspections execute. The wrapper injects an external
        # source mutation at the publication race boundary; it supplies no fake
        # parsed geometry, capability snapshot or positive execution result.
        with patch('continuity_inspection.inspect_asset',actual_inspection_then_external_change):
            with self.assertRaisesRegex(ValueError,'CONTINUITY_HASH'):
                compare_assets(self.root,dict(source=pointer(self.root,a)),dict(source=pointer(self.root,b)))
        self.assertEqual(calls,2)

    def test_native_rig_inspection_measures_real_bind_weights_and_sockets(self):
        rig=fixture(self.root,animated=False)
        request=native_request(self.root,rig)
        protected={r['path']:r['sha256'] for r in [request['source'],*request['dependencies']]}
        result=inspect_asset(self.root,**request)['inspection']
        self.assertEqual(len(result['skeleton']['bones']),3)
        self.assertEqual(result['skins']['strip']['weights']['shape'],[4,4])
        self.assertEqual(len(result['skins']['strip']['inverse_bind_matrices']),3)
        self.assertLess(result['skins']['strip']['maximum_weight_sum_error'],1e-12)
        self.assertEqual(result['hierarchy'][1]['parent'],'b0')
        self.assertEqual(protected,{p:hashlib.sha256((self.root/p).read_bytes()).hexdigest() for p in protected})
        result=classify_rigs(self.root,request,request)
        self.assertEqual(result['status'],'unverified');self.assertTrue(result['structure_and_binding_equal'])
        self.assertEqual(result['issues'][0]['code'],'APPROVAL_REQUIRED')

    def test_native_gltf_skin_and_original_hierarchy_inspection(self):
        rig=fixture(self.root,animated=False);path=self.root/'skinned.glb';export_gltf(rig,path)
        result=inspect_asset(self.root,pointer(self.root,path))['inspection']
        self.assertTrue(result['skins']);self.assertEqual(len(result['skeleton']['bones']),3)
        self.assertTrue(any(n['skin'] is not None for n in result['hierarchy']))

    def test_missing_required_socket_and_bone_are_precise_incompatible_states(self):
        rig=fixture(self.root,animated=False);r=native_request(self.root,rig)
        result=classify_rigs(self.root,r,r,required_bones=['missing-joint'],required_sockets=['missing-hand'])
        self.assertEqual(result['status'],'incompatible')
        self.assertEqual({x['code'] for x in result['issues']},{'MISSING_BONE','MISSING_SOCKET'})

    def test_partial_correspondence_uses_measured_rest_structure(self):
        a=fixture(self.root,name='a',animated=False);b=fixture(self.root,name='b',animated=False)
        b.geometry.meshes[0].positions*=2
        b.weights=generate_skin_weights(b.geometry,b.skeleton);save_rig(b,operation='explicit-fixture-scale')
        result=classify_rigs(self.root,native_request(self.root,a),native_request(self.root,b))
        self.assertEqual(result['status'],'partial');self.assertEqual(result['verified_rest_joint_overlap'],['b0','b1','b2'])
        self.assertEqual(result['issues'][0]['code'],'APPROVED_MAPPING_REQUIRED')

    def test_exact_classification_uses_native_retained_review_gate_not_an_approval_flag(self):
        from rig_review import apply_review
        from rig_validation import REVIEW_DIMENSIONS
        from rig_models import rig_signature
        rig=fixture(self.root,animated=True)
        evidence=rig.manifest['geometry']['data']
        decision=dict(schema_version=1,kind='hatch-3d-rig-review',rig_id=rig.manifest['id'],
            rig_version=rig.manifest['version'],expected_sha256=rig_signature(rig.manifest),
            author='Explicit simulated review decision for fixture persistence gate; no actual artwork approval',
            reviewer_kind='agent',created_at='2026-11-13T00:00:00Z',fixture=True,evidence=[evidence],
            dimensions={name:dict(verdict='approved',note='Simulated fixture decision; not independent visual evidence') for name in REVIEW_DIMENSIONS})
        applied=apply_review(rig,decision)
        self.assertFalse(applied['production_artwork_approved'])
        request=native_request(self.root,rig)
        result=classify_rigs(self.root,request,request)
        self.assertEqual(result['status'],'exact');self.assertFalse(result['production_artwork_approved'])

    def test_supported_approved_mapping_executes_native_retarget_and_deformation(self):
        a=fixture(self.root,name='a',animated=False);b=fixture(self.root,name='b',animated=False)
        b.geometry.meshes[0].positions*=2
        b.weights=generate_skin_weights(b.geometry,b.skeleton);save_rig(b,operation='explicit-fixture-scale')
        left,right=native_request(self.root,a),native_request(self.root,b)
        profile=build_retarget_profile(a,b)
        (self.root/'map.json').write_bytes(canonical_bytes(profile))
        mapref=pointer(self.root,self.root/'map.json',kind='rig-retarget')
        decision=sample('review');decision.update(reviewer_kind='assistant',fixture=True,verdict='approved',
                notes='Simulated retained mapping decision for fixture gate test, not production or visual approval.',
                subject_sha256=mapref['sha256'],inspected=[mapref,left['source'],right['source']])
        (self.root/'retained-decision.json').write_bytes(canonical_bytes({k:v for k,v in decision.items() if k!='native_receipt'}))
        decision['native_receipt']=pointer(self.root,self.root/'retained-decision.json',kind='review-evidence')
        (self.root/'decision.json').write_bytes(canonical_bytes(decision))
        reviewref=pointer(self.root,self.root/'decision.json',kind='continuity-review')
        r=classify_rigs(self.root,left,right,profile=mapref,review=reviewref)
        self.assertEqual(r['status'],'retargetable');self.assertEqual(r['native_pose_proof']['deformed_vertices'],4)
        self.assertGreater(r['native_pose_proof']['maximum_displacement_m'],0)
        self.assertFalse(r['production_artwork_approved']);self.assertFalse(r['visual_approval'])
        bad=deepcopy(decision);bad['subject_sha256']='0'*64
        (self.root/'decision.json').write_bytes(canonical_bytes(bad))
        with self.assertRaisesRegex(ValueError,'INSPECTION_REVIEW'):
            classify_rigs(self.root,left,right,profile=mapref,review=pointer(self.root,self.root/'decision.json',kind='continuity-review'))

    def test_missing_texture_corrupt_glb_and_skin_reference_fail_precisely(self):
        path,b=gltf(self.root,'missing.gltf',external=True)
        with self.assertRaisesRegex(Exception,'Missing|missing|SOURCE|PATH'):
            inspect_asset(self.root,pointer(self.root,path,kind='gltf'))
        texture=pointer(self.root,self.root/'texture.png',kind='texture')
        self.assertTrue(inspect_asset(self.root,pointer(self.root,path,kind='gltf'),dependencies=[texture])['inspection']['textures'])
        (self.root/'corrupt.glb').write_bytes(b'bad')
        with self.assertRaisesRegex(Exception,'GLB'):
            inspect_asset(self.root,pointer(self.root,self.root/'corrupt.glb'))
        b.json['nodes'][1]['skin']=99;b.write(self.root/'bad-skin.glb')
        with self.assertRaisesRegex(Exception,'REFERENCE|NATIVE_DOCUMENT|SKIN|skin'):
            inspect_asset(self.root,pointer(self.root,self.root/'bad-skin.glb'),dependencies=[texture])

    def test_source_hash_path_symlink_limits_unknown_fields_and_untrusted_scripts_fail_closed(self):
        path,b=gltf(self.root);good=pointer(self.root,path)
        for bad in [dict(good,sha256='0'*64),dict(good,path='../source.glb'),dict(good,arbitrary=True)]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):inspect_asset(self.root,bad)
        for limits in [dict(vertices=1),dict(memory_bytes=1),dict(vertices=True),dict(vertices=100001)]:
            with self.subTest(limits=limits),self.assertRaises(ValueError):inspect_asset(self.root,good,limits=limits)
        (self.root/'unsafe.blend').write_bytes(b'untrusted executable data')
        with self.assertRaisesRegex(ValueError,'ADAPTER_REQUIRED'):
            inspect_asset(self.root,pointer(self.root,self.root/'unsafe.blend',kind='blend'))
        if os.name!='nt':
            (self.root/'link.glb').symlink_to(path)
            with self.assertRaises(ValueError):inspect_asset(self.root,dict(good,path='link.glb'))

    def test_aggregate_instanced_geometry_budget_precedes_native_import(self):
        path,b=gltf(self.root)
        b.json['nodes']=[dict(mesh=0),dict(mesh=0)];b.json['scenes']=[dict(nodes=[0,1])];b.write(path)
        from rig_errors import RigError
        with self.assertRaises(RigError) as caught:
            inspect_asset(self.root,pointer(self.root,path),limits=dict(vertices=4))
        self.assertEqual(caught.exception.report['code'],'RIG_LIMIT_VERTICES')

    def test_optional_unhandled_extensions_remain_explicitly_partial(self):
        path,b=gltf(self.root)
        b.json['extensionsUsed']=['EXT_fixture_unknown'];b.json['nodes'][0]['extensions']={'EXT_fixture_unknown':{'authored':True}}
        b.write(path);source=pointer(self.root,path)
        result=inspect_asset(self.root,source)['inspection']
        self.assertEqual(result['status'],'partial')
        self.assertIn('EXT_fixture_unknown',result['source_features']['unsupported_extensions'])
        (self.root/'evidence').mkdir();owner=NativeInspectionOwner(self.root,source,'evidence')
        with owner.reserve(None):
            executed=owner.execute(None)
        self.assertEqual(executed['outcome'],'partial')
        self.assertNotIn('extensions-and-channels',executed['verified_fields'])

    def test_actual_capability_admission_executes_and_retains_source_bound_receipt(self):
        from continuity_capabilities import Admission,Policy
        path,_=gltf(self.root);source=pointer(self.root,path)
        (self.root/'evidence').mkdir()
        owner=NativeInspectionOwner(self.root,source,'evidence')
        engine=Admission(self.root,policy=Policy(asset_rights=True,allowed_owners=frozenset(['hatch-rig'])),owners=[owner])
        resources=dict(memory_bytes=512*1024**2,disk_bytes=64*1024**2,cpu_seconds=60,gpu_bytes=0,concurrency=1)
        request=dict(schema_version='1.0.0',kind='continuity-capability-request',request_id='actual-inspection',project_revision=source,
            checks=[dict(id='geometry',capability='geometry-inspection',schema_version='1.0.0',inputs=[source],
                input_types=['mesh'],output_types=['native-inspection'],resources=resources,required_fields=['source-hierarchy'],
                allow_fallback=True,gpu_required=False,needs_visual_approval=False)])
        result=engine.run(request)
        self.assertIn(result['checks'][0]['outcome'],['executed','supported_fallback'],json.dumps(result))
        self.assertFalse(result['checks'][0]['missing_fields']);self.assertFalse(result['visual_approval'])
        ref=result['checks'][0]['evidence'][0]
        payload=(self.root/ref['path']).read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(),ref['sha256'])
        retained=json.loads(payload)
        self.assertEqual(retained['sha256'],digest(retained['inspection']))
        self.assertEqual(retained['inspection']['source'],source)
        self.assertEqual(retained['inspection']['analysis']['vertices'],3)
        request['checks'][0]['resources']['memory_bytes']=1
        blocked=engine.run(request)
        self.assertNotIn(blocked['checks'][0]['outcome'],['executed','supported_fallback'])

    def test_cli_retains_hash_bound_receipt_and_rejects_unknown_request(self):
        path,_=gltf(self.root);request=self.root/'request.json'
        request.write_bytes(canonical_bytes(dict(source=pointer(self.root,path))))
        p=subprocess.run([sys.executable,str(ROOT/'scripts/continuity_inspection.py'),'--root',str(self.root),'--request',str(request)],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        r=json.loads(p.stdout);self.assertEqual(r['sha256'],digest(r['inspection']))
        request.write_bytes(canonical_bytes(dict(source=pointer(self.root,path),execute_script=True)))
        p=subprocess.run([sys.executable,str(ROOT/'scripts/continuity_inspection.py'),'--root',str(self.root),'--request',str(request)],capture_output=True,text=True)
        self.assertEqual(p.returncode,1);self.assertIn('INSPECTION_REQUEST',p.stdout)


if __name__=='__main__':unittest.main()
