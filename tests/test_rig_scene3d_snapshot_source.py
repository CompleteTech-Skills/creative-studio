"""Actual snapshot source revision binding before any snapshot copy or publication."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from rig_errors import RigError
from scene3d_example import build_example
from scene3d_contracts import canonical_bytes, load_project
from scene3d_graph import SceneGraph
import scene3d_snapshot

class SourceRevisionSnapshots(unittest.TestCase):
    def test_expected_raw_root_hash_admits_exact_source_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';expected=build_example(source)
            digest=hashlib.sha256((source/'project.json').read_bytes()).hexdigest()
            receipt=scene3d_snapshot.snapshot_project(source,root/'published',expected_project_sha256=digest)
            self.assertEqual(load_project(root/'published')['status'],'hash-and-contract-verified')
            self.assertEqual(load_project(source)['inventory'],expected['inventory'])
            self.assertEqual(hashlib.sha256((source/'project.json').read_bytes()).hexdigest(),digest)
            self.assertEqual(receipt['status'],'complete-byte-and-contract-verified')

    def test_valid_foreign_root_rejected_before_copy_or_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';build_example(source)
            expected=hashlib.sha256((source/'project.json').read_bytes()).hexdigest()
            document=load_project(source)['project'];document['name']='Valid foreign revision'
            (source/'project.json').write_bytes(canonical_bytes(document));foreign=load_project(source)['inventory']
            with patch.object(scene3d_snapshot,'read_source_bytes',wraps=scene3d_snapshot.read_source_bytes) as reader:
                with self.assertRaises(RigError) as caught:
                    scene3d_snapshot.snapshot_project(source,root/'rejected',expected_project_sha256=expected)
                reader.assert_not_called()
            self.assertEqual(caught.exception.report['code'],'SCENE3D_SOURCE_CHANGED')
            self.assertFalse((root/'rejected').exists())
            self.assertEqual(load_project(source)['inventory'],foreign)
            self.assertEqual(sorted(p.name for p in root.iterdir()),['source'])

    def test_graph_requires_exact_source_digest_before_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'
            bundle = build_example(source)
            graph = SceneGraph.from_bundle(bundle, 'example-scene.json')

            with patch.object(scene3d_snapshot, 'load_project') as loader:
                with self.assertRaises(RigError) as caught:
                    scene3d_snapshot.snapshot_project(source, root/'rejected', graph, scene_path='example-scene.json')
                loader.assert_not_called()
            self.assertEqual(caught.exception.report['code'], 'SCENE3D_SOURCE_CHANGED')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['source'])

    def test_snapshot_preserves_scene3d_manifest_name_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'
            bundle = build_example(source)
            project = bundle['project']
            animation = project['animations'][0]
            original_path = animation['path']
            path = 'example-\x7f-animation.json'
            (source/original_path).rename(source/path)
            for reference in [animation, *project['dependency_lock']]:
                if reference['path'] == original_path:
                    reference['path'] = path
            (source/'project.json').write_bytes(canonical_bytes(project))

            verified = load_project(source)
            receipt = scene3d_snapshot.snapshot_project(source, root/'published')
            self.assertEqual(receipt['inventory'][path], verified['inventory'][path])
            self.assertEqual((root/'published'/path).read_bytes(), (source/path).read_bytes())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux staging admission')
    def test_unavailable_protected_staging_fails_before_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'
            build_example(source)

            with patch.object(scene3d_snapshot, 'private_stage_supported', return_value=False):
                with self.assertRaises(RigError) as caught:
                    scene3d_snapshot.snapshot_project(source, root/'rejected')
            self.assertEqual(caught.exception.report['code'], 'SCENE3D_STAGING_UNAVAILABLE')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['source'])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux staging admission')
    def test_pinned_parent_must_match_admitted_identity_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'
            build_example(source)
            destination = root/'rejected'
            _, (device, inode) = scene3d_snapshot._admit_destination(destination)

            with (
                patch.object(scene3d_snapshot, '_admit_destination', return_value=(destination, (device, inode + 1))),
                patch.object(scene3d_snapshot, 'private_stage') as stage,
            ):
                with self.assertRaises(RigError) as caught:
                    scene3d_snapshot.snapshot_project(source, destination)
                stage.assert_not_called()
            self.assertEqual(caught.exception.report['code'], 'SCENE3D_DESTINATION')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['source'])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux staging admission')
    def test_missing_publication_entry_surfaces_integrity_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'
            build_example(source)
            destination = root/'rejected'

            with patch.object(scene3d_snapshot, '_rename_no_replace', side_effect=FileNotFoundError):
                with self.assertRaises(RigError) as caught:
                    scene3d_snapshot.snapshot_project(source, destination)
            self.assertEqual(caught.exception.report['code'], 'SCENE3D_SOURCE_CHANGED')
            self.assertFalse(destination.exists())

    def test_malformed_expected_hashes_rejected_without_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';build_example(source)
            for invalid in (True,'0'*63,'A'*64,'g'*64,[],0):
                with self.subTest(value=invalid),self.assertRaises(RigError):
                    scene3d_snapshot.snapshot_project(source,root/'rejected',expected_project_sha256=invalid)
                self.assertFalse((root/'rejected').exists())

if __name__=='__main__':unittest.main()
