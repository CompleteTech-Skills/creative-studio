"""Actual owner admission regressions, not rendering/visual approval."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))

from animation3d_assets import load_project
from animation3d_test_helpers import samples, LAYER
from rig_errors import RigError
from rig_test_helpers import fixture


class NativeClosureRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.values = samples(); self.project = self.values['project']
        self.project['budgets'].update(max_work_bytes=10000000,max_memory_bytes=10000000)

    def put(self, name, value, kind=None):
        path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value))
        return dict(path=name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    kind=kind or value['kind'],id=value['id'],version=value['version'])

    def check(self):
        self.put('animation-project.json',self.project)
        return load_project(self.root)

    def native_scene(self):
        shutil.copytree(Path(__file__).resolve().parents[1]/'examples/scene3d-contracts',self.root/'native')
        native=json.loads((self.root/'native/project.json').read_text())
        ref=self.put('native/project.json',native,'scene')
        scene=self.values['scene'];scene['scene_asset']=ref
        return scene,ref

    def bound_clip(self):
        rig=fixture(self.root,animated=False)
        manifest=json.loads((rig.root/'rig.json').read_text())
        ref=self.put('fixture/rig.json',manifest,'rig')
        clip=self.values['clip'];clip['rig_binding']=ref
        clip['tracks'][0]['target']['bone_id']='b0'
        self.project['rig_assets']=[ref]
        self.project['clips']=[self.put('clip.json',clip,'clip')]
        return rig,clip,ref

    def test_project_memory_caps_native_texture_capacity_before_materialization(self):
        import io
        from unittest.mock import patch
        from PIL import Image
        from rig_models import load_rig, TextureImage, save_geometry
        rig, clip, _ = self.bound_clip()
        stream = io.BytesIO()
        Image.new('RGB', (1024,1024), 'red').save(stream, format='PNG')
        rig.geometry.images = [TextureImage('texture', 'image/png', stream.getvalue())]
        rig.manifest['geometry'] = save_geometry(rig.root, rig.geometry, rig.manifest['limits'])
        reference = self.put('fixture/rig.json', rig.manifest, 'rig')
        clip['rig_binding'] = reference
        self.project['rig_assets'] = [reference]
        self.project['clips'] = [self.put('clip.json', clip, 'clip')]
        before = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaises(RigError) as native_rejection:
            load_rig(rig.root, limits={'memory_bytes': self.project['budgets']['max_memory_bytes']})
        self.assertEqual(native_rejection.exception.report['code'], 'RIG_LIMIT_MEMORY')
        with patch('rig_models.load_geometry', side_effect=AssertionError('native materialization before project admission')):
            with self.assertRaises(RigError) as rejected:
                self.check()
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')
        self.project['budgets']['max_memory_bytes'] = 64*1024*1024
        result = self.check()
        self.assertTrue(result['source_ready'])
        self.assertLessEqual(result['memory_admission']['estimated_bytes'], result['memory_admission']['limit_bytes'])
        self.assertGreater(result['memory_admission']['native_estimated_bytes'], 10_000_000)
        for name, digest in before.items():
            self.assertEqual(hashlib.sha256((self.root/name).read_bytes()).hexdigest(), digest)

    def test_project_memory_aggregates_distinct_retained_native_rigs(self):
        from unittest.mock import patch
        from rig_models import load_geometry, load_rig
        first, _, first_reference = self.bound_clip()
        second = fixture(self.root, name='second', animated=False)
        second_reference = self.put('second/rig.json', second.manifest, 'rig')
        self.project['rig_assets'] = [first_reference, second_reference]
        first_cost = load_rig(first.root).resource_usage['estimated_memory_bytes']
        second_cost = load_rig(second.root).resource_usage['estimated_memory_bytes']
        self.project['budgets']['max_memory_bytes'] = first_cost + second_cost - 1
        with self.assertRaises(RigError) as rejected:
            self.check()
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')
        self.project['budgets']['max_memory_bytes'] = 4*(first_cost + second_cost)
        with patch('rig_models.load_rig', wraps=load_rig) as native_loads:
            result = self.check()
        self.assertTrue(result['source_ready'])
        self.assertEqual(native_loads.call_count, 2)
        caps = [call.kwargs['limits']['memory_bytes'] for call in native_loads.call_args_list]
        self.assertGreater(caps[0], caps[1])
        self.assertGreaterEqual(result['memory_admission']['native_estimated_bytes'], first_cost + second_cost)
        self.assertLessEqual(result['memory_admission']['estimated_bytes'], result['memory_admission']['limit_bytes'])

    def test_cli_exact_relative_inventory_and_typed_hash_failure(self):
        rig, _, reference = self.bound_clip()
        self.project['budgets']['max_work_bytes'] = 500_000.0
        self.put('animation-project.json', self.project)
        before = {p.relative_to(self.root).as_posix():
                  {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size}
                  for p in self.root.rglob('*') if p.is_file()}
        invoked = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/animation3d.py'),
                   'admit-project', '--root', str(self.root)]
        success = subprocess.run(invoked, capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
        receipt = json.loads(success.stdout)
        self.assertTrue(receipt['source_ready'])
        manifest = json.loads((rig.root / 'rig.json').read_bytes())
        native_payloads = [manifest['geometry']['data'], *(skin['data'] for skin in manifest['skins']),
                           *manifest['source']['dependencies']]
        names = {'animation-project.json', 'clip.json', 'fixture/identity.json', 'fixture/rig.json'}
        names.update('fixture/' + item['path'] for item in native_payloads)
        expected = {name: before[name] for name in names}
        self.assertEqual(receipt['inventory'], expected)
        self.assertEqual(receipt['bytes'], sum(item['bytes'] for item in expected.values()))
        self.assertNotIn(str(self.root), success.stdout)
        self.assertNotIn('animation3d-project-', success.stdout)
        for name, item in before.items():
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), item['sha256'])
        manifest = rig.root / 'rig.json'
        manifest.write_bytes(manifest.read_bytes() + b' ')
        invalid = subprocess.run(invoked, capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2, invalid.stdout + invalid.stderr)
        self.assertEqual(json.loads(invalid.stdout)['error']['code'], 'ANIMATION_HASH')
        self.assertNotIn(str(self.root), invalid.stdout)
        self.assertNotIn('animation3d-project-', invalid.stdout)
        self.assertNotEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(), reference['sha256'])

    def test_rig_opaque_source_and_evidence_follow_native_hash_authority(self):
        from animation3d_registry import TargetRegistry
        from rig_models import load_rig

        rig, clip, _ = self.bound_clip()

        def opaque(name, value):
            path = rig.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
            return dict(path=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())

        sidecar = opaque('source/sidecar.json', [{'value': 'opaque source'}])
        validation = opaque('validation/report.json', ['opaque validation'])
        evidence = opaque('evidence/data.json', [{'path': 'unrelated.json', 'sha256': '0' * 64}])
        review = opaque('review/receipt', dict(schema_version=1, kind='hatch-3d-rig-review',
            rig_id=rig.manifest['id'], rig_version=rig.manifest['version'], expected_sha256='0' * 64,
            author='synthetic fixture', reviewer_kind='synthetic-test', created_at='2026-10-23T00:00:00Z',
            fixture=True, evidence=[evidence], dimensions={}))
        rig.manifest['source']['dependencies'].append(sidecar)
        rig.manifest['validation'].append(validation)
        rig.manifest['review']['receipts'].append(review)
        reference = self.put('fixture/rig.json', rig.manifest, 'rig')
        clip['rig_binding'] = reference
        self.project['rig_assets'] = [reference]
        self.project['clips'] = [self.put('clip.json', clip, 'clip')]

        load_rig(rig.root)
        admitted = self.check()
        self.assertTrue(admitted['source_ready'])
        self.assertTrue(TargetRegistry.from_rig(rig.root, 'actor').document['entries'])
        cases = [(sidecar, 'RIG_SOURCE_CHANGED'), (validation, 'RIG_EVIDENCE_CHANGED'),
                 (review, 'RIG_EVIDENCE_CHANGED'), (evidence, 'RIG_EVIDENCE_CHANGED')]
        for descriptor, code in cases:
            with self.subTest(descriptor=descriptor['path']):
                path = rig.root / descriptor['path']
                original = path.read_bytes()
                self.assertEqual(admitted['inventory']['fixture/' + descriptor['path']],
                                 {'sha256': descriptor['sha256'], 'bytes': len(original)})
                path.write_bytes(original + b' ')
                try:
                    for load in (lambda: load_rig(rig.root), self.check,
                                 lambda: TargetRegistry.from_rig(rig.root, 'actor')):
                        with self.assertRaises(RigError) as rejected:
                            load()
                        self.assertEqual(rejected.exception.report['code'], code)
                finally:
                    path.write_bytes(original)

    def test_cli_admission_errors_do_not_emit_upstream_paths(self):
        missing_root = self.root / 'source-host-directory'
        invoked = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/animation3d.py'),
                   'admit-project', '--root', str(missing_root)]
        result = subprocess.run(invoked, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'ANIMATION_OWNER')
        self.assertNotIn(str(missing_root), result.stdout)

    def test_lookdev_receives_only_its_own_payloads_in_either_owner_order(self):
        from unittest.mock import patch
        from animation3d_assets import Snapshot
        from lookdev_contracts import Registry
        self.bound_clip()
        references = self.project['rig_assets']
        base = Path(__file__).resolve().parents[1] / 'examples/lookdev-contracts'
        material = json.loads((base / 'ceramic.json').read_bytes())
        texture = json.loads((base / 'albedo.json').read_bytes())
        pixels = (base / texture['file']['path']).read_bytes()
        texture['file']['path'] = 'albedo-pixels.json'
        (self.root / texture['file']['path']).write_bytes(pixels)
        texture_ref = self.put('albedo-descriptor', texture)
        material['dependencies'] = [texture_ref]
        material_ref = self.put('material.json', material)

        for refs in ([*references, material_ref], [material_ref, *references]):
            with self.subTest(order=[ref['kind'] for ref in refs]), tempfile.TemporaryDirectory() as capture:
                snapshot = Snapshot(self.root, capture)
                with patch('lookdev_contracts.Registry', wraps=Registry) as native_registry:
                    for reference in refs:
                        snapshot.native(reference['path'], reference)
                self.assertEqual(native_registry.call_count, 1)
                self.assertEqual(native_registry.call_args.kwargs['payloads'], {'albedo-pixels.json': pixels})
                self.assertIn('fixture/rig.json', snapshot.inventory)

    def test_scene_descriptor_roles_preserve_opaque_json_and_legacy_project(self):
        from production_fixture import build_fixture_project
        from rig_load_limits import _JSON_FACTOR
        from scene3d_contracts import load_project as load_scene_project

        scene, _ = self.native_scene()
        scene_root = self.root / 'native'
        native = json.loads((scene_root / 'project.json').read_bytes())
        validation_path = 'opaque-report.json'
        payload = json.dumps([{'path': 'unrelated.json', 'sha256': '0' * 64}]).encode()
        (scene_root / validation_path).write_bytes(payload)
        native['validation'] = [dict(path=validation_path, sha256=hashlib.sha256(payload).hexdigest())]
        build_fixture_project(scene_root / 'legacy')
        legacy_path = 'legacy/project.json'
        legacy_bytes = (scene_root / legacy_path).read_bytes()
        legacy = json.loads(legacy_bytes)
        reference = dict(path=legacy_path, sha256=hashlib.sha256(legacy_bytes).hexdigest(),
                         kind='project', id=legacy['id'], version=legacy['version'])
        native['linked_2d_project'] = reference
        native['dependency_lock'].append(reference)

        scene['scene_asset'] = self.put('native/project.json', native, 'scene')
        self.project['scene_assets'] = [self.put('scene.json', scene, 'scene')]
        native_closure = load_scene_project(scene_root)
        admitted = self.check()
        expected = {'native/' + path: item for path, item in native_closure['inventory'].items()}
        for name in ('animation-project.json', 'scene.json'):
            data = (self.root / name).read_bytes()
            expected[name] = dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        self.assertEqual(admitted['inventory'], expected)
        decoded = {'animation-project.json', 'scene.json', 'native/' + legacy_path}
        decoded.update('native/' + name for name in native_closure['documents'])
        expected_memory = sum(item['bytes'] * (_JSON_FACTOR if name in decoded else 1)
                              for name, item in expected.items())
        self.assertEqual(admitted['memory_admission']['estimated_bytes'], expected_memory)

    def test_project_rejects_non_native_zip_compression_before_opening_member(self):
        import io
        import zipfile
        from unittest.mock import patch
        import numpy as np

        rig, _, _ = self.bound_clip()
        normal = rig.manifest['geometry']['data']
        path = rig.root / normal['path']
        member = io.BytesIO()
        np.save(member, np.array([1], dtype=np.int8), allow_pickle=False)
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_BZIP2) as bundle:
            bundle.writestr('metadata.json', '{}')
            bundle.writestr('array.npy', member.getvalue())
        normal['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        reference = self.put('fixture/rig.json', rig.manifest, 'rig')
        self.project['rig_assets'] = [reference]
        self.project['clips'] = []
        with patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('opened an unsupported member')):
            with self.assertRaises(RigError) as rejected:
                self.check()
        self.assertEqual(rejected.exception.report['code'], 'RIG_BINARY')

    def test_native_decoder_error_uses_binary_diagnostic(self):
        from unittest.mock import patch
        import zlib

        self.bound_clip()
        with patch('rig_models.load_rig', side_effect=zlib.error('Invalid deflate stream')):
            with self.assertRaises(RigError) as rejected:
                self.check()
        self.assertEqual(rejected.exception.report['code'], 'RIG_BINARY')

    def test_camera_suffixless_documents_keep_exact_project_inventory_and_charge(self):
        from rig_load_limits import _JSON_FACTOR

        base = Path(__file__).resolve().parents[1] / 'examples/camera3d'
        shutil.copytree(base, self.root / 'camera')
        registry_path = self.root / 'camera/registry.json'
        registry = json.loads(registry_path.read_bytes())
        original = self.root / 'camera/main-camera.json'
        new_name = 'main-camera'
        original.rename(original.with_name(new_name))
        next(ref for ref in registry['resources'] if ref['path'] == original.name)['path'] = new_name
        reference = self.put('camera/registry.json', registry, 'render-profile')
        self.project['default_render_profile'] = reference
        admitted = self.check()
        paths = {'animation-project.json', 'camera/registry.json'}
        paths.update('camera/' + ref['path'] for ref in registry['resources'])
        expected = {name: dict(sha256=hashlib.sha256((self.root / name).read_bytes()).hexdigest(),
                               bytes=(self.root / name).stat().st_size) for name in paths}
        self.assertEqual(admitted['inventory'], expected)
        expected_memory = sum(item['bytes'] for item in expected.values()) * _JSON_FACTOR
        self.assertEqual(admitted['memory_admission']['estimated_bytes'], expected_memory)

    def test_project_pins_rig_manifest_and_applies_remaining_geometry_caps(self):
        from unittest.mock import patch
        from rig_models import load_geometry, load_rig
        first, _, first_ref = self.bound_clip()
        second = fixture(self.root, name='second', animated=False)
        second_ref = self.put('second/rig.json', second.manifest, 'rig')
        self.project['rig_assets'] = [first_ref, second_ref]
        with patch('rig_models.load_rig', wraps=load_rig) as native_loads:
            self.assertTrue(self.check()['source_ready'])
        self.assertEqual(native_loads.call_count, 2)
        first_call, second_call = native_loads.call_args_list
        for call, ref in zip((first_call, second_call), (first_ref, second_ref)):
            self.assertEqual(call.kwargs['expected_manifest_sha256'], ref['sha256'])
            self.assertEqual(call.kwargs['limits']['bones'], self.project['budgets']['max_bones_per_rig'])
        self.assertEqual(second_call.kwargs['limits']['vertices'],
                         first_call.kwargs['limits']['vertices'] - first.geometry.vertex_count)
        self.assertEqual(second_call.kwargs['max_total_morph_targets'],
                         first_call.kwargs['max_total_morph_targets'] - 1)
        self.project['budgets']['max_morph_targets'] = 1
        with patch('rig_models.load_geometry', wraps=load_geometry) as geometry_loads:
            with self.assertRaises(RigError) as rejected:
                self.check()
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')
        self.assertIn('morph budget', rejected.exception.report['message'])
        self.assertEqual(geometry_loads.call_count, 1)

        self.project['budgets'].update(max_morph_targets=100, max_vertices=first.geometry.vertex_count)
        with patch('rig_models.load_geometry', wraps=load_geometry) as geometry_loads:
            with self.assertRaises(RigError) as rejected:
                self.check()
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')
        self.assertIn('vertex/morph budget', rejected.exception.report['message'])
        self.assertEqual(geometry_loads.call_count, 1)

    def test_json_object_capacity_is_admitted_before_dependency_decode(self):
        from unittest.mock import patch
        from animation3d_assets import Snapshot
        for suffix in ('.json', '.data'):
            with self.subTest(suffix=suffix):
                source = self.root/('large'+suffix)
                source.write_text(json.dumps(['small']*100))
                snapshot = Snapshot(self.root, self.root/('snapshot'+suffix))
                snapshot.memory_limit = 1000
                self.assertLess(source.stat().st_size, snapshot.memory_limit)
                with patch('animation3d_assets.decode_json', side_effect=AssertionError('decoder ran before capacity admission')):
                    with self.assertRaises(RigError) as rejected:
                        snapshot.json(source.name)
                self.assertEqual(rejected.exception.report['code'], 'ANIMATION_LIMIT')
        # Valid JSON with a non-JSON filename still uses the same decoder policy.
        source = self.root/'valid.data'
        source.write_text(json.dumps({'ok': True}))
        snapshot = Snapshot(self.root, self.root/'snapshot-valid')
        self.assertEqual(snapshot.json(source.name), {'ok': True})

    def test_unbound_or_unknown_skeletal_target_rejects(self):
        clip=self.values['clip'];self.project['clips']=[self.put('clip.json',clip,'clip')]
        with self.assertRaisesRegex(RigError,'rig binding'):self.check()
        _,clip,_=self.bound_clip()
        self.assertTrue(self.check()['source_ready'])
        clip['tracks'][0]['target']['bone_id']='absent'
        self.project['clips']=[self.put('clip.json',clip,'clip')]
        with self.assertRaisesRegex(RigError,'Bone is absent'):self.check()

    def test_project_native_bone_vertex_and_morph_budgets(self):
        self.bound_clip()
        for key in ('max_bones_per_rig','max_vertices'):
            previous=self.project['budgets'][key];self.project['budgets'][key]=1
            with self.subTest(key=key),self.assertRaisesRegex(RigError,'budget'):self.check()
            self.project['budgets'][key]=previous
        self.assertTrue(self.check()['source_ready'])

    def test_project_memory_and_native_node_budgets(self):
        scene,_=self.native_scene();self.project['scene_assets']=[self.put('scene.json',scene,'scene')]
        self.assertTrue(self.check()['source_ready'])
        self.project['budgets']['max_scene_nodes']=1
        with self.assertRaisesRegex(RigError,'node budget'):self.check()
        self.project['budgets']['max_scene_nodes']=100
        self.project['budgets']['max_vertices']=1
        with self.assertRaisesRegex(RigError,'vertex budget'):self.check()
        self.project['budgets']['max_vertices']=100
        self.project['budgets']['max_memory_bytes']=1
        with self.assertRaisesRegex(RigError,'memory'):self.check()

    def test_contextual_rig_and_shot_placement_bindings(self):
        _,clip,rigref=self.bound_clip();scene,nativeref=self.native_scene()
        layer=deepcopy(LAYER)
        actor=dict(actor_id='actor',model={**nativeref,'kind':'model'},rig=rigref,
                   initial_transform=dict(position=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1]),
                   animation_layers=[layer],constraints=[])
        scene['actors']=[actor]
        self.project['scene_assets']=[self.put('scene.json',scene,'scene')]
        sequence=self.values['sequence']
        value=dict(id='placement',clip=self.project['clips'][0],actor_id='actor',layer_id='body',
                   start_frame=0,duration_frames=24,source_in_frame=0,source_out_frame=24,
                   playback_rate=dict(numerator=1,denominator=1),blend_in_frames=0,blend_out_frames=0,weight=1,enabled=True)
        sequence['shots'][0]['clip_placements']=[value]
        def refresh():self.project['sequences']=[self.put('sequence.json',sequence,'sequence')]
        refresh();self.assertTrue(self.check()['source_ready'])
        for key,bad in (('source_in_frame',1000),('source_out_frame',1000),('actor_id','absent'),('layer_id','absent')):
            previous=value[key];value[key]=bad;refresh()
            with self.subTest(key=key),self.assertRaises(RigError):self.check()
            value[key]=previous
        refresh()
        # Clip binding is optional only when a real placement supplies actor.rig.
        del clip['rig_binding'];self.project['clips']=[self.put('clip.json',clip,'clip')]
        value['clip']=self.project['clips'][0];refresh()
        self.assertTrue(self.check()['source_ready'])

    def test_shot_camera_resolves_once_across_bound_native_scenes(self):
        scene, _ = self.native_scene()
        native_root = self.root / 'native'
        native = json.loads((native_root / 'project.json').read_bytes())
        second = json.loads((native_root / 'example-scene.json').read_bytes())
        second.update(id='second-scene', shots=[])
        camera = next(node for node in second['nodes'] if node['type'] == 'camera')
        sequence = self.values['sequence']

        def admit(second_camera_id, active_camera_id, *, register_second=True):
            camera['id'] = second_camera_id
            second['cameras'] = [second_camera_id]
            second_ref = self.put('native/second-scene.json', second)
            second_ref['path'] = 'second-scene.json'
            native['scenes'] = native['scenes'][:1] + ([second_ref] if register_second else [])
            native['dependency_lock'] = [ref for ref in native['dependency_lock']
                                         if ref['id'] != second['id']] + [second_ref]
            scene['scene_asset'] = self.put('native/project.json', native, 'scene')
            self.project['scene_assets'] = [self.put('scene.json', scene, 'scene')]
            sequence['shots'][0]['active_camera_id'] = active_camera_id
            self.project['sequences'] = [self.put('sequence.json', sequence, 'sequence')]
            return self.check()

        self.assertTrue(admit('second-camera', 'camera')['source_ready'])
        self.assertTrue(admit('second-camera', 'second-camera')['source_ready'])
        for second_camera_id, active_camera_id in (('second-camera', 'root'), ('camera', 'camera')):
            with self.subTest(second_camera_id=second_camera_id, active_camera_id=active_camera_id):
                with self.assertRaises(RigError) as rejected:
                    admit(second_camera_id, active_camera_id)
                self.assertEqual(rejected.exception.report['code'], 'ANIMATION_TARGET')

        admitted = admit('camera', 'camera', register_second=False)
        self.assertTrue(admitted['source_ready'])
        self.assertEqual(admitted['inventory']['native/second-scene.json']['sha256'],
                         native['dependency_lock'][-1]['sha256'])
        with self.assertRaises(RigError) as unregistered:
            admit('second-camera', 'second-camera', register_second=False)
        self.assertEqual(unregistered.exception.report['code'], 'ANIMATION_TARGET')

    def test_actor_retarget_profile_pins_its_actual_target_rig(self):
        from rig_errors import object_hash

        actor_rig, _, actor_ref = self.bound_clip()
        source = fixture(self.root, name='retarget-source', animated=False)
        source_ref = self.put('retarget-source/rig.json', source.manifest, 'rig')
        profile = self.values['retarget']
        profile.update(source_rig=source_ref, target_rig=actor_ref,
                       source_skeleton_signature=object_hash(source.skeleton),
                       target_skeleton_signature=object_hash(actor_rig.skeleton))
        profile['mappings'][0].update(source_bone='b0', target_bone='b0')
        profile_ref = self.put('retarget.json', profile, 'retarget')
        scene, native = self.native_scene()
        actor = dict(actor_id='actor', model={**native, 'kind': 'model'}, rig=actor_ref,
                     initial_transform=dict(position=[0, 0, 0], rotation=[0, 0, 0, 1], scale=[1, 1, 1]),
                     animation_layers=[], constraints=[], retarget_profile=profile_ref)
        scene['actors'] = [actor]
        self.project['scene_assets'] = [self.put('scene.json', scene, 'scene')]
        self.assertTrue(self.check()['source_ready'])
        actor['rig'] = source_ref
        self.project['scene_assets'] = [self.put('scene.json', scene, 'scene')]
        with self.assertRaisesRegex(RigError, 'retarget profile target rig') as rejected:
            self.check()
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_TARGET')

    def test_character_state_binding_requires_canonical_assignment_owner(self):
        _, _, rig_ref = self.bound_clip()
        self.project['character_assets'] = [{**rig_ref, 'kind': 'character'}]
        machine = self.values['state-machine']
        machine['states'][0]['clip'] = self.project['clips'][0]
        reference = self.put('machine.json', machine, 'state-machine')
        self.project['state_machines'] = [reference]
        self.assertTrue(self.check()['source_ready'])
        self.project['character_state_bindings'] = {rig_ref['id']: reference}
        with self.assertRaisesRegex(RigError, 'character-to-actor owner') as rejected:
            self.check()
        self.assertEqual(rejected.exception.report['code'], 'ANIMATION_OWNER')

    def test_scene_track_rejects_missing_native_node(self):
        scene,_=self.native_scene();track=deepcopy(self.values['clip']['tracks'][0])
        track['target']=dict(kind='node',node_id='root',property='translation');scene['object_tracks']=[track]
        self.project['scene_assets']=[self.put('scene.json',scene,'scene')]
        self.assertTrue(self.check()['source_ready'])
        track['target']['node_id']='absent'
        self.project['scene_assets']=[self.put('scene.json',scene,'scene')]
        with self.assertRaisesRegex(RigError,'absent or ambiguous'):self.check()

    def test_native_wrong_json_type_rejects_with_typed_error(self):
        path=self.root/'bad.json';path.write_text('[]')
        self.project['rig_assets']=[dict(path='bad.json',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                       kind='rig',id='bad',version='1.0.0')]
        with self.assertRaises(RigError):self.check()

    def test_binary_expansion_and_false_array_header_reject_before_load(self):
        import io
        import zipfile
        import numpy as np
        from animation3d_assets import Snapshot
        for forged_header in (False,True):
            stream=io.BytesIO()
            if forged_header:
                np.lib.format.write_array_header_1_0(stream,dict(descr='<f8',fortran_order=False,shape=(2000000,)))
                payload=stream.getvalue()
            else:payload=b'0'*10000
            with zipfile.ZipFile(self.root/'payload.npz','w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('data.npy',payload)
            for name in ('payload.npz','renamed.bin'):
                if name != 'payload.npz': shutil.copyfile(self.root/'payload.npz',self.root/name)
                with tempfile.TemporaryDirectory() as target:
                    snapshot=Snapshot(self.root,target);snapshot.memory_limit=1000
                    with self.subTest(forged_header=forged_header,name=name),self.assertRaises(RigError):snapshot.copy(name)

    def test_supported_foreign_properties_and_typed_corrupt_native_error(self):
        from animation3d_owners import resolve_property
        base=Path(__file__).resolve().parents[1]
        shutil.copytree(base/'examples/camera3d',self.root/'camera')
        camera=json.loads((self.root/'camera/registry.json').read_text())
        camera_ref=self.put('camera/registry.json',camera)
        material=json.loads((base/'examples/lookdev-contracts/ceramic.json').read_text())
        material['surface']['emission']=dict(color=[1,0,0],strength_watts_per_square_meter=2)
        material_ref=self.put('material.json',material)
        light=json.loads((base/'examples/lookdev-contracts/key.json').read_text())
        light_ref=self.put('light.json',light)
        for owner,source,domain,identifier,properties in (
            ('camera3d',camera_ref,'camera','camera',('translation','rotation','focal_length','focus_distance','aperture')),
            ('lookdev',material_ref,'material',material['id'],('base_color','opacity','metallic','roughness','emissive_color','emissive_intensity')),
            ('lookdev',light_ref,'light',light['id'],('translation','rotation','color','intensity'))):
            for prop in properties:
                value=dict(kind=domain,property=prop,**{domain+'_id':identifier})
                with self.subTest(domain=domain,prop=prop):
                    self.assertIsNotNone(resolve_property(self.root,dict(owner=owner,source=source,target=value)))
        (self.root/'camera/optics.json').write_text('{}')
        with self.assertRaises(RigError):
            resolve_property(self.root,dict(owner='camera3d',source=camera_ref,
                target=dict(kind='camera',camera_id='camera',property='focal_length')))

    def test_aggregate_morph_budget_and_missing_native_skeleton(self):
        _,clip,_=self.bound_clip()
        other=fixture(self.root,name='other',animated=False)
        manifest=json.loads((other.root/'rig.json').read_text())
        self.project['rig_assets'].append(self.put('other/rig.json',manifest,'rig'))
        self.project['budgets']['max_morph_targets']=1
        with self.assertRaisesRegex(RigError,'morph budget'):self.check()
        self.project['budgets']['max_morph_targets']=100
        self.assertTrue(self.check()['source_ready'])
        manifest['skeleton']=None
        self.project['rig_assets'][-1]=self.put('other/rig.json',manifest,'rig')
        with self.assertRaises(RigError):self.check()

    def test_scene_visibility_and_unsupported_camera_properties(self):
        from animation3d_owners import resolve_property
        scene,ref=self.native_scene()
        ref['kind']='scene3d-project'
        value=resolve_property(self.root,dict(owner='scene3d',source=ref,
             target=dict(kind='node',node_id='root',property='visibility')))
        self.assertIs(type(value),bool)
        base=Path(__file__).resolve().parents[1]
        shutil.copytree(base/'examples/camera3d',self.root/'camera')
        ref=self.put('camera/registry.json',json.loads((self.root/'camera/registry.json').read_text()))
        for prop in ('exposure','field_of_view','orthographic_size'):
            with self.subTest(prop=prop),self.assertRaises(RigError):
                resolve_property(self.root,dict(owner='camera3d',source=ref,
                    target=dict(kind='camera',camera_id='camera',property=prop)))

    def test_native_identity_aliases_reject(self):
        rig,_,_=self.bound_clip()
        shutil.copytree(rig.root,self.root/'alias')
        manifest=json.loads((self.root/'alias/rig.json').read_text())
        self.project['rig_assets'].append(self.put('alias/rig.json',manifest,'rig'))
        # A list can contain duplicate native identities only via another typed
        # reference path; the project schema rejects duplicate IDs in one list.
        self.project['rig_assets'].pop()
        self.project['character_assets']=[self.put('alias/rig.json',manifest,'character')]
        with self.assertRaisesRegex(RigError,'Duplicate native'):self.check()
