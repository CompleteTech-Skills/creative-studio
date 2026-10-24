"""Source-bound DIRECT solved-state replay in the trusted Blender renderer.

Only integer cached poses are verified. Animation between samples is not a
solver result. This produces diagnostic evidence, never artistic approval.
"""
from pathlib import Path
import shutil
import tempfile
import time

import physics_backends as p


def render_direct_cache(cache, destination, executable=None):
    destination = p._new_destination(destination)
    p.verify_cache(cache)
    with tempfile.TemporaryDirectory(prefix='hatch-physics-prepared-') as temporary:
        root = Path(temporary)
        source = root/'source-cache'
        source.mkdir(mode=0o700)
        with p._Reader(cache) as reader:
            manifest_bytes = reader.read('cache.json', p.MAX_DOCUMENT_BYTES)
            manifest = p.parse(manifest_bytes)
            p.require(manifest['backend']['id'] == 'pybullet', 'PHYSICS_REPLAY_SOURCE',
                      'Replay requires an explicitly selected DIRECT cache.')
            p._write(source/'cache.json', manifest_bytes)
            for item in manifest['files']:
                payload = reader.read(item['path'], int(item['bytes']))
                p.require(len(payload) == item['bytes'] and p.digest(payload) == item['sha256'],
                          'PHYSICS_CACHE_CORRUPT', 'Source changed during replay snapshot.')
                p._write(source/item['path'], payload)
        verified = p.verify_cache(source)
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
        for mode in ('render-replay', 'reopen-replay'):
            job['mode'] = mode
            job_path = root/'worker-job.json'
            if job_path.exists(): job_path.unlink()
            p._write(job_path, p.canonical(job))
            report_path = root/'worker-report.json'
            if report_path.exists(): report_path.unlink()
            p._run_process(backend._command(root), root, timeout=p._remaining(deadline),
                           memory_bytes=world['budgets']['memory_bytes'],
                           output_bytes=min(world['budgets']['temporary_bytes'], p.MAX_CACHE_BYTES), label=mode)
            with p._Reader(root) as reader:
                report = p.parse(reader.read('worker-report.json', p.MAX_DOCUMENT_BYTES))
            p.require(report.get('ok') is True and report.get('operation') == mode and
                      report.get('source_cache_sha256') == replay['cache_manifest_sha256'] and
                      report.get('states_sha256') == replay['states_sha256'] and
                      report.get('maximum_replay_error', 1) <= 1e-5 and
                      report.get('verified_frames') == len(p.parse((source/'states.json').read_bytes())['samples']),
                      'PHYSICS_REPLAY_FAILED', 'Actual Blender replay verification failed.', report=report)
            reports.append(report)
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
                    p.require(total <= min(p.MAX_CACHE_BYTES, world['budgets']['cache_bytes'], world['budgets']['temporary_bytes']),
                              'PHYSICS_RESOURCE_LIMIT', 'Replay retained output exceeds admitted budget.')
                    p._write(stage/name, payload)
                    inventory.append(dict(path=name, bytes=len(payload), sha256=p.digest(payload)))
            p._write(stage/'replay.json', p.canonical(dict(**result, files=inventory)))
            p._publish(stage, destination)
        finally:
            if stage.exists(): shutil.rmtree(stage)
        return result
