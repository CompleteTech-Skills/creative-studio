"""Fault injection for staged rig revisions and idempotent crash recovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rig_test_helpers import fixture
from rig_errors import RigError, atomic_json, read_json, sha256
from rig_jobs import retain_artifact, run_step, save_rig
from rig_models import load_rig, rig_signature
import rig_jobs

ROOT = Path(__file__).resolve().parents[1]


class RigTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = fixture(self.temp.name)

    def modify(self, rig):
        rig.manifest['archetype'] = 'robotic'
        save_rig(rig, operation='transaction-test')
        return {'artifact_paths': []}

    def test_failure_after_save_keeps_active_revision_and_attempt_budget(self):
        before = (self.rig.root / 'rig.json').read_bytes()
        def failure(rig):
            self.modify(rig)
            self.assertEqual((rig.root / 'rig.json').read_bytes(), before)
            return {'artifact_paths': ['missing-artifact.json']}
        for attempt in range(3):
            with self.assertRaises(RigError):
                run_step(self.rig.root, 'fault', {}, failure, status='animation-ready')
            self.assertEqual((self.rig.root / 'rig.json').read_bytes(), before)
            step = read_json(self.rig.root / 'job.json')['steps']['fault']
            self.assertEqual(step['attempts'], attempt + 1)
            self.assertEqual(step['status'], 'failed')
        with self.assertRaises(RigError) as error:
            run_step(self.rig.root, 'fault', {}, failure, status='animation-ready')
        self.assertEqual(error.exception.report['code'], 'RIG_ATTEMPTS')
        self.assertEqual(rig_signature(load_rig(self.rig.root).manifest), rig_signature(self.rig.manifest))

    def crash(self, point):
        program = r'''
import os, sys
from pathlib import Path
sys.path[:0] = [str(Path(sys.argv[1]) / 'scripts')]
import rig_jobs
from rig_jobs import run_step, save_rig
root = Path(sys.argv[2])
point = sys.argv[3]
original = rig_jobs.atomic_json

def intercepted(path, value):
    path = Path(path)
    if point == 'prepared' and path.name == 'record.json' and value.get('phase') == 'prepared':
        original(path, value)
        os._exit(42)
    if point == 'promoted' and path == root / 'job.json' and value.get('steps', {}).get('crash', {}).get('status') == 'succeeded':
        os._exit(43)
    return original(path, value)

rig_jobs.atomic_json = intercepted

def action(rig):
    rig.manifest['archetype'] = 'robotic'
    save_rig(rig, operation='crash-test')
    if point == 'action':
        os._exit(41)
    return {'artifact_paths': []}

run_step(root, 'crash', {}, action, status='animation-ready')
'''
        result = subprocess.run([sys.executable, '-c', program, str(ROOT), str(self.rig.root), point],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, {'action': 41, 'prepared': 42, 'promoted': 43}[point], result.stderr)

    def test_process_exit_during_action_leaves_previous_active_revision(self):
        before = sha256(self.rig.root / 'rig.json')
        self.crash('action')
        self.assertEqual(sha256(self.rig.root / 'rig.json'), before)
        result = run_step(self.rig.root, 'crash', {}, self.modify, status='animation-ready')
        self.assertEqual(result['status'], 'succeeded')
        job = read_json(self.rig.root / 'job.json')
        self.assertEqual(job['steps']['crash']['attempts'], 2)
        self.assertIn('interrupted', [entry.get('status') for entry in job['history']])

    def test_prepared_process_exit_commits_without_replaying_action(self):
        before = sha256(self.rig.root / 'rig.json')
        self.crash('prepared')
        self.assertEqual(sha256(self.rig.root / 'rig.json'), before)
        def forbidden(_):
            self.fail('Prepared action must not execute twice')
        result = run_step(self.rig.root, 'crash', {}, forbidden, status='animation-ready')
        self.assertEqual(result['status'], 'cached')
        self.assertNotEqual(sha256(self.rig.root / 'rig.json'), before)
        self.assertEqual(load_rig(self.rig.root).manifest['archetype'], 'robotic')
        self.assertEqual(read_json(self.rig.root / 'job.json')['steps']['crash']['attempts'], 1)

    def test_exit_after_pointer_promotion_finishes_only_journal(self):
        self.crash('promoted')
        promoted = sha256(self.rig.root / 'rig.json')
        def forbidden(_):
            self.fail('Promoted action must not execute twice')
        result = run_step(self.rig.root, 'crash', {}, forbidden, status='animation-ready')
        self.assertEqual(result['status'], 'cached')
        self.assertEqual(sha256(self.rig.root / 'rig.json'), promoted)

    def test_prepared_recovery_does_not_overwrite_other_revision(self):
        self.crash('prepared')
        other = load_rig(self.rig.root)
        other.manifest['archetype'] = 'quadruped'
        # Simulate a noncooperating external writer; the recovery CAS must reject.
        atomic_json(other.root / 'rig.json', other.manifest)
        before = sha256(other.root / 'rig.json')
        with self.assertRaises(RigError) as error:
            run_step(other.root, 'another', {}, lambda _: {}, status='animation-ready')
        self.assertEqual(error.exception.report['code'], 'RIG_TRANSACTION_CONFLICT')
        self.assertEqual(sha256(other.root / 'rig.json'), before)

    def prepared_record(self):
        step = read_json(self.rig.root / 'job.json')['steps']['crash']
        directory = self.rig.root / 'rig/transactions' / step['token']
        return directory, read_json(directory / 'record.json')

    def test_tampered_candidate_is_not_promoted(self):
        self.crash('prepared')
        before = sha256(self.rig.root / 'rig.json')
        directory, record = self.prepared_record()
        candidate = read_json(directory / 'manifest.json')
        candidate['archetype'] = 'quadruped'
        atomic_json(directory / 'manifest.json', candidate)
        with self.assertRaises(RigError) as error:
            run_step(self.rig.root, 'crash', {}, self.modify, status='animation-ready')
        self.assertEqual(error.exception.report['code'], 'RIG_TRANSACTION')
        self.assertEqual(sha256(self.rig.root / 'rig.json'), before)

    def test_malformed_prepared_step_fails_before_pointer_publication(self):
        for field, value in [('artifacts', None), ('job_status', 'unknown')]:
            with self.subTest(field=field):
                if (self.rig.root / 'job.json').exists():
                    job = read_json(self.rig.root / 'job.json')
                    job['steps'] = {}
                    atomic_json(self.rig.root / 'job.json', job)
                self.crash('prepared')
                before = sha256(self.rig.root / 'rig.json')
                directory, record = self.prepared_record()
                if field == 'artifacts':
                    record['step']['artifacts'] = value
                else:
                    record[field] = value
                atomic_json(directory / 'record.json', record)
                with self.assertRaises(RigError):
                    run_step(self.rig.root, 'crash', {}, self.modify, status='animation-ready')
                self.assertEqual(sha256(self.rig.root / 'rig.json'), before)

    def test_result_status_is_validated_before_revision_publication(self):
        before = sha256(self.rig.root / 'rig.json')
        def invalid(rig):
            self.modify(rig)
            return {'job_status': 'not-a-status'}
        with self.assertRaises(RigError):
            run_step(self.rig.root, 'invalid', {}, invalid, status='animation-ready')
        self.assertEqual(sha256(self.rig.root / 'rig.json'), before)

    def test_retained_artifact_never_rewrites_existing_evidence(self):
        path = retain_artifact(self.rig.root, 'example', {'one': 1, 'two': 2})
        target = self.rig.root / path
        original = target.read_bytes()
        self.assertEqual(retain_artifact(self.rig.root, 'example', {'two': 2, 'one': 1}), path)
        self.assertEqual(target.read_bytes(), original)
        target.write_text(json.dumps({'one': 1, 'two': 2}))
        with self.assertRaises(RigError) as error:
            retain_artifact(self.rig.root, 'example', {'one': 1, 'two': 2})
        self.assertEqual(error.exception.report['code'], 'RIG_ARTIFACT_COLLISION')
