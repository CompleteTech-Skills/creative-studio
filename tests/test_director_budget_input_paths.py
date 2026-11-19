"""Offline bounded-file regressions at the production/native dispatch boundary.

A child process uses real temporary special files so a blocking open cannot hang
this test runner. Timing hooks call the real validator first; they only exchange
one path before the subsequent production read. No image provider is invoked.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

import hatch
import production_workflow as workflow
from director_budget import BudgetLedger, bind_native_budget, METRICS
from fixture_workflow import small_profile
from production_fixture import build_fixture_project
from rig_errors import RigError


def _quantities(**values):
    return {key: values.get(key, 0) for key in METRICS}


def _fixture(home):
    project = home / 'project'
    build_fixture_project(project)
    value = hatch.read(project / 'project.json')
    value['limits']['max_generation_calls'] = 4
    hatch.atomic(project / 'project.json', value)
    ledger = BudgetLedger.create(project / 'ledger', _quantities(provider_calls=6, amount=12),
        provider_limits={'imagegen': _quantities(provider_calls=6, amount=12)}, currency='USD')
    evidence = home / 'offline-evidence.json'
    evidence.write_text('{"fixture":true,"source":"bounded input regression"}', encoding='utf-8')
    proof = [ledger.retain_evidence(evidence)]
    profile = home / 'profile.json'
    hatch.atomic(profile, small_profile())
    run = project / 'acquisition' / 'fixture'
    run.parent.mkdir(parents=True, exist_ok=True)
    hatch.prepare(run, argparse.Namespace(replace_inputs=False, fixture_mode=True,
        profile=str(profile), pet_name='Bounded input fixture', pet_notes='No provider calls',
        reference=[], avoid=[], require=[], brand_source=[], style_preset='clay', style_notes='fixture'))
    bind_native_budget(run, ledger.root, owner_root=project, provider='imagegen',
        estimate=_quantities(provider_calls=1, amount=2), retry_allowance=2, evidence=proof)
    workflow.bind_generation_run(project, 'fixture', run, ['synthetic raster requirement'])
    shutil.copyfile(project / 'art/a-body.png', run / 'references/canonical-base.png')
    hatch.atomic(run / 'extraction-settings.json', hatch.read(run / 'pet_request.json')['extraction'])
    (home / 'external-prompt.md').write_text('External input must remain untouched.\n', encoding='utf-8')
    return project, run, ledger


def _exchange(path, kind, home):
    if kind == 'regular':
        return
    path.unlink()
    if kind == 'external-symlink':
        path.symlink_to(home / 'external-prompt.md')
    elif os.name == 'nt':
        # Windows lacks POSIX FIFOs; a real directory exercises nonregular input.
        path.mkdir()
    else:
        os.mkfifo(path)


def _child_probe(home, boundary, kind):
    project = home / 'project'
    run = project / 'acquisition' / 'fixture'
    marker = home / 'boundary-reached.json'

    def exchange(path, stage):
        _exchange(path, kind, home)
        marker.write_text(json.dumps({'boundary': boundary, 'stage': stage,
                                      'kind': kind}), encoding='utf-8')

    if boundary == 'request':
        original = hatch.manifest
        exchanged = False

        def manifest_then_exchange(*args, **kwargs):
            nonlocal exchanged
            value = original(*args, **kwargs)
            if Path(args[0]).resolve() == run.resolve() and not exchanged:
                exchanged = True
                exchange(run / 'pet_request.json', 'real-manifest-returned')
            return value

        context = patch.object(hatch, 'manifest', manifest_then_exchange)
        action = lambda: workflow.claim_generation(project, 'fixture', 'base')
    elif boundary == 'prompt':
        original = workflow._generation_report

        def report_then_exchange(*args, **kwargs):
            value = original(*args, **kwargs)
            exchange(run / 'prompts/base-pet.md', 'real-generation-report-returned')
            return value

        context = patch.object(workflow, '_generation_report', report_then_exchange)
        action = lambda: workflow.claim_generation(project, 'fixture', 'base')
    else:
        canonical = run / 'references/canonical-base.png'
        settings = run / 'extraction-settings.json'
        data = {'base_review': {'canonical_hash': hashlib.sha256(canonical.read_bytes()).hexdigest(),
                                'settings_hash': hashlib.sha256(settings.read_bytes()).hexdigest()}}
        exchange(canonical if boundary == 'canonical' else settings, 'base-review-input-prepared')
        context = nullcontext()
        action = lambda: hatch.base_review_current(run, data)
    try:
        with context:
            result = action()
    except (ValueError, OSError, RigError) as exc:
        print(json.dumps({'outcome': 'refused', 'exception': type(exc).__name__}), flush=True)
    else:
        accepted = bool(result) if boundary in ('canonical', 'settings') else isinstance(result, dict) and 'token' in result
        print(json.dumps({'outcome': 'accepted' if accepted else 'refused'}), flush=True)


class BudgetInputPathTests(unittest.TestCase):
    def _authority(self, project, run, ledger):
        return {'ledger': ledger.snapshot(),
                'journal': (run / 'imagegen-jobs.json').read_bytes(),
                'workflow': (project / workflow.WORKFLOW_FILE).read_bytes(),
                'owner': (project / 'director-budget-owner.json').read_bytes()}

    def _exercise(self, boundary, kind):
        with tempfile.TemporaryDirectory(prefix='director-bounded-input-') as temporary:
            home = Path(temporary).resolve()
            project, run, ledger = _fixture(home)
            before = self._authority(project, run, ledger)
            external = (home / 'external-prompt.md').read_bytes()
            expired = None
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                    '--probe', str(home), boundary, kind], capture_output=True, text=True, timeout=5)
            except subprocess.TimeoutExpired as exc:
                expired = exc
                result = None
            marker = home / 'boundary-reached.json'
            self.assertTrue(marker.is_file(), 'The intended native file boundary was never reached')
            reached = json.loads(marker.read_text(encoding='utf-8'))
            self.assertEqual(reached['boundary'], boundary)
            self.assertEqual(reached['kind'], kind)
            expected_stage = {'request': 'real-manifest-returned',
                              'prompt': 'real-generation-report-returned',
                              'canonical': 'base-review-input-prepared',
                              'settings': 'base-review-input-prepared'}[boundary]
            self.assertEqual(reached['stage'], expected_stage)
            self.assertEqual((home / 'external-prompt.md').read_bytes(), external,
                             'Production prompt append changed an external file')
            if kind != 'regular':
                self.assertEqual(self._authority(project, run, ledger), before,
                    'Refused input published a token, changed accounting, or rewrote enrollment authority')
            self.assertIsNone(expired,
                f'{boundary} blocked on a real nonregular input after {reached["stage"]}; '
                'the child was terminated after five seconds')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            outcome = json.loads(result.stdout.strip().splitlines()[-1])['outcome']
            self.assertEqual(outcome, 'accepted' if kind == 'regular' else 'refused')
            if kind == 'regular' and boundary in ('request', 'prompt'):
                job = hatch.job_for(hatch.manifest(run), 'base')
                self.assertEqual(job['status'], 'running')
                self.assertEqual(job['attempts'], 1)
                self.assertEqual(len(ledger.snapshot()['state']['reservations']), 1)
            elif boundary in ('canonical', 'settings'):
                self.assertEqual(self._authority(project, run, ledger), before)

    def test_bound_request_replacement_after_real_manifest_is_bounded(self):
        for kind in ('regular', 'nonregular'):
            with self.subTest(kind=kind):
                self._exercise('request', kind)

    def test_production_prompt_read_append_refuses_nonregular_and_external_paths(self):
        kinds = ('regular', 'nonregular', 'external-symlink') if os.name != 'nt' else ('regular', 'nonregular')
        for kind in kinds:
            with self.subTest(kind=kind):
                self._exercise('prompt', kind)

    def test_current_base_review_requires_bounded_regular_canonical_image(self):
        for kind in ('regular', 'nonregular'):
            with self.subTest(kind=kind):
                self._exercise('canonical', kind)

    def test_current_base_review_requires_bounded_regular_extraction_settings(self):
        for kind in ('regular', 'nonregular'):
            with self.subTest(kind=kind):
                self._exercise('settings', kind)


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--probe':
        _child_probe(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        unittest.main()
