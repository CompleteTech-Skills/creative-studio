"""Real CLI dry runs enforce the same immutable snapshot admission as writes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scene3d_contracts import canonical_bytes, load_project
from scene3d_example import build_example
from scene3d_graph import SceneGraph
from scene3d_language_environment import compile_environment_description


class CliAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.bundle = build_example(self.source)
        self.graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json', source_root=self.source)
        self.command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/scene3d.py')]
        self.parameters = self.root / 'parameters.json'
        self.parameters.write_text(json.dumps(dict(identifier='mesh', new_id='copy')))
        self.batch = self.root / 'batch.json'
        self.batch.write_text(json.dumps(dict(schema_version=1, commands=[
            dict(operation='duplicate', parameters=dict(identifier='mesh', new_id='copy'))])))

    def run_cli(self, arguments, destination, *, dry_run=True):
        return subprocess.run(self.command + arguments + ['--destination', str(destination)] +
                              (['--dry-run'] if dry_run else []), capture_output=True, text=True)

    def edits(self):
        common = ['--project', str(self.source), '--scene', 'example-scene.json']
        token = ['--expected-revision', '0', '--expected-sha256', self.graph.digest]
        return [(['edit'] + common + ['--file', str(self.batch)] + token),
                (['object', 'duplicate'] + common + ['--file', str(self.parameters)] + token)]

    def test_raw_and_named_typed_edits_reject_over_budget_dry_and_actual(self):
        project = json.loads((self.source / 'project.json').read_bytes())
        project['budgets']['max_nodes'] = 3
        (self.source / 'project.json').write_bytes(canonical_bytes(project))
        before = load_project(self.source)['inventory']
        for arguments in self.edits():
            for dry_run in (True, False):
                target = self.root / 'not-published'
                result = self.run_cli(arguments, target, dry_run=dry_run)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('node budget', json.loads(result.stderr)['error']['message'])
                self.assertFalse(target.exists())
        self.assertEqual(load_project(self.source)['inventory'], before)

    def test_snapshot_dry_run_counts_promoted_project_history(self):
        project = json.loads((self.source / 'project.json').read_bytes())
        project['budgets']['max_files'] = len(self.bundle['inventory'])
        (self.source / 'project.json').write_bytes(canonical_bytes(project))
        before = load_project(self.source)['inventory']
        for dry_run in (True, False):
            target = self.root / 'not-published'
            result = self.run_cli(['snapshot', '--project', str(self.source)], target, dry_run=dry_run)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(json.loads(result.stderr)['error']['code'], 'SCENE3D_SNAPSHOT_LIMIT')
            self.assertFalse(target.exists())
        self.assertEqual(load_project(self.source)['inventory'], before)

    def all_routes(self):
        common = ['--project', str(self.source), '--scene', 'example-scene.json']
        recipe = dict(id='shop', version=1, seed=42, generator='workshop',
                      parameters=dict(width=5, depth=5, height=3, rooms=2), budget=256)
        recipe_path = self.root / 'recipe.json'
        recipe_path.write_text(json.dumps(recipe))
        text = 'Create a workshop named shop with two rooms, width 5 meters, depth 5 meters, height 3 meters, seed 42'
        plan_path = self.root / 'plan.json'
        plan_path.write_text(json.dumps(compile_environment_description(self.graph, text)))
        return self.edits() + [
            ['snapshot', '--project', str(self.source)],
            ['revise'] + common + ['--text', 'hide mesh'],
            ['generate'] + common + ['--file', str(plan_path)],
            ['building', 'generate'] + common + ['--file', str(recipe_path),
                '--expected-revision', '0', '--expected-sha256', self.graph.digest]]

    def test_every_mutating_dry_run_checks_destination_before_success(self):
        existing = self.root / 'existing'
        existing.mkdir()
        (existing / 'marker').write_bytes(b'preserved')
        before = load_project(self.source)['inventory']
        for index, arguments in enumerate(self.all_routes()):
            for destination in (existing, self.root / 'missing-parent' / ('output-' + str(index))):
                with self.subTest(arguments=arguments[:2], destination=str(destination)):
                    result = self.run_cli(arguments, destination)
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertEqual(json.loads(result.stderr)['error']['code'], 'SCENE3D_DESTINATION')
        self.assertEqual((existing / 'marker').read_bytes(), b'preserved')
        self.assertFalse((self.root / 'missing-parent').exists())
        self.assertEqual(load_project(self.source)['inventory'], before)

    def test_valid_dry_runs_preserve_sources_and_publish_nothing(self):
        before = load_project(self.source)['inventory']
        for index, arguments in enumerate(self.all_routes()):
            target = self.root / ('dry-' + str(index))
            result = self.run_cli(arguments, target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['dry_run'])
            self.assertFalse(target.exists())
        self.assertEqual(load_project(self.source)['inventory'], before)


if __name__ == '__main__':
    unittest.main()
