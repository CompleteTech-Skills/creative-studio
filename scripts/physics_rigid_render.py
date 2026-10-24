"""Source-bound DIRECT solved-state replay in the trusted Blender renderer.

Only integer cached poses are verified. Animation between samples is not a
solver result. This produces diagnostic evidence, never artistic approval.
"""
from pathlib import Path
import math
import re
import shutil
import tempfile
import time

import physics_backends as p


def _check_report(report, mode, replay, frame_count, *, worker_sha256, world_sha256):
    """Treat worker output as untrusted structured data, even on failure."""
    fail = lambda condition: p.require(condition, 'PHYSICS_REPLAY_FAILED',
                                       'Malformed or mismatched Blender replay report.')
    keys = {'ok', 'operation', 'backend', 'verified_frames', 'maximum_replay_error',
            'source_cache_sha256', 'states_sha256', 'previews', 'blend_sha256', 'scope', 'artistic_review',
            'worker_sha256', 'world_sha256', 'source_backend'}
    fail(type(report) is dict and set(report) == keys)
    fail(report['ok'] is True and report['operation'] == mode and
         report['source_cache_sha256'] == replay['cache_manifest_sha256'] and
         report['states_sha256'] == replay['states_sha256'] and
         report['worker_sha256'] == worker_sha256 and report['world_sha256'] == world_sha256 and
         report['source_backend'] == replay['source_backend'] and
         type(report['verified_frames']) is int and report['verified_frames'] == frame_count and
         type(report['maximum_replay_error']) in (int, float) and
         math.isfinite(report['maximum_replay_error']) and 0 <= report['maximum_replay_error'] <= 1e-5 and
         type(report['blend_sha256']) is str and re.fullmatch('[0-9a-f]{64}', report['blend_sha256']) is not None and
         report['scope'] == 'DIRECT solved-state replay; Blender rendering only' and
         report['artistic_review'] == 'not-performed')
    backend = report['backend']
    fail(type(backend) is dict)
    fail(set(backend) == {'application_version', 'application_build', 'solver_name', 'solver_version',
                          'platform', 'precision', 'device', 'render_device_discovery', 'render_devices',
                          'physics_gpu_support', 'render_gpu_execution'})
    fail(type(backend.get('application_version')) is str and
         re.fullmatch(r'4\.5\.3(?: LTS)?', backend['application_version']) is not None and
         backend.get('application_build') == p.SUPPORTED_BLENDER_BUILD and
         backend.get('solver_name') == 'Blender Bullet rigid-body world' and
         backend.get('device') == 'cpu' and backend.get('physics_gpu_support') == 'unsupported' and
         backend.get('render_gpu_execution') == 'not-executed' and
         backend.get('solver_version') == 'not exposed separately by Blender' and
         backend.get('precision') == 'mixed' and type(backend.get('platform')) is str and
         type(backend.get('render_device_discovery')) is str and
         type(backend.get('render_devices')) is list and len(backend['render_devices']) <= 64)
    for device in backend['render_devices']:
        fail(type(device) is dict and set(device) == {'name', 'type'} and
             all(type(device[key]) is str and len(device[key]) <= 256 for key in ('name', 'type')))
    previews = report['previews']
    fail(type(previews) is list)
    frames = sorted({0, frame_count//2, frame_count-1}) if mode == 'render-replay' else []
    fail(len(previews) == len(frames))
    image_keys = {'path', 'sha256', 'engine', 'device', 'width', 'height', 'samples',
                  'scope', 'visual_review', 'frame'}
    for image, frame in zip(previews, frames):
        fail(type(image) is dict and set(image) == image_keys)
        fail(type(image['frame']) is int and image['frame'] == frame and
             image['path'] == ('preview.png' if frame == 0 else 'preview-%04d.png' % frame) and
             type(image['sha256']) is str and re.fullmatch('[0-9a-f]{64}', image['sha256']) is not None and
             image['engine'] == 'Cycles' and image['device'] == 'CPU' and
             all(type(image[key]) is int for key in ('width', 'height', 'samples')) and
             (image['width'], image['height'], image['samples']) == (128, 128, 4) and
             image['scope'] == 'DIRECT solved-state replay; neutral diagnostic only' and
             image['visual_review'] == 'not-performed')


def _unchanged(root, bindings):
    with p._Reader(root) as reader:
        for name, expected in bindings.items():
            p.require(p.digest(reader.read(name)) == expected, 'PHYSICS_INPUT_CHANGED',
                      'Private replay input/output changed: '+name)


def render_direct_cache(cache, destination, executable=None):
    destination = p._new_destination(destination)
    initial = p.verify_cache(cache)
    with tempfile.TemporaryDirectory(prefix='hatch-physics-prepared-') as temporary:
        root = Path(temporary)
        source = root/'source-cache'
        source.mkdir(mode=0o700)
        with p._Reader(cache) as reader:
            manifest_bytes = reader.read('cache.json', p.MAX_DOCUMENT_BYTES)
            manifest = p.parse(manifest_bytes)
            validator, error_type = p._contracts()
            try:
                validator(manifest, 'PhysicsCache')
            except (error_type, KeyError, TypeError, ValueError) as exc:
                raise p.BackendError('PHYSICS_CACHE_CORRUPT', 'Invalid replay source manifest.') from exc
            p.require(len(manifest['files']) <= p.MAX_FILES and
                      len(manifest_bytes)+sum(item['bytes'] for item in manifest['files']) <= p.MAX_CACHE_BYTES,
                      'PHYSICS_RESOURCE_LIMIT', 'Replay snapshot exceeds source limits.')
            p._paths(item['path'] for item in manifest['files'])
            p.require('cache.json' not in {item['path'] for item in manifest['files']} and
                      reader.names() == {item['path'] for item in manifest['files']} | {'cache.json'},
                      'PHYSICS_CACHE_CORRUPT', 'Source inventory changed during replay snapshot.')
            p.require(manifest['backend']['id'] == 'pybullet', 'PHYSICS_REPLAY_SOURCE',
                      'Replay requires an explicitly selected DIRECT cache.')
            p._write(source/'cache.json', manifest_bytes)
            for item in manifest['files']:
                payload = reader.read(item['path'], int(item['bytes']))
                p.require(len(payload) == item['bytes'] and p.digest(payload) == item['sha256'],
                          'PHYSICS_CACHE_CORRUPT', 'Source changed during replay snapshot.')
                p._write(source/item['path'], payload)
        verified = p.verify_cache(source, expected_world_sha256=initial['world_sha256'])
        world = p.parse((source/'world.json').read_bytes())
        receipt = p.parse((source/'verification.json').read_bytes())
        _, meshes, _, _ = p._source_admission(source/'inputs', world, receipt['project_manifest'])
        worker = p._worker_bytes('blender')
        p._write(root/'worker.py', worker)
        files = [{'path': 'cache.json', 'bytes': len(manifest_bytes), 'sha256': p.digest(manifest_bytes)},
                 *manifest['files']]
        replay = dict(cache_manifest_sha256=p.digest(manifest_bytes),
                      states_sha256=p.digest((source/'states.json').read_bytes()),
                      source_files=files, project_manifest=receipt['project_manifest'],
                      source_backend=manifest['backend'], meshes_sha256=p.digest(p.canonical(meshes)))
        job = dict(protocol=p.PROTOCOL, mode='render-replay', world=world,
                   world_sha256=p.digest(p.canonical(world)), meshes=meshes,
                   worker_sha256=p.digest(worker), fixture=manifest['fixture'], replay=replay)
        backend = p.BlenderBackend(executable)
        deadline = time.monotonic()+min(300., float(world['budgets']['timeout_seconds']))
        identity = backend._discover(timeout=min(10., p._remaining(deadline)))
        reports = []
        frame_count = len(p.parse((source/'states.json').read_bytes())['samples'])
        bindings = {'worker.py': p.digest(worker)}
        bindings.update({'source-cache/'+item['path']: item['sha256'] for item in files})
        for mode in ('render-replay', 'reopen-replay'):
            job['mode'] = mode
            job_path = root/'worker-job.json'
            if job_path.exists(): job_path.unlink()
            job_bytes = p.canonical(job)
            p._write(job_path, job_bytes)
            bindings['worker-job.json'] = p.digest(job_bytes)
            _unchanged(root, bindings)
            report_path = root/'worker-report.json'
            if report_path.exists(): report_path.unlink()
            p._run_process(backend._command(root), root, timeout=p._remaining(deadline),
                           memory_bytes=world['budgets']['memory_bytes'],
                           output_bytes=min(world['budgets']['temporary_bytes'], p.MAX_CACHE_BYTES), label=mode)
            _unchanged(root, bindings)
            with p._Reader(root) as reader:
                report = p.parse(reader.read('worker-report.json', p.MAX_DOCUMENT_BYTES))
            _check_report(report, mode, replay, frame_count, worker_sha256=job['worker_sha256'],
                          world_sha256=job['world_sha256'])
            with p._Reader(root) as reader:
                p.require(p.digest(reader.read('scene.blend')) == report['blend_sha256'],
                          'PHYSICS_REPLAY_FAILED', 'Actual retained blend differs from report.')
            bindings['scene.blend'] = report['blend_sha256']
            p._write(root/(mode+'-report.json'), p.canonical(report))
            reports.append(report)
            for image in report['previews']:
                bindings[image['path']] = image['sha256']
        with p._Reader(root) as reader:
            previews = reports[0].get('previews', [])
            expected_frames = sorted({0, len(p.parse((source/'states.json').read_bytes())['samples'])//2,
                                      len(p.parse((source/'states.json').read_bytes())['samples'])-1})
            p.require([image.get('frame') for image in previews] == expected_frames,
                      'PHYSICS_REPLAY_FAILED', 'Representative replay renders missing.')
            for image in previews:
                payload = reader.read(image['path'])
                p.require(p.digest(payload) == image['sha256'] and payload[:8] == b'\x89PNG\r\n\x1a\n' and
                          len(payload) > 24 and int.from_bytes(payload[16:20], 'big') == 128 and
                          int.from_bytes(payload[20:24], 'big') == 128,
                          'PHYSICS_REPLAY_FAILED', 'Actual neutral replay PNG differs.')
        # Both the retained native scene and every source byte survive independent reopen.
        p.verify_cache(source)
        p.require(reports[0]['blend_sha256'] == reports[1]['blend_sha256'],
                  'PHYSICS_REPLAY_FAILED', 'Independent reopen modified the replay scene.')
        result = dict(kind='physics-direct-blender-replay', version=1, source=verified,
                      source_cache_sha256=replay['cache_manifest_sha256'], renderer=identity,
                      reports=reports, artistic_review='not-performed',
                      scope='DIRECT solved-state replay; Blender rendering only; integer cached frames verified',
                      interpolation_accuracy='not-established')
        p._write(root/'replay-report.json', p.canonical(result))
        # Exclusive publication stages on the destination filesystem.
        stage = Path(tempfile.mkdtemp(prefix='.hatch-replay-', dir=destination.parent))
        try:
            with p._Reader(root) as reader:
                inventory = []
                total = 0
                for name in sorted(reader.names()):
                    payload = reader.read(name)
                    total += len(payload)
                    p.require(total <= min(p.MAX_CACHE_BYTES, world['budgets']['cache_bytes'], world['budgets']['temporary_bytes']) and
                              len(payload) <= world['budgets']['bake_file_bytes'],
                              'PHYSICS_RESOURCE_LIMIT', 'Replay retained output exceeds admitted budget.')
                    p._write(stage/name, payload)
                    inventory.append(dict(path=name, bytes=len(payload), sha256=p.digest(payload)))
            replay_manifest = p.canonical(dict(**result, files=inventory))
            p.require(total+len(replay_manifest) <= min(p.MAX_CACHE_BYTES, world['budgets']['cache_bytes'],
                                                       world['budgets']['temporary_bytes']) and
                      len(replay_manifest) <= world['budgets']['bake_file_bytes'],
                      'PHYSICS_RESOURCE_LIMIT', 'Replay inventory exceeds admitted retained budget.')
            p._write(stage/'replay.json', replay_manifest)
            p._publish(stage, destination)
        finally:
            if stage.exists(): shutil.rmtree(stage)
        return result
