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

    def test_generate_rejects_an_ordinary_edit_plan(self):
        from scene3d_language import compile_description
        plan_file = self.directory / 'edit-plan.json'
        plan_file.write_text(json.dumps(compile_description(self.graph, 'hide mesh')))
        target = self.directory / 'not-generated'
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/scene3d.py')]
        result = subprocess.run(command + ['generate', '--project', str(self.source), '--scene', 'example-scene.json',
            '--file', str(plan_file), '--destination', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)['error']['code'], 'SCENE3D_LANGUAGE_KIND')
        self.assertFalse(target.exists())

    def test_aggregate_triangle_budget_is_unresolved_before_publication(self):
        text = '; '.join('create terrain named land' + str(index) +
            ' with width 10 meters, depth 10 meters, elevation 0 meters, resolution 99, seed 1'
            for index in range(11))
        plan = compile_environment_description(self.graph, text)
        self.assertEqual(plan['status'], 'unresolved')
        self.assertIn('Aggregate', plan['diagnostics'][0]['message'])
        self.assertLessEqual(sum(item['triangles'] for item in plan['geometry']), 200000)

    def _set_project_budget(self, **changes):
        from scene3d_contracts import canonical_bytes
        project = json.loads((self.source / 'project.json').read_bytes())
        project['budgets'].update(changes)
        (self.source / 'project.json').write_bytes(canonical_bytes(project))
        self.bundle = load_project(self.source)
        self.graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json', source_root=self.source)

    def test_project_node_budget_is_unresolved_before_ready_or_dry_run(self):
        text = self.text.replace('two rooms', 'twenty rooms')
        before = load_project(self.source)['inventory']
        plan = compile_environment_description(self.graph, text)
        self.assertEqual(plan['status'], 'unresolved')
        self.assertIn('node budget', plan['diagnostics'][0]['message'])
        target = self.directory / 'over-budget'
        for dry_run in (False, True):
            with self.assertRaises(RigError):
                apply_environment_plan(self.source, target, 'example-scene.json', plan, dry_run=dry_run)
        self.assertFalse(target.exists())
        self.assertEqual(load_project(self.source)['inventory'], before)

    def test_batch_node_budget_includes_existing_scene_and_all_recipes(self):
        text = '; '.join(self.text.replace('named shop', 'named shop' + str(i)) for i in range(9))
        plan = compile_environment_description(self.graph, text)
        self.assertEqual(plan['status'], 'unresolved')
        self.assertIn('node budget', plan['diagnostics'][0]['message'])
        self.assertGreater(sum(item['nodes'] for item in plan['geometry']) + len(self.graph.document()['nodes']), 100)

    def test_named_generator_cli_dry_run_rejects_project_node_overflow(self):
        recipe = self.directory / 'large-recipe.json'
        recipe.write_text(json.dumps({'id': 'shop', 'version': 1, 'seed': 42, 'generator': 'workshop',
            'parameters': {'width': 5, 'depth': 5, 'height': 3, 'rooms': 20}, 'budget': 256}))
        target = self.directory / 'dry-invalid'
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/scene3d.py')]
        result = subprocess.run(command + ['building', 'generate', '--project', str(self.source),
            '--scene', 'example-scene.json', '--file', str(recipe), '--expected-revision', '0',
            '--expected-sha256', self.graph.digest, '--destination', str(target), '--dry-run'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('node budget', json.loads(result.stderr)['error']['message'])
        self.assertFalse(target.exists())

    def test_snapshot_file_and_byte_limits_are_preflighted(self):
        self._set_project_budget(max_files=len(self.bundle['inventory']))
        plan = compile_environment_description(self.graph, self.text)
        self.assertEqual(plan['status'], 'unresolved')
        self.assertEqual(plan['diagnostics'][0]['code'], 'SCENE3D_SNAPSHOT_LIMIT')
        self._set_project_budget(max_files=4096)
        size = sum(record['bytes'] for record in self.bundle['inventory'].values())
        self._set_project_budget(max_bytes=size + 1024)
        plan = compile_environment_description(self.graph, self.text)
        self.assertEqual(plan['status'], 'unresolved')
        self.assertEqual(plan['diagnostics'][0]['code'], 'SCENE3D_SNAPSHOT_LIMIT')

    def test_contextless_graph_does_not_claim_project_readiness(self):
        plan = compile_environment_description(SceneGraph(self.graph.document()), self.text)
        self.assertEqual(plan['status'], 'unresolved')
        self.assertEqual(plan['diagnostics'][0]['code'], 'SCENE3D_LANGUAGE_CONTEXT')

    def test_regeneration_counts_effective_nodes_instead_of_adding_them_twice(self):
        self._set_project_budget(max_files=4096, max_bytes=256 * 1024 * 1024)
        first = compile_environment_description(self.graph, self.text)
        target = self.directory / 'first'
        result = apply_environment_plan(self.source, target, 'example-scene.json', first)
        scene_path = result['snapshot']['active_scene']['path']
        bundle = load_project(target)
        project = json.loads((target / 'project.json').read_bytes())
        project['budgets']['max_nodes'] = len(bundle['documents'][scene_path]['nodes'])
        from scene3d_contracts import canonical_bytes
        (target / 'project.json').write_bytes(canonical_bytes(project))
        bundle = load_project(target)
        graph = SceneGraph.from_bundle(bundle, scene_path, source_root=target)
        before = bundle['inventory']
        plan = compile_environment_description(graph, self.text)
        self.assertEqual(plan['status'], 'ready', plan['diagnostics'])
        dry_target = self.directory / 'dry-regeneration'
        self.assertTrue(apply_environment_plan(target, dry_target, scene_path, plan, dry_run=True)['dry_run'])
        self.assertFalse(dry_target.exists())
        actual = self.directory / 'regeneration'
        result = apply_environment_plan(target, actual, scene_path, plan)
        reopened = load_project(actual)
        self.assertEqual(len(reopened['documents'][result['snapshot']['active_scene']['path']]['nodes']),
                         len(bundle['documents'][scene_path]['nodes']))
        self.assertEqual(load_project(target)['inventory'], before)

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
