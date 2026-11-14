"""Real-file editor/native closure regression tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from editor3d_closure import verify_editor_bundle
from editor3d_contracts import contract_hash
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]


def example(name):
    return json.loads((ROOT / 'examples/editor3d-contracts' / (name + '.json')).read_text())


def reference(root, relative, owner):
    payload = (root / relative).read_bytes()
    value = json.loads(payload)
    return dict(owner=owner, id=value['id'], version=value.get('version', '1.0.0'),
        native_kind=value.get('kind', 'Rig'), file=dict(path=relative,
        sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload), media_type='application/json'))


def bundle(root):
    source = reference(root, 'native/project.json', 'scene3d')
    native = reference(root, 'native/example-scene.json', 'scene3d')
    project, scene = example('project'), example('scene')
    scene['native'] = native
    scene['animations'] = []
    scene['default_camera'] = 'camera'
    nodes = []
    for name, parent, kind, order in [('root', None, 'group', 0), ('mesh', 'root', 'mesh', 0), ('camera', 'root', 'camera', 1)]:
        node = example('node');node.update(id=name, native=deepcopy(native), native_node_id=name,
            node_type=kind, parent=parent, sibling_order=order, asset_id=None)
        nodes.append(node)
    scene['nodes'] = [dict(id=n['id'], sha256=contract_hash(n)) for n in nodes]
    project.update(scenes=[dict(id=scene['id'], sha256=contract_hash(scene))], shared_assets=[],
                   animation_clips=[], source_projects=[source], dependency_manifest=[source,native])
    return project, [scene], nodes, [], []


class EditorNativeClosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / 'examples/scene3d-contracts', self.root / 'native')
        self.bundle = bundle(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_real_native_scene_payload_and_binding_closure(self):
        result = verify_editor_bundle(*self.bundle, self.root)
        self.assertEqual(result['status'], 'verified-native-closure')
        self.assertTrue(result['native_resources_verified'])
        self.assertFalse(result['rendered_output_verified'])
        self.assertEqual(result['review_status'], 'unreviewed')
        self.assertIn('native/triangle.obj', result['files'])
        self.assertGreater(result['bytes'], 0)
        self.assertEqual(result['native_reports']['scene3d:native/project.json']['status'], 'hash-and-contract-verified')

    def test_missing_or_corrupt_real_geometry_fails(self):
        target = self.root / 'native/triangle.obj'
        target.write_bytes(b'bad geometry bytes')
        with self.assertRaises(RigError):
            verify_editor_bundle(*self.bundle, self.root)
        target.unlink()
        with self.assertRaises(RigError):
            verify_editor_bundle(*self.bundle, self.root)

    def test_node_identity_and_native_parent_must_agree(self):
        for field, value in [('native_node_id', 'missing'), ('parent', None)]:
            bad = deepcopy(self.bundle);bad[2][1][field] = value
            bad[1][0]['nodes'] = [dict(id=n['id'], sha256=contract_hash(n)) for n in bad[2]]
            bad[0]['scenes'] = [dict(id=bad[1][0]['id'], sha256=contract_hash(bad[1][0]))]
            with self.subTest(field=field), self.assertRaises(RigError):
                verify_editor_bundle(*bad, self.root)

    def test_native_source_project_is_required(self):
        bad = deepcopy(self.bundle);bad[0]['source_projects'] = []
        with self.assertRaises(RigError):
            verify_editor_bundle(*bad, self.root)

    def test_symlink_payload_is_rejected(self):
        source = self.root / 'native/triangle.obj'; original=source.read_bytes();source.unlink()
        external=self.root/'elsewhere.obj';external.write_bytes(original)
        try:source.symlink_to(external)
        except OSError as exc:self.skipTest(str(exc))
        with self.assertRaises(RigError):verify_editor_bundle(*self.bundle,self.root)

    def test_source_mutation_after_native_validation_is_rejected(self):
        import scene3d_contracts
        actual = scene3d_contracts.load_project
        def mutate(*args, **kwargs):
            result = actual(*args, **kwargs)
            target=self.root/'native/triangle.obj';target.write_bytes(target.read_bytes()+b'\n')
            return result
        with patch.object(scene3d_contracts,'load_project',side_effect=mutate):
            with self.assertRaises(RigError):verify_editor_bundle(*self.bundle,self.root)

    def test_real_camera_registry_closure(self):
        shutil.copytree(ROOT/'examples/camera3d',self.root/'camera')
        source=reference(self.root,'camera/registry.json','camera3d')
        camera=reference(self.root,'camera/main-camera.json','camera3d')
        project=deepcopy(self.bundle[0]);project['source_projects'].append(source)
        project['dependency_manifest'].extend([source,camera])
        result=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertIn('camera/optics.json',result['files'])
        self.assertIn('camera3d:camera/registry.json',result['native_reports'])

    def test_real_material_texture_closure(self):
        shutil.copytree(ROOT/'examples/lookdev-contracts',self.root/'lookdev')
        material=reference(self.root,'lookdev/ceramic.json','lookdev')
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(material)
        result=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertIn('lookdev:lookdev/ceramic.json',result['native_reports'])

    def test_dependency_manifest_cannot_omit_native_bindings(self):
        bad = deepcopy(self.bundle);bad[0]['dependency_manifest'] = []
        with self.assertRaises(RigError) as raised:
            verify_editor_bundle(*bad,self.root)
        self.assertEqual(raised.exception.report['code'],'EDITOR3D_LOCK')

    def test_node_kind_cannot_misrepresent_native_geometry(self):
        bad = deepcopy(self.bundle);bad[2][1]['node_type']='camera'
        bad[1][0]['nodes']=[dict(id=n['id'],sha256=contract_hash(n)) for n in bad[2]]
        bad[0]['scenes']=[dict(id=bad[1][0]['id'],sha256=contract_hash(bad[1][0]))]
        with self.assertRaises(RigError):verify_editor_bundle(*bad,self.root)

    def test_real_model_decoding_is_delegated_and_missing_export_fails(self):
        from model3d_contracts import file_references
        relative='examples/model3d-contracts/robotic.json'
        native=json.loads((ROOT/relative).read_bytes())
        paths={relative,*[item['path'] for item in file_references(native)]}
        for path in paths:
            target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/path,target)
        model=reference(self.root,relative,'model3d')
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(model)
        report=verify_editor_bundle(project,*self.bundle[1:],self.root)
        model_report=report['native_reports']['model3d:'+relative]
        self.assertTrue(model_report['geometry_inspected'])
        self.assertTrue(model_report['payloads_verified'])
        self.assertIn('model3d',report['geometry_inspected_owners'])
        (self.root/'examples/rig-3d/robotic/exports/character.glb').unlink()
        with self.assertRaises(RigError):verify_editor_bundle(project,*self.bundle[1:],self.root)

    def test_real_texture_bytes_are_required(self):
        shutil.copytree(ROOT/'examples/lookdev-contracts',self.root/'lookdev')
        texture=reference(self.root,'lookdev/albedo.json','lookdev')
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(texture)
        report=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertIn('lookdev/textures/albedo.png',report['files'])
        (self.root/'lookdev/textures/albedo.png').write_bytes(b'corrupt')
        with self.assertRaises(RigError):verify_editor_bundle(project,*self.bundle[1:],self.root)

    def test_native_rig_payloads_are_reopened_and_numerically_validated(self):
        shutil.copytree(ROOT/'examples/rig-3d/robotic',self.root/'rig')
        native=reference(self.root,'rig/rig.json','rig');native['native_kind']='Rig'
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(native)
        report=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertTrue(report['native_reports']['rig:rig/rig.json']['ok'])
        self.assertIn('rig',report['geometry_inspected_owners'])
        self.assertIn('rig/identity.json',report['files'])
        (self.root/'rig/identity.json').write_bytes(b'{}')
        with self.assertRaises(RigError):verify_editor_bundle(project,*self.bundle[1:],self.root)

    def test_existing_production_project_reopens_with_original_sources(self):
        from production_fixture import build_fixture_project
        build_fixture_project(self.root/'production')
        source=reference(self.root,'production/project.json','production2d')
        project=deepcopy(self.bundle[0]);project['source_projects'].append(source)
        project['dependency_manifest'].append(source)
        report=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertGreater(report['native_reports']['production2d:production/project.json']['resources']['assets'],0)

    def test_manifest_media_type_controls_parsing_after_rename(self):
        shutil.copyfile(ROOT/'examples/lookdev-contracts/ceramic.json',self.root/'ceramic.data')
        material=reference(self.root,'ceramic.data','lookdev')
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(material)
        report=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertIn('lookdev:ceramic.data',report['native_reports'])

    def test_root_level_material_dependency_uses_same_registry_scope(self):
        for name in ('blue-coat.json','ceramic.json'):
            shutil.copyfile(ROOT/'examples/lookdev-contracts'/name,self.root/name)
        material=reference(self.root,'blue-coat.json','lookdev')
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(material)
        report=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertEqual(report['native_reports']['lookdev:blue-coat.json']['resources'],2)

    def test_native_light_cannot_be_misrepresented_as_mesh(self):
        scene_path=self.root/'native/example-scene.json'
        native=json.loads(scene_path.read_bytes());native['lights']=['mesh']
        node=native['nodes'][1];node['type']='light';node.pop('asset')
        node['light']=dict(type='point',color=[1,1,1],intensity=100)
        payload=json.dumps(native).encode();scene_path.write_bytes(payload)
        digest=hashlib.sha256(payload).hexdigest()
        project_path=self.root/'native/project.json'
        native_project=json.loads(project_path.read_bytes())
        for key in ('scenes','dependency_lock'):
            for ref in native_project[key]:
                if ref['path']=='example-scene.json':ref['sha256']=digest
        project_path.write_text(json.dumps(native_project))
        # Rebind the real shot/render/project dependency DAG after this native edit.
        def rebind(value):
            if isinstance(value,dict):
                if 'path' in value and 'sha256' in value:
                    target=self.root/'native'/value['path']
                    if target.is_file():value['sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
                for child in value.values():rebind(child)
            elif isinstance(value,list):
                for child in value:rebind(child)
        for _ in range(5):
            for path in (self.root/'native').glob('*.json'):
                value=json.loads(path.read_bytes());rebind(value);path.write_text(json.dumps(value))
        edited=bundle(self.root)
        with self.assertRaises(RigError) as raised:
            verify_editor_bundle(*edited,self.root)
        self.assertEqual(raised.exception.report['code'],'EDITOR3D_NODE')
        edited[2][1]['node_type']='point-light'
        edited[1][0]['nodes']=[dict(id=n['id'],sha256=contract_hash(n)) for n in edited[2]]
        edited[0]['scenes']=[dict(id=edited[1][0]['id'],sha256=contract_hash(edited[1][0]))]
        self.assertTrue(verify_editor_bundle(*edited,self.root)['native_resources_verified'])

    def test_binary_texture_named_json_is_preserved_without_json_parsing(self):
        shutil.copytree(ROOT/'examples/lookdev-contracts',self.root/'lookdev')
        metadata=self.root/'lookdev/albedo.json'
        value=json.loads(metadata.read_bytes())
        pixels=self.root/'lookdev/textures/albedo.png'
        renamed=self.root/'lookdev/textures/pixels.json'
        pixels.rename(renamed)
        value['file']['path']='textures/pixels.json'
        metadata.write_text(json.dumps(value))
        texture=reference(self.root,'lookdev/albedo.json','lookdev')
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(texture)
        report=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertIn('lookdev/textures/pixels.json',report['files'])

    def test_unimplemented_foreign_node_binding_does_not_verify(self):
        shutil.copytree(ROOT/'examples/camera3d',self.root/'camera')
        source=reference(self.root,'camera/registry.json','camera3d')
        camera=reference(self.root,'camera/main-camera.json','camera3d')
        edited=deepcopy(self.bundle)
        edited[0]['source_projects'].append(source)
        edited[0]['dependency_manifest'].extend([source,camera])
        edited[2][1]['native']=camera
        edited[2][1]['native_node_id']='does-not-exist'
        edited[1][0]['nodes']=[dict(id=n['id'],sha256=contract_hash(n)) for n in edited[2]]
        edited[0]['scenes']=[dict(id=edited[1][0]['id'],sha256=contract_hash(edited[1][0]))]
        with self.assertRaises(RigError) as raised:verify_editor_bundle(*edited,self.root)
        self.assertEqual(raised.exception.report['code'],'EDITOR3D_UNAVAILABLE')

    def test_native_metadata_is_not_a_file_reference(self):
        scene_path=self.root/'native/example-scene.json'
        value=json.loads(scene_path.read_bytes())
        value['metadata']={'path':'must-not-open.json','sha256':'0'*64}
        scene_path.write_text(json.dumps(value))
        def rebind(value):
            if isinstance(value,dict):
                if 'path' in value and 'sha256' in value:
                    target=self.root/'native'/value['path']
                    if target.is_file():value['sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
                for child in value.values():rebind(child)
            elif isinstance(value,list):
                for child in value:rebind(child)
        for _ in range(5):
            for path in (self.root/'native').glob('*.json'):
                value=json.loads(path.read_bytes());rebind(value);path.write_text(json.dumps(value))
        result=verify_editor_bundle(*bundle(self.root),self.root)
        self.assertNotIn('native/must-not-open.json',result['files'])

    def test_additional_rig_cannot_borrow_source_identity_validation(self):
        shutil.copytree(ROOT/'examples/rig-3d/robotic',self.root/'rig')
        original=reference(self.root,'rig/rig.json','rig');original['native_kind']='Rig'
        forged=json.loads((self.root/'rig/rig.json').read_bytes())
        forged['id']=forged['character']['id']='forged'
        forged['version']='2.0.0'
        (self.root/'rig/forged.json').write_text(json.dumps(forged))
        additional=reference(self.root,'rig/forged.json','rig');additional['native_kind']='Rig'
        project=deepcopy(self.bundle[0]);project['source_projects'].append(original)
        project['dependency_manifest'].extend([original,additional])
        with self.assertRaises(RigError):verify_editor_bundle(project,*self.bundle[1:],self.root)
        project['source_projects'] = []
        with self.assertRaises(RigError):verify_editor_bundle(project,*self.bundle[1:],self.root)

    def test_material_does_not_consume_other_owners_opaque_payloads(self):
        from lookdev_contracts import Registry
        shutil.copyfile(ROOT/'examples/lookdev-contracts/ceramic.json',self.root/'ceramic.json')
        material=reference(self.root,'ceramic.json','lookdev')
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(material)
        observed=[]
        def checked_registry(entries, *, payloads=None):
            observed.extend(payloads or {})
            return Registry(entries,payloads=payloads)
        with patch('lookdev_contracts.Registry',side_effect=checked_registry):
            result=verify_editor_bundle(project,*self.bundle[1:],self.root)
        self.assertTrue(result['native_resources_verified'])
        self.assertEqual(observed,[])

    def test_cached_dependencies_cannot_bypass_depth_limit(self):
        folder=self.root/'materials';folder.mkdir()
        value=json.loads((ROOT/'examples/lookdev-contracts/ceramic.json').read_bytes())
        value['id']='m0';(folder/'m0.json').write_text(json.dumps(value))
        refs=[reference(self.root,'materials/m0.json','lookdev')]
        for n in range(1,35):
            value=json.loads((ROOT/'examples/lookdev-contracts/blue-coat.json').read_bytes())
            prior=refs[-1]
            pointer=dict(path=f'm{n-1}.json',id=prior['id'],version=prior['version'],
                         kind=prior['native_kind'],sha256=prior['file']['sha256'])
            value.update(id=f'm{n}',base_material=pointer,dependencies=[pointer])
            (folder/f'm{n}.json').write_text(json.dumps(value))
            refs.append(reference(self.root,f'materials/m{n}.json','lookdev'))
        for selected in ([refs[-1]],refs):
            project=deepcopy(self.bundle[0]);project['dependency_manifest'].extend(selected)
            with self.subTest(cached=len(selected)>1),self.assertRaises(RigError) as caught:
                verify_editor_bundle(project,*self.bundle[1:],self.root)
            self.assertEqual(caught.exception.report['code'],'EDITOR3D_DEPTH')

    def test_malformed_bound_rig_identity_returns_structured_failure(self):
        shutil.copytree(ROOT/'examples/rig-3d/robotic',self.root/'rig')
        payload=b'{}';(self.root/'rig/identity.json').write_bytes(payload)
        path=self.root/'rig/rig.json';value=json.loads(path.read_bytes())
        value['character']['identity_sha256']=hashlib.sha256(payload).hexdigest()
        path.write_text(json.dumps(value))
        native=reference(self.root,'rig/rig.json','rig');native['native_kind']='Rig'
        project=deepcopy(self.bundle[0]);project['dependency_manifest'].append(native)
        with self.assertRaises(RigError) as caught:verify_editor_bundle(project,*self.bundle[1:],self.root)
        # Preserve the canonical rig owner's structured identity diagnostic.
        self.assertEqual(caught.exception.report['code'],'RIG_IDENTITY_SCHEMA')


if __name__ == '__main__':
    unittest.main()
