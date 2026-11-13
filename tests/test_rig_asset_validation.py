"""Actual immutable registry payload technical validation and adversarial cases."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from asset_library import AssetLibrary, file_record
from asset_library_contracts import seal
from asset_library_validation import inspect_geometry, validate_asset
from rig_errors import RigError
from rig_gltf import GltfBuilder, export_gltf
from rig_test_helpers import fixture

ROOT = Path(__file__).resolve().parents[1]


class AssetTechnicalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.library = AssetLibrary(self.root / 'library', create=True)
        self.base = json.loads((ROOT / 'examples/asset-library-foundation/manifest.json').read_text())
        self.license = self.root / 'license.txt'; self.license.write_text('CC0 test fixture')

    def register(self, files, kind='prop', version='1.0.0'):
        manifest = deepcopy(self.base)
        manifest['identity']['kind'] = kind
        manifest['identity']['id'] = 'asset-' + kind
        manifest['version'] = version
        records = [file_record(path, name, role) for name, (path, role) in files.items()]
        license_record = file_record(self.license, 'source/license.txt', 'license')
        manifest['files'] = records + [license_record]
        manifest['provenance']['sources'] = records
        manifest['provenance']['license_evidence'] = [license_record]
        manifest['publication']['inventory'] = [r['path'] for r in manifest['files']]
        manifest['compatibility']['formats'] = sorted({r['format'] for r in records})
        manifest = seal(manifest)
        return self.library.register(manifest, {**{name: path for name, (path, _) in files.items()},
                                                'source/license.txt': self.license})

    def geometry(self, *, translation=None, malformed=None):
        builder = GltfBuilder()
        positions = [[0,0,0],[1,0,0],[0,1,0]]
        attrs = {'POSITION': builder.accessor(positions, 'VEC3', bounds=True),
                 'NORMAL': builder.accessor([[0,0,1]]*3, 'VEC3'),
                 'TANGENT': builder.accessor([[1,0,0,1]]*3, 'VEC4'),
                 'TEXCOORD_0': builder.accessor([[0,0],[1,0],[0,1]], 'VEC2')}
        builder.json.update(meshes=[{'primitives': [{'attributes': attrs}]}],
                            nodes=[{'mesh':0, 'translation': translation or [0,0,0]}],
                            scenes=[{'nodes':[0]}], scene=0)
        if malformed: malformed(builder)
        path = self.root / 'source.glb'; builder.write(path)
        return path

    def test_geometry_inspects_actual_transformed_positions_channels_and_hashes(self):
        path = self.geometry(translation=[2,3,4])
        ref = self.register({'runtime/asset.glb': (path, 'runtime')})
        before = path.read_bytes(); manifest = self.library.resolve(ref)
        report = validate_asset(self.library, ref, requirements={'capabilities':['mesh']})
        actual = report['geometry']['runtime/asset.glb']
        self.assertEqual(actual['geometry']['bounds'], [[2,3,4],[3,4,4]])
        self.assertEqual(actual['meshes'][0]['vertices'], 3)
        self.assertEqual(set(actual['meshes'][0]['attributes']), {'NORMAL','TANGENT','TEXCOORD_0'})
        self.assertEqual(actual['hierarchy']['world_matrices']['0'][0][3], 2)
        self.assertEqual(report['file_hashes']['runtime/asset.glb'], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.library.resolve(ref), manifest)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(report['production_approved'])
        self.assertTrue(actual['independent_validation']['ok'])

    def test_skin_rest_and_animation_exact_time_are_measured(self):
        run = fixture(self.root / 'rig')
        path = self.root / 'rigged.glb'; export_gltf(run, path)
        ref = self.register({'runtime/asset.glb': (path, 'runtime')}, 'character')
        report = validate_asset(self.library, ref, requirements={'capabilities':['skin','animation']})
        item = report['geometry']['runtime/asset.glb']
        self.assertGreater(item['skeleton']['bones'], 0)
        self.assertTrue(item['skins'])
        self.assertTrue(item['clips'])
        self.assertEqual(item['clips'][0]['tracks'][0]['key_times_exact'][0], {'numerator':0,'denominator':1})
        signature = item['skeleton']['rest_sha256']
        validate_asset(self.library, ref, requirements={'skeleton_rest_sha256': signature})
        with self.assertRaisesRegex(RigError, 'structure/rest'):
            validate_asset(self.library, ref, requirements={'skeleton_rest_sha256':'0'*64})

    def test_material_texture_pixels_are_decoded_and_inspected(self):
        import io
        from PIL import Image
        builder = GltfBuilder()
        position = builder.accessor([[0,0,0],[1,0,0],[0,1,0]],'VEC3',bounds=True)
        uv = builder.accessor([[0,0],[1,0],[0,1]],'VEC2')
        image=io.BytesIO(); Image.new('RGB',(2,3),(20,40,60)).save(image,format='PNG')
        image_view=builder.view(image.getvalue())
        builder.json.update(images=[{'bufferView':image_view,'mimeType':'image/png'}],
            textures=[{'source':0}],materials=[{'pbrMetallicRoughness':{'baseColorTexture':{'index':0}}}],
            meshes=[{'primitives':[{'attributes':{'POSITION':position,'TEXCOORD_0':uv},'material':0}]}],
            nodes=[{'mesh':0}],scenes=[{'nodes':[0]}],scene=0)
        path=self.root/'textured.glb';builder.write(path)
        ref=self.register({'runtime/asset.glb':(path,'runtime')})
        item=validate_asset(self.library,ref,requirements={'capabilities':['texture']})['geometry']['runtime/asset.glb']
        self.assertEqual((item['textures'][0]['width'],item['textures'][0]['height']),(2,3))
        self.assertEqual(item['materials'][0]['pbrMetallicRoughness']['baseColorTexture']['index'],0)
        self.assertEqual(item['authored_materials'],1)
        self.assertIn('material', validate_asset(self.library,ref)['capabilities'])

    def test_source_geometry_without_animation_cannot_claim_animation_category(self):
        path=self.geometry()
        ref=self.register({'runtime/asset.glb':(path,'runtime')},'animation')
        with self.assertRaisesRegex(RigError,'category'):
            validate_asset(self.library,ref)

    def test_actual_missing_corrupt_bytes_fail_before_ready(self):
        import sqlite3
        path=self.geometry();ref=self.register({'runtime/asset.glb':(path,'runtime')})
        sha=self.library.resolve(ref)['files'][0]['sha256']
        with sqlite3.connect(self.library.database) as db:
            db.execute('UPDATE blobs SET payload=? WHERE sha256=?',(b'bad',sha))
        with self.assertRaises(RigError):validate_asset(self.library,ref)

    def test_invalid_mesh_indices_animation_targets_and_nonfinite_reject(self):
        cases=[lambda b:b.json['meshes'][0]['primitives'][0].update(indices=b.accessor([0,1,99],'SCALAR',5123)),
               lambda b:b.json['nodes'][0].update(translation=[float('inf'),0,0]),
               lambda b:b.json.update(animations=[{'samplers':[{'input':b.accessor([0,1],'SCALAR'),'output':b.accessor([[0,0,0],[1,0,0]],'VEC3')}], 'channels':[{'sampler':0,'target':{'node':99,'path':'translation'}}]}])]
        for modify in cases:
            with self.subTest(case=modify),self.assertRaises((RigError,ValueError)):
                inspect_geometry(self.geometry(malformed=modify))

    def test_tightened_resource_bounds_and_invalid_requirements_fail(self):
        path=self.geometry();ref=self.register({'runtime/asset.glb':(path,'runtime')})
        with self.assertRaises(RigError):validate_asset(self.library,ref,limits={'vertices':2})
        for requirements in [{'capabilities':['not-a-feature']},{'unknown':1},{'capabilities':'mesh'}]:
            with self.subTest(requirements=requirements),self.assertRaises(RigError):
                validate_asset(self.library,ref,requirements=requirements)
        for limits in [{'vertices':True},{'vertices':-1},{'vertices':10**12},{'other':1}]:
            with self.subTest(limits=limits),self.assertRaises(RigError):validate_asset(self.library,ref,limits=limits)

    def scene_files(self, *, prefab=True, change=None):
        names = ['triangle.obj','triangle.json', 'example-prefab.json' if prefab else 'example-scene.json']
        result = {}
        for name in names:
            data = (ROOT / 'examples/scene3d-contracts' / name).read_bytes()
            if change and name == 'triangle.json':
                value = json.loads(data); change(value)
                data = json.dumps(value,sort_keys=True,separators=(',',':')).encode()
            path = self.root / name;path.write_bytes(data)
            result[name] = (path, 'runtime' if name != 'triangle.obj' else 'geometry')
        if change:
            doc_name = names[-1]
            value = json.loads(result[doc_name][0].read_bytes())
            digest = hashlib.sha256(result['triangle.json'][0].read_bytes()).hexdigest()
            value['dependencies'][0]['sha256'] = digest
            for node in value['nodes']:
                if 'asset' in node:node['asset']['sha256'] = digest
            result[doc_name][0].write_text(json.dumps(value))
        return result

    def test_prefab_and_environment_verify_actual_child_geometry(self):
        ref = self.register(self.scene_files(), 'prefab')
        report = validate_asset(self.library,ref,requirements={'capabilities':['prefab','mesh']})
        self.assertEqual(report['geometry']['triangle.obj']['geometry']['vertices'],3)
        self.assertEqual(report['scene_documents'][0]['node_count'],2)
        files = self.scene_files(prefab=False)
        ref = self.register(files,'environment','2.0.0')
        report = validate_asset(self.library,ref,requirements={'capabilities':['scene']})
        self.assertEqual(report['scene_documents'][0]['world_matrices']['mesh'][3],[0,0,0,1])

    def test_prefab_resource_lies_and_missing_payload_do_not_verify(self):
        files = self.scene_files(change=lambda value:value['resources'].update(vertices=99))
        ref = self.register(files,'prefab')
        with self.assertRaisesRegex(RigError,'resources differ'):validate_asset(self.library,ref)
        files = self.scene_files()
        del files['triangle.obj']
        ref = self.register(files,'prefab','2.0.0')
        with self.assertRaisesRegex(RigError,'absent'):validate_asset(self.library,ref)

    def test_standalone_inspection_snapshots_exact_source_and_tightens_bytes(self):
        path = self.geometry()
        before = path.read_bytes()
        report = inspect_geometry(path)
        self.assertEqual(report['source_files'][path.name],
                         {'bytes': len(before), 'sha256': hashlib.sha256(before).hexdigest()})
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(RigError):
            inspect_geometry(path, limits={'dependency_bytes': len(before) - 1})

    def test_category_and_manifest_identity_are_bound_to_actual_payload(self):
        path = self.geometry()
        ref = self.register({'runtime/asset.glb': (path, 'runtime')})
        report = validate_asset(self.library, ref)
        self.assertEqual(report['manifest_sha256'], self.library.resolve(ref)['manifest_sha256'])
        self.assertGreater(report['limits']['vertices'], 0)
        for kind, message in [('texture', 'category'), ('material', 'category'), ('camera', 'adapter')]:
            ref = self.register({'runtime/asset.glb': (path, 'runtime')}, kind)
            with self.subTest(kind=kind), self.assertRaisesRegex(RigError, message):
                validate_asset(self.library, ref)

    def test_prefab_deep_hierarchy_is_rejected_without_python_recursion(self):
        files = self.scene_files()
        path = files['example-prefab.json'][0]
        document = json.loads(path.read_bytes())
        template = document['nodes'][0]
        document['nodes'] = []
        for index in range(130):
            node = deepcopy(template)
            node['id'] = 'node-' + str(index)
            node['parent'] = None if index == 0 else 'node-' + str(index - 1)
            document['nodes'].append(node)
        document['roots'] = ['node-0']
        path.write_text(json.dumps(document))
        ref = self.register(files, 'prefab')
        with self.assertRaisesRegex(RigError, 'hierarchy exceeds'):
            validate_asset(self.library, ref)

    def test_obj_is_geometry_only_and_reports_repairable_topology(self):
        path=self.root/'open.obj';path.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 0\nf 1 2 3\n')
        item=inspect_geometry(path)
        self.assertEqual(item['format'],'obj');self.assertEqual(item['clips'],[])
        self.assertEqual(item['meshes'][0]['boundary_edges'],3)
        self.assertEqual(item['independent_validation']['status'],'not-applicable')


if __name__=='__main__': unittest.main()
