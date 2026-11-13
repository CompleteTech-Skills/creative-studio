"""Language edits through real repository history, transactions and reopening."""
from copy import deepcopy
from pathlib import Path
import tempfile
import json
import subprocess
import sys

from rig_errors import RigError
import unittest

from editor3d_repository import EditorRepository
from scene3d_example import build_example
from scene3d_editor_language import plan_repository_description, apply_repository_plan


class EditorLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        source = self.directory / 'source'
        build_example(source)
        self.repository = EditorRepository.create(source, self.directory / 'repo', scene_path='example-scene.json')

    def test_real_revision_reopens_idempotent_retry_and_undo(self):
        plan = plan_repository_description(self.repository, 'hide mesh')
        result = apply_repository_plan(self.repository, plan, 'language-edit')
        self.assertEqual(result['revision'], 1)
        mesh = next(n for n in result['scene']['nodes'] if n['id'] == 'mesh')
        self.assertFalse(mesh['visible'])
        self.assertEqual(apply_repository_plan(self.repository, plan, 'language-edit')['revision'], 1)
        reopened = EditorRepository(self.directory / 'repo')
        restored = reopened.undo(1, 'undo-language', expected_sha256=result['sha256'])
        self.assertTrue(next(n for n in restored['scene']['nodes'] if n['id'] == 'mesh')['visible'])

    def test_stale_and_forged_plan_leave_repository_unchanged(self):
        plan = plan_repository_description(self.repository, 'hide mesh')
        forged = deepcopy(plan)
        forged['scene_plan']['commands'][0]['parameters']['identifier'] = 'camera'
        with self.assertRaises(RigError):
            apply_repository_plan(self.repository, forged, 'forged-language')
        self.assertEqual(self.repository.snapshot()['revision'], 0)
        self.assertEqual(self.repository.recovery(), [])
        apply_repository_plan(self.repository, plan, 'first-edit')
        with self.assertRaises(RigError):
            apply_repository_plan(self.repository, plan, 'stale-edit')
        self.assertEqual(self.repository.snapshot()['revision'], 1)

    def test_lifecycle_cli_archive_restores_exact_repository(self):
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/scene3d.py')]
        archive = self.directory / 'history.zip'
        restored = self.directory / 'restored'
        for arguments in (["archive", "--repository", str(self.directory / 'repo'), "--destination", str(archive)],
                          ["restore", "--file", str(archive), "--destination", str(restored)]):
            result = subprocess.run(command + arguments, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(EditorRepository(restored).snapshot()['sha256'], self.repository.snapshot()['sha256'])
        targets = self.directory / 'targets.json'
        targets.write_text(json.dumps(['example-scene.json']))
        result = subprocess.run(command + ['impact', '--project', str(self.directory / 'source'), '--file', str(targets)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
