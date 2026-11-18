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
    """Run one stage with a shared wall deadline and kernel-measured peak resident usage."""
    remaining = deadline - time.monotonic()
    require(remaining > 0, 'ASSET_BLENDER_TIMEOUT', 'Blender fixture deadline expired.')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    env.update(HOME=str(root), TMPDIR=str(root), BLENDER_USER_RESOURCES=str(root),
               OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2')
    # Blender's mappings reserve more address space than the resident working
    # set. Verify the resident budget separately at child exit.
    memory = limits['memory_bytes']
    arguments = ['/usr/bin/prlimit', f'--as={memory * 8}', f'--cpu={limits["timeout_seconds"]}',
               f'--fsize={byte_limit}', '--nofile=128:128', '--', '<blender>',
               '--background', '--factory-startup', '--disable-autoexec', '--threads', '2',
               '--python-exit-code', '1', '--python', '<worker>', '--', mode]
    substitutions = {'<blender>': executable, '<worker>': str(WORKER)}
    command = [substitutions.get(argument, argument) for argument in arguments]
    log = root / f'{mode}.log'
    start = time.monotonic()
    with log.open('xb') as output:
        process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while True:
                try:
                    pid, status, usage = os.wait4(process.pid, os.WNOHANG)
                except OSError as exc:
                    raise RigError('ASSET_BLENDER_RSS', 'Blender child resource accounting failed.') from exc
                if pid == process.pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
                    break
                if time.monotonic() >= deadline:
                    raise RigError('ASSET_BLENDER_TIMEOUT', 'Blender fixture deadline expired.', retryable=True)
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
    require(usage is not None and usage.ru_maxrss > 0, 'ASSET_BLENDER_RSS',
            'Blender child did not provide a measured peak resident size.')
    peak = usage.ru_maxrss * 1024  # Linux reports KiB.
    require(peak <= memory, 'RIG_LIMIT_MEMORY', 'Blender peak resident memory exceeds admission.',
            expected=memory, observed=peak)
    data = read_source_bytes(log, MAX_LOG_BYTES)
    require(process.returncode == 0, 'ASSET_BLENDER_WORKER',
            'The pinned Blender worker failed during the requested fixture stage.',
            observed=process.returncode)
    return dict(status='completed', exit_code=process.returncode, argv=arguments, cwd='<workspace>',
                seconds=time.monotonic() - start, peak_rss_bytes=peak, peak_rss_source='linux-wait4',
                resident_budget_bytes=memory, virtual_limit_bytes=memory * 8, log_limit_bytes=MAX_LOG_BYTES,
                log_sha256=hashlib.sha256(data).hexdigest())


def create_fixture(root, limits):
    """Save a packed app-owned triangle and reopen that exact file before export."""
    require(sys.platform == 'linux' and hasattr(os, 'wait4') and Path('/usr/bin/prlimit').is_file(),
            'ASSET_BLENDER_UNAVAILABLE', 'Blender fixture requires Linux prlimit and wait4.')
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
    require(os.fsencode(root.parent.resolve()) not in data, 'ASSET_BLENDER_SOURCE',
            'Blender serialized a private fixture staging path.')
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
        shared_wall_timeout_seconds=limits['timeout_seconds'],
        command_policy='private-cwd/fixed-package-worker/no-shell/factory/autoexec-disabled'))
