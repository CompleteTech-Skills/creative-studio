"""Save and reopen an edited Scene3D snapshot on the Windows publication lane."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))

from rig_errors import RigError
from scene3d_contracts import canonical_bytes, load_project
from scene3d_example import build_example
from scene3d_graph import SceneGraph
import scene3d_snapshot


class WindowsScene3DSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('HATCH_SCENE3D_WINDOWS_SNAPSHOT') == '1':
            if os.name != 'nt' or not scene3d_snapshot.windows_private_stage_supported():
                raise RuntimeError('Scene3D native Windows snapshot tests require Windows handle publication')

    def test_save_and_reopen_edited_snapshot_with_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            destination = root / 'saved'
            bundle = build_example(source)
            originals = {path: (source / path).read_bytes() for path in bundle['inventory']}
            expected_digest = bundle['inventory']['project.json']['sha256']
            scene_path = 'example-scene.json'
            graph = SceneGraph.from_bundle(bundle, scene_path)
            graph.rename('mesh', 'Published triangle')

            with patch.object(scene3d_snapshot, 'windows_private_stage',
                              wraps=scene3d_snapshot.windows_private_stage) as windows_stage:
                receipt = scene3d_snapshot.snapshot_project(source, destination, graph, scene_path=scene_path,
                                                            expected_project_sha256=expected_digest)
            if os.name == 'nt':
                windows_stage.assert_called_once()

            reopened = load_project(destination)
            self.assertEqual(receipt['status'], 'complete-byte-and-contract-verified')
            self.assertEqual(reopened['status'], 'hash-and-contract-verified')
            self.assertEqual(reopened['inventory']['project.json']['sha256'], receipt['project_sha256'])
            self.assertEqual(reopened['inventory'], receipt['inventory'])
            promoted = reopened['documents'][receipt['active_scene']['path']]
            self.assertEqual(next(node['name'] for node in promoted['nodes'] if node['id'] == 'mesh'),
                             'Published triangle')
            self.assertTrue(any(shot['scene'] == receipt['active_scene']
                                for shot in reopened['documents'].values() if shot['kind'] == 'scene3d-shot'))

            with self.assertRaises(RigError) as caught:
                scene3d_snapshot.snapshot_project(source, destination, expected_project_sha256=expected_digest)
            self.assertEqual(caught.exception.report['code'], 'SCENE3D_DESTINATION')
            for path, payload in originals.items():
                self.assertEqual((source / path).read_bytes(), payload)
                if path != 'project.json':
                    self.assertEqual((destination / path).read_bytes(), payload)
            history = destination / 'history' / f'project-{expected_digest}.json'
            self.assertEqual(history.read_bytes(), originals['project.json'])
            self.assertEqual(hashlib.sha256((destination / 'project.json').read_bytes()).hexdigest(),
                             receipt['project_sha256'])

    def test_save_and_reopen_hybrid_snapshot_with_legacy_bytes(self):
        from production_contracts import load_project as load_legacy_project
        from production_fixture import build_fixture_project

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            destination = root / 'saved'
            project = build_example(source)['project']
            legacy_root = source / 'legacy'
            build_fixture_project(legacy_root)
            legacy = load_legacy_project(legacy_root)['project']
            originals = {path.relative_to(legacy_root): path.read_bytes()
                         for path in legacy_root.rglob('*') if path.is_file()}
            reference = dict(kind='project', id=legacy['id'], version=legacy['version'], path='legacy/project.json',
                             sha256=hashlib.sha256(originals[Path('project.json')]).hexdigest())
            project['linked_2d_project'] = reference
            project['dependency_lock'].append(reference)
            project['budgets'].update(max_files=512, max_bytes=64 * 1024 * 1024)
            project_payload = canonical_bytes(project)
            (source / 'project.json').write_bytes(project_payload)

            with patch.object(scene3d_snapshot, 'windows_private_stage',
                              wraps=scene3d_snapshot.windows_private_stage) as windows_stage:
                receipt = scene3d_snapshot.snapshot_project(
                    source, destination, expected_project_sha256=hashlib.sha256(project_payload).hexdigest())
            if os.name == 'nt':
                windows_stage.assert_called_once()

            self.assertEqual(load_project(destination)['project']['linked_2d_project'], reference)
            self.assertEqual(load_legacy_project(destination / 'legacy')['project'], legacy)
            self.assertEqual((source / 'project.json').read_bytes(), project_payload)
            for path, payload in originals.items():
                inventory = receipt['inventory'][f'legacy/{path.as_posix()}']
                self.assertEqual(inventory['sha256'], hashlib.sha256(payload).hexdigest())
                self.assertEqual((legacy_root / path).read_bytes(), payload)
                self.assertEqual((destination / 'legacy' / path).read_bytes(), payload)

    @unittest.skipUnless(os.name == 'nt', 'Windows destination naming')
    def test_invalid_destination_name_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            build_example(source)
            for name in ('NUL.json', 'saved.'):
                with self.subTest(name=name):
                    with self.assertRaises(RigError) as caught:
                        scene3d_snapshot.snapshot_project(source, root / name)
                    self.assertEqual(caught.exception.report['code'], 'SCENE3D_DESTINATION')
            self.assertEqual(list(root.iterdir()), [source])

    @unittest.skipUnless(os.name == 'nt', 'Windows handle-pinned stage cleanup')
    def test_staged_verification_io_failure_cleans_owned_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            destination = root / 'saved'
            bundle = build_example(source)
            originals = {path: (source / path).read_bytes() for path in bundle['inventory']}

            def reopen(path):
                if path == source:
                    return load_project(path)
                self.assertEqual(path.name, 'payload')
                self.assertEqual(path.parent.parent, root)
                self.assertEqual((path / 'triangle.obj').read_bytes(), originals['triangle.obj'])
                raise OSError('cannot read staged project for verification')

            with patch.object(scene3d_snapshot, 'load_project', side_effect=reopen):
                with self.assertRaisesRegex(OSError, 'cannot read staged project for verification'):
                    scene3d_snapshot.snapshot_project(source, destination,
                                                      expected_project_sha256=bundle['inventory']['project.json']['sha256'])

            self.assertFalse(destination.exists())
            wrappers = list(root.glob('.scene3d-snapshot-*'))
            self.assertEqual(len(wrappers), 1)
            self.assertEqual(list(wrappers[0].iterdir()), [])
            self.assertEqual({path: (source / path).read_bytes() for path in originals}, originals)
