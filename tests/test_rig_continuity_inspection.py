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
from continuity_inspection import NativeInspectionOwner, classify_rigs, inspect_asset, compare_assets
from continuity_contracts import canonical_bytes,digest
from continuity_test_helpers import sample
from rig_gltf import GltfBuilder, read_gltf
from rig_jobs import save_rig
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


def gltf(root,name='source.glb',*,color=(20,40,60),width=1.,external=False, skinned=False):
    b=GltfBuilder(); image=io.BytesIO();Image.new('RGB',(2,2),color).save(image,format='PNG')
    attrs=dict(POSITION=b.accessor([[0,0,0],[width,0,0],[0,2,0]],'VEC3', bounds=True),
               NORMAL=b.accessor([[0,0,1]]*3,'VEC3'),TANGENT=b.accessor([[1,0,0,1]]*3,'VEC4'),
               TEXCOORD_0=b.accessor([[0,0],[1,0],[0,1]],'VEC2'))
    primitive=dict(attributes=attrs,material=0,targets=[dict(POSITION=b.accessor([[0,0,.1]]*3,'VEC3', bounds=True))])
    images=[dict(uri='texture.png')] if external else [dict(mimeType='image/png',bufferView=b.view(image.getvalue()))]
    b.json.update(nodes=[dict(name='Actual parent',translation=[3,0,0],children=[1]),dict(name='Actual mesh',mesh=0)],
                  meshes=[dict(name='same descriptive metadata',extras=dict(targetNames=['smile']),primitives=[primitive])],
                  scenes=[dict(nodes=[0])],images=images,
                  materials=[dict(name='same descriptive material',pbrMetallicRoughness=dict(baseColorTexture=dict(index=0)))],
                  textures=[dict(source=0)])
    if skinned:
        attrs['JOINTS_0'] = b.accessor([[0, 1, 0, 0]] * 3, 'VEC4', component=5121)
        attrs['WEIGHTS_0'] = b.accessor([[1, 0, 0, 0], [.5, .5, 0, 0], [0, 1, 0, 0]], 'VEC4')
        binds = np.stack([np.eye(4), np.eye(4)])
        binds[1, 1, 3] = -1
        b.json['nodes'] += [dict(name='Root joint', children=[3]),
                            dict(name='Tip joint', translation=[0, 1, 0])]
        b.json['nodes'][0]['children'].append(2)
        b.json['nodes'][1]['skin'] = 0
        b.json['skins'] = [dict(joints=[2, 3], skeleton=2, inverseBindMatrices=b.accessor(binds, 'MAT4'))]
    b.json['asset']['extras']=dict(hatchSource=dict(fixture=True))
    if external:
        (root/'texture.png').write_bytes(image.getvalue())
    b.write(root/name)
    return root/name,b


class ContinuityInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()

    def test_actual_hierarchy_geometry_uv_normals_material_texture_morph_and_landmarks(self):
        path,_=gltf(self.root, skinned=True)
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
        self.assertIn(digest('smile'),d['meshes'][0]['morphs']);self.assertEqual(d['textures'][0]['bytes'],len(self.image_bytes(path)))
        self.assertIn('sRGB',d['texture_color_spaces'][0]['color_space'])
        self.assertEqual(d['materials'][0]['inputs']['pbrMetallicRoughness']['baseColorTexture']['index'],0)
        self.assertEqual(d['measurements']['distances']['height']['distance_m'],2.)
        self.assertEqual(d['measurements']['distances']['height']['relative_to_height'],1.)
        skin = d['skins']['mesh-1-0']
        self.assertEqual(skin['method'], 'imported')
        self.assertEqual(skin['inverse_bind_space'], 'normalized importer rest space')
        self.assertEqual(skin['inverse_bind_matrices'][0][0][3], -3.)
        self.assertEqual(len(skin['joints']), 2)
        self.assertEqual(skin['weights']['shape'], [3, 4])
        self.assertEqual(skin['maximum_weight_sum_error'], 0.)
        self.assertEqual(d['hierarchy'][1]['skin'], 0)
        self.assertFalse(d['visual_approval']);self.assertEqual(d['semantic_equivalence'],'unverified')

    def test_texture_coordinates_follow_transform_override_and_core_fallback(self):
        path, builder = gltf(self.root)
        attrs = builder.json['meshes'][0]['primitives'][0]['attributes']
        attrs['TEXCOORD_1'] = builder.accessor([[0, .5], [.5, 0], [1, 1]], 'VEC2')
        material = builder.json['materials'][0]
        builder.json['extensionsUsed'] = ['KHR_texture_transform']
        material['pbrMetallicRoughness']['baseColorTexture'].update(
            texCoord=0, extensions={'KHR_texture_transform': {'texCoord': 1}},
        )
        material['emissiveTexture'] = dict(
            index=0, texCoord=1, extensions={'KHR_texture_transform': {'texCoord': 0}},
        )
        material['occlusionTexture'] = dict(
            index=0, texCoord=1, extensions={'KHR_texture_transform': {'offset': [.25, 0]}},
        )
        material['normalTexture'] = dict(index=0)
        builder.write(path)

        receipt = inspect_asset(self.root, pointer(self.root, path))['inspection']
        coordinates = {slot['slot_path'][-1]: slot['texcoord']
                       for slot in receipt['texture_color_spaces']}
        self.assertEqual(coordinates, dict(baseColorTexture=1, emissiveTexture=0,
                                           occlusionTexture=1, normalTexture=0))
        base_color = receipt['materials'][0]['inputs']['pbrMetallicRoughness']['baseColorTexture']
        self.assertEqual(base_color['texCoord'], 0)
        self.assertEqual(base_color['extensions']['KHR_texture_transform']['texCoord'], 1)

    def test_fixture_position_accessors_retain_bounds(self):
        path, _ = gltf(self.root, skinned=True)
        document = read_gltf(path)
        primitive = document.json['meshes'][0]['primitives'][0]
        base = document.json['accessors'][primitive['attributes']['POSITION']]
        morph = document.json['accessors'][primitive['targets'][0]['POSITION']]
        self.assertEqual(base['min'], [0., 0., 0.])
        self.assertEqual(base['max'], [1., 2., 0.])
        np.testing.assert_allclose(morph['min'], [0., 0., .1])
        np.testing.assert_allclose(morph['max'], [0., 0., .1])

    def image_bytes(self,path):
        from rig_import import load_character_model
        return load_character_model(path).geometry.images[0].data

    def test_changed_geometry_and_texture_with_identical_names_require_real_review(self):
        a,_=gltf(self.root,'a.glb');b,_=gltf(self.root,'b.glb',width=1.5)
        r=compare_assets(self.root,dict(source=pointer(self.root,a)),dict(source=pointer(self.root,b)))
        self.assertFalse(r['byte_identity'])
        self.assertFalse(r['geometry_identity'])
        self.assertTrue(r['material_identity'])
        self.assertTrue(r['observed_material_identity'])
        self.assertTrue(r['needs_visual_review'])
        c,_=gltf(self.root,'c.glb',color=(200,30,10))
        r=compare_assets(self.root,dict(source=pointer(self.root,a)),dict(source=pointer(self.root,c)))
        self.assertTrue(r['geometry_identity']);self.assertFalse(r['material_identity']);self.assertFalse(r['visual_approval'])
        self.assertFalse(r['observed_material_identity'])

    def test_unobserved_recognized_extension_scalar_leaves_material_identity_unknown(self):
        left, builder = gltf(self.root, 'ior-left.glb')
        builder.json['extensionsUsed'] = ['KHR_materials_ior']
        builder.json['materials'][0]['extensions'] = {'KHR_materials_ior': {'ior': 1.3}}
        builder.write(left)
        right = self.root / 'ior-right.glb'
        builder.json['materials'][0]['extensions']['KHR_materials_ior']['ior'] = 1.7
        builder.write(right)
        result = compare_assets(self.root, dict(source=pointer(self.root, left)),
                                dict(source=pointer(self.root, right)))
        self.assertFalse(result['source_byte_identity'])
        self.assertTrue(result['observed_material_identity'])
        self.assertIsNone(result['material_identity'])
        for side in ('left', 'right'):
            report = result[side]['inspection']
            self.assertEqual(report['status'], 'partial')
            self.assertEqual(report['material_coverage'], 'partial')
            self.assertEqual(report['source_features']['partial_material_extensions'], ['KHR_materials_ior'])
            self.assertNotIn('KHR_materials_ior', report['materials'][0]['inputs'].get('extensions', {}))

    def test_assignment_changes_material_identity_with_equal_slot_inventory(self):
        baseline, builder = gltf(self.root, 'baseline.glb')
        builder.json['materials'].append(dict(name='Alternate slot'))
        builder.write(baseline)
        changed = self.root / 'changed.glb'
        builder.json['meshes'][0]['primitives'][0]['material'] = 1
        builder.write(changed)
        result = compare_assets(self.root, dict(source=pointer(self.root, baseline)),
                                dict(source=pointer(self.root, changed)))
        self.assertTrue(result['geometry_identity'])
        self.assertFalse(result['material_identity'])
        self.assertEqual(result['left']['inspection']['materials'], result['right']['inspection']['materials'])

    def test_unskinned_instance_gets_importer_generated_rigid_binding(self):
        path, builder = gltf(self.root, skinned=True)
        builder.json['nodes'][0]['children'].append(4)
        builder.json['nodes'].append(dict(name='Unskinned instance', mesh=0))
        builder.write(path)
        result = inspect_asset(self.root, pointer(self.root, path))['inspection']
        self.assertEqual(result['skins']['mesh-1-0']['method'], 'imported')
        rigid = result['skins']['mesh-4-0']
        self.assertEqual(rigid['method'], 'rigid')
        self.assertEqual(len(rigid['joints']), 1)

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
        from continuity_inspection import _inspect_asset
        a,_=gltf(self.root,'a.glb');b,_=gltf(self.root,'b.glb')
        calls=0
        def actual_inspection_then_external_change(*args,**kwargs):
            nonlocal calls
            result=_inspect_asset(*args,**kwargs)
            calls+=1
            if calls==2:
                a.write_bytes(a.read_bytes()+b' ')
            return result
        # Both native inspections execute. The wrapper injects an external
        # source mutation at the publication race boundary; it supplies no fake
        # parsed geometry, capability snapshot or positive execution result.
        with patch('continuity_inspection._inspect_asset',actual_inspection_then_external_change):
            with self.assertRaisesRegex(ValueError,'CONTINUITY_INSPECTION_HASH'):
                compare_assets(self.root,dict(source=pointer(self.root,a)),dict(source=pointer(self.root,b)))
        self.assertEqual(calls,2)

    def test_receipt_omits_authored_labels_and_resource_extras(self):
        baseline, builder = gltf(self.root, 'baseline.glb', skinned=True)
        label = 'private fixture metadata'
        builder.json['nodes'][0]['name'] = label
        builder.json['nodes'][2]['name'] = label
        builder.json['meshes'][0]['name'] = label
        builder.json['meshes'][0]['extras'].update(hatchRegion=label, hatchMeshId=label)
        builder.json['images'][0]['name'] = label
        material = builder.json['materials'][0]
        material.update(name=label, extras={'arbitrary': label})
        material['pbrMetallicRoughness']['baseColorTexture']['scale'] = label
        builder.json['textures'][0]['extras'] = {'arbitrary': label}
        builder.json['samplers'] = [dict(name=label, extras={'arbitrary': label})]
        changed = self.root / 'metadata.glb'
        builder.write(changed)
        report = inspect_asset(self.root, pointer(self.root, changed), landmarks={
            'point': dict(mesh_id='mesh-1-0', vertex_index=0),
        })['inspection']
        self.assertNotIn(label, canonical_bytes(report).decode())
        plain = inspect_asset(self.root, pointer(self.root, baseline))['inspection']
        self.assertEqual(report['materials'], plain['materials'])
        self.assertEqual(report['texture_color_spaces'], plain['texture_color_spaces'])

    def test_comparison_rejects_directory_replacement_with_identical_bytes(self):
        from shutil import copytree
        from unittest.mock import patch
        from continuity_inspection import _inspect_asset

        directory = self.root / 'assets'
        directory.mkdir()
        path, _ = gltf(directory)
        spec = dict(source=pointer(directory, path))
        original = self.root / 'original'
        def inspect_then_replace(*args, **kwargs):
            receipt = _inspect_asset(*args, **kwargs)
            if not original.exists():
                directory.rename(original)
                copytree(original, directory)
            return receipt

        with patch('continuity_inspection._inspect_asset', inspect_then_replace):
            with self.assertRaisesRegex(ValueError, 'Selected source directory changed'):
                compare_assets(directory, spec, spec)

    def test_standalone_receipt_rechecks_variant_before_return(self):
        from unittest.mock import patch

        path, _ = gltf(self.root)
        alternate, _ = gltf(self.root, 'alternate.glb')
        variant = pointer(self.root, alternate)
        def hash_then_change(value):
            result = digest(value)
            if isinstance(value, dict) and value.get('kind') == 'continuity-asset-inspection':
                alternate.write_bytes(alternate.read_bytes() + b' ')
            return result

        with patch('continuity_inspection.digest', hash_then_change):
            with self.assertRaisesRegex(ValueError, 'HASH.*alternate[.]glb'):
                inspect_asset(self.root, pointer(self.root, path), variants=[variant])

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
        self.assertIn(digest('EXT_fixture_unknown'),result['source_features']['unsupported_extensions'])

    def test_combined_texture_capacity_precedes_pixel_decode(self):
        from unittest.mock import patch
        from rig_errors import RigError
        path,b=gltf(self.root)
        image=io.BytesIO();Image.new('RGBA',(512,512),(20,40,60,255)).save(image,format='PNG')
        b.json['images']=[dict(mimeType='image/png',bufferView=b.view(image.getvalue()))]
        b.write(path)
        accepted=inspect_asset(self.root,pointer(self.root,path),limits=dict(memory_bytes=8*1024**2))
        self.assertEqual(accepted['inspection']['native_image_admission']['images'][0]['width'],512)
        b.json['images'].append(dict(mimeType='image/png',bufferView=b.view(image.getvalue())))
        b.write(path)
        with patch('PIL.Image.open',side_effect=AssertionError('pixel decoder must not run before capacity admission')):
            with self.assertRaises(RigError) as caught:
                inspect_asset(self.root,pointer(self.root,path),limits=dict(memory_bytes=8*1024**2))
        self.assertEqual(caught.exception.report['code'],'RIG_LIMIT_MEMORY')

    def test_closure_requires_all_and_only_native_external_files(self):
        source_path, builder = gltf(self.root, 'source.gltf', external=True, skinned=True)
        body = json.loads(source_path.read_bytes())
        body['buffers'][0]['uri'] = 'geo%20data.bin'
        source_path.write_text(json.dumps(body))
        (self.root / 'geo data.bin').write_bytes(builder.binary)
        source = pointer(self.root, source_path)
        pins = [pointer(self.root, self.root / path, kind='native-dependency')
                for path in ('texture.png', 'geo data.bin')]
        result = inspect_asset(self.root, source, dependencies=pins)['inspection']
        self.assertEqual(result['dependencies'], [source, *pins])
        self.assertGreater(result['bytes_inspected'], source_path.stat().st_size)
        for pin in pins:
            with self.subTest(pin=pin['path']), self.assertRaisesRegex(ValueError, 'missing'):
                inspect_asset(self.root, source, dependencies=[r for r in pins if r != pin])
        unused = self.root / 'unused.bin'
        unused.write_bytes(b'unreferenced')
        with self.assertRaisesRegex(ValueError, 'CLOSURE.*unused[.]bin'):
            inspect_asset(self.root, source, dependencies=[*pins, pointer(self.root, unused)])
        with self.assertRaisesRegex(ValueError, 'HASH.*texture[.]png'):
            inspect_asset(self.root, source, dependencies=[dict(pins[0], sha256='0'*64), pins[1]])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'registered directory capabilities require Linux')
    def test_registered_root_retains_descriptor_authority(self):
        from rig_errors import directory_capability

        selected = self.root / 'selected'
        selected.mkdir()
        path, _ = gltf(selected, 'source.gltf', external=True)
        source = pointer(selected, path)
        texture = pointer(selected, selected / 'texture.png', kind='texture')
        ordinary = inspect_asset(selected, source, dependencies=[texture])
        descriptor = os.open(selected, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with directory_capability(descriptor) as capability:
                selected.rename(self.root / 'renamed')
                selected.mkdir()
                (selected / path.name).write_bytes(b'replacement')
                report = inspect_asset(capability.path, source, dependencies=[texture])
                self.assertEqual(report, ordinary)
                with self.assertRaisesRegex(ValueError, 'HASH'):
                    inspect_asset(selected, source, dependencies=[texture])
        finally:
            os.close(descriptor)

    def test_cli_retains_hash_bound_receipt_and_rejects_unknown_request(self):
        path,_=gltf(self.root);request=self.root/'request.json'
        request.write_bytes(canonical_bytes(dict(source=pointer(self.root,path))))
        p=subprocess.run([sys.executable,str(ROOT/'scripts/continuity_inspection.py'),'--root',str(self.root),'--request',str(request)],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        r=json.loads(p.stdout);self.assertEqual(r['sha256'],digest(r['inspection']))
        specs = {'left': {'source': pointer(self.root, path)}, 'right': {'source': pointer(self.root, path)}}
        request.write_bytes(canonical_bytes(specs))
        p = subprocess.run([sys.executable, str(ROOT/'scripts/continuity_inspection.py'), '--root',
                            str(self.root), '--request', str(request)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(json.loads(p.stdout)['byte_identity'])
        request.write_bytes(canonical_bytes(dict(source=pointer(self.root,path),execute_script=True)))
        p=subprocess.run([sys.executable,str(ROOT/'scripts/continuity_inspection.py'),'--root',str(self.root),'--request',str(request)],capture_output=True,text=True)
        self.assertEqual(p.returncode,1);self.assertIn('INSPECTION_REQUEST',p.stdout)

    def test_cli_failure_reports_codes_and_counts_without_authored_text(self):
        label = 'private_fixture_label'
        path, builder = gltf(self.root, f'{label}.glb')
        request = self.root / 'request.json'
        command = [sys.executable, str(ROOT/'scripts/continuity_inspection.py'),
                   '--root', str(self.root), '--request', str(request)]

        def rejected(payload):
            request.write_bytes(payload)
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertNotIn(label, result.stdout)
            return json.loads(result.stdout)

        extension = f'EXT_{label}'
        builder.json['extensionsUsed'] = builder.json['extensionsRequired'] = [extension]
        builder.write(path)
        error = rejected(canonical_bytes(dict(source=pointer(self.root, path))))
        self.assertEqual(error['native_diagnostic'], {'code': 'RIG_GLTF_EXTENSION'})
        error = rejected(f'{{"{label}":1,"{label}":2}}'.encode())
        self.assertEqual(error['native_diagnostic'], {'code': 'RIG_JSON_DUPLICATE'})

        del builder.json['extensionsUsed'], builder.json['extensionsRequired']
        builder.write(path)
        source = pointer(self.root, path)
        error = rejected(canonical_bytes(dict(source=source, limits=dict(vertices=2))))
        self.assertEqual(error['native_diagnostic'],
                         dict(code='RIG_LIMIT_VERTICES', expected=2, observed=3))
        error = rejected(canonical_bytes(dict(source=dict(source, sha256='0' * 64))))
        self.assertIn('CONTINUITY_INSPECTION_HASH', error['error'])


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

    def test_inspection_owner_reservation_excludes_concurrent_caller(self):
        from concurrent.futures import ThreadPoolExecutor
        path,_=gltf(self.root);(self.root/'evidence').mkdir()
        owner=NativeInspectionOwner(self.root,pointer(self.root,path),'evidence')
        def attempt():
            with owner.reserve(None):
                return 'incorrectly-reserved'
        with owner.reserve(None):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaisesRegex(ValueError,'INSPECTION_OWNERSHIP'):
                    pool.submit(attempt).result(timeout=5)

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

    def test_partial_correspondence_uses_measured_rest_structure(self):
        a=fixture(self.root,name='a',animated=False);b=fixture(self.root,name='b',animated=False)
        b.geometry.meshes[0].positions*=2
        b.weights=generate_skin_weights(b.geometry,b.skeleton);save_rig(b,operation='explicit-fixture-scale')
        result=classify_rigs(self.root,native_request(self.root,a),native_request(self.root,b))
        self.assertEqual(result['status'],'partial');self.assertEqual(result['verified_rest_joint_overlap'],['b0','b1','b2'])
        self.assertEqual(result['issues'][0]['code'],'APPROVED_MAPPING_REQUIRED')

    def test_missing_required_socket_and_bone_are_precise_incompatible_states(self):
        rig=fixture(self.root,animated=False);r=native_request(self.root,rig)
        result=classify_rigs(self.root,r,r,required_bones=['missing-joint'],required_sockets=['missing-hand'])
        self.assertEqual(result['status'],'incompatible')
        self.assertEqual({x['code'] for x in result['issues']},{'MISSING_BONE','MISSING_SOCKET'})

    def test_unavailable_facial_controls_are_unknown_not_a_difference(self):
        path,_=gltf(self.root,skinned=True);asset=dict(source=pointer(self.root,path))
        native=native_request(self.root,fixture(self.root,name='native',animated=False))
        self.assertIsNone(compare_assets(self.root,asset,native)['facial_controls_identity'])
        for left,right in ((asset,native),(native,asset)):
            with self.subTest(left=left is native):
                codes={i['code'] for i in classify_rigs(self.root,left,right)['issues']}
                self.assertNotIn('FACIAL_CONTROLS_DIFFER',codes)
                self.assertIn('FACIAL_CONTROLS_UNAVAILABLE',codes)

    def test_separately_reviewed_native_facial_controls_affect_exact_identity(self):
        from rig_review import apply_review
        from rig_validation import REVIEW_DIMENSIONS
        from rig_models import rig_signature

        def reviewed(name, change=None):
            rig=fixture(self.root,name=name,animated=True)
            if change == 'expression':
                rig.manifest['facial']['expressions']['blink']['morphs']['blink']=0.5
            elif change == 'eyes':
                rig.manifest['facial']['eyes']=['b1']
            if change:
                save_rig(rig,operation='simulated-fixture-facial-change')
            decision=dict(schema_version=1,kind='hatch-3d-rig-review',rig_id=rig.manifest['id'],
                rig_version=rig.manifest['version'],expected_sha256=rig_signature(rig.manifest),
                author='Simulated fixture decision; no production artistic approval',reviewer_kind='agent',
                created_at='2026-11-13T00:00:00Z',fixture=True,evidence=[rig.manifest['geometry']['data']],
                dimensions={name:dict(verdict='approved',note='Simulated fixture decision') for name in REVIEW_DIMENSIONS})
            self.assertFalse(apply_review(rig,decision)['production_artwork_approved'])
            return native_request(self.root,rig)

        base=reviewed('facial-base')
        unchanged=reviewed('facial-unchanged')
        result=classify_rigs(self.root,base,unchanged)
        self.assertEqual(result['status'],'exact')
        for change in ('expression','eyes'):
            with self.subTest(change=change):
                other=reviewed('facial-'+change,change)
                result=classify_rigs(self.root,base,other)
                self.assertEqual(result['source']['inspection']['native_approval_status'],'approved')
                self.assertEqual(result['target']['inspection']['native_approval_status'],'approved')
                self.assertNotEqual(result['status'],'exact')
                self.assertEqual({issue['code'] for issue in result['issues']},
                                 {'FACIAL_CONTROLS_DIFFER','APPROVED_MAPPING_REQUIRED'})
                self.assertFalse(result['production_artwork_approved'])
                self.assertFalse(compare_assets(self.root,base,other)['facial_controls_identity'])
        self.assertTrue(compare_assets(self.root,base,unchanged)['facial_controls_identity'])


    def test_approved_mapping_with_facial_difference_and_required_socket(self):
        # All retained decisions below are simulated fixture decisions, not artwork approval.
        a=fixture(self.root,name='map-face-base',animated=True)
        socket=dict(schema_version=1,id='hand',bone='b2',role='hand',owner='fixture',
                    transform=dict(translation=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1]))
        a.manifest['attachments']=[deepcopy(socket)]
        save_rig(a,operation='fixture-socket')
        left=native_request(self.root,a)
        for change in ('expression','eyes','socket'):
            with self.subTest(change=change):
                b=fixture(self.root,name='map-face-'+change,animated=True)
                b.manifest['attachments']=[deepcopy(socket)]
                b.manifest['facial']['expressions']['blink']['morphs']['blink']=0.5
                if change=='eyes':
                    b.manifest['facial']['expressions']['blink']['morphs']['blink']=1.
                    b.manifest['facial']['eyes']=['b1']
                if change=='socket':
                    b.manifest['attachments'][0]['transform']['translation']=[0.1,0,0]
                save_rig(b,operation='simulated-fixture-change')
                right=native_request(self.root,b)
                profile=build_retarget_profile(a,b)
                path=self.root/('map-'+change+'.json');path.write_bytes(canonical_bytes(profile))
                mapref=pointer(self.root,path,kind='rig-retarget')
                decision=sample('review');decision.update(reviewer_kind='assistant',fixture=True,verdict='approved',
                    notes='Simulated fixture decision; no production or artistic approval',
                    subject_sha256=mapref['sha256'],inspected=[mapref,left['source'],right['source']])
                receipt=self.root/('retained-'+change+'.json')
                receipt.write_bytes(canonical_bytes({k:v for k,v in decision.items() if k!='native_receipt'}))
                decision['native_receipt']=pointer(self.root,receipt,kind='review-evidence')
                review=self.root/('decision-'+change+'.json');review.write_bytes(canonical_bytes(decision))
                result=classify_rigs(self.root,left,right,required_sockets=['hand'],profile=mapref,
                                     review=pointer(self.root,review,kind='continuity-review'))
                codes={issue['code'] for issue in result['issues']}
                self.assertIn('FACIAL_CONTROLS_DIFFER',codes)
                self.assertFalse(compare_assets(self.root,left,right)['facial_controls_identity'])
                self.assertFalse(result['production_artwork_approved']);self.assertFalse(result['visual_approval'])
                if change=='socket':
                    self.assertEqual(result['status'],'incompatible')
                    self.assertIn('INCOMPATIBLE_SOCKET',codes)
                    self.assertNotIn('native_pose_proof',result)
                else:
                    self.assertEqual(result['status'],'retargetable')
                    self.assertEqual(codes,{'FACIAL_CONTROLS_DIFFER'})
                    self.assertEqual(result['native_pose_proof']['deformed_vertices'],4)
                    self.assertGreater(result['native_pose_proof']['maximum_displacement_m'],0)

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


if __name__=='__main__':unittest.main()
