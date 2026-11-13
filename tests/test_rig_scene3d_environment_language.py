"""Actual structured environment prose, geometry publication and hostile plan handling."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from rig_errors import RigError
from scene3d_contracts import load_project
from scene3d_example import build_example
from scene3d_graph import SceneGraph
from scene3d_language_environment import compile_environment_description, apply_environment_plan


class EnvironmentLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'source'
        self.bundle = build_example(self.source)
        self.graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json', source_root=self.source)
        self.text = 'Create a workshop named shop with two rooms, width 5 meters, depth 5 meters, height 3 meters, seed 42'

    def test_template_produces_editable_real_geometry_and_preserves_source(self):
        plan = compile_environment_description(self.graph, self.text)
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(plan, compile_environment_description(self.graph, self.text))
        self.assertGreater(plan['geometry'][0]['nodes'], 1)
        target = self.directory / 'edited'
        receipt = apply_environment_plan(self.source, target, 'example-scene.json', plan)
        reopened = load_project(target)
        graph = SceneGraph.from_bundle(reopened, receipt['snapshot']['active_scene']['path'], source_root=target)
        generated = [node for node in graph.document()['nodes'] if node['metadata'].get('generator_id') == 'shop']
        self.assertEqual(len(generated), plan['geometry'][0]['nodes'])
        for node in generated:
            asset = reopened['documents'][node['asset']['path']]
            payload = (target / asset['content']['path']).read_bytes()
            self.assertIn(b'\nv ', b'\n' + payload)
            self.assertIn(b'\nf ', b'\n' + payload)
        self.assertEqual(load_project(self.source)['inventory'], self.bundle['inventory'])
        self.assertEqual((target / 'triangle.obj').read_bytes(), (self.source / 'triangle.obj').read_bytes())

    def test_unknown_requirements_bad_dimensions_and_duplicate_ids_are_unresolved(self):
        for text in (self.text + ' and summon a dragon', self.text.replace('width 5', 'width 0'),
                     self.text + ';' + self.text, self.text.replace('two rooms', '2.5 rooms'),
                     self.text.replace('seed 42', 'seed 4294967296')):
            with self.subTest(text=text):
                plan = compile_environment_description(self.graph, text)
                self.assertEqual(plan['status'], 'unresolved')
                with self.assertRaises(RigError):
                    apply_environment_plan(self.source, self.directory / 'bad', 'example-scene.json', plan)
                self.assertFalse((self.directory / 'bad').exists())

    def test_aggregate_triangle_budget_is_unresolved_before_publication(self):
        text = '; '.join('create terrain named land' + str(index) +
            ' with width 10 meters, depth 10 meters, elevation 0 meters, resolution 99, seed 1'
            for index in range(11))
        plan = compile_environment_description(self.graph, text)
        self.assertEqual(plan['status'], 'unresolved')
        self.assertIn('Aggregate', plan['diagnostics'][0]['message'])
        self.assertLessEqual(sum(item['triangles'] for item in plan['geometry']), 200000)

    def test_modified_and_stale_plans_cannot_authorize_different_geometry(self):
        plan = compile_environment_description(self.graph, self.text)
        changed = deepcopy(plan)
        changed['recipes'][0]['parameters']['height'] = 20
        with self.assertRaises(RigError):
            apply_environment_plan(self.source, self.directory / 'bad', 'example-scene.json', changed)
        changed = deepcopy(plan)
        changed['base_sha256'] = '0' * 64
        with self.assertRaisesRegex(RigError, 'changed after'):
            apply_environment_plan(self.source, self.directory / 'bad', 'example-scene.json', changed)

    def test_cli_plans_then_generates_and_native_terrain_recipe_dry_run(self):
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/scene3d.py')]
        plan_file = self.directory / 'plan.json'
        result = subprocess.run(command + ['--output', str(plan_file), 'plan', '--environment', '--project', str(self.source),
            '--scene', 'example-scene.json', '--text', self.text], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.directory / 'result'
        result = subprocess.run(command + ['generate', '--project', str(self.source), '--scene', 'example-scene.json',
            '--file', str(plan_file), '--destination', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(load_project(target))
        recipe = self.directory / 'recipe.json'
        recipe.write_text(json.dumps({'id': 'land', 'version': 1, 'seed': 4, 'generator': 'terrain',
            'parameters': {'width': 8, 'depth': 8, 'resolution': 5, 'elevation': 1}, 'budget': 32}))
        dry = self.directory / 'dry'
        result = subprocess.run(command + ['terrain', 'generate', '--project', str(self.source), '--scene', 'example-scene.json',
            '--file', str(recipe), '--expected-revision', '0', '--expected-sha256', self.graph.digest,
            '--destination', str(dry), '--dry-run'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(json.loads(result.stdout)['triangles'], 0)
        self.assertFalse(dry.exists())


if __name__ == '__main__':
    unittest.main()
