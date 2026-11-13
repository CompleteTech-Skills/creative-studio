"""Actual named native adapters and rational timeline CLI evaluation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scene3d_contracts import load_project
from scene3d_example import build_example
from scene3d_graph import SceneGraph


class NativeCommandCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'source'
        self.bundle = build_example(self.source)
        self.graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json', source_root=self.source)
        self.command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/scene3d.py')]

    def run_cli(self, *arguments):
        return subprocess.run(self.command + list(arguments), capture_output=True, text=True)

    def edit(self, category, action, parameters, destination='edited'):
        parameter_file = self.directory / 'parameters.json'
        parameter_file.write_text(json.dumps(parameters))
        return self.run_cli(category, action, '--project', str(self.source), '--scene', 'example-scene.json',
            '--file', str(parameter_file), '--expected-revision', '0', '--expected-sha256', self.graph.digest,
            '--destination', str(self.directory / destination))

    def test_document_categories_inspect_real_hash_bound_manifests(self):
        for category, identifier in (('scene', 'example-scene.json'), ('asset', 'triangle'),
                                     ('prefab', 'example-prefab'), ('shot', 'example-shot')):
            with self.subTest(category=category):
                listed = self.run_cli(category, 'list', '--project', str(self.source))
                self.assertEqual(listed.returncode, 0, listed.stderr)
                result = self.run_cli(category, 'validate', '--project', str(self.source), '--identifier', identifier)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], 'valid')

    def test_named_object_edit_reopens_with_original_geometry(self):
        result = self.edit('object', 'rename', {'identifier': 'mesh', 'name': 'Renamed triangle'})
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        target = self.directory / 'edited'
        graph = SceneGraph.from_bundle(load_project(target), receipt['active_scene']['path'], source_root=target)
        self.assertEqual(next(n for n in graph.document()['nodes'] if n['id'] == 'mesh')['name'], 'Renamed triangle')
        self.assertEqual((target / 'triangle.obj').read_bytes(), (self.source / 'triangle.obj').read_bytes())

    def test_camera_and_light_reject_wrong_node_type_before_publication(self):
        for category in ('camera', 'lighting'):
            result = self.edit(category, 'configure', {'identifier': 'mesh', 'properties': {'name': 'wrong'}})
            self.assertEqual(result.returncode, 2)
            self.assertFalse((self.directory / 'edited').exists())
        result = self.edit('camera', 'configure', {'identifier': 'camera', 'properties': {'camera': {
            'projection': 'orthographic', 'near': 0.1, 'far': 100, 'vertical_fov': 0.8, 'orthographic_height': 8}}})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_rational_evaluation_and_invalid_frames(self):
        arguments = ['evaluate', '--project', str(self.source), '--scene', 'example-scene.json', '--shot', 'example-shot.json']
        result = self.run_cli(*arguments, '--frame', '1/2')
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['frame'], {'numerator': 1, 'denominator': 2})
        for frame in ('1/0', 'bad', '-1', '24'):
            with self.subTest(frame=frame):
                result = self.run_cli(*arguments, '--frame', frame)
                self.assertEqual(result.returncode, 2)
        self.assertEqual(load_project(self.source)['inventory'], self.bundle['inventory'])

    def test_export_cli_real_glb_and_format_losses(self):
        target = self.directory / 'export'
        result = self.run_cli('export-glb', '--project', str(self.source), '--scene', 'example-scene.json',
            '--shot', 'example-shot.json', '--frame', '1/2', '--destination', str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / 'scene.glb').read_bytes()[:4], b'glTF')
        self.assertTrue((target / 'report.json').is_file())
        again = self.run_cli('export-glb', '--project', str(self.source), '--scene', 'example-scene.json',
            '--shot', 'example-shot.json', '--frame', '1/2', '--destination', str(target))
        self.assertEqual(again.returncode, 2)


if __name__ == '__main__':
    unittest.main()
