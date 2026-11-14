"""Real CLI dry runs enforce the same immutable snapshot admission as writes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_all_mutating_routes_pin_the_admitted_source_across_concurrent_changes(self):
        import scene3d
        import scene3d_snapshot
        from rig_errors import RigError
        actual_snapshot = scene3d_snapshot.snapshot_project
        counter = 0
        for arguments in self.all_routes():
            for dry_run in (True, False):
                target = self.root / ('source-race-' + str(counter))
                label = 'CONCURRENT USER PROJECT ' + str(counter)
                counter += 1
                def interleave(*args, **kwargs):
                    current = load_project(self.source)['project']
                    current['name'] = label
                    (self.source / 'project.json').write_bytes(canonical_bytes(current))
                    load_project(self.source)  # A valid actual concurrent source edit.
                    return actual_snapshot(*args, **kwargs)
                tokens = arguments + ['--destination', str(target)] + (['--dry-run'] if dry_run else [])
                with self.subTest(route=arguments[:2], dry_run=dry_run), \
                     patch.object(scene3d_snapshot, 'snapshot_project', side_effect=interleave):
                    with self.assertRaises(RigError):
                        scene3d.execute(scene3d._parser().parse_args(tokens))
                self.assertFalse(target.exists())
                self.assertEqual(load_project(self.source)['project']['name'], label)

    def test_plan_and_recipe_reads_preserve_the_original_admission_token(self):
        import asset_library
        import scene3d
        from rig_errors import RigError
        actual_read = asset_library.read_source_bytes
        counter = 0
        for arguments in self.all_routes():
            if '--file' not in arguments:
                continue
            source_file = Path(arguments[arguments.index('--file') + 1])
            for dry_run in (True, False):
                target = self.root / ('file-read-race-' + str(counter))
                label = 'CONCURRENT FILE READ ' + str(counter)
                counter += 1
                def read_then_edit(path, *args, **kwargs):
                    raw = actual_read(path, *args, **kwargs)
                    if Path(path) == source_file:
                        current = load_project(self.source)['project']
                        current['name'] = label
                        (self.source / 'project.json').write_bytes(canonical_bytes(current))
                        load_project(self.source)
                    return raw
                tokens = arguments + ['--destination', str(target)] + (['--dry-run'] if dry_run else [])
                with self.subTest(route=arguments[:2], dry_run=dry_run), \
                     patch.object(asset_library, 'read_source_bytes', side_effect=read_then_edit):
                    with self.assertRaises(RigError):
                        scene3d.execute(scene3d._parser().parse_args(tokens))
                self.assertFalse(target.exists())
                self.assertEqual(load_project(self.source)['project']['name'], label)

    def test_environment_api_retains_its_first_root_across_parser_and_preflight(self):
        import scene3d_language_environment as language
        from rig_errors import RigError
        actual_recipe = language._recipe
        actual_preflight = language._preflight_project
        text = 'Create a workshop named shop with two rooms, width 5 meters, depth 5 meters, height 3 meters, seed 42'
        counter = 0
        for hook in ('parser', 'after-preflight'):
            for dry_run in (True, False):
                initial = language.compile_environment_description(self.graph, text)
                target = self.root / ('api-root-race-' + str(counter))
                label = 'CONCURRENT API ROOT ' + str(counter)
                counter += 1
                changed = False
                def edit_once():
                    nonlocal changed
                    if changed:
                        return
                    current = load_project(self.source)['project']
                    current['name'] = label
                    (self.source / 'project.json').write_bytes(canonical_bytes(current))
                    load_project(self.source)
                    changed = True
                def parse_then_edit(statement):
                    result = actual_recipe(statement)
                    edit_once()
                    return result
                def preflight_then_edit(*args, **kwargs):
                    result = actual_preflight(*args, **kwargs)
                    edit_once()
                    return result
                name, function = ('_recipe', parse_then_edit) if hook == 'parser' else ('_preflight_project', preflight_then_edit)
                with self.subTest(hook=hook, dry_run=dry_run), patch.object(language, name, side_effect=function):
                    with self.assertRaises(RigError):
                        language.apply_environment_plan(self.source, target, 'example-scene.json', initial, dry_run=dry_run)
                self.assertTrue(changed)
                self.assertFalse(target.exists())
                self.assertEqual(load_project(self.source)['project']['name'], label)

    def test_invalid_project_pin_types_reject_without_publication_or_raw_errors(self):
        import numpy as np
        import scene3d_language_environment as language
        from rig_errors import RigError
        text = 'Create a workshop named shop with two rooms, width 5 meters, depth 5 meters, height 3 meters, seed 42'
        valid = language.compile_environment_description(self.graph, text)
        before = load_project(self.source)['inventory']
        for value in (True, 42, [], {}, np.array(['a', 'b'])):
            with self.subTest(value=repr(value)):
                plan = language.compile_environment_description(self.graph, text, expected_project_sha256=value)
                self.assertEqual(plan['status'], 'unresolved')
                self.assertEqual(plan['diagnostics'][0]['code'], 'SCENE3D_SOURCE_CHANGED')
                with self.assertRaises(RigError):
                    language.apply_environment_plan(self.source, self.root / 'invalid-pin', 'example-scene.json',
                                                     valid, expected_project_sha256=value)
                self.assertFalse((self.root / 'invalid-pin').exists())
        self.assertEqual(load_project(self.source)['inventory'], before)

    def test_environment_public_api_rejects_invalid_modes_before_source_io(self):
        import numpy as np
        import scene3d_contracts
        import scene3d_language_environment as language
        from rig_errors import RigError
        text = 'Create a workshop named shop with two rooms, width 5 meters, depth 5 meters, height 3 meters, seed 42'
        valid = language.compile_environment_description(self.graph, text)
        before = load_project(self.source)['inventory']
        for value in (None, 0, 1, [], [1], {}, '', 'false', np.bool_(False), np.array([False])):
            target = self.root / 'invalid-mode'
            with self.subTest(value=repr(value)), \
                 patch.object(scene3d_contracts, 'load_project', wraps=load_project) as reader:
                with self.assertRaises(RigError) as error:
                    language.apply_environment_plan(self.source, target, 'example-scene.json', valid, dry_run=value)
                self.assertIn('boolean', error.exception.report['message'])
                reader.assert_not_called()
            self.assertFalse(target.exists())
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
