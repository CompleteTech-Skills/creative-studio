"""Actual registered imports resolved from complete immutable private captures."""
from pathlib import Path
import unittest

from rig_errors import RigError
from scene3d_contracts import load_project
from scene3d_example import build_example
from scene3d_graph import SceneGraph
from scene3d_import import add_import,evaluate_imported_vertices,import_gltf
from scene3d_import_capture import resolve_captured_import
from scene3d_snapshot import snapshot_project
import test_rig_scene3d_materials as material_projection_tests


class CapturedImportTests(unittest.TestCase):
    setUp=material_projection_tests.SceneMaterialProjectionTests.setUp

    def project(self,imported=None):
        imported=imported or self.imported
        original=build_example(self.root/'project')
        graph=SceneGraph.from_bundle(original,'example-scene.json')
        add_import(graph,imported,'actor')
        manifest=self.library.resolve(imported.library_reference)
        payloads={record['path']:self.library.read_file(imported.library_reference,record['path'])
            for record in manifest['files'] if record['path']!=imported.asset_reference['path']}
        output=snapshot_project(self.root/'project',self.root/'portable',graph,scene_path='example-scene.json',
            derived_documents={imported.asset_reference['path']:imported.asset},extra_payloads=payloads)
        root=self.root/'portable';bundle=load_project(root)
        graph=SceneGraph.from_bundle(bundle,output['active_scene']['path'],source_root=root)
        return root,bundle,graph

    def test_actual_static_import_resolves_exact_topology_in_private_lifetime(self):
        root,bundle,graph=self.project()
        with resolve_captured_import(bundle,self.imported.asset_reference,root) as resolved:
            private=resolved['root'];native=resolved['native_root']
            self.assertNotEqual(private,root);self.assertTrue(native.is_dir())
            self.assertIsNone(resolved['rig'].manifest['skeleton'])
            self.assertEqual(resolved['asset_reference'],self.imported.asset_reference)
            self.assertEqual(set(resolved['rig'].geometry.by_id()),
                             {mesh for meshes in self.imported.asset['components'].values() for mesh in meshes})
            sampled=evaluate_imported_vertices(graph,resolved['imported'],native)
            self.assertEqual(len(sampled['nodes']),2)
        self.assertFalse(private.exists());self.assertFalse(native.exists())

    def test_caller_source_swap_after_capture_cannot_change_loaded_geometry(self):
        root,bundle,graph=self.project()
        manifest=next(record for record in self.imported.asset['dependencies'] if record['path'].endswith('/native/rig.json'))
        path=root/manifest['path'];original=path.read_bytes()
        with resolve_captured_import(bundle,self.imported.asset_reference,root) as resolved:
            before=evaluate_imported_vertices(graph,resolved['imported'],resolved['native_root'])
            try:
                path.write_bytes(b'{}\n')
                after=evaluate_imported_vertices(graph,resolved['imported'],resolved['native_root'])
                self.assertEqual(before,after)
            finally:path.write_bytes(original)
        self.assertEqual(path.read_bytes(),original)

    def test_caller_asset_pin_mutation_after_preflight_cannot_change_admitted_identity(self):
        from contextlib import contextmanager
        from copy import deepcopy
        from unittest.mock import patch
        from scene3d_capture import capture_verified_files
        root,bundle,graph=self.project()
        expected=deepcopy(self.imported.asset_reference);caller_pin=deepcopy(expected)
        @contextmanager
        def capture_then_mutate(source,inventory):
            with capture_verified_files(source,inventory) as private:
                # Real captured bytes remain unchanged. Only the caller-owned
                # mutable pin changes after portable-path/type preflight.
                caller_pin['path']='../../unregistered-asset.json'
                caller_pin['sha256']='0'*64
                yield private
        with patch('scene3d_capture.capture_verified_files',capture_then_mutate):
            with resolve_captured_import(bundle,caller_pin,root) as resolved:
                self.assertEqual(resolved['asset_reference'],expected)
                self.assertEqual(resolved['imported'].asset_reference,expected)
                self.assertTrue(evaluate_imported_vertices(graph,resolved['imported'],resolved['native_root'])['nodes'])
        self.assertNotEqual(caller_pin,expected)

    def test_changed_registered_bytes_and_wrong_asset_hash_reject(self):
        root,bundle,graph=self.project()
        record=next(item for item in self.imported.asset['dependencies'] if item['path'].endswith('/hierarchy.json'))
        path=root/record['path'];original=path.read_bytes()
        try:
            path.write_bytes(b'{}\n')
            with self.assertRaises(RigError):
                with resolve_captured_import(bundle,self.imported.asset_reference,root):pass
        finally:path.write_bytes(original)
        with self.assertRaises(RigError):
            with resolve_captured_import(bundle,dict(self.imported.asset_reference,sha256='0'*64),root):pass

    def test_actual_skinned_import_retains_exact_native_skeleton_pin(self):
        from rig_test_helpers import fixture
        from rig_gltf import export_gltf
        rig=fixture(self.root,name='source-rig');source=self.root/'skinned.glb';export_gltf(rig,source)
        imported=import_gltf(source,self.library,asset_id='skinned-fixture',attribution='Self-authored fixture',license_file=self.root/'license.txt')
        root,bundle,graph=self.project(imported)
        with resolve_captured_import(bundle,imported.asset_reference,root) as resolved:
            self.assertIsNotNone(resolved['rig'].skeleton)
            self.assertEqual(resolved['rig'].manifest['id'],imported.asset['skeleton']['id'])
            self.assertEqual(resolved['rig'].manifest['version'],imported.asset['skeleton']['version'])
            sampled=evaluate_imported_vertices(graph,resolved['imported'],resolved['native_root'])
            self.assertTrue(sampled['actual_deformation']);self.assertTrue(sampled['nodes'])


if __name__=='__main__':unittest.main()
