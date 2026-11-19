"""Actual saved native sources; fixture decisions never prove artwork approval."""
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
from continuity_inspection import inspect_asset, compare_assets
from continuity_contracts import canonical_bytes,digest
from rig_gltf import GltfBuilder, read_gltf


def pointer(root,path,*,kind='mesh',identity='fixture',version='1.0.0'):
    path=Path(path)
    return dict(id=identity,version=version,path=path.relative_to(root).as_posix(),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),kind=kind)


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



def animated_gltf(root, *, primitives=1, clips=1, interpolation='LINEAR', target_path='weights'):
    """Two-key synthetic source; shared accessors exercise retained destinations."""
    path, builder = gltf(root)
    primitive = builder.json['meshes'][0]['primitives'][0]
    builder.json['meshes'][0]['primitives'] = [primitive.copy() for _ in range(primitives)]
    times = builder.accessor([0., 1.], 'SCALAR', bounds=True)
    width = 1 if target_path == 'weights' else 4 if target_path == 'rotation' else 3
    value = [0., 0., 0., 1.] if target_path == 'rotation' else [1.] * width
    values = ([row for _ in range(2) for row in ([0.] * width, value, [0.] * width)]
              if interpolation == 'CUBICSPLINE' else [value, value])
    output = builder.accessor(values, {1: 'SCALAR', 3: 'VEC3', 4: 'VEC4'}[width])
    builder.json['animations'] = [
        dict(name=f'clip-{i}', samplers=[dict(input=times, output=output, interpolation=interpolation)],
             channels=[dict(sampler=0, target=dict(node=1, path=target_path))])
        for i in range(clips)]
    builder.write(path)
    return path


def instanced_gltf(root, *, vertices=512, instances=4):
    """Small real indexed mesh; each active instance shares source accessors."""
    builder = GltfBuilder()
    points = np.zeros((vertices, 3), dtype=np.float32)
    points[:, 0] = np.arange(vertices) / max(1, vertices - 1)
    points[2, 1] = 1.
    primitive = dict(attributes=dict(POSITION=builder.accessor(points, 'VEC3', bounds=True)),
                     indices=builder.accessor([0, 1, 2], 'SCALAR', component=5123))
    builder.json.update(meshes=[dict(primitives=[primitive])],
                        nodes=[dict(mesh=0, translation=[0, 0, i]) for i in range(instances)],
                        scenes=[dict(nodes=list(range(instances)))])
    builder.json['asset']['extras'] = dict(hatchSource=dict(fixture=True))
    path = root / 'instances.glb'
    builder.write(path)
    return path


class ContinuityInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

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



    def test_analysis_capacity_precedes_import_and_allocation_for_shared_instances(self):
        from unittest.mock import patch
        from continuity_inspection import (_analysis_admission, _animation_admission,
                                           _image_admission, _limits, _public_failure)
        from rig_errors import RigError
        from rig_gltf import preflight_gltf, scene_graph

        path = instanced_gltf(self.root)
        source = pointer(self.root, path)
        limits = _limits(None)
        document = read_gltf(path, limits)
        geometry = preflight_gltf(document, encoded_bytes=path.stat().st_size, limits=limits)
        images = _image_admission(document, geometry, limits)
        order = scene_graph(document)[2]
        animations = _animation_admission(document, images, limits, order)
        analysis = _analysis_admission(document, geometry, animations, limits, order)
        cap = 5 * 1024**2
        self.assertLess(animations['estimated_memory_bytes'], cap)
        self.assertEqual(geometry['mesh_instances'], 4)
        self.assertEqual(geometry['vertices'], 2048)
        self.assertEqual(analysis['concatenated_samples'], 2048)
        self.assertEqual(analysis['symmetry_samples'], 2048)
        self.assertEqual(analysis['symmetry_batch_rows'], 128)
        self.assertEqual(analysis['symmetry_difference_bytes'], 6291456)
        self.assertEqual(analysis['symmetry_square_bytes'], 6291456)
        self.assertEqual(analysis['symmetry_reduction_bytes'], 2097152)
        self.assertEqual(analysis['sample_working_bytes'], 347136)
        self.assertEqual(analysis['topology_working_bytes'], 141312)
        self.assertEqual(analysis['analysis_working_bytes'], 15168512)
        combined = analysis['estimated_memory_bytes']
        self.assertEqual(combined, animations['estimated_memory_bytes'] + analysis['analysis_working_bytes'])
        self.assertEqual(analysis['array_bytes'],
                         animations['array_bytes'] + analysis['analysis_working_bytes'])
        self.assertEqual(analysis['scope'],
                         'combined geometry/images/animations and native geometry analysis working arrays; '
                         'not process RSS, later evaluation or rendering')
        # Reject both the reported low ceiling and one byte below exact
        # combined admission, before entering native import or array decoding.
        for ceiling in (cap, combined - 1):
            with self.subTest(ceiling=ceiling), patch('rig_import.load_character_model') as importer, patch(
                    'rig_import.analyze_geometry') as importer_analysis, patch(
                    'rig_geometry.analyze_geometry') as projector_analysis, patch(
                    'rig_gltf.GltfDocument.accessor',
                    side_effect=AssertionError('array decoding must follow analysis admission')):
                with self.assertRaises(RigError) as caught:
                    inspect_asset(self.root, source, limits=dict(memory_bytes=ceiling))
                importer.assert_not_called()
                importer_analysis.assert_not_called()
                projector_analysis.assert_not_called()
            self.assertEqual(_public_failure(caught.exception)['native_diagnostic'],
                             dict(code='RIG_LIMIT_MEMORY', expected=ceiling, observed=combined))

        # Actual importer, analyzer and projector run at exactly the admitted
        # combined capacity; no mocked positive geometry or execution result.
        receipt = inspect_asset(self.root, source, limits=dict(memory_bytes=combined))
        report = receipt['inspection']
        self.assertEqual(receipt['sha256'], digest(report))
        self.assertEqual(report['native_analysis_admission'], analysis)
        self.assertEqual(report['analysis']['vertices'], 2048)
        self.assertEqual(report['analysis']['triangles'], 4)
        self.assertEqual(len(report['meshes']), 4)
        self.assertTrue(np.isfinite(report['analysis']['symmetry_mean_error_m']))
        self.assertEqual(pointer(self.root, path), source)

    def test_analysis_capacity_matches_native_sampling_and_uneven_batches(self):
        # The first slice may retain >512 per instance; the final slice keeps
        # its entire concatenated base. array_split can make >128-row batches.
        for vertices, instances, concatenated, samples, batch in (
                (255, 1, 255, 255, 255), (1023, 4, 4092, 2048, 128)):
            with self.subTest(vertices=vertices, instances=instances):
                path = instanced_gltf(self.root, vertices=vertices, instances=instances)
                receipt = inspect_asset(self.root, pointer(self.root, path))
                report = receipt['inspection']
                admission = report['native_analysis_admission']
                self.assertEqual(receipt['sha256'], digest(report))
                self.assertEqual(admission['concatenated_samples'], concatenated)
                self.assertEqual(admission['symmetry_samples'], samples)
                self.assertEqual(admission['symmetry_batch_rows'], batch)
                self.assertEqual(admission['symmetry_difference_bytes'], batch * samples * 3 * 8)
                self.assertEqual(report['analysis']['vertices'], vertices * instances)

    def test_animation_destination_budget_precedes_native_import_and_accessor_decode(self):
        from unittest.mock import patch
        from continuity_inspection import _image_admission, _limits, _public_failure
        from rig_errors import RigError
        from rig_gltf import preflight_gltf

        for interpolation in ('STEP', 'LINEAR', 'CUBICSPLINE'):
            # Exercise primitive multiplication and accumulation across clips
            # separately; each source has only two samples per channel.
            for primitives, clips in ((2, 1), (1, 2)):
                with self.subTest(interpolation=interpolation, primitives=primitives, clips=clips):
                    path = animated_gltf(self.root, primitives=primitives, clips=clips,
                                         interpolation=interpolation)
                    limits = _limits(None)
                    document = read_gltf(path, limits)
                    geometry = preflight_gltf(document, encoded_bytes=path.stat().st_size, limits=limits)
                    images = _image_admission(document, geometry, limits)
                    # The old geometry+image admission fits; one tiny track
                    # fits this allowance, but retained copies must not.
                    cap = images['estimated_memory_bytes'] + 12 * 1024
                    with patch('rig_import.load_character_model') as importer, patch(
                            'rig_gltf.GltfDocument.accessor',
                            side_effect=AssertionError('accessor decoding must follow capacity admission')):
                        with self.assertRaises(RigError) as caught:
                            inspect_asset(self.root, pointer(self.root, path), limits=dict(memory_bytes=cap))
                        importer.assert_not_called()
                    report = caught.exception.report
                    self.assertEqual(report['code'], 'RIG_LIMIT_MEMORY')
                    self.assertEqual(report['expected'], cap)
                    self.assertGreater(report['observed'], cap)
                    self.assertEqual(_public_failure(caught.exception)['native_diagnostic'],
                                     dict(code='RIG_LIMIT_MEMORY', expected=cap, observed=report['observed']))

    def test_small_animated_sources_remain_admitted_by_real_importer(self):
        from continuity_inspection import (_analysis_admission, _animation_admission,
                                           _image_admission, _limits)
        from rig_gltf import preflight_gltf, scene_graph

        for interpolation in ('STEP', 'LINEAR', 'CUBICSPLINE'):
            for target_path in ('weights', 'translation', 'rotation', 'scale'):
                with self.subTest(interpolation=interpolation, target_path=target_path):
                    path = animated_gltf(self.root, interpolation=interpolation, target_path=target_path)
                    limits = _limits(None)
                    document = read_gltf(path, limits)
                    geometry = preflight_gltf(document, encoded_bytes=path.stat().st_size, limits=limits)
                    images = _image_admission(document, geometry, limits)
                    order = scene_graph(document)[2]
                    animations = _animation_admission(document, images, limits, order)
                    analysis = _analysis_admission(document, geometry, animations, limits, order)
                    # Preserve the original restrictive animation allowance;
                    # add only the newly required, independently reported native
                    # analysis reserve. No importer mock or blanket cap increase.
                    animation_cap = images['estimated_memory_bytes'] + 12 * 1024
                    cap = animation_cap + analysis['analysis_working_bytes']
                    receipt = inspect_asset(self.root, pointer(self.root, path), limits=dict(memory_bytes=cap))
                    report = receipt['inspection']
                    self.assertEqual(receipt['sha256'], digest(report))
                    admission = report['native_animation_admission']
                    self.assertEqual(admission['channels'], 1)
                    self.assertEqual(admission['destinations'], 1)
                    self.assertEqual(admission['source_keyframes'], 2)
                    self.assertEqual(admission['instantiated_keyframes'], 2)
                    self.assertGreater(admission['estimated_memory_bytes'], images['estimated_memory_bytes'])
                    self.assertLessEqual(admission['estimated_memory_bytes'], cap)
                    self.assertLessEqual(admission['estimated_memory_bytes'], animation_cap)
                    self.assertEqual(report['native_analysis_admission'], analysis)
                    self.assertLessEqual(report['native_analysis_admission']['estimated_memory_bytes'], cap)
                    self.assertEqual(report['source_features']['animation_channels'],
                                     [dict(animation=0, target_node=1, path=target_path)])

    def test_animation_capacity_receipt_counts_shared_accessor_destinations(self):
        path = animated_gltf(self.root, primitives=2, clips=2, interpolation='CUBICSPLINE')
        report = inspect_asset(self.root, pointer(self.root, path))['inspection']
        admission = report['native_animation_admission']
        self.assertEqual(admission['channels'], 2)
        self.assertEqual(admission['destinations'], 4)
        self.assertEqual(admission['source_keyframes'], 4)
        self.assertEqual(admission['instantiated_keyframes'], 8)
        self.assertGreater(admission['animation_retained_bytes'], 0)
        self.assertGreater(admission['animation_working_bytes'], 0)
        self.assertGreater(admission['estimated_memory_bytes'],
                           report['native_image_admission']['estimated_memory_bytes'])

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


if __name__=='__main__':unittest.main()
