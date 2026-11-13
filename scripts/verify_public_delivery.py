#!/usr/bin/env python3
"""Reproduce the public delivery gates using only this checkout's pinned inputs.

The default restores the production project and verifies a portable archive.
--tests runs every 2D regression; --test-scope all also includes optional 3D
regressions and requires their tools. --render validates both complete films and
editor handoffs. --video additionally requires working FFmpeg/ffprobe. --compact keeps
all frame/audio checks but retains only reports, storyboards, movies and one
layered drawing per scene; complete editor handoffs are exercised in normal mode.
No provider, new TTS recording or artistic approval is requested or inferred.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unittest

from PIL import __version__ as pillow_version

from production_contracts import load_project
from production_delivery import restore_pilot, verify_delivery
from production_sources import _new_destination
import production_workflow as workflow

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / 'examples/production-pilot'
OPTIONAL_NATIVE_KRITA = (
    'test_production_adapter.ProductionAdapterTests.'
    'test_actual_local_krita_import_export_and_layer_roundtrip')
WINDOWS_NO_FIFO = (
    'test_style_catalog.CatalogIntegrityTests.'
    'test_loader_rejects_named_pipes_without_blocking')


def write_json(path, value):
    path = Path(path)
    # Keep the last valid checkpoint if a disk fills during a later write.
    payload = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def checkout_snapshot():
    names = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z', '--cached', '--others',
                            '--exclude-standard'], check=True, capture_output=True).stdout
    paths = sorted(set(os.fsdecode(name) for name in names.split(b'\0') if name))
    return {name: file_sha(ROOT / name) for name in paths if (ROOT / name).is_file()}


def build_regression_suite(scope='2d'):
    """Select modules before import so 2D delivery needs no optional 3D tools."""
    if scope not in ('2d', 'all'):
        raise ValueError('unknown regression scope: ' + str(scope))
    directory = ROOT / 'tests'
    sys.path.insert(0, str(directory))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for path in sorted(directory.glob('test_*.py')):
        if scope == '2d' and path.name.startswith(('test_rig_', 'test_model3d_', 'test_render3d_')):
            continue
        suite.addTests(loader.discover(str(directory), pattern=path.name))
    return suite


def run_tests(destination, *, scope='2d'):
    """Missing required dependencies and unexpected skips fail the delivery gate."""
    os.environ['HATCH_DECOMPOSITION_BROWSER_TEST'] = '1'
    suite = build_regression_suite(scope)
    started = time.monotonic()
    with (destination / 'tests.log').open('w', encoding='utf-8', newline='\n') as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    skipped = [{'test': test.id(), 'reason': reason} for test, reason in result.skipped]
    allowed = {OPTIONAL_NATIVE_KRITA}
    if os.name == 'nt' and not hasattr(os, 'mkfifo'):
        allowed.add(WINDOWS_NO_FIFO)
    unexpected = [item for item in skipped if item['test'] not in allowed]
    return {'ok': result.wasSuccessful() and not unexpected, 'scope': scope,
            'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': skipped, 'unexpected_skips': unexpected,
            'seconds': round(time.monotonic() - started, 3), 'log': 'tests.log',
            'log_sha256': file_sha(destination / 'tests.log'),
            'failure_details': [{'test': test.id(), 'traceback': details}
                                for test, details in result.failures + result.errors]}


def archive_roundtrip(project, destination):
    archive = destination / 'portable-project.zip'
    exported = workflow.archive_project(project, archive)
    original = json.loads((project / 'production-workflow.json').read_bytes())
    with tempfile.TemporaryDirectory(prefix='.archive-check-', dir=destination) as temporary:
        restored_path = Path(temporary) / 'project'
        restored = workflow.restore_project(archive, restored_path)
        state = json.loads((restored_path / 'production-workflow.json').read_bytes())
        if state['reviews'] != original['reviews']:
            raise ValueError('portable archive changed the historical review records')
        load_project(restored_path)
    return {'verified': True, 'sha256': exported['sha256'],
            'file_count': exported['file_count'], 'path': archive.name,
            'review_records_preserved': len(original['reviews']),
            'restoration_event_appended': True, 'renderer_changed': restored['renderer_changed'],
            'external_integration_verified': False}


def render_delivery(project, destination, *, compact, video, checkpoint):
    from production_adapter import export_editor_handoff, export_openraster, validate_handoff
    from production_render import contact_sheet, render_scene, render_shot, validate_render, mux_video
    bundle = load_project(project)
    evidence = destination / 'evidence'
    evidence.mkdir()
    reports = {}

    def progress(event):
        done = event.get('completed', 0)
        if done == 1 or done == event.get('total') or done % 120 == 0:
            print(json.dumps({'render_progress': event}), flush=True)

    for scene_id, frames, seconds in (('seed-exchange', 432, 36), ('garden-promise', 264, 22)):
        render = project / 'renders' / scene_id
        first = render_scene(bundle, scene_id, render, track_layers=not compact, progress=progress)
        valid = validate_render(render)
        if (valid['frame_count'] != frames or valid['duration'] != {'numerator': seconds, 'denominator': 1}
                or valid['sample_count'] != seconds * 48000 or first['fixture']):
            raise ValueError('production scene frame/audio contract differs: ' + scene_id)
        # Resume must reuse every frame, preserve each raster hash and preserve
        # the persisted manifest. Counters themselves are intentionally transient.
        second = render_scene(bundle, scene_id, render, track_layers=not compact)
        repeated = validate_render(render)
        if (second['operation'] != {'rendered_frames': 0, 'reused_frames': frames}
                or valid != repeated or first['frames'] != second['frames']):
            raise ValueError('repeat render differs or failed to reuse verified frames: ' + scene_id)
        sheet = contact_sheet(render, evidence / (scene_id + '.png'))
        summary = {'validation': valid, 'initial_operation': first['operation'],
                   'repeat_operation': second['operation'], 'deterministic_resume': True,
                   'storyboard_sha256': sheet['sha256'], 'track_layers': not compact,
                   'frame_hashes': [entry['sha256'] for entry in first['frames']],
                   'audio_sha256': first['audio']['sha256'],
                   'visual_review_performed': False, 'new_approval_recorded': False}
        shutil.copyfile(render / 'render-manifest.json', evidence / (scene_id + '-manifest.json'))
        if video:
            movie = mux_video(render)
            shutil.copyfile(render / movie['path'], evidence / (scene_id + '.mp4'))
            summary['video'] = movie
        if compact:
            # A real production frame tests raster layer export without retaining
            # hundreds of auxiliary layer PNGs on a space-constrained workstation.
            shot = first['shots'][0]['id']
            drawing_render = destination / ('drawing-' + scene_id)
            render_shot(bundle, shot, drawing_render, max_frames=1, track_layers=True)
            summary['drawing'] = export_openraster(
                drawing_render, 0, evidence / (scene_id + '.ora'))
            summary['editor_handoff'] = {'performed': False,
                'reason': 'Compact mode verifies a layered drawing; run without --compact for complete editor handoffs.'}
            shutil.rmtree(drawing_render)
            shutil.rmtree(render)
            summary['full_render_files_retained'] = False
        else:
            handoff = destination / 'editor-handoffs' / scene_id
            exported = export_editor_handoff(render, handoff)
            summary['editor_handoff'] = validate_handoff(handoff)
            summary['editor_handoff']['drawings'] = exported['drawings']
            summary['full_render_files_retained'] = True
        reports[scene_id] = summary
        checkpoint(reports)
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, help='New directory for reports and reconstructed outputs')
    parser.add_argument('--tests', action='store_true', help='Run the selected regression scope with strict skip accounting')
    parser.add_argument('--test-scope', choices=('2d', 'all'), default='2d',
                        help='2d includes every existing delivery test; all also requires optional 3D dependencies/tools')
    parser.add_argument('--render', action='store_true', help='Render both entire production scenes')
    parser.add_argument('--video', action='store_true', help='Require and validate H.264/AAC movies as well')
    parser.add_argument('--compact', action='store_true', help='Keep reports and media; remove completed frame trees')
    args = parser.parse_args()
    if args.video and not args.render:
        parser.error('--video requires --render')
    for name in ('HATCH_PRODUCTION_PILOT', 'HATCH_PRODUCTION_PILOT_ROOT'):
        if os.environ.get(name):
            parser.error(name + ' must be unset; verification uses only the public checkout')
    destination = _new_destination(args.destination)
    if destination == ROOT or destination.is_relative_to(PILOT):
        parser.error('verification output must be outside the retained public pilot')
    destination.mkdir(parents=True)
    revision = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True).stdout.strip()
    report = {'schema_version': 1, 'kind': 'hatch-public-delivery-verification', 'status': 'running',
              'revision': revision, 'python': platform.python_version(), 'pillow': pillow_version,
              'platform': platform.system(), 'fixture': False, 'private_asset_override': False,
              'options': {'tests': args.tests, 'test_scope': args.test_scope,
                          'render': args.render, 'video': args.video, 'compact': args.compact},
              'provider_called': False, 'tts_called': False, 'new_approval_recorded': False}
    report['checkout_files_sha256'] = checkout_snapshot()
    report['worktree_dirty'] = bool(subprocess.run(
        ['git', '-C', str(ROOT), 'status', '--porcelain'], check=True,
        capture_output=True, text=True).stdout.strip())
    report_path = destination / 'report.json'
    write_json(report_path, report)
    try:
        print('Verifying all public bundles, provenance and historical evidence.', flush=True)
        report['inventory'] = verify_delivery(PILOT)
        write_json(report_path, report)
        print('Restoring the production project and verifying portable archive roundtrip.', flush=True)
        project = destination / 'project'
        report['restoration'] = restore_pilot(PILOT, project)
        report['archive'] = archive_roundtrip(project, destination)
        write_json(report_path, report)
        if args.tests:
            print('Running the complete ' + args.test_scope + ' regression scope; detailed progress is in tests.log.', flush=True)
            report['tests'] = run_tests(destination, scope=args.test_scope)
            write_json(report_path, report)
            if not report['tests']['ok']:
                raise ValueError('required regression failed or skipped; inspect report.json and tests.log')
        if args.render:
            def checkpoint(scenes):
                report['scenes'] = scenes
                write_json(report_path, report)
            report['scenes'] = render_delivery(project, destination, compact=args.compact,
                                                video=args.video, checkpoint=checkpoint)
        if checkout_snapshot() != report['checkout_files_sha256']:
            raise ValueError('checkout changed while validation was running; freeze inputs and rerun')
        report['checkout_unchanged'] = True
        report['status'] = 'passed'
        # A compact run is useful evidence but does not claim the complete CI gate.
        report['complete_delivery_gate'] = args.tests and args.render and not args.compact
        write_json(report_path, report)
        print(str(report_path), flush=True)
        return 0
    except Exception as exc:
        report.update(status='failed', error=str(exc), traceback=traceback.format_exc(), complete_delivery_gate=False)
        try:
            write_json(report_path, report)
        except OSError as checkpoint_error:
            print('Could not write failure checkpoint; prior valid report retained: ' + str(checkpoint_error),
                  file=sys.stderr)
        print(str(report_path), file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
