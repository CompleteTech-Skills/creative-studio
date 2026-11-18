"""Operate only on a fixture created by the fixed worker in private staging."""
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from asset_library import read_source_bytes
from asset_library_contracts import MAX_FILE_BYTES
from rig_blender import find_blender
from rig_errors import RigError, decode_json, require

BLENDER_VERSION = '4.5.3'
BLENDER_BUILD = '67807e1800cc'
WORKER = Path(__file__).resolve().with_name('asset_import_worker.py')
MAX_LOG_BYTES = 128 * 1024


def _run(executable, mode, root, limits, byte_limit, deadline):
    """Run one stage with a shared wall deadline and OS-enforced child bounds."""
    remaining = deadline - time.monotonic()
    require(remaining > 0, 'ASSET_BLENDER_TIMEOUT', 'Blender fixture deadline expired.')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    env.update(HOME=str(root), TMPDIR=str(root), BLENDER_USER_RESOURCES=str(root),
               OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2')
    # Blender's mappings reserve more address space than the resident working
    # set. Retain the legacy virtual ceiling plus the effective RSS ceiling.
    memory = limits['memory_bytes']
    command = ['/usr/bin/prlimit', f'--as={memory * 8}', f'--cpu={limits["timeout_seconds"]}',
               f'--fsize={byte_limit}', '--nofile=128:128', '--', executable,
               '--background', '--factory-startup', '--disable-autoexec', '--threads', '2',
               '--python-exit-code', '1', '--python', str(WORKER), '--', mode]
    log = root / f'{mode}.log'
    start = time.monotonic()
    peak = 0
    with log.open('xb') as output:
        process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise RigError('ASSET_BLENDER_TIMEOUT', 'Blender fixture deadline expired.', retryable=True)
                try:
                    status = Path(f'/proc/{process.pid}/status').read_text()
                    rss = next((int(line.split()[1]) * 1024 for line in status.splitlines()
                                if line.startswith('VmRSS:')), 0)
                    peak = max(peak, rss)
                except FileNotFoundError:
                    pass
                require(peak <= memory, 'RIG_LIMIT_MEMORY', 'Blender exceeds the admitted resident memory.')
                require(log.stat().st_size <= MAX_LOG_BYTES, 'ASSET_BLENDER_LOG', 'Blender log is too large.')
                time.sleep(.02)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    require(time.monotonic() < deadline, 'ASSET_BLENDER_TIMEOUT', 'Blender fixture deadline expired.',
            retryable=True)
    data = read_source_bytes(log, MAX_LOG_BYTES)
    require(process.returncode == 0, 'ASSET_BLENDER_WORKER',
            'The pinned Blender worker failed during the requested fixture stage.',
            observed=process.returncode)
    return dict(seconds=time.monotonic() - start, peak_rss_bytes=peak,
                log_sha256=hashlib.sha256(data).hexdigest())


def create_fixture(root, limits):
    """Save a packed app-owned triangle and reopen that exact file before export."""
    require(sys.platform == 'linux' and Path('/usr/bin/prlimit').is_file(),
            'ASSET_BLENDER_UNAVAILABLE', 'Blender fixture requires Linux prlimit.')
    executable = find_blender()
    root.mkdir(mode=0o700)
    limit = min(MAX_FILE_BYTES, limits['source_bytes'], limits['dependency_bytes'])
    require(limits['memory_bytes'] >= 384 * 1024**2,
            'ASSET_BLENDER_BUDGET', 'Insufficient resource budget for a Blender process.')
    deadline = time.monotonic() + limits['timeout_seconds']
    create = _run(executable, 'create', root, limits, limit, deadline)
    original = root / 'fixture.blend'
    data = read_source_bytes(original, limit)
    require(data.startswith(b'BLENDER'), 'ASSET_BLENDER_SOURCE', 'Missing native Blender fixture.')
    digest = hashlib.sha256(data).hexdigest()
    reopen = _run(executable, 'reopen', root, limits, limit, deadline)
    require(hashlib.sha256(read_source_bytes(original, limit)).hexdigest() == digest,
            'ASSET_BLENDER_SOURCE', 'The staged source changed during reopen.')
    report = decode_json(read_source_bytes(root / 'reopen.json', 4096))
    observed = dict(version=BLENDER_VERSION, build=BLENDER_BUILD, vertices=3, triangles=1,
                    unit_scale=1, external_dependencies=0)
    require(report == observed, 'ASSET_BLENDER_SOURCE',
            'The worker did not report the admitted Blender fixture profile.')
    return (original, root / 'roundtrip.glb', dict(inspection=report,
        worker_sha256=hashlib.sha256(read_source_bytes(WORKER, 65536)).hexdigest(),
        source_sha256=digest, source_bytes=len(data), create=create, reopen=reopen,
        command_policy='private-cwd/fixed-package-worker/no-shell/factory/autoexec-disabled'))
