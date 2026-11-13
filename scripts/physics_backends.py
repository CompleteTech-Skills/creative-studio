"""Trusted, bounded optional physics backend lifecycle.

Imports are standard-library-only. Native contracts, geometry and scene owners
are loaded only at explicit physics admission. No uploaded Python or .blend is
ever executed. Source bytes, native scene validation, solver execution and
production approval are different evidence classes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from fractions import Fraction
from physics_timing import clock as timing_clock, interpolate as interpolate_state, rational
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Literal, Mapping
import unicodedata
import uuid

from rig_errors import RigError, atomic_json, decode_json, finite_json, job_lock, object_hash, portable_name
from physics_backend_evidence import (EvidenceError, records as evidence_records,
    native_report as evidence_native_report, direct_report as evidence_direct_report,
    module_identity as evidence_module_identity,blend_container)


ADAPTER_VERSION = '1.0.0'
PROTOCOL = 1
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_FILES = 256
MAX_BODIES = 32
MAX_FRAMES = 1024
LEGACY_WORKERS = {
    'blender':'bb5acec20ceb5210c48e29c1dc7f0247c82264d133c84b158ac867f68e58d7d5',
    'pybullet':'93536f325e1eac20343f8a99eca629a57b9dee8b00bc2dcbf7b8fa688f89fca0',
}
SUPPORTED_BLENDER = (4, 5, 3)
SUPPORTED_BLENDER_BUILD = '67807e1800cc'
SUPPORTED_CATEGORIES = ('rigid-body',)
ALL_CATEGORIES = ('rigid-body', 'character', 'cloth', 'soft-body', 'hair', 'particles', 'gas', 'liquid', 'fracture')
Status = Literal['available', 'unavailable', 'unsupported', 'experimental', 'degraded', 'failed-validation']


class BackendError(ValueError):
    def __init__(self, code, message, *, category='setup-error', last_valid_frame=None):
        super().__init__(message)
        self.report = dict(status='failed', code=code, message=message, category=category,
                           last_valid_frame=last_valid_frame, cache_valid=False,
                           recoverable=code in ('PHYSICS_TIMEOUT', 'PHYSICS_CANCELLED', 'PHYSICS_BACKEND_CRASH'),
                           recommended_action='Correct the reported cause; restart cleanly. No partial result is reusable.')


def require(condition, code, message, **details):
    if not condition:
        raise BackendError(code, message, **details)


def canonical(value):
    try:
        finite_json(value)
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, RigError) as exc:
        raise BackendError('PHYSICS_SCHEMA', 'Expected bounded finite JSON: ' + str(exc), category='invalid-input') from exc


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def parse(payload):
    require(len(payload) <= MAX_DOCUMENT_BYTES, 'PHYSICS_RESOURCE_LIMIT', 'JSON exceeds 32 MiB.', category='resource-exhaustion')
    try:
        return decode_json(payload)
    except (RigError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise BackendError('PHYSICS_SCHEMA', 'Invalid bounded JSON: ' + str(exc), category='invalid-input') from exc


def path_name(value):
    try:
        require(type(value) is str and portable_name(value) == value,
                'PHYSICS_PATH', 'Use a canonical project-relative portable path.', category='invalid-input')
        require(unicodedata.normalize('NFC',value)==value and not any(
            part.split('.',1)[0].casefold() in {prefix+digit for prefix in ('com','lpt') for digit in '¹²³'}
            for part in value.split('/')),
            'PHYSICS_PATH','Unicode aliases and Windows device names are not portable paths.',category='invalid-input')
    except RigError as exc:
        raise BackendError('PHYSICS_PATH', str(exc), category='invalid-input') from exc
    return value


def _paths(names):
    seen = set()
    for name in names:
        path_name(name)
        folded = name.casefold()
        require(folded not in seen, 'PHYSICS_PATH_ALIAS', 'Case-alias or duplicate file path.', category='invalid-input')
        seen.add(folded)
    for name in seen:
        parts = name.split('/')
        require(not any('/'.join(parts[:i]) in seen for i in range(1, len(parts))),
                'PHYSICS_PATH_ALIAS', 'A file path is also an ancestor of another file.', category='invalid-input')


def _contracts():
    try:
        from physics_contracts import validate_contract, PhysicsError
    except ImportError as exc:
        raise BackendError('PHYSICS_DEPENDENCY', 'Install the physics contract foundation (#167).') from exc
    return validate_contract, PhysicsError


def _world(value):
    validator, error_type = _contracts()
    value = parse(canonical(value))
    try:
        validator(value, 'PhysicsWorld')
    except (error_type, RigError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise BackendError('PHYSICS_SCHEMA', str(exc), category='invalid-input') from exc
    # JSON Schema's integer type also admits exact integral JSON floats.
    # Canonicalize only semantically integer fields before indexing/ranges.
    for field in ('fps',):
        value[field] = {key: int(item) for key, item in value[field].items()}
    value['frame_range'] = {key: int(item) for key, item in value['frame_range'].items()}
    for force in value['forces']:
        force['frame_range'] = {key:int(item) for key,item in force['frame_range'].items()}
        force['seed'] = int(force['seed'])
    value['solver']['fixed_step'] = {key: int(item) for key, item in value['solver']['fixed_step'].items()}
    for key in ('substeps', 'iterations', 'seed'):
        value['solver'][key] = int(value['solver'][key])
    for body in value['bodies']:
        for collider in body['colliders']:
            collider['group']=int(collider['group']);collider['mask']=int(collider['mask'])
            shapes=[collider['shape']]
            while shapes:
                shape=shapes.pop()
                if shape['type']=='compound':shapes.extend(child['shape'] for child in shape['children'])
                elif shape['type'] in ('convex-hull','triangle-mesh'):
                    shape['triangles']=[[int(index) for index in face] for face in shape['triangles']]
    from physics_contracts import definitions
    for key, schema in definitions()['PhysicsBudgets']['properties'].items():
        if schema.get('type') == 'integer':
            value['budgets'][key] = int(value['budgets'][key])
    return value


def _ratio(value):
    return Fraction(int(value['numerator']), int(value['denominator']))


@dataclass(frozen=True)
class FeatureCapability:
    state: Status
    smoke_verified: bool
    message: str


@dataclass(frozen=True)
class CapabilityReport:
    backend: str
    adapter_version: str
    state: Status
    executable: str | None
    application_version: str | None
    executable_sha256: str | None
    discovery_verified: bool
    headless_smoke_verified: bool
    categories: dict[str, FeatureCapability]
    shapes: tuple[str, ...]
    joints: tuple[str, ...]
    caches: tuple[str, ...]
    exports: tuple[str, ...]
    hardware: dict
    limitations: tuple[str, ...]
    evidence: dict | None

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ResourceEstimate:
    source_bytes: int
    mesh_vertices: int
    mesh_triangles: int
    output_samples: int
    solver_steps: int
    estimated_memory_bytes: int
    estimated_cache_bytes: int
    timeout_seconds: float
    processes: int = 1
    estimate_only: bool = True


@dataclass(frozen=True)
class PreparedSimulation:
    token: str
    root: Path
    world_sha256: str
    backend: str
    estimate: ResourceEstimate
    input_inventory: dict
    fixture: bool


@dataclass(frozen=True)
class SimulationResult:
    token: str
    root: Path
    world_sha256: str
    backend: dict
    files: tuple[dict, ...]
    usage: dict
    verification: dict
    fixture: bool


def hardware(directory=None):
    memory = None
    try:
        memory = os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except (ValueError, OSError, AttributeError):
        pass
    return dict(os=platform.system(), architecture=platform.machine(), cpu_concurrency=os.cpu_count(),
                available_memory_bytes=memory, available_disk_bytes=shutil.disk_usage(directory or tempfile.gettempdir()).free,
                gpu_discovery='not-executed', physics_gpu_support='unsupported',
                physics_device='cpu', render_gpu_execution='not-executed')


def _failure_state(code):
    if code in ('PHYSICS_BACKEND_UNAVAILABLE','PHYSICS_DEPENDENCY','PHYSICS_SOURCE_OWNER_UNAVAILABLE'):
        return 'unavailable'
    if code in ('PHYSICS_BACKEND_VERSION_UNSUPPORTED','PHYSICS_PLATFORM_UNSUPPORTED'):
        return 'unsupported'
    if code=='PHYSICS_BUSY':
        return 'degraded'
    return 'failed-validation'


def _new_destination(value):
    path = Path(value).absolute()
    require(path.name not in ('', '.', '..'),
            'PHYSICS_PATH', 'Destination requires a portable final directory name.', category='invalid-input')
    path_name(path.name)
    require(path.parent.is_dir(), 'PHYSICS_PATH', 'Create the destination parent explicitly.', category='invalid-input')
    # Normalize ancestors while preserving the leaf: never redirect a racing link.
    path = path.parent.resolve() / path.name
    require(not os.path.lexists(path), 'PHYSICS_DESTINATION_EXISTS', 'Use a new destination directory.', category='invalid-input')
    return path


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)


def _publish(stage,destination):
    try:
        from atomic_publish import _rename_no_replace
    except ImportError as exc:
        raise BackendError('PHYSICS_DEPENDENCY','Install the reviewed shared exclusive publication primitive (#198).') from exc
    try:
        _rename_no_replace(stage,destination)
    except FileExistsError as exc:
        raise BackendError('PHYSICS_DESTINATION_EXISTS','A destination created during staging was preserved.',category='invalid-input') from exc
    except OSError as exc:
        raise BackendError('PHYSICS_PUBLICATION_UNAVAILABLE','Exclusive output publication failed safely: '+str(exc),category='setup-error') from exc


class _Reader:
    """Descriptor-relative cache reads; unsupported platforms fail closed.

    A root descriptor is held across the full inventory check. It is safe to
    inspect a user-supplied cache without following a swapped child symlink.
    This is not a filesystem write sandbox or a portable Windows implementation.
    """
    def __init__(self, root):
        require(os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'),
                'PHYSICS_PLATFORM_UNSUPPORTED', 'Secure cache inspection currently requires POSIX descriptor-relative I/O.')
        root = Path(root)
        require(not root.is_symlink(), 'PHYSICS_PATH', 'Cache root must not be a symlink.', category='cache-corruption')
        try:
            self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise BackendError('PHYSICS_CACHE_CORRUPT', 'Cannot open cache directory: ' + str(exc), category='cache-corruption') from exc

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def read(self, name, limit=MAX_CACHE_BYTES):
        path_name(name)
        require(type(limit) is int and 0<=limit<=MAX_CACHE_BYTES,'PHYSICS_CACHE_CORRUPT',
                'Cache read requires a bounded canonical integer byte count.',category='cache-corruption')
        fd = os.dup(self.fd)
        try:
            parts = name.split('/')
            for part in parts[:-1]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                require(stat.S_ISREG(before.st_mode) and before.st_size <= limit,
                        'PHYSICS_CACHE_CORRUPT', 'Cache payload must be a bounded regular file.', category='cache-corruption')
                value = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
                require(len(value) <= limit and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                        'PHYSICS_CACHE_CORRUPT', 'Cache file changed during verification.', category='cache-corruption')
                return value
        except OSError as exc:
            raise BackendError('PHYSICS_CACHE_CORRUPT', 'Missing or unsafe cache payload: ' + name, category='cache-corruption') from exc
        finally:
            os.close(fd)

    def names(self):
        files = set()
        count = 0
        pending = [(os.dup(self.fd),'')]
        try:
            while pending:
                descriptor,prefix=pending.pop()
                try:
                    with os.scandir(descriptor) as entries:
                        for entry in entries:
                            count += 1
                            require(count <= MAX_FILES+32,'PHYSICS_CACHE_CORRUPT','Cache tree entry limit exceeded.',category='cache-corruption')
                            name=prefix+entry.name
                            path_name(name)
                            info=os.stat(entry.name,dir_fd=descriptor,follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                child=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                                pending.append((child,name+'/'))
                            else:
                                require(stat.S_ISREG(info.st_mode),'PHYSICS_CACHE_CORRUPT','Cache contains an unexpected link/special file.',category='cache-corruption')
                                files.add(name)
                finally:
                    os.close(descriptor)
            return files
        except OSError as exc:
            raise BackendError('PHYSICS_CACHE_CORRUPT','Cache tree changed during verification.',category='cache-corruption') from exc
        finally:
            for descriptor,_ in pending:
                os.close(descriptor)


def _worker_bytes(backend):
    filename = {'blender': 'physics_blender_worker.py', 'pybullet': 'physics_pybullet_worker.py'}[backend]
    source = Path(__file__).with_name(filename)
    require(source.is_file() and not source.is_symlink(), 'PHYSICS_WORKER_TRUST', 'Package worker must be a regular source file.')
    payload = source.read_bytes()
    if backend == 'pybullet':
        helper = Path(__file__).with_name('physics_collision_native.py')
        require(helper.is_file() and not helper.is_symlink(), 'PHYSICS_WORKER_TRUST', 'Missing package-owned collision helper.')
        prefix = helper.read_bytes()
        require(0 < len(prefix) <= 256*1024, 'PHYSICS_WORKER_TRUST', 'Collision helper exceeds source bound.')
        payload = prefix + b'\n' + payload
    require(0 < len(payload) <= 1024 * 1024, 'PHYSICS_WORKER_TRUST', 'Package worker size is invalid.')
    return payload


def _environment(root):
    # Never propagate PYTHONPATH, startup scripts, user add-ons, loader variables,
    # proxy credentials, or arbitrary environment from scene JSON.
    keep = ('SystemRoot', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL')
    result = {key: os.environ[key] for key in keep if key in os.environ}
    result.update(HOME=str(root), USERPROFILE=str(root), PYTHONNOUSERSITE='1',
                  BLENDER_USER_CONFIG=str(root / 'user-config'),
                  BLENDER_USER_SCRIPTS=str(root / 'user-scripts'),
                  BLENDER_USER_DATAFILES=str(root / 'user-data'),
                  OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    return result


def _rss(pid):
    if sys.platform.startswith('linux'):
        try:
            for line in Path('/proc', str(pid), 'status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            return 0
    elif sys.platform == 'darwin':
        try:
            value = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(pid)], capture_output=True,
                                   text=True, timeout=1, check=False)
            return int(value.stdout.strip()) * 1024 if value.returncode == 0 else 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0
    return 0


def _tree_size(root):
    total = 0
    count = 0
    for directory, names, files in os.walk(root, followlinks=False):
        require(not any((Path(directory) / name).is_symlink() for name in names),
                'PHYSICS_WORKER_OUTPUT', 'Worker produced an unexpected linked directory.')
        for name in files:
            try:
                info = (Path(directory) / name).lstat()
            except FileNotFoundError:
                # Blender atomically renames its own scene.blend@ while saving.
                # Final inventory verification runs after the process exits.
                continue
            require(stat.S_ISREG(info.st_mode), 'PHYSICS_WORKER_OUTPUT', 'Worker produced an unexpected nonregular file.')
            total += info.st_size
            count += 1
            require(count <= MAX_FILES + 32, 'PHYSICS_RESOURCE_LIMIT', 'Worker file count exceeds admission.', category='resource-exhaustion')
    return total


def _stop(process):
    if getattr(process,'_physics_group_stopped',False):
        return
    if os.name=='posix':
        # The leader can exit on TERM while one of its own children ignores it.
        # Always escalate the *owned session's group*, not only a live leader.
        try:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:pass
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            if process.poll() is None:process.wait(timeout=5)
        finally:
            process._physics_group_stopped=True
        return
    try:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    finally:
        process._physics_group_stopped=True


@contextmanager
def _slot():
    # One private global slot across processes. Native OS locks release on crash.
    parent = Path(tempfile.gettempdir()).resolve()
    name = 'hatch-physics-workers-' + str(os.getuid() if hasattr(os, 'getuid') else os.getpid())
    root = parent / name
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = root.lstat()
    require(stat.S_ISDIR(info.st_mode) and (not hasattr(os, 'getuid') or info.st_uid == os.getuid()) and
            (os.name != 'posix' or info.st_mode & 0o077 == 0),
            'PHYSICS_LOCK', 'Worker slot directory is not private and owned.')
    manager=job_lock(root/'slot')
    try:
        manager.__enter__()
    except RigError as exc:
        raise BackendError('PHYSICS_BUSY', 'Another physics worker owns the process slot.') from exc
    try:
        yield
    finally:
        manager.__exit__(None,None,None)


def _run_process(command, root, *, timeout, memory_bytes, output_bytes, cancel_token=None, progress=None, label='simulation'):
    stdout = root / (label + '-stdout.log')
    stderr = root / (label + '-stderr.log')
    start = time.monotonic()
    peak = 0
    process = None
    try:
        with _slot(), stdout.open('wb') as out, stderr.open('wb') as err:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       env=_environment(root), shell=False, start_new_session=os.name == 'posix')
            while process.poll() is None:
                if cancel_token is not None and cancel_token.is_set():
                    raise BackendError('PHYSICS_CANCELLED', 'The user cancelled this owned worker.', category='timeout')
                if time.monotonic() - start > timeout:
                    raise BackendError('PHYSICS_TIMEOUT', 'Physics worker exceeded its wall-clock deadline.', category='timeout')
                peak = max(peak, _rss(process.pid))
                require(peak <= memory_bytes, 'PHYSICS_RESOURCE_LIMIT', 'Worker RSS exceeded the configured memory cap.', category='resource-exhaustion')
                require(_tree_size(root) <= output_bytes and stdout.stat().st_size <= MAX_LOG_BYTES and stderr.stat().st_size <= MAX_LOG_BYTES,
                        'PHYSICS_RESOURCE_LIMIT', 'Worker files/logs exceeded the configured output cap.', category='resource-exhaustion')
                if progress is not None:
                    progress(dict(status='running', elapsed_seconds=time.monotonic()-start,
                                  completed_frames=None, progress_kind='process-liveness-only'))
                time.sleep(.025)
            # A worker can finish between polls, including after writing more
            # than its budget. Stop any owned descendants before checking the
            # settled inventory and before trusting or reading worker reports.
            _stop(process)
            require(_tree_size(root) <= output_bytes and stdout.stat().st_size <= MAX_LOG_BYTES and stderr.stat().st_size <= MAX_LOG_BYTES,
                    'PHYSICS_RESOURCE_LIMIT', 'Worker files/logs exceeded the configured output cap.', category='resource-exhaustion')
            if process.returncode != 0 and (root/'worker-report.json').is_file() and (root/'worker-report.json').stat().st_size <= 32768:
                failure=parse((root/'worker-report.json').read_bytes())
                if isinstance(failure,dict) and failure.get('ok') is False and isinstance(failure.get('error'),dict):
                    detail=failure['error']
                    if detail.get('code')=='PHYSICS_NUMERICAL_INSTABILITY':
                        frame=detail.get('last_valid_frame')
                        raise BackendError('PHYSICS_NUMERICAL_INSTABILITY',str(detail.get('message','Native numerical state failed.'))[:2048],
                            category='numerical-instability',last_valid_frame=frame if type(frame) is int and 0<=frame<MAX_FRAMES else None)
            require(process.returncode == 0, 'PHYSICS_BACKEND_CRASH',
                    'Worker exited with code ' + str(process.returncode) + '; inspect retained process logs.', category='backend-crash')
    except BackendError as exc:
        if process is not None:
            _stop(process)
        exc.report.update(preserved_workspace=str(root), process_exit_code=process.returncode if process else None,
                          stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
        raise
    except OSError as exc:
        error = BackendError('PHYSICS_BACKEND_CRASH', 'Cannot execute configured backend: ' + str(exc), category='backend-crash')
        error.report.update(preserved_workspace=str(root), stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
        raise error from exc
    finally:
        if process is not None:
            _stop(process)
    return dict(elapsed_seconds=time.monotonic()-start, peak_memory_bytes=peak,
                exit_code=process.returncode, rss_monitor_supported=sys.platform.startswith('linux') or sys.platform == 'darwin')


def _tail(path):
    try:
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-8192))
            return stream.read(8192).decode('utf-8',errors='replace')
    except OSError:
        return ''


def _remaining(deadline):
    value=deadline-time.monotonic()
    require(value>0,'PHYSICS_TIMEOUT','The complete physics operation exceeded its wall-clock deadline.',category='timeout')
    return value


def _timing_receipt(world, identity):
    return dict(schema_version=1, timing=timing_clock(world), backend=identity,
        settings_sha256=digest(canonical(world['solver'])), seed=world['solver']['seed'],
        seed_policy='identity only; rigid-body adapter uses no stochastic parameter',
        threads=dict(cpu_workers=world['budgets']['cpu_workers'],
                     solver_parallelism='backend default; cross-platform repeatability not guaranteed'),
        stability=dict(requested=world['solver'].get('stability', {}),
            iterations=world['solver']['iterations'],
            velocity_policy='fail on observed excess; never clamp',
            penetration_policy='PyBullet contactSlop when explicitly requested; otherwise backend default'),
        reproducibility=dict(replay='verified-cache-replay after manifest hash verification',
            rerun='not-guaranteed', pinned_environment='unverified until paired numerical comparison',
            approximate='only when a named numerical oracle and tolerance passes',
            cross_platform='no bitwise guarantee'),
        contact_clock='contacts observed at upper solver bracket; not interpolated')


def _primitive_inertia(body):
    shape = body['colliders'][0]['shape']
    mass = body['mass']['kilograms']
    if shape['type'] == 'sphere':
        return [2 * mass * shape['radius'] ** 2 / 5] * 3
    x, y, z = shape['size']
    return [mass*(y*y+z*z)/12, mass*(x*x+z*z)/12, mass*(x*x+y*y)/12]


def _admit_world(value, backend):
    world = _world(value)
    solver, budget = world['solver'], world['budgets']
    require(solver['backend'] == backend and solver['adapter_version'] == ADAPTER_VERSION,
            'PHYSICS_BACKEND', 'World must select this exact backend and adapter version.', category='invalid-input')
    require(set(solver['required_categories']) == {'rigid-body'} and not world['components'] and not world['joints'] and not world['events'],
            'PHYSICS_CAPABILITY_UNAVAILABLE', 'Only uncoupled rigid-body worlds are implemented in this adapter.', category='capability-gap')
    require(solver['reproducibility'] in ('not-guaranteed', 'verified-cache-replay'),
            'PHYSICS_REPRODUCIBILITY', 'Recomputation is not promised deterministic; request verified cache replay or not-guaranteed.', category='capability-gap')
    stability = solver.get('stability', {})
    supported = {'contact_penetration_tolerance', 'max_linear_velocity', 'max_angular_velocity'} if backend == 'pybullet' else set()
    require(backend != 'pybullet' or all(stability.get(key,50) <= 50 for key in ('max_linear_velocity','max_angular_velocity')),
            'PHYSICS_STABILITY_UNSUPPORTED','This native adapter supports at most 50 m/s and 50 rad/s; higher limits could conceal native velocity clamping.',category='capability-gap')
    require(not (set(stability)-supported), 'PHYSICS_STABILITY_UNSUPPORTED',
            'Requested stability controls are unsupported by this backend: '+str(sorted(set(stability)-supported)), category='capability-gap')
    require(world['frame_range']['start'] == 0 and 2 <= world['frame_range']['end'] <= MAX_FRAMES,
            'PHYSICS_TIME_UNSUPPORTED', 'Native v1 supports zero-origin jobs with 2..1024 output frames.', category='capability-gap')
    fps, step = _ratio(world['fps']), _ratio(solver['fixed_step']) / int(solver['substeps'])
    steps_per_frame = 1 / (fps * step)
    require((backend == 'pybullet' or steps_per_frame.denominator == 1) and 1 <= steps_per_frame <= 1000 and 1 <= fps <= 240 and world['fps']['numerator'] <= 32767,
            'PHYSICS_TIME_UNSUPPORTED', 'Blender requires integral substeps per output; PyBullet brackets output with full fixed steps; ratio must be 1..1000 at 1..240fps.', category='capability-gap')
    require(1 <= len(world['bodies']) <= MAX_BODIES and any(b['mode'] == 'dynamic' for b in world['bodies']),
            'PHYSICS_RESOURCE_LIMIT', 'A readiness world requires 1..32 bodies including a dynamic body.', category='resource-exhaustion')
    require(max(map(abs, world['gravity'])) <= 1000, 'PHYSICS_SETTINGS', 'Gravity exceeds the documented native bound.', category='invalid-input')
    require(budget['memory_bytes'] >= 512 * 1024 * 1024 and budget['cpu_workers'] >= 1,
            'PHYSICS_RESOURCE_LIMIT', 'Native launch needs at least 512MiB memory and one CPU worker.', category='resource-exhaustion')
    require(budget['cache_bytes'] <= MAX_CACHE_BYTES and budget['temporary_bytes'] <= MAX_CACHE_BYTES,
            'PHYSICS_RESOURCE_LIMIT', 'Native v1 supports cache/temporary caps at most 256MiB.', category='resource-exhaustion')
    for material in world['materials']:
        require(material['pbr_material'] is None, 'PHYSICS_MATERIAL_UNSUPPORTED',
                'This physics-only native adapter does not import PBR rendering resources.', category='capability-gap')
        require(material['static_friction'] == material['dynamic_friction'] and material['dynamic_friction'] <= 1,
                'PHYSICS_MATERIAL_UNSUPPORTED', 'Blender/PyBullet expose one friction coefficient; unequal static/dynamic requests are unsupported.', category='capability-gap')
        require(material['collision_margin'] == 0, 'PHYSICS_MARGIN_UNSUPPORTED',
                'Nonzero additional contact skin has not been verified; native margin property storage is not physical separation support.',category='capability-gap')
    proxy_totals=[0,0,0]
    for body in world['bodies']:
        require(len(body['id'].encode()) <= 63 and body['category'] == 'rigid-body' and body['mode'] in ('dynamic', 'static', 'kinematic') and body['parent'] is None,
                'PHYSICS_BODY_UNSUPPORTED', 'Only unparented static/dynamic/kinematic rigid bodies with native names <=63 bytes are implemented.', category='capability-gap')
        require(backend == 'pybullet' or not any(body['linear_velocity'] + body['angular_velocity'] + body['mass']['center_of_mass']),
                'PHYSICS_BODY_UNSUPPORTED', 'Blender direct initial velocity and shifted center of mass lack an admitted native API.', category='capability-gap')
        require(all(math.sqrt(sum(x*x for x in body[key])) <= 50 for key in ('linear_velocity','angular_velocity')) and max(map(abs,body['mass']['center_of_mass'])) <= 1000,
                'PHYSICS_SETTINGS', 'Initial speed must be <=50 m/s, angular speed <=50 rad/s, and COM within native bounds.', category='invalid-input')
        require(body['mode'] != 'kinematic' or not any(body['mass']['center_of_mass']), 'PHYSICS_BODY_UNSUPPORTED',
                'The bounded kinematic profile requires a centered zero-mass inertial frame.',category='capability-gap')
        if backend == 'pybullet' and 'sleep' in body:
            require(not {'linear_threshold','angular_threshold'}.intersection(body['sleep']),
                    'PHYSICS_SLEEP_UNSUPPORTED', 'PyBullet API exposes activation states but not configurable native sleep thresholds.', category='capability-gap')
        require(max(map(abs, body['transform']['translation'])) <= 10000 and body['mass']['kilograms'] <= 1e6,
                'PHYSICS_SETTINGS', 'Position/mass exceeds documented native bounds.', category='invalid-input')
        explicit_proxies = world.get('collision_profile') == 'explicit-proxies-v1'
        if explicit_proxies:
            require(backend == 'pybullet', 'PHYSICS_SHAPE_UNSUPPORTED',
                    'The explicit proxy profile currently requires the verified DIRECT adapter.', category='capability-gap')
            from physics_collisions import validate_colliders, CollisionError
            try:
                proxy_vertices, proxy_triangles, proxy_leaves = validate_colliders(body,
                    source_ids={item['id'] for item in world['assets'] if item['kind'] == 'geometry'})
            except CollisionError as exc:
                raise BackendError('PHYSICS_GEOMETRY', str(exc), category='invalid-input') from exc
            proxy_totals=[total+count for total,count in zip(proxy_totals,(proxy_vertices,proxy_triangles,proxy_leaves))]
            require(proxy_totals[0] <= budget['vertices'] and proxy_totals[1] <= budget['triangles'] and proxy_totals[2]<=128,
                    'PHYSICS_RESOURCE_LIMIT', 'Collision proxy exceeds declared mesh budget.', category='resource-exhaustion')
        else:
            require(len(body['colliders']) == 1, 'PHYSICS_SHAPE_UNSUPPORTED', 'Exactly one box/sphere collider per body is supported.', category='capability-gap')
            collider = body['colliders'][0]
            require(collider['shape']['type'] in ('box', 'sphere') and not collider['sensor'] and collider['group'] == 1 and collider['mask'] == 2**32-1,
                    'PHYSICS_SHAPE_UNSUPPORTED', 'Only nonsensor box/sphere colliders with the unfiltered default group/mask are supported.', category='capability-gap')
            require(collider['transform'] == dict(translation=[0, 0, 0], rotation=[0, 0, 0, 1]),
                    'PHYSICS_SHAPE_UNSUPPORTED', 'Offset/rotated local colliders require a later adapter.', category='capability-gap')
            dimensions = collider['shape'].get('size', [collider['shape'].get('radius', 0)])
            require(all(.001 <= x <= 1000 for x in dimensions), 'PHYSICS_SETTINGS', 'Primitive dimensions must be .001..1000 meters.', category='invalid-input')
        if body['mode'] == 'dynamic':
            require(body['mass']['kilograms'] >= .001, 'PHYSICS_MASS_UNSUPPORTED',
                    'Blender rigid-body mass has a 0.001kg lower bound.', category='capability-gap')
            tensor=body['mass']['inertia'];scale=max(abs(x) for row in tensor for x in row)
            a,b,c=[x/scale for x in tensor[0]];_,d,e=[x/scale for x in tensor[1]];_,_,f=[x/scale for x in tensor[2]]
            determinant=a*d*f+2*b*c*e-a*e*e-d*c*c-f*b*b
            require(scale*determinant/(a+d+f)**2 >= 1e-12 and determinant/(a+d+f)**2 >= 1e-10, 'PHYSICS_INERTIA_UNSUPPORTED',
                    'Native tensor needs minimum principal inertia at least 1e-12 kg m² and a conservative bounded condition ratio.',category='capability-gap')
            expected = _primitive_inertia(body) if backend == 'blender' else None
            for i in range(3) if backend == 'blender' else []:
                for j in range(3):
                    observed = body['mass']['inertia'][i][j]
                    target = expected[i] if i == j else 0.
                    require(abs(observed-target) <= max(expected)*1e-6,
                            'PHYSICS_INERTIA_UNSUPPORTED', 'Requested inertia differs from the native uniform primitive.', category='capability-gap')
    require(not world.get('collision_exclusions') or world.get('collision_profile') == 'explicit-proxies-v1',
            'PHYSICS_SHAPE_UNSUPPORTED', 'Pair exclusions require the explicit proxy profile.', category='capability-gap')
    _admit_rigid_controls(world, backend)
    return world, math.ceil(steps_per_frame)



def _admit_rigid_controls(world, backend):
    dynamic = {body['id'] for body in world['bodies'] if body['mode'] == 'dynamic'}
    require(len(world['forces']) <= 128, 'PHYSICS_RESOURCE_LIMIT', 'At most 128 explicit force controls are admitted.')
    effective = _ratio(world['solver']['fixed_step']) / world['solver']['substeps']
    fps = _ratio(world['fps'])
    control_steps = timing_clock(world)['solver_steps']
    require(sum(len(force['bodies']) for force in world['forces']) * control_steps <= 5_000_000,
            'PHYSICS_RESOURCE_LIMIT', 'Aggregate native force/body/step operation budget exceeded.',category='resource-exhaustion')
    for force in world['forces']:
        require(force['frame_range']['start'] < world['frame_range']['end']-1, 'PHYSICS_FORCE_TIME_UNSUPPORTED',
                'Physical controls must start before the final completed output sample.',category='capability-gap')
        application = force.get('application', 'world-point')
        require(set(force['bodies']) <= dynamic and force['type'] in ('force','torque','impulse','angular-impulse'),
                'PHYSICS_FORCE_UNSUPPORTED', 'Native controls apply only to dynamic rigid bodies and explicit physical wrenches.', category='capability-gap')
        require(force['categories'] == ['rigid-body'] and force['seed'] == 0 and force['strength'] <= 1e6 and
                max(map(abs,force['vector'])) <= 1e6 and max(map(abs,force['point'])) <= 10000 and
                max(abs(x*force['strength']) for x in force['vector']) <= 1e6,
                'PHYSICS_FORCE_UNSUPPORTED', 'Native physical control exceeds bounded nonstochastic profile.', category='capability-gap')
        if application == 'center':
            require(force['point'] == [0,0,0], 'PHYSICS_FORCE_UNSUPPORTED', 'Center application requires zero unused point.',category='capability-gap')
        if force['type'] in ('torque','angular-impulse'):
            require(force['point'] == [0,0,0] and application == 'center', 'PHYSICS_FORCE_UNSUPPORTED',
                    'Torque controls must explicitly use center and zero unused point.', category='capability-gap')
        if force['type'] in ('impulse','angular-impulse'):
            require(force['frame_range']['end'] == force['frame_range']['start']+1 and
                    (Fraction(force['frame_range']['start'],1)/fps/effective).denominator == 1,
                    'PHYSICS_FORCE_TIME_UNSUPPORTED', 'A discrete impulse occupies exactly one native step at an aligned frame boundary.', category='capability-gap')
        if backend == 'blender':
            require(force['type'] == 'force' and application == 'center' and force['point'] == [0,0,0] and set(force['bodies']) == dynamic,
                    'PHYSICS_FORCE_UNSUPPORTED', 'Blender native effectors expose global central force only; off-center wrenches, torque and impulse are unavailable.', category='capability-gap')


def _source_admission(root, world, project_manifest):
    try:
        from scene3d_contracts import load_project
        from scene3d_math import evaluate_transforms
        from rig_import import load_character_model
        from rig_contracts import DEFAULT_LIMITS
    except ImportError as exc:
        raise BackendError('PHYSICS_SOURCE_OWNER_UNAVAILABLE', 'Install native scene (#59) and rig geometry validators before simulation.') from exc
    try:
        bundle = load_project(root, project_manifest)
        scene = bundle['documents'].get(world['scene']['path'])
        require(scene is not None and scene['kind'] == 'scene3d-scene' and
                (scene['id'], scene['version']) == (world['scene']['id'], world['scene']['version']) and
                bundle['inventory'][world['scene']['path']]['sha256'] == world['scene']['sha256'],
                'PHYSICS_SOURCE_IDENTITY', 'Physics world must bind an exact native Scene3D scene.', category='invalid-input')
        require(scene['provenance']['fixture'] == bundle['project']['provenance']['fixture'],
                'PHYSICS_PROVENANCE', 'Project/scene fixture evidence conflicts.', category='invalid-input')
        transforms = evaluate_transforms(scene)
        nodes = {node['id']: node for node in scene['nodes']}
        assets = {asset['id']: asset for asset in world['assets']}
        used = set()
        meshes = {}
        vertices = 0
        triangles = 0
        import numpy as np
        from rig_math import compose
        for body in world['bodies']:
            node = nodes.get(body['id'])
            require(node is not None and node['type'] in ('mesh_instance', 'static_prop') and node.get('asset'),
                    'PHYSICS_SOURCE_IDENTITY', 'Each physical body must bind a real native mesh node.', category='invalid-input')
            require(not node['animation_bindings'] and not node['constraints'] and not node['material_overrides'] and 'attachment' not in node,
                    'PHYSICS_CAPABILITY_UNAVAILABLE', 'Animated/overridden/attached source bodies are unsupported.', category='capability-gap')
            require(np.allclose(transforms[body['id']]['world_matrix'], compose(body['transform']['translation'], body['transform']['rotation']), atol=1e-6, rtol=0),
                    'PHYSICS_SOURCE_TRANSFORM', 'Physical and native scene transforms disagree.', category='invalid-input')
            asset = bundle['documents'][node['asset']['path']]
            reference = assets.get(body['asset'])
            require(reference is not None and reference['kind'] == 'geometry' and reference['id'] == asset['id'] and
                    reference['version'] == asset['version'] and reference['path'] == asset['content']['path'] and reference['sha256'] == asset['content']['sha256'],
                    'PHYSICS_SOURCE_IDENTITY', 'Body geometry reference differs from its native asset.', category='invalid-input')
            require(asset['coordinate_system'] == world['coordinate_system'] and asset['pivot'] == [0, 0, 0] and
                    not asset['materials'] and not asset['animations'] and not asset['lods'] and asset['skeleton'] is None,
                    'PHYSICS_CAPABILITY_UNAVAILABLE', 'Only native canonical, unrigged, untextured primitive meshes are supported.', category='capability-gap')
            path = root / reference['path']
            require(path.suffix.lower() == '.obj', 'PHYSICS_FORMAT_UNSUPPORTED', 'The initial native primitive adapter accepts OBJ geometry only.', category='capability-gap')
            require(not any(line.split(maxsplit=1)[0] in ('mtllib', 'usemtl') for line in path.read_text('utf-8').splitlines() if line.strip() and not line.lstrip().startswith('#')),
                    'PHYSICS_FORMAT_UNSUPPORTED', 'External OBJ materials are unsupported.', category='capability-gap')
            limits = {**DEFAULT_LIMITS, 'vertices': 4096, 'triangles': 8192, 'source_bytes': MAX_INPUT_BYTES}
            imported = load_character_model(path, limits=limits)
            require(len(imported.geometry.meshes) == 1 and len(imported.source_files) == 1,
                    'PHYSICS_GEOMETRY_UNSUPPORTED', 'A body requires one independently decoded mesh and no external resources.', category='capability-gap')
            mesh = imported.geometry.meshes[0]
            points = np.asarray(mesh.positions)
            actual_bounds = np.array([points.min(axis=0), points.max(axis=0)])
            if world.get('collision_profile') == 'explicit-proxies-v1':
                # Visual geometry remains owned by its native asset; independently
                # admitted collision proxies deliberately need not match it.
                require(asset['bounds'] is not None and
                        np.allclose([asset['bounds']['minimum'],asset['bounds']['maximum']],actual_bounds,atol=1e-6,rtol=1e-5),
                        'PHYSICS_GEOMETRY', 'Declared visual mesh bounds differ from actual geometry.', category='invalid-input')
            else:
                from rig_geometry import mesh_edges, connected_components
                _, edge_counts = mesh_edges(mesh.triangles)
                require(np.all(edge_counts == 2) and len(connected_components(mesh)) == 1,
                        'PHYSICS_GEOMETRY', 'Primitive source must be one closed manifold surface.', category='invalid-input')
                faces = points[mesh.triangles]
                normals = np.cross(faces[:,1]-faces[:,0], faces[:,2]-faces[:,0])
                scale = max(float(np.abs(points).max()), 1e-10)
                require(np.all(np.sum(normals*faces.mean(axis=1),axis=1) > scale**3*1e-12),
                        'PHYSICS_GEOMETRY', 'Primitive faces must be nondegenerate and consistently outward.', category='invalid-input')
                for group in np.array_split(np.arange(len(faces)), max(1, math.ceil(len(faces)/128))):
                    offsets = np.sum(normals[group]*faces[group,0],axis=1)
                    require(np.all(points @ normals[group].T <= offsets + scale**3*1e-7),
                            'PHYSICS_GEOMETRY', 'Primitive source must describe a convex physical surface.', category='invalid-input')
                shape = body['colliders'][0]['shape']
                if shape['type'] == 'sphere':
                    require(np.allclose(np.linalg.norm(points, axis=1), shape['radius'], rtol=1e-5, atol=1e-7),
                            'PHYSICS_GEOMETRY', 'Sphere source vertices disagree with its analytic collision proxy.', category='invalid-input')
                    expected_bounds = np.array([-np.ones(3)*shape['radius'], np.ones(3)*shape['radius']])
                else:
                    half = np.asarray(shape['size'])/2
                    require(np.allclose(np.abs(points), half, rtol=1e-5, atol=1e-7) and len(np.unique(points, axis=0)) == 8,
                            'PHYSICS_GEOMETRY', 'Box source must contain all eight actual box corners.', category='invalid-input')
                    expected_bounds = np.array([-half, half])
                actual_bounds = np.array([points.min(axis=0), points.max(axis=0)])
                require(np.allclose(actual_bounds, expected_bounds, atol=1e-6, rtol=1e-5) and asset['bounds'] is not None and
                        np.allclose([asset['bounds']['minimum'], asset['bounds']['maximum']], actual_bounds, atol=1e-6, rtol=1e-5),
                        'PHYSICS_GEOMETRY', 'Actual mesh bounds differ from proxy/native manifest bounds.', category='invalid-input')
            require(asset['resources']['vertices'] == len(points) and asset['resources']['triangles'] == len(mesh.triangles),
                    'PHYSICS_GEOMETRY', 'Declared native mesh counts differ from actual decoded geometry.', category='invalid-input')
            require(bool(asset['provenance']['fixture']) == bool(scene['provenance']['fixture']),
                    'PHYSICS_PROVENANCE', 'Geometry/scene fixture evidence conflicts.', category='invalid-input')
            meshes[body['id']] = dict(vertices=points.tolist(), triangles=mesh.triangles.tolist())
            vertices += len(points)
            triangles += len(mesh.triangles)
            used.add(reference['id'])
        for body in world['bodies']:
            if body['mode'] == 'kinematic':
                from physics_rigid import compile_motion
                reference = assets[body['animation']]
                clip = bundle['documents'].get(reference['path'])
                require(clip is not None and bundle['inventory'][reference['path']]['sha256'] == reference['sha256'] and
                        clip['version'] == reference['version'] and clip['provenance']['fixture'] == scene['provenance']['fixture'],
                        'PHYSICS_SOURCE_IDENTITY', 'Kinematic clip must belong to the exact validated native closure.')
                meshes[body['id']]['motion'] = compile_motion(body, clip, world,
                    lambda okay,message: require(okay,'PHYSICS_MOTION_UNSUPPORTED',message,category='capability-gap'))
                used.add(reference['id'])
        require(used == set(assets), 'PHYSICS_UNUSED_ASSET', 'Every declared world asset must be used by its native body.', category='invalid-input')
        require(vertices <= world['budgets']['vertices'] and triangles <= world['budgets']['triangles'],
                'PHYSICS_RESOURCE_LIMIT', 'Actual decoded mesh counts exceed physical admission.', category='resource-exhaustion')
        return bundle, meshes, vertices, triangles
    except (RigError, KeyError, ValueError, UnicodeError, TypeError, OSError, RecursionError) as exc:
        if isinstance(exc, BackendError):
            raise
        raise BackendError('PHYSICS_SOURCE_INVALID', 'Native source admission failed: ' + str(exc), category='invalid-input') from exc


class BlenderBackend:
    """Real, narrow Blender Bullet adapter; no general .blend import route."""
    backend_id = 'blender'

    def __init__(self, executable=None):
        value = executable or os.environ.get('HATCH_BLENDER') or shutil.which('blender')
        self.executable = str(Path(value).resolve()) if value else None
        self._owned = {}
        self._results = {}
        self._attempted = set()
        self._attempt_lock = threading.Lock()
        self._identity = None

    def _claim_attempt(self, prepared):
        # Reserve before discovery or file I/O can release the GIL. Every
        # admitted invocation consumes the token, including failed attempts.
        with self._attempt_lock:
            owner = self._owner(prepared)
            require(prepared.token not in self._attempted, 'PHYSICS_STATE',
                    'A prepared attempt executes once; restart with a fresh preparation.')
            self._attempted.add(prepared.token)
            return owner

    def _discover(self, *, timeout=10):
        require(self.executable and Path(self.executable).is_file(), 'PHYSICS_BACKEND_UNAVAILABLE',
                'Install Blender 4.5.3 and set HATCH_BLENDER to its executable.')
        path = Path(self.executable)
        payload_hash = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                payload_hash.update(chunk)
        with tempfile.TemporaryDirectory(prefix='hatch-physics-version-') as name:
            root = Path(name)
            # Bounded file capture and no inherited Python/add-on environment.
            _run_process([self.executable, '--version'], root, timeout=timeout,
                         memory_bytes=2*1024**3, output_bytes=MAX_LOG_BYTES, label='version')
            text = (root/'version-stdout.log').read_text('utf-8', errors='replace')
        match = re.search(r'^Blender (\d+)\.(\d+)\.(\d+)', text)
        if not match or tuple(map(int, match.groups())) != SUPPORTED_BLENDER or not re.search(r'build hash:\s*'+SUPPORTED_BLENDER_BUILD+r'\b',text):
            error=BackendError('PHYSICS_BACKEND_VERSION_UNSUPPORTED','Only actually tested Blender4.5.3 build67807e1800cc is admitted.')
            error.report.update(observed_application_version=text.splitlines()[0] if text.splitlines() else 'unknown',
                                observed_executable_sha256=payload_hash.hexdigest())
            raise error
        self._identity = dict(id='blender', adapter_version=ADAPTER_VERSION,
            solver_name='Blender Bullet rigid-body world', solver_version='not exposed separately by Blender',
            application_version=text.splitlines()[0], platform=platform.platform(),
            precision='mixed', device='cpu', executable_sha256=payload_hash.hexdigest())
        return self._identity

    def validate_scene(self, world):
        admitted, steps = _admit_world(world, self.backend_id)
        return dict(status='configuration-valid', world_sha256=digest(canonical(admitted)),
                    category='rigid-body', native_scene_verified=False, payloads_verified=False,
                    solver_executed=False, steps_per_frame=steps, timing=timing_clock(admitted),
                    unsupported_categories=[c for c in ALL_CATEGORIES if c != 'rigid-body'])

    def estimate_resources(self, world, *, source_bytes=0, mesh_vertices=0, mesh_triangles=0):
        world, steps = _admit_world(world, self.backend_id)
        require(type(source_bytes) is int and 0 <= source_bytes <= MAX_INPUT_BYTES,
                'PHYSICS_RESOURCE_LIMIT', 'Native source byte bound exceeded.', category='resource-exhaustion')
        frames, bodies = world['frame_range']['end'], len(world['bodies'])
        cache_samples = timing_clock(world)['solver_steps']+1 if self.backend_id=='pybullet' else frames
        memory = 512*1024**2 + source_bytes*8 + mesh_vertices*128 + bodies*cache_samples*1024
        output = 8*1024**2 + source_bytes*2 + bodies*cache_samples*1024
        budgets = world['budgets']
        require(memory <= budgets['memory_bytes'] and output <= min(budgets['cache_bytes'], budgets['temporary_bytes']) and
                output <= budgets['bake_file_bytes'],
                'PHYSICS_RESOURCE_LIMIT', 'Conservative native allocation estimate exceeds resource admission.', category='resource-exhaustion')
        return ResourceEstimate(source_bytes, mesh_vertices, mesh_triangles, frames, timing_clock(world)['solver_steps'],
                                memory, output, min(float(budgets['timeout_seconds']), 300.))

    def prepare(self, world, source_payloads: Mapping[str, bytes], *, project_manifest='project.json'):
        world, _ = _admit_world(world, self.backend_id)
        require(os.name == 'posix' and hasattr(os, 'O_NOFOLLOW'), 'PHYSICS_PLATFORM_UNSUPPORTED',
                'Trusted cache admission currently supports POSIX; Windows native execution is not advertised.')
        require(isinstance(source_payloads, Mapping) and 1 <= len(source_payloads) <= MAX_FILES,
                'PHYSICS_INPUTS', 'Supply a complete bounded native project byte mapping.', category='invalid-input')
        source_payloads = dict(source_payloads)
        require(1 <= len(source_payloads) <= MAX_FILES-32,
                'PHYSICS_RESOURCE_LIMIT','Native input inventory must leave room for bounded solver artifacts.',category='resource-exhaustion')
        _paths(source_payloads)
        path_name(project_manifest)
        require(all(type(payload) is bytes for payload in source_payloads.values()),
                'PHYSICS_INPUTS', 'Source payloads must be immutable exact bytes.', category='invalid-input')
        source_bytes = sum(map(len, source_payloads.values()))
        self.estimate_resources(world, source_bytes=source_bytes)
        identity = self._discover()
        root = Path(tempfile.mkdtemp(prefix='hatch-physics-prepared-')).resolve()
        try:
            inputs = root / 'inputs'
            inputs.mkdir(mode=0o700)
            for path, payload in source_payloads.items():
                _write(inputs / path, payload)
            bundle, meshes, vertices, triangles = _source_admission(inputs, world, project_manifest)
            require(set(bundle['inventory']) == set(source_payloads), 'PHYSICS_INPUTS',
                    'Source mapping must equal the validated native dependency closure.', category='invalid-input')
            estimate = self.estimate_resources(world, source_bytes=source_bytes, mesh_vertices=vertices, mesh_triangles=triangles)
            worker = _worker_bytes(self.backend_id)
            worker_hash = digest(worker)
            world_bytes = canonical(world)
            _write(root/'world.json', world_bytes)
            _write(root/'worker.py', worker)
            fixture = bool(bundle['project']['provenance']['fixture'])
            job = dict(protocol=PROTOCOL, mode='simulate', world=world, world_sha256=digest(world_bytes),
                       meshes=meshes, worker_sha256=worker_hash, fixture=fixture)
            _write(root/'worker-job.json', canonical(job))
            token = uuid.uuid4().hex
            info = root.stat()
            prepared = PreparedSimulation(token, root, digest(world_bytes), self.backend_id,
                                          estimate, deepcopy(bundle['inventory']), fixture)
            self._owned[token] = dict(root=root, inode=(info.st_dev, info.st_ino),
                                      world=world, worker_hash=worker_hash, identity=deepcopy(identity), prepared=prepared,
                                      inventory=deepcopy(bundle['inventory']), prepared_snapshot=deepcopy(prepared),
                                      worker_job_sha256=digest(canonical(job)),project_manifest=project_manifest)
            return prepared
        except BaseException:
            shutil.rmtree(root)
            raise

    def _owner(self, value):
        require(isinstance(value, (PreparedSimulation, SimulationResult)) and value.token in self._owned,
                'PHYSICS_OWNERSHIP', 'Only this adapter instance may execute/clean its own prepared attempt.')
        owner = self._owned[value.token]
        if isinstance(value, PreparedSimulation):
            require(value is owner['prepared'] and value == owner['prepared_snapshot'],
                    'PHYSICS_OWNERSHIP', 'Prepared record was substituted or modified.')
        require(value.root == owner['root'] and value.root.is_dir() and not value.root.is_symlink(),
                'PHYSICS_OWNERSHIP', 'Prepared workspace identity changed.')
        info = value.root.stat()
        require((info.st_dev, info.st_ino) == owner['inode'], 'PHYSICS_OWNERSHIP', 'Prepared directory was replaced.')
        return owner

    def _command(self, root):
        return [self.executable, '--background', '--factory-startup', '--disable-autoexec', '--threads', '1',
                '--python-exit-code', '2', '--python', str(root/'worker.py'), '--', str(root/'worker-job.json')]

    def simulate(self, prepared, progress: Callable | None = None, cancel_token=None, *, verify_consumption=False):
        started = time.monotonic()
        require(isinstance(prepared,PreparedSimulation) and type(verify_consumption) is bool and
                (progress is None or callable(progress)) and
                (cancel_token is None or callable(getattr(cancel_token,'is_set',None))),
                'PHYSICS_REQUEST', 'Invalid simulation handle/callback/cancellation request.', category='invalid-input')
        require(cancel_token is None or not cancel_token.is_set(), 'PHYSICS_CANCELLED', 'The request was already cancelled.', category='timeout')
        owner = self._claim_attempt(prepared)
        world, root = owner['world'], owner['root']
        deadline = started + prepared.estimate.timeout_seconds
        with _Reader(root) as reader:
            require(digest(reader.read('world.json', MAX_DOCUMENT_BYTES)) == prepared.world_sha256 and
                    digest(reader.read('worker.py', 1024*1024)) == owner['worker_hash'],
                    'PHYSICS_INPUT_CHANGED', 'Prepared world or worker bytes changed.')
        # Rehash all immutable native inputs at the process boundary.
        with _Reader(root/'inputs') as reader:
            for path, expected in owner['inventory'].items():
                payload = reader.read(path, MAX_INPUT_BYTES)
                require(digest(payload) == expected['sha256'] and len(payload) == expected['bytes'],
                        'PHYSICS_INPUT_CHANGED', 'Prepared source bytes changed.')
        identity = self._discover(timeout=min(10.,_remaining(deadline)))
        require(identity == owner['identity'], 'PHYSICS_BACKEND_CHANGED', 'Backend executable/version changed after preparation.')
        original_job = (root/'worker-job.json').read_bytes()
        require(digest(original_job) == owner['worker_job_sha256'] and parse(original_job)['mode'] == 'simulate' and parse(original_job)['world_sha256'] == prepared.world_sha256 and
                digest(canonical(parse(original_job)['world'])) == prepared.world_sha256,
                'PHYSICS_INPUT_CHANGED', 'Prepared worker request changed.')
        usage = _run_process(self._command(root), root, timeout=_remaining(deadline),
            memory_bytes=world['budgets']['memory_bytes'], output_bytes=min(world['budgets']['temporary_bytes'], MAX_CACHE_BYTES),
            cancel_token=cancel_token, progress=progress)
        require((root/'worker-report.json').is_file() and (root/'states.json').is_file() and (root/'scene.blend').is_file() and (root/'preview.png').is_file(),
                'PHYSICS_OUTPUT_MISSING', 'Successful exit omitted required actual simulation outputs.', category='backend-crash')
        report = parse((root/'worker-report.json').read_bytes())
        self._check_report(report, world, 'simulate')
        sample_bytes = (root/'states.json').read_bytes()
        blend_hash = digest((root/'scene.blend').read_bytes())
        _validate_samples(parse(sample_bytes), world, prepared.world_sha256, prepared.fixture)
        _write(root/'timing.json', canonical(_timing_receipt(world, identity)))
        _write(root/'simulation-report.json', canonical(report))
        job = parse((root/'worker-job.json').read_bytes())
        job['mode'] = 'reopen'
        atomic_json(root/'worker-job.json', job)
        (root/'worker-report.json').unlink()
        replay_usage = _run_process(self._command(root), root, timeout=_remaining(deadline),
            memory_bytes=world['budgets']['memory_bytes'], output_bytes=min(world['budgets']['temporary_bytes'], MAX_CACHE_BYTES),
            cancel_token=cancel_token, progress=progress, label='reopen')
        require((root/'worker-report.json').is_file(), 'PHYSICS_OUTPUT_MISSING', 'Saved-cache verification omitted its report.', category='backend-crash')
        replay = parse((root/'worker-report.json').read_bytes())
        self._check_report(replay, world, 'reopen')
        require(replay['reversed_samples'] == world['frame_range']['end'] and replay['maximum_replay_error'] <= 1e-5,
                'PHYSICS_CACHE_CORRUPT', 'Independent native cache replay differs from simulation.', category='cache-corruption')
        require((root/'states.json').read_bytes() == sample_bytes, 'PHYSICS_INPUT_CHANGED', 'Native replay changed retained sample bytes.')
        require(digest((root/'scene.blend').read_bytes()) == blend_hash,
                'PHYSICS_INPUT_CHANGED', 'Native replay modified the retained solver scene.')
        control = None
        if verify_consumption:
            job['mode'] = 'consumption-control'
            atomic_json(root/'worker-job.json', job)
            (root/'worker-report.json').unlink()
            control_usage = _run_process(self._command(root), root, timeout=_remaining(deadline),
                memory_bytes=world['budgets']['memory_bytes'], output_bytes=min(world['budgets']['temporary_bytes'], MAX_CACHE_BYTES),
                cancel_token=cancel_token, progress=progress, label='consumption')
            require((root/'worker-report.json').is_file(), 'PHYSICS_OUTPUT_MISSING', 'Native consumption control omitted its report.')
            control = parse((root/'worker-report.json').read_bytes())
            require(control.get('operation') == 'consumption-control' and control.get('consumption_demonstrated') is True,
                    'PHYSICS_CACHE_CONSUMPTION_UNPROVEN', 'Control did not distinguish baked reads from clean recomputation.', category='cache-corruption')
            require(digest((root/'scene.blend').read_bytes()) == blend_hash and (root/'states.json').read_bytes() == sample_bytes,
                    'PHYSICS_INPUT_CHANGED', 'Consumption control changed retained solver artifacts.')
            _write(root/'native-consumption.json', canonical(control))
            replay_usage['elapsed_seconds'] += control_usage['elapsed_seconds']
            replay_usage['peak_memory_bytes'] = max(replay_usage['peak_memory_bytes'], control_usage['peak_memory_bytes'])
        # The saved job identifies the actual original simulation request, not
        # an independent verification control with intentionally changed gravity.
        (root/'worker-job.json').write_bytes(original_job)
        _remaining(deadline)
        elapsed = time.monotonic()-started
        measured = dict(elapsed_seconds=elapsed, peak_memory_bytes=max(usage['peak_memory_bytes'], replay_usage['peak_memory_bytes']),
                        output_bytes=0, solver_steps=prepared.estimate.solver_steps)
        _write(root/'verification.json', canonical(dict(schema_version=1, world_sha256=prepared.world_sha256,
            source_admission='native Scene3D contract/hash closure and actual rig OBJ decode',
            project_manifest=owner['project_manifest'],backend_identity=identity,worker_job_sha256=owner['worker_job_sha256'],
            worker_sha256=owner['worker_hash'], executable_sha256=identity['executable_sha256'],
            native_reopen=replay, solver_report=report,
            native_consumption_control=control,
            native_continuation='unsupported-clean-restart-only',
            unsupported=['joints','off-center-forces','torques','impulses','initial-velocity','shifted-center-of-mass','custom-inertia','sensors','mask-filtering','other-categories'],
            restart='clean-restart-only', artistic_review='not-performed')))
        files = tuple(_inventory(root))
        measured['output_bytes'] = sum(item['bytes'] for item in files)
        require(measured['output_bytes'] <= world['budgets']['cache_bytes'], 'PHYSICS_RESOURCE_LIMIT', 'Final files exceed cache budget.', category='resource-exhaustion')
        result = SimulationResult(prepared.token, root, prepared.world_sha256, identity, files, measured, replay, prepared.fixture)
        self._results[prepared.token] = deepcopy(result)
        return result

    @staticmethod
    def _check_report(report, world, operation):
        try:
            evidence_native_report(report,world,operation)
        except EvidenceError as exc:
            raise BackendError('PHYSICS_BAKE_FAILED','Malformed native solver observations: '+str(exc),category='backend-crash') from exc
        require(isinstance(report, dict) and report.get('ok') is True and report.get('operation') == operation and
                report.get('native_bake_completed') is True and report.get('native_cache_is_baked') is True and
                isinstance(report.get('backend'),dict) and report['backend'].get('application_version', '').startswith('4.5.3') and
                report['backend'].get('application_build')==SUPPORTED_BLENDER_BUILD and type(report.get('native_cache_is_outdated')) is bool,
                'PHYSICS_BAKE_FAILED', 'Native report does not establish the actual supported baked solver operation.', category='backend-crash')
        if operation == 'simulate':
            require(report.get('diagnostic_render',{}).get('scope')=='neutral solver diagnostic only' and
                    report['diagnostic_render'].get('visual_review')=='not-performed',
                    'PHYSICS_OUTPUT_MISSING','Actual diagnostic render metadata is missing.',category='backend-crash')
            steps = int(1 / (_ratio(world['fps']) * _ratio(world['solver']['fixed_step']) / int(world['solver']['substeps'])))
            require(report.get('sample_count') == world['frame_range']['end'] and report.get('object_count') == len(world['bodies']) and
                    report.get('actual_substeps_per_frame') == steps and report.get('actual_solver_iterations') == world['solver']['iterations'],
                    'PHYSICS_BAKE_FAILED', 'Actual native settings/object/sample counts differ from admission.', category='backend-crash')
            require(report.get('actual_fps') == world['fps'] and
                    len(report.get('actual_gravity_canonical', [])) == 3 and
                    all(abs(a-b) <= max(1,abs(b))*1e-6 for a,b in zip(report['actual_gravity_canonical'],world['gravity'])),
                    'PHYSICS_BAKE_FAILED', 'Native frame rate or gravity was clamped/changed.', category='backend-crash')
            require(type(report.get('actual_bodies')) is list and len(report['actual_bodies']) == len(world['bodies']),
                    'PHYSICS_BAKE_FAILED', 'Native physical parameter observations are missing.', category='backend-crash')
            materials = {item['id']:item for item in world['materials']}
            for observed, body in zip(report['actual_bodies'],world['bodies']):
                material = materials[body['colliders'][0]['material']]
                require(observed.get('id') == body['id'] and observed.get('type') == ('PASSIVE' if body['mode']=='static' else 'ACTIVE') and
                        observed.get('shape') == body['colliders'][0]['shape']['type'].upper(),
                        'PHYSICS_BAKE_FAILED', 'Native authority/shape differs from the physical world.', category='backend-crash')
                values = {'effective_mass':body['mass']['kilograms'], 'friction':material['dynamic_friction'],
                          **{key:material[key] for key in ('restitution','linear_damping','angular_damping','collision_margin')}}
                require(all(type(observed.get(key)) in (int,float) and math.isfinite(observed[key]) and
                            abs(observed[key]-expected) <= max(1e-9,abs(expected))*1e-6 for key,expected in values.items()),
                        'PHYSICS_BAKE_FAILED', 'Native mass/material settings differ from the admitted request.', category='backend-crash')

    def bake(self, result, destination):
        owner = self._owner(result)
        require(self._results.get(result.token) == result, 'PHYSICS_OWNERSHIP', 'Only this attempt\'s unmodified actual simulation result may be published.')
        destination = _new_destination(destination)
        stage = Path(tempfile.mkdtemp(prefix='.' + destination.name + '.physics-', dir=destination.parent))
        try:
            with _Reader(result.root) as reader:
                for file in result.files:
                    payload = reader.read(file['path'])
                    require(digest(payload) == file['sha256'] and len(payload) == file['bytes'], 'PHYSICS_CACHE_CORRUPT', 'Simulation artifacts changed before publication.', category='cache-corruption')
                    _write(stage/file['path'], payload)
            world = owner['world']
            cache = dict(schema_version=1, kind='hatch-3d-physics-cache', id='cache-' + result.token,
                world_sha256=result.world_sha256, physics_input_sha256=result.world_sha256, backend=result.backend,
                frame_range=world['frame_range'], fps=world['fps'], fixed_step=world['solver']['fixed_step'],
                substeps=world['solver']['substeps'], sample_count=world['frame_range']['end'], files=list(result.files),
                upstream_hashes=sorted({item['sha256'] for item in owner['inventory'].values()}),
                seed=world['solver']['seed'], reproducibility='verified-cache-replay', numerical_tolerance=1e-5,
                usage=result.usage, fixture=result.fixture, review_status='unreviewed')
            validator, error_type = _contracts()
            try:
                validator(cache, 'PhysicsCache')
            except error_type as exc:
                raise BackendError('PHYSICS_SCHEMA', str(exc), category='invalid-input') from exc
            _write(stage/'cache.json', canonical(cache))
            verify_cache(stage, expected_world_sha256=result.world_sha256)
            # Reuse the reviewed common exclusive publication primitive (#198).
            _publish(stage, destination)
            return dict(status='baked-and-verified', destination=str(destination), cache_sha256=digest((destination/'cache.json').read_bytes()),
                        world_sha256=result.world_sha256, sample_count=cache['sample_count'], fixture=result.fixture,
                        review_status='unreviewed')
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def inspect(self, cache, frame):
        return inspect_cache(cache, frame)

    def verify(self, cache, *, expected_world_sha256=None):
        return verify_cache(cache, expected_world_sha256=expected_world_sha256)

    def validate_cache(self, cache, *, expected_world_sha256=None):
        return self.verify(cache, expected_world_sha256=expected_world_sha256)

    def export_animation(self, cache, destination, *, format='native-transform-json'):
        return export_animation(cache, destination, format=format)

    def cleanup(self, value):
        owner = self._owner(value)
        shutil.rmtree(owner['root'])
        self._owned.pop(value.token)
        self._results.pop(value.token, None)
        self._attempted.discard(value.token)

    def probe(self, *, smoke=False):
        require(type(smoke) is bool, 'PHYSICS_REQUEST', 'smoke must be an explicit boolean.', category='invalid-input')
        limitations = ('Only sphere/box OBJ collision shapes are admitted; other shape/category profiles fail closed.',
                      'Central global effectors, native kinematic translation and sleep controls are implemented; basic readiness smoke does not exercise them.',
                      'Direct off-center forces, torque, impulses, initial velocity, shifted COM and custom inertia lack an admitted Blender API.',
                      'Nonzero additional contact skin is unsupported; native shape margin property is not evidence of extra separation.',
                      'CPU Bullet only; GPU rendering hardware does not establish physics acceleration.',
                      'Blender does not expose its embedded Bullet version separately.',
                      'Numerical recomputation is not promised deterministic; verified cache replay is supported.',
                      'POSIX native execution/descriptor cache admission only; Windows is not advertised.',
                      'Blender4.5.3 can mark embedded baked cache outdated on reopen; every retained frame is independently compared.',
                      'RSS/disk monitoring is bounded polling, not an adversarial OS sandbox; no network namespace is claimed.')
        categories = {name: FeatureCapability('unsupported', False, 'No adapter implementation for this category.') for name in ALL_CATEGORIES}
        try:
            identity = self._discover()
        except BackendError as exc:
            state=_failure_state(exc.report['code'])
            categories['rigid-body']=FeatureCapability(state,False,str(exc))
            return CapabilityReport('blender', ADAPTER_VERSION, state, self.executable, exc.report.get('observed_application_version'), exc.report.get('observed_executable_sha256'),
                False, False, categories, (), (), (), (), hardware(), limitations, exc.report)
        categories['rigid-body'] = FeatureCapability('experimental', False, 'Executable discovered; no solver smoke executed in this probe.')
        evidence = None
        state = 'experimental'
        if smoke:
            try:
                from physics_backend_example import build_inputs
                world, payloads = build_inputs(backend=self.backend_id)
                prepared = self.prepare(world, payloads)
                try:
                    result = self.simulate(prepared, verify_consumption=True)
                    with tempfile.TemporaryDirectory(prefix='hatch-physics-smoke-') as name:
                        cache = Path(name)/'cache'
                        self.bake(result, cache)
                        inspection = verify_cache(cache)
                        first = inspect_cache(cache, 0)
                        middle = inspect_cache(cache, 12)
                        last = inspect_cache(cache, world['frame_range']['end']-1)
                        start = next(b for b in first['bodies'] if b['id'] == 'ball')['translation'][1]
                        halfway = next(b for b in middle['bodies'] if b['id'] == 'ball')['translation'][1]
                        final = next(b for b in last['bodies'] if b['id'] == 'ball')['translation'][1]
                        require(start > halfway > .5 and abs(final-.5) < .03,
                                'PHYSICS_SMOKE_FAILED', 'Actual solver did not demonstrate gravity and resting floor response.', category='numerical-instability')
                        evidence = dict(inspection=inspection, actual_start_height=start, actual_mid_height=halfway,
                            actual_end_height=final, fixture=True, source='procedural native mesh engineering fixture',
                            usage=result.usage, native_replay=result.verification,
                            native_consumption_control=parse((prepared.root/'native-consumption.json').read_bytes()))
                finally:
                    self.cleanup(prepared)
                evidence['additional_shape_smoke'] = _additional_shape_smoke(self)
                categories['rigid-body'] = FeatureCapability('available', True, 'Actual sphere/box gravity/contact bake and independent native reopen passed.')
                state = 'available'
            except (BackendError, RigError, OSError, ValueError) as exc:
                evidence = exc.report if hasattr(exc, 'report') else dict(message=str(exc))
                state = _failure_state(exc.report['code']) if isinstance(exc,BackendError) else 'failed-validation'
                categories['rigid-body'] = FeatureCapability(state, False, 'Real smoke failed; readiness is not established.')
        host=hardware()
        if evidence and isinstance(evidence.get('native_replay'),dict):
            native=evidence['native_replay']['backend']
            host.update(gpu_discovery=native['render_device_discovery'],render_devices=native['render_devices'])
        return CapabilityReport('blender', ADAPTER_VERSION, state, self.executable,
            identity['application_version'], identity['executable_sha256'], True, state == 'available', categories,
            ('sphere', 'box'), (), ('native-sampled-json', 'embedded-baked-blend'), ('native-transform-json',),
            host, limitations, evidence)


class PyBulletBackend(BlenderBackend):
    """Optional isolated DIRECT implementation; never a mandatory dependency.

    Availability is established by the chosen isolated interpreter, not the
    parent's import path. An installed module alone is still not solver readiness.
    """
    backend_id='pybullet'

    def __init__(self, interpreter=None):
        super().__init__(interpreter or sys.executable)
        # Preserve the venv launch pathname. Resolving its executable symlink
        # would accidentally select the base interpreter without optional deps.
        self.executable=str(Path(interpreter or sys.executable).absolute())
        self._module_record=None

    def _discover(self, *, timeout=10):
        require(self.executable and Path(self.executable).is_file(),'PHYSICS_BACKEND_UNAVAILABLE',
                'Choose an existing Python interpreter with optional PyBullet3.2.7 installed.')
        with tempfile.TemporaryDirectory(prefix='hatch-pybullet-discovery-') as directory:
            root=Path(directory)
            _write(root/'worker.py',_worker_bytes('pybullet'))
            _run_process([self.executable,'-I',str(root/'worker.py'),'--discover'],root,
                timeout=timeout,memory_bytes=2*1024**3,output_bytes=MAX_LOG_BYTES,label='discovery')
            lines=(root/'discovery-stdout.log').read_bytes().splitlines()
            require(lines,'PHYSICS_BACKEND_UNAVAILABLE','The isolated optional module probe returned no report.')
            record=parse(lines[-1])
        require(isinstance(record,dict) and record.get('available') is True,'PHYSICS_BACKEND_UNAVAILABLE',
                record.get('message','Optional PyBullet is unavailable in this isolated interpreter.') if isinstance(record,dict) else 'Invalid optional module probe.')
        require(record.get('solver_version')=='3.2.7','PHYSICS_BACKEND_VERSION_UNSUPPORTED',
                'Only the explicitly selected PyBullet3.2.7 adapter version is admitted; run real smoke before readiness.')
        try:evidence_module_identity(record)
        except EvidenceError as exc:
            raise BackendError('PHYSICS_BACKEND_VERSION_UNSUPPORTED','Unsupported isolated optional module identity: '+str(exc)) from exc
        self._module_record=record
        binary_hash=hashlib.sha256()
        with Path(self.executable).open('rb') as stream:
            for value in iter(lambda:stream.read(1024*1024),b''):binary_hash.update(value)
        self._identity=dict(id='pybullet',adapter_version=ADAPTER_VERSION,solver_name='PyBullet DIRECT (bundled Bullet)',
            solver_version='PyBullet '+record['solver_version'],application_version='Python '+record['python_version'],
            platform=platform.platform(),precision='mixed',device='cpu',executable_sha256=binary_hash.hexdigest())
        return self._identity

    def prepare(self, world, source_payloads, *, project_manifest='project.json'):
        prepared=super().prepare(world,source_payloads,project_manifest=project_manifest)
        self._owned[prepared.token]['module_record']=deepcopy(self._module_record)
        return prepared

    def _command(self,root):
        return [self.executable,'-I',str(root/'worker.py'),str(root/'worker-job.json')]

    def simulate(self, prepared, progress=None, cancel_token=None, *, verify_consumption=False):
        started=time.monotonic()
        require(isinstance(prepared,PreparedSimulation) and verify_consumption is False and
                (progress is None or callable(progress)) and
                (cancel_token is None or callable(getattr(cancel_token,'is_set',None))),
                'PHYSICS_REQUEST','Invalid PyBullet simulation request; Blender point-cache controls do not apply.',category='invalid-input')
        require(cancel_token is None or not cancel_token.is_set(),'PHYSICS_CANCELLED','The request was already cancelled.',category='timeout')
        owner=self._claim_attempt(prepared)
        root,world=owner['root'],owner['world']
        deadline=started+prepared.estimate.timeout_seconds
        with _Reader(root) as reader:
            require(digest(reader.read('world.json',MAX_DOCUMENT_BYTES))==prepared.world_sha256 and
                    digest(reader.read('worker.py',1024*1024))==owner['worker_hash'] and
                    digest(reader.read('worker-job.json',MAX_DOCUMENT_BYTES))==owner['worker_job_sha256'],
                    'PHYSICS_INPUT_CHANGED','Prepared world/worker/job bytes changed.')
        with _Reader(root/'inputs') as reader:
            for path,expected in owner['inventory'].items():
                value=reader.read(path,MAX_INPUT_BYTES)
                require(len(value)==expected['bytes'] and digest(value)==expected['sha256'],
                        'PHYSICS_INPUT_CHANGED','Prepared source bytes changed.')
        identity=self._discover(timeout=min(10.,_remaining(deadline)))
        require(identity==owner['identity'] and self._module_record==owner['module_record'],
                'PHYSICS_BACKEND_CHANGED','The isolated interpreter/module changed after preparation.')
        try:
            measured=_run_process(self._command(root),root,timeout=_remaining(deadline),
                memory_bytes=world['budgets']['memory_bytes'],output_bytes=min(world['budgets']['temporary_bytes'],MAX_CACHE_BYTES),
                cancel_token=cancel_token,progress=progress)
        except BackendError:
            if (root/'worker-failure.json').exists():
                with _Reader(root) as reader:failure=parse(reader.read('worker-failure.json',65536))
                fields={'code','world_sha256','solver_step','solver_time','setting','limit','observed'}
                require(type(failure) is dict and set(failure)==fields and failure['code']=='PHYSICS_NUMERICAL_INSTABILITY' and
                        failure['world_sha256']==prepared.world_sha256 and type(failure['solver_step']) is int and
                        0<=failure['solver_step']<=timing_clock(world)['solver_steps'] and failure['setting'] in ('max_linear_velocity','max_angular_velocity') and
                        failure['limit']==min(world['solver'].get('stability',{}).get(failure['setting'],50.),50.) and type(failure['observed']) in (int,float) and failure['observed']>failure['limit'],
                        'PHYSICS_BACKEND_CRASH','Malformed numerical failure observations.',category='backend-crash')
                time_value=failure['solver_step']*_ratio(world['solver']['fixed_step'])/world['solver']['substeps']
                require(failure['solver_time']==rational(time_value),'PHYSICS_BACKEND_CRASH','Failure solver clock differs.',category='backend-crash')
                error=BackendError('PHYSICS_NUMERICAL_INSTABILITY','Observed '+failure['setting']+' exceeded limit at solver step '+str(failure['solver_step'])+' time '+str(time_value),
                    category='numerical-instability',last_valid_frame=None if failure['solver_step']==0 else max(0,math.floor((time_value-_ratio(world['solver']['fixed_step'])/world['solver']['substeps'])*_ratio(world['fps']))))
                error.report.update(solver_step=failure['solver_step'],solver_time=failure['solver_time'],setting=failure['setting'],limit=failure['limit'],observed=failure['observed'])
                raise error
            raise
        require(all((root/name).is_file() for name in ('states.json','solver-states.json','contacts.json','worker-report.json')),
                'PHYSICS_OUTPUT_MISSING','DIRECT execution omitted required actual state/contact outputs.',category='backend-crash')
        report=parse((root/'worker-report.json').read_bytes())
        _check_pybullet_report(report,world,owner['module_record'])
        states=parse((root/'states.json').read_bytes())
        _validate_samples(states,world,prepared.world_sha256,prepared.fixture)
        contacts=parse((root/'contacts.json').read_bytes())
        _validate_contacts(contacts,world,prepared.world_sha256)
        _validate_solver_samples(parse((root/'solver-states.json').read_bytes()),world,prepared.world_sha256,prepared.fixture,states)
        _write(root/'timing.json',canonical(_timing_receipt(world, identity)))
        _write(root/'simulation-report.json',canonical(report))
        _write(root/'verification.json',canonical(dict(schema_version=1,world_sha256=prepared.world_sha256,
            source_admission='native Scene3D contract/hash closure and actual rig OBJ decode',
            project_manifest=owner['project_manifest'],backend_identity=identity,worker_job_sha256=owner['worker_job_sha256'],
            worker_sha256=owner['worker_hash'],executable_sha256=identity['executable_sha256'],
            backend_module=owner['module_record'],solver_report=report,
            sample_cache_verification=dict(status='structure-and-finite-values-verified',sample_count=len(states['samples']),
                contact_frames=len(contacts['samples'])),
            native_continuation='unsupported-clean-restart-only',artistic_review='not-performed')))
        _remaining(deadline)
        files=tuple(_inventory(root))
        usage=dict(elapsed_seconds=time.monotonic()-started,peak_memory_bytes=measured['peak_memory_bytes'],
                   output_bytes=sum(file['bytes'] for file in files),solver_steps=prepared.estimate.solver_steps)
        require(usage['output_bytes']<=world['budgets']['cache_bytes'],'PHYSICS_RESOURCE_LIMIT',
                'Final DIRECT files exceed cache admission.',category='resource-exhaustion')
        result=SimulationResult(prepared.token,root,prepared.world_sha256,identity,files,usage,
                                dict(sample_cache='structure-and-finite-values-verified',native_checkpoint='unsupported'),prepared.fixture)
        self._results[prepared.token]=deepcopy(result)
        return result

    def probe(self, *, smoke=False):
        require(type(smoke) is bool,'PHYSICS_REQUEST','smoke must be an explicit boolean.',category='invalid-input')
        categories={name:FeatureCapability('unsupported',False,'No adapter implementation for this category.') for name in ALL_CATEGORIES}
        limitations=('Optional PyBullet3.2.7; not installed or imported by normal2D workflows.',
                     'DIRECT CPU only; distinct numerical behavior from Blender.',
                     'The default profile and basic readiness smoke cover box/sphere rigid bodies, fixed stepping and actual contacts.',
                     'Opt-in explicit-proxies-v1 additionally supports capsule/cylinder/convex/compound/static-mesh proxies; the basic smoke does not establish readiness for those shapes.',
                     'The explicit proxy profile provides cache-bound native ray queries; cylinder/hull-inclusive rays, shape sweeps and CCD guarantees remain unsupported.',
                     'Bounded native force/torque/one-step integrated impulses, initial velocities and inertial frames are implemented; basic readiness smoke does not exercise these controls.',
                     'Joints and solver checkpoint continuation remain unsupported; explicit sleep thresholds are unavailable.',
                     'Cache export contains exact sampled transforms/contacts, not native solver checkpoint state.')
        try:
            identity=self._discover()
        except BackendError as exc:
            state=_failure_state(exc.report['code'])
            categories['rigid-body']=FeatureCapability(state,False,str(exc))
            return CapabilityReport('pybullet',ADAPTER_VERSION,state,self.executable,None,None,False,False,
                categories,(),(),(),(),hardware(),limitations,exc.report)
        state='experimental';evidence=deepcopy(self._module_record)
        categories['rigid-body']=FeatureCapability('experimental',False,'Module discovered, no real DIRECT smoke executed.')
        if smoke:
            prepared=None
            try:
                from physics_backend_example import build_inputs
                world,payloads=build_inputs(backend='pybullet')
                prepared=self.prepare(world,payloads)
                result=self.simulate(prepared)
                with tempfile.TemporaryDirectory(prefix='hatch-pybullet-smoke-') as directory:
                    cache=Path(directory)/'cache'
                    self.bake(result,cache)
                    initial=inspect_cache(cache,0)
                    final=inspect_cache(cache,world['frame_range']['end']-1)
                    y0=next(body for body in initial['bodies'] if body['id']=='ball')['translation'][1]
                    yn=next(body for body in final['bodies'] if body['id']=='ball')['translation'][1]
                    contacts=parse((prepared.root/'contacts.json').read_bytes())
                    require(y0>yn and abs(yn-.5)<.03 and any(frame['contacts'] for frame in contacts['samples']),
                            'PHYSICS_SMOKE_FAILED','Actual DIRECT run did not establish gravity/contact readiness.',category='numerical-instability')
                    evidence=dict(module=self._module_record,verification=verify_cache(cache),initial_height=y0,
                                  final_height=yn,actual_contact_frames=sum(bool(frame['contacts']) for frame in contacts['samples']),
                                  usage=result.usage,fixture=True)
                categories['rigid-body']=FeatureCapability('available',True,'Actual DIRECT connection/step/contact/cache/export smoke passed.')
                evidence['additional_shape_smoke']=_additional_shape_smoke(self)
                state='available'
            except (BackendError,RigError,OSError,ValueError) as exc:
                evidence=exc.report if hasattr(exc,'report') else dict(message=str(exc))
                state=_failure_state(exc.report['code']) if isinstance(exc,BackendError) else 'failed-validation'
                categories['rigid-body']=FeatureCapability(state,False,'Actual DIRECT smoke failed.')
            finally:
                if prepared is not None:self.cleanup(prepared)
        return CapabilityReport('pybullet',ADAPTER_VERSION,state,self.executable,identity['application_version'],
            identity['executable_sha256'],True,state=='available',categories,('box','sphere'),(),
            ('native-sampled-json',),('native-transform-json',),hardware(),limitations,evidence)


def _check_pybullet_report(report,world,module,*,legacy=False):
    try:evidence_direct_report(report,world)
    except EvidenceError as exc:
        raise BackendError('PHYSICS_BAKE_FAILED','Malformed DIRECT solver observations: '+str(exc),category='backend-crash') from exc
    if not legacy or 'effective_engine' in report:
        expected_engine=dict(fixed_time_step=float(_ratio(world['solver']['fixed_step'])/world['solver']['substeps']),
            internal_substeps=0,iterations=world['solver']['iterations'])
        require(report.get('effective_engine')==expected_engine,'PHYSICS_BAKE_FAILED',
                'Observed native timestep/internal substeps/iterations differ.',category='backend-crash')
    steps=math.ceil(1/(_ratio(world['fps'])*_ratio(world['solver']['fixed_step'])/world['solver']['substeps']))
    require(isinstance(report,dict) and isinstance(module,dict) and report.get('ok') is True and report.get('operation')=='simulate' and
            report.get('connection_mode')=='DIRECT' and report.get('contacts_queried') is True and
            report.get('sample_count')==world['frame_range']['end'] and report.get('object_count')==len(world['bodies']) and
            (report.get('actual_steps_per_frame')==steps if legacy else report.get('maximum_steps_per_output_interval')==steps) and report.get('actual_solver_steps')==timing_clock(world)['solver_steps'] and
            report.get('backend')==module and module.get('solver_version')=='3.2.7',
            'PHYSICS_BAKE_FAILED','Actual DIRECT lifecycle/module/step observations differ from admission.',category='backend-crash')
    if not legacy:
        mappings=timing_clock(world)['sample_brackets']
        expected_counts=[entry['upper']-(mappings[index-1]['upper'] if index else 0) for index,entry in enumerate(mappings)]
        require(report.get('steps_per_output_interval')==expected_counts,'PHYSICS_BAKE_FAILED',
                'Observed solver step counts per output interval differ.',category='backend-crash')
    require(type(report.get('actual_bodies')) is list and len(report['actual_bodies'])==len(world['bodies']),
            'PHYSICS_BAKE_FAILED','DIRECT effective physical observations are missing.',category='backend-crash')
    materials={value['id']:value for value in world['materials']}
    for actual,body in zip(report['actual_bodies'],world['bodies']):
        material=materials[body['colliders'][0]['material']]
        shape=body['colliders'][0]['shape']
        controls=report.get('rigid_controls',{}).get('bodies',[None]*len(world['bodies']))[world['bodies'].index(body)]
        wrapped=controls is not None and (any(controls['inertial_position']) or abs(abs(controls['inertial_rotation'][3])-1)>1e-10)
        expected_margin=(0 if wrapped else shape['radius']) if shape['type']=='sphere' else material['collision_margin']
        explicit_proxies=world.get('collision_profile')=='explicit-proxies-v1'
        if explicit_proxies:
            child=controls['collision_children'][0]
            wrapped=any(abs(x)>1e-10 for x in child['translation']) or abs(abs(child['rotation'][3])-1)>1e-10
            expected_margin=(0 if wrapped else shape['radius']) if shape['type']=='sphere' else material['collision_margin']
        expected_semantics=('inherent sphere radius' if shape['type']=='sphere' and not wrapped else 'explicit proxy margin') if explicit_proxies else (('transformed primitive wrapper margin' if wrapped else 'inherent sphere radius') if shape['type']=='sphere' else 'explicit box collision margin')
        require(actual['margin_semantics']==expected_semantics,'PHYSICS_BAKE_FAILED','Native margin semantics differ from the admitted child representation.',category='backend-crash')
        require(type(actual.get('center_of_mass')) is list and len(actual['center_of_mass'])==3 and all(type(a) in (int,float) and math.isfinite(a) and abs(a-b)<=1e-6 for a,b in zip(actual['center_of_mass'],body['mass']['center_of_mass'])),
                'PHYSICS_BAKE_FAILED','DIRECT observed center of mass differs.',category='backend-crash')
        require(actual.get('id')==body['id'] and all(type(actual.get(key)) in (int,float) and math.isfinite(actual[key]) and
                    abs(actual[key]-expected)<=max(1e-9,abs(expected))*1e-6 for key,expected in
                    [('mass',body['mass']['kilograms']),('friction',material['dynamic_friction']),('restitution',material['restitution']),('native_margin',expected_margin)]),
                'PHYSICS_BAKE_FAILED','DIRECT effective mass/friction/restitution/margin differs.',category='backend-crash')
        if 'rigid_controls' in report:
            import numpy as np
            from rig_math import qmatrix, qmul
            controls=report['rigid_controls']['bodies'][world['bodies'].index(body)]
            rotation=qmatrix(controls['inertial_rotation'])
            if explicit_proxies:
                from physics_collisions import verify_native_children, CollisionError
                try: verify_native_children(body,controls)
                except (CollisionError,KeyError,TypeError,ValueError) as exc:
                    raise BackendError('PHYSICS_BAKE_FAILED',str(exc),category='backend-crash') from exc
            else:
                collision=controls['collision'];native_shape=body['colliders'][0]['shape']
                expected_dimensions=[native_shape['radius']]*3 if native_shape['type']=='sphere' else native_shape['size']
                require(collision['type']==native_shape['type'] and np.allclose(collision['dimensions'],expected_dimensions,rtol=1e-6,atol=0) and
                        np.max(np.abs(rotation @ np.asarray(collision['translation'])+np.asarray(body['mass']['center_of_mass'])))<=1e-6 and
                        np.allclose(qmatrix(qmul(controls['inertial_rotation'],collision['rotation'])),np.eye(3),rtol=0,atol=1e-6),
                        'PHYSICS_BAKE_FAILED','Native collision child/frame differs from the unchanged declared primitive.',category='backend-crash')
            tensor=rotation @ np.diag(controls['principal_inertia']) @ rotation.T
            requested=np.asarray(body['mass']['inertia'])
            if body['mode']=='dynamic':
                moments,axes=np.linalg.eigh(requested)
                relative=(axes.T @ tensor @ axes) / np.sqrt(np.outer(moments,moments))
                matches=all(x>0 for x in controls['principal_inertia']) and float(np.max(np.abs(relative-np.eye(3)))) <= 1e-5
            else:
                matches=all(x==0 for x in controls['principal_inertia']) and not np.any(tensor)
            require(matches, 'PHYSICS_BAKE_FAILED',
                    'Native principal inertia/frame does not reconstruct every requested relative inertia direction.',category='backend-crash')
            expected_inertia=controls['principal_inertia']
        else:
            expected_inertia=[body['mass']['inertia'][i][i] for i in range(3)]
        require(type(actual.get('inertia')) is list and len(actual['inertia'])==3 and
                all(type(a) in (int,float) and math.isfinite(a) and abs(a-b)<=max(1e-300,abs(b))*1e-5 for a,b in zip(actual['inertia'],expected_inertia)),
                'PHYSICS_BAKE_FAILED','DIRECT native inertia differs from the admitted primitive.',category='backend-crash')


def _additional_shape_smoke(backend):
    from physics_backend_example import build_inputs
    world,payloads=build_inputs(backend=backend.backend_id,dynamic_shape='box',floor_shape='sphere')
    prepared=backend.prepare(world,payloads)
    try:
        result=backend.simulate(prepared)
        values=parse((prepared.root/'states.json').read_bytes())
        y0=values['samples'][0]['bodies'][0]['translation'][1]
        yn=values['samples'][-1]['bodies'][0]['translation'][1]
        require(y0>yn and abs(yn-.5)<.1,'PHYSICS_SMOKE_FAILED',
                'Actual dynamic box/static sphere profile did not demonstrate expected gravity/contact response.',category='numerical-instability')
        with tempfile.TemporaryDirectory(prefix='hatch-physics-shape-') as directory:
            cache=Path(directory)/'cache'
            backend.bake(result,cache)
            report=verify_cache(cache)
        return dict(dynamic_shape='box',static_shape='sphere',actual_initial_height=y0,actual_final_height=yn,
                    verification=report,usage=result.usage,fixture=True)
    finally:
        backend.cleanup(prepared)


def _validate_contacts(value,world,world_hash,*,legacy=False):
    body_map={body['id']:body for body in world['bodies']}
    ids=set(body_map)
    require(isinstance(value,dict) and set(value)=={'schema_version','kind','world_sha256','samples'} and
            value['schema_version']==1 and value['kind']=='hatch-pybullet-contacts' and value['world_sha256']==world_hash and
            type(value['samples']) is list and len(value['samples'])==world['frame_range']['end'],
            'PHYSICS_CACHE_CORRUPT','DIRECT contact header/count differs.',category='cache-corruption')
    for index,frame in enumerate(value['samples']):
        require(isinstance(frame,dict) and set(frame)==({'frame','contacts'} if legacy else {'frame','contacts','solver_step','solver_time'}) and type(frame['frame']) is int and frame['frame']==index and
                type(frame['contacts']) is list and len(frame['contacts'])<=4096,
                'PHYSICS_CACHE_CORRUPT','DIRECT contact frame/count differs.',category='cache-corruption')
        if not legacy:
            step=_ratio(world['solver']['fixed_step'])/world['solver']['substeps']
            native_index=math.ceil(Fraction(index,1)/_ratio(world['fps'])/step)
            require(type(frame['solver_step']) is int and frame['solver_step']==native_index and frame['solver_time']==rational(native_index*step),
                    'PHYSICS_CACHE_CORRUPT','Contact observation clock differs from actual solver bracket.',category='cache-corruption')
        for contact in frame['contacts']:
            expected_fields={'body_a','body_b','position_a','position_b','normal_b','distance','normal_force'}
            if world.get('collision_profile')=='explicit-proxies-v1':
                expected_fields.update(('normal_impulse','lateral_force_1','lateral_direction_1','lateral_force_2','lateral_direction_2'))
            require(isinstance(contact,dict) and set(contact)==expected_fields and
                    type(contact['body_a']) is str and type(contact['body_b']) is str and
                    contact['body_a'] in ids and contact['body_b'] in ids and contact['body_a']!=contact['body_b'],
                    'PHYSICS_CACHE_CORRUPT','DIRECT contact endpoints are invalid.',category='cache-corruption')
            if world.get('collision_profile')=='explicit-proxies-v1':
                scalar_keys=('normal_impulse','lateral_force_1','lateral_force_2')
                require(all(type(contact[k]) in (int,float) and math.isfinite(contact[k]) and abs(contact[k])<=1e12 for k in scalar_keys),
                        'PHYSICS_CACHE_CORRUPT','Invalid native contact impulse or tangent force.',category='cache-corruption')
                require(abs(contact['normal_impulse']-contact['normal_force']*float(step))<=1e-8*max(1,contact['normal_impulse']),
                        'PHYSICS_CACHE_CORRUPT','Native contact impulse differs from the observed step force.',category='cache-corruption')
                left=body_map[contact['body_a']]['colliders'][0];right=body_map[contact['body_b']]['colliders'][0]
                require(bool(left['group'] & right['mask']) and bool(right['group'] & left['mask']),
                        'PHYSICS_CACHE_CORRUPT','Filtered collision pair produced contact.',category='cache-corruption')
                excluded={frozenset(pair) for pair in world.get('collision_exclusions',[])}
                require(frozenset((contact['body_a'],contact['body_b'])) not in excluded,
                        'PHYSICS_CACHE_CORRUPT','Excluded collision pair produced contact.',category='cache-corruption')
            for name in (('position_a','position_b','normal_b','lateral_direction_1','lateral_direction_2') if world.get('collision_profile')=='explicit-proxies-v1' else ('position_a','position_b','normal_b')):
                require(type(contact[name]) is list and len(contact[name])==3 and
                        all(type(x) in (int,float) and abs(x)<=1e12 and math.isfinite(x) for x in contact[name]),
                        'PHYSICS_CACHE_CORRUPT','DIRECT contact vector is invalid.',category='cache-corruption')
            require(type(contact['distance']) in (int,float) and abs(contact['distance'])<=1e6 and math.isfinite(contact['distance']) and
                    type(contact['normal_force']) in (int,float) and 0<=contact['normal_force']<=1e12 and math.isfinite(contact['normal_force']) and
                    abs(sum(x*x for x in contact['normal_b'])-1)<=1e-4,
                    'PHYSICS_CACHE_CORRUPT','DIRECT contact scalar is invalid.',category='cache-corruption')


def _inventory(root):
    files = []
    for path in sorted(Path(root).rglob('*')):
        require(not path.is_symlink(), 'PHYSICS_WORKER_OUTPUT', 'Linked output is not a physics artifact.')
        if path.is_file():
            payload = path.read_bytes()
            require(len(payload) <= MAX_CACHE_BYTES, 'PHYSICS_RESOURCE_LIMIT', 'A physics artifact exceeds its per-file bound.', category='resource-exhaustion')
            files.append(dict(path=path.relative_to(root).as_posix(), sha256=digest(payload), bytes=len(payload)))
    require(len(files) <= MAX_FILES, 'PHYSICS_RESOURCE_LIMIT', 'Too many generated cache files.', category='resource-exhaustion')
    return files


def _validate_solver_samples(value, world, world_hash, fixture, output):
    require(type(value) is dict and set(value)=={'schema_version','kind','world_sha256','effective_step','samples'} and
            value['schema_version']==1 and value['kind']=='hatch-physics-solver-states' and value['world_sha256']==world_hash and
            value['effective_step']==timing_clock(world)['effective_solver_step'],
            'PHYSICS_CACHE_CORRUPT','Solver-step cache identity/clock differs.',category='cache-corruption')
    schedule=timing_clock(world)
    count=schedule['solver_steps']+1
    require(type(value['samples']) is list and len(value['samples'])==count,
            'PHYSICS_CACHE_CORRUPT','Solver-step cache count differs.',category='cache-corruption')
    projected=[]
    for entry in value['samples']:
        require(type(entry) is dict and set(entry)=={'step','bodies'},'PHYSICS_CACHE_CORRUPT','Malformed solver sample.',category='cache-corruption')
        projected.append(dict(frame=entry['step'],bodies=entry['bodies']))
    synthetic=deepcopy(world);synthetic['frame_range']=dict(start=0,end=count)
    _validate_samples(dict(schema_version=1,kind='hatch-physics-samples',world_sha256=world_hash,
        frame_range=synthetic['frame_range'],fps=world['fps'],fixture=fixture,samples=projected),synthetic,world_hash,fixture)
    for sample, mapping in zip(output['samples'],schedule['sample_brackets']):
        lower=value['samples'][mapping['lower']]['bodies']
        upper=value['samples'][mapping['upper']]['bodies']
        alpha=_ratio(mapping['alpha'])
        expected=[interpolate_state(a,b,alpha) for a,b in zip(lower,upper)]
        for actual,target in zip(sample['bodies'],expected):
            translation=max(abs(a-b) for a,b in zip(actual['translation'],target['translation']))
            rotation=min(max(abs(a-sign*b) for a,b in zip(actual['rotation'],target['rotation'])) for sign in (-1,1))
            require(translation<=1e-5 and rotation<=1e-5,'PHYSICS_CACHE_CORRUPT',
                    'Output pose disagrees with completed solver-state interpolation.',category='cache-corruption')



def _validate_samples(value, world, world_hash, fixture):
    require(isinstance(value, dict) and set(value) == {'schema_version', 'kind', 'world_sha256', 'frame_range', 'fps', 'fixture', 'samples'} and
            value['schema_version'] == 1 and value['kind'] == 'hatch-physics-samples' and value['world_sha256'] == world_hash and
            value['frame_range'] == world['frame_range'] and value['fps'] == world['fps'] and value['fixture'] is fixture,
            'PHYSICS_CACHE_CORRUPT', 'Sample header disagrees with its admitted world.', category='cache-corruption')
    count = world['frame_range']['end']
    ids = [body['id'] for body in world['bodies']]
    require(type(value['samples']) is list and len(value['samples']) == count,
            'PHYSICS_CACHE_CORRUPT', 'Sample count differs from the complete half-open range.', category='cache-corruption')
    for index, sample in enumerate(value['samples']):
        require(isinstance(sample, dict) and set(sample) == {'frame', 'bodies'} and type(sample['frame']) is int and sample['frame'] == index and
                isinstance(sample['bodies'], list) and len(sample['bodies']) == len(ids),
                'PHYSICS_CACHE_CORRUPT', 'Sample ordering/object counts are invalid.', category='cache-corruption')
        for identifier, body in zip(ids, sample['bodies']):
            require(isinstance(body, dict) and set(body) == {'id', 'translation', 'rotation'} and body['id'] == identifier,
                    'PHYSICS_CACHE_CORRUPT', 'Sample body identity is invalid.', category='cache-corruption')
            for field, size in [('translation', 3), ('rotation', 4)]:
                require(type(body[field]) is list and len(body[field]) == size and all(type(x) in (int, float) and abs(x)<=1e12 and math.isfinite(x) for x in body[field]),
                        'PHYSICS_CACHE_CORRUPT', 'Sample transform is malformed or nonfinite.', category='cache-corruption')
            require(abs(sum(x*x for x in body['rotation'])-1) < 1e-4 and max(map(abs, body['translation'])) <= 1e6,
                    'PHYSICS_NUMERICAL_INSTABILITY', 'Sample quaternion/position exceeds numerical limits.', category='numerical-instability')


def _read_cache(root, *, expected_world_sha256=None):
    try:
        return _read_cache_impl(root,expected_world_sha256=expected_world_sha256)
    except BackendError:
        raise
    except (RigError,OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError) as exc:
        raise BackendError('PHYSICS_CACHE_CORRUPT','Malformed or inconsistent cache input: '+str(exc),category='cache-corruption') from exc


def _read_cache_impl(root, *, expected_world_sha256=None):
    validator, error_type = _contracts()
    with _Reader(root) as reader:
        manifest_payload=reader.read('cache.json', MAX_DOCUMENT_BYTES)
        manifest = parse(manifest_payload)
        try:
            validator(manifest, 'PhysicsCache')
        except (error_type, KeyError, TypeError, ValueError) as exc:
            raise BackendError('PHYSICS_CACHE_CORRUPT', 'Invalid cache contract: ' + str(exc), category='cache-corruption') from exc
        # Standard JSON Schema integer admits integral-valued JSON floats.
        # Preserve that contract while using canonical ints at I/O boundaries.
        for file in manifest['files']:file['bytes']=int(file['bytes'])
        for key in ('sample_count','substeps','seed'):manifest[key]=int(manifest[key])
        for key in ('fps','fixed_step','frame_range'):
            manifest[key]={name:int(value) for name,value in manifest[key].items()}
        for key in ('peak_memory_bytes','output_bytes','solver_steps'):
            manifest['usage'][key]=int(manifest['usage'][key])
        require(manifest['review_status'] == 'unreviewed' and manifest['reproducibility'] == 'verified-cache-replay',
                'PHYSICS_CACHE_CORRUPT', 'Native adapter cache cannot manufacture review or recomputation guarantees.', category='cache-corruption')
        _paths(file['path'] for file in manifest['files'])
        require(reader.names() == {file['path'] for file in manifest['files']} | {'cache.json'},
                'PHYSICS_CACHE_CORRUPT','Actual cache files differ from the complete manifest inventory.',category='cache-corruption')
        total = 0
        payloads = {}
        require(len(manifest['files']) <= MAX_FILES, 'PHYSICS_CACHE_CORRUPT', 'Cache inventory exceeds native admission.', category='cache-corruption')
        for file in manifest['files']:
            require(file['bytes'] <= MAX_CACHE_BYTES-total, 'PHYSICS_CACHE_CORRUPT', 'Cache inventory byte total is excessive.', category='cache-corruption')
            payload = reader.read(file['path'], file['bytes'])
            require(len(payload) == file['bytes'] and digest(payload) == file['sha256'],
                    'PHYSICS_CACHE_CORRUPT', 'Cache payload hash/size mismatch: ' + file['path'], category='cache-corruption')
            payloads[file['path']] = payload
            total += len(payload)
        required={'world.json', 'states.json', 'simulation-report.json', 'verification.json', 'worker.py', 'worker-job.json','worker-report.json'}
        required.update({'scene.blend','preview.png'} if manifest['backend']['id']=='blender' else {'contacts.json'})
        require(required <= set(payloads),
                'PHYSICS_CACHE_CORRUPT', 'Required native physics artifacts are missing.', category='cache-corruption')
        require(manifest['backend']['id']=='pybullet' or 'solver-states.json' not in payloads,
                'PHYSICS_CACHE_CORRUPT','Blender solver-substep cache extraction is unsupported.',category='cache-corruption')
        world = _world(parse(payloads['world.json']))
        _admit_world(world,manifest['backend']['id'])
        require(total+len(manifest_payload)<=min(world['budgets']['cache_bytes'],world['budgets']['temporary_bytes'],MAX_CACHE_BYTES) and
                all(file['bytes']<=world['budgets']['bake_file_bytes'] for file in manifest['files']),
                'PHYSICS_CACHE_CORRUPT','Actual retained cache files/manifest exceed bound world budgets.',category='cache-corruption')
        if digest(payloads['worker.py'])==LEGACY_WORKERS[world['solver']['backend']]:
            legacy_ratio=1/(_ratio(world['fps'])*_ratio(world['solver']['fixed_step'])/world['solver']['substeps'])
            require(legacy_ratio.denominator==1 and not world['solver'].get('stability'),
                    'PHYSICS_CACHE_CORRUPT','Known predecessor worker cannot honor this timing/stability profile.',category='cache-corruption')
        if 'timing.json' in payloads:
            require(parse(payloads['timing.json']) == _timing_receipt(world,manifest['backend']),
                    'PHYSICS_CACHE_CORRUPT','Exact timing/settings/reproducibility receipt changed.',category='cache-corruption')
        else:
            require(digest(payloads['worker.py']) == LEGACY_WORKERS[world['solver']['backend']],
                    'PHYSICS_CACHE_CORRUPT','Current adapter output omitted exact timing receipt.',category='cache-corruption')
        expected_steps=timing_clock(world)['solver_steps']
        require(manifest['usage']['solver_steps']==expected_steps and manifest['numerical_tolerance']==1e-5,
                'PHYSICS_CACHE_CORRUPT','Cache step/tolerance observations differ from the admitted profile.',category='cache-corruption')
        world_hash = digest(payloads['world.json'])
        require(world_hash == manifest['world_sha256'] == manifest['physics_input_sha256'] and
                (expected_world_sha256 is None or world_hash == expected_world_sha256),
                'PHYSICS_CACHE_STALE', 'Cache does not belong to the expected exact world bytes.', category='cache-corruption')
        require(manifest['frame_range'] == world['frame_range'] and manifest['fps'] == world['fps'] and
                manifest['fixed_step'] == world['solver']['fixed_step'] and manifest['substeps'] == world['solver']['substeps'] and
                manifest['seed'] == world['solver']['seed'],
                'PHYSICS_CACHE_STALE', 'Cache timing/settings differ from the bound world.', category='cache-corruption')
        require(manifest['backend']['id'] == world['solver']['backend'] and
                manifest['backend']['adapter_version'] == world['solver']['adapter_version'] == ADAPTER_VERSION,
                'PHYSICS_CACHE_STALE','Cache backend identity differs from its physical world.',category='cache-corruption')
        inputs={path.removeprefix('inputs/'):payload for path,payload in payloads.items() if path.startswith('inputs/')}
        require(world['scene']['path'] in inputs and digest(inputs[world['scene']['path']]) == world['scene']['sha256'],
                'PHYSICS_CACHE_STALE','Exact native scene source is missing or changed.',category='cache-corruption')
        scene=parse(inputs[world['scene']['path']])
        require(type(scene) is dict and type(scene.get('provenance')) is dict and
                (scene.get('kind'),scene.get('id'),scene.get('version')) == ('scene3d-scene',world['scene']['id'],world['scene']['version']) and
                scene['provenance'].get('fixture') is manifest['fixture'],
                'PHYSICS_PROVENANCE','Cache/native scene identity or fixture status differs.',category='cache-corruption')
        for asset in world['assets']:
            require(asset['path'] in inputs and digest(inputs[asset['path']]) == asset['sha256'],
                    'PHYSICS_CACHE_STALE','A required actual geometry source is missing or changed.',category='cache-corruption')
        require(manifest['upstream_hashes'] == sorted({digest(payload) for payload in inputs.values()}),
                'PHYSICS_CACHE_CORRUPT','Cache upstream hash inventory differs from retained source bytes.',category='cache-corruption')
        states = parse(payloads['states.json'])
        _validate_samples(states, world, world_hash, manifest['fixture'])
        receipt = parse(payloads['verification.json'])
        require(type(receipt) is dict and type(receipt.get('project_manifest')) is str and
                receipt.get('world_sha256') == world_hash and receipt.get('artistic_review') == 'not-performed' and
                receipt.get('worker_sha256') == digest(payloads['worker.py']) and
                receipt.get('executable_sha256') == manifest['backend']['executable_sha256'],
                'PHYSICS_CACHE_CORRUPT', 'Backend/worker evidence binding differs.', category='cache-corruption')
        project_manifest=path_name(receipt['project_manifest'])
        require(1<=len(inputs)<=MAX_FILES-32 and sum(map(len,inputs.values()))<=MAX_INPUT_BYTES,
                'PHYSICS_CACHE_CORRUPT','Retained native source inventory exceeds admission.',category='cache-corruption')
        try:
            with tempfile.TemporaryDirectory(prefix='hatch-physics-cache-source-') as temporary:
                private=Path(temporary)
                for name,payload in inputs.items():_write(private/name,payload)
                bundle,meshes,_,_=_source_admission(private,world,project_manifest)
                require(set(bundle['inventory'])==set(inputs),'PHYSICS_CACHE_CORRUPT',
                        'Retained native source closure differs from its declared inventory.',category='cache-corruption')
            evidence_records(manifest,world,receipt,parse(payloads['worker-job.json']),
                parse(payloads['simulation-report.json']),parse(payloads['worker-report.json']),meshes,
                digest(payloads['worker.py']),digest(payloads['worker-job.json']))
        except EvidenceError as exc:
            raise BackendError('PHYSICS_CACHE_CORRUPT','Malformed or contradictory execution evidence: '+str(exc),category='cache-corruption') from exc
        except BackendError as exc:
            if exc.report['code']=='PHYSICS_SOURCE_OWNER_UNAVAILABLE':raise
            raise BackendError('PHYSICS_CACHE_CORRUPT','Retained source/receipt admission failed: '+str(exc),category='cache-corruption') from exc
        if manifest['backend']['id']=='blender':
            try:blend_container(payloads['scene.blend'])
            except EvidenceError as exc:
                raise BackendError('PHYSICS_CACHE_CORRUPT','Malformed retained native artifact: '+str(exc),category='cache-corruption') from exc
            BlenderBackend._check_report(receipt.get('solver_report'), world, 'simulate')
            image=receipt['solver_report']['diagnostic_render']
            require(image.get('path')=='preview.png' and image.get('sha256')==digest(payloads['preview.png']) and
                    (image.get('width'),image.get('height'),image.get('samples'),image.get('device'))==(128,128,4,'CPU') and
                    payloads['preview.png'][:8]==b'\x89PNG\r\n\x1a\n' and len(payloads['preview.png'])>24 and
                    int.from_bytes(payloads['preview.png'][16:20],'big')==128 and int.from_bytes(payloads['preview.png'][20:24],'big')==128,
                    'PHYSICS_CACHE_CORRUPT','Diagnostic image bytes/header differ from the actual worker receipt.',category='cache-corruption')
            BlenderBackend._check_report(receipt.get('native_reopen'), world, 'reopen')
            require(receipt['native_reopen'].get('reversed_samples') == len(states['samples']) and
                    receipt['native_reopen'].get('maximum_replay_error', math.inf) <= 1e-5,
                    'PHYSICS_CACHE_CORRUPT', 'Cache lacks independent full native replay evidence.', category='cache-corruption')
        else:
            _check_pybullet_report(receipt.get('solver_report'),world,receipt.get('backend_module',{}),legacy=digest(payloads['worker.py'])==LEGACY_WORKERS['pybullet'])
            _validate_contacts(parse(payloads['contacts.json']),world,world_hash,legacy=digest(payloads['worker.py'])==LEGACY_WORKERS['pybullet'])
            if 'solver-states.json' in payloads:
                _validate_solver_samples(parse(payloads['solver-states.json']),world,world_hash,manifest['fixture'],states)
            else:
                require(digest(payloads['worker.py'])==LEGACY_WORKERS['pybullet'],
                        'PHYSICS_CACHE_CORRUPT','Current PyBullet output omitted solver cache.',category='cache-corruption')
            require(receipt.get('sample_cache_verification') == dict(status='structure-and-finite-values-verified',
                    sample_count=len(states['samples']),contact_frames=len(states['samples'])),
                    'PHYSICS_CACHE_CORRUPT','DIRECT sampled-cache verification receipt differs.',category='cache-corruption')
        if manifest['backend']['id']=='pybullet' and 'solver-states.json' in payloads:
            receipt['_solver_states']=parse(payloads['solver-states.json'])
        return manifest, world, states, receipt


def verify_cache(root, *, expected_world_sha256=None):
    manifest, world, states, receipt = _read_cache(root, expected_world_sha256=expected_world_sha256)
    return dict(status='hash-and-contract-verified', world_sha256=manifest['world_sha256'],
                cache_files=len(manifest['files']), output_bytes=manifest['usage']['output_bytes'],
                sample_count=len(states['samples']), backend=manifest['backend'],
                fixture=manifest['fixture'], review_status='unreviewed',
                native_outdated_flag=receipt.get('native_reopen',{}).get('native_cache_is_outdated'),
                evidence_authentication='unsigned retained execution receipts, not a cryptographic identity attestation',
                native_continuation='unsupported; clean restart only',
                numerical_recompute_guarantee='not-established')


def inspect_time(root, timestamp):
    """Seek verified completed output states at an exact rational timestamp."""
    require(isinstance(timestamp,Fraction), 'PHYSICS_CACHE_SAMPLE', 'Timestamp must be an exact Fraction.',category='invalid-input')
    manifest, world, states, receipt = _read_cache(root)
    output_coordinate=timestamp*_ratio(world['fps'])
    require(0<=output_coordinate<=len(states['samples'])-1,'PHYSICS_CACHE_SAMPLE',
            'Timestamp lies outside completed cache samples; extrapolation is unsupported.',category='invalid-input')
    coordinate=output_coordinate
    interval=1/_ratio(world['fps'])
    if '_solver_states' in receipt:
        solver_states=receipt['_solver_states']
        interval=_ratio(solver_states['effective_step'])
        coordinate=timestamp/interval
        states=solver_states
    left=math.floor(coordinate); right=math.ceil(coordinate)
    alpha=coordinate-left
    bodies=[interpolate_state(a,b,alpha) for a,b in zip(states['samples'][left]['bodies'],states['samples'][right]['bodies'])]
    return dict(time=rational(timestamp),bodies=bodies,bracket_frames=[math.floor(output_coordinate),math.ceil(output_coordinate)],
        interpolation_bracket=[left,right],alpha=rational(alpha),bracket_times=[rational(left*interval),rational(right*interval)],sample_clock='solver' if states.get('kind')=='hatch-physics-solver-states' else 'output',
        interpolation='linear translation and shortest-arc quaternion slerp',
        source=('hash-verified-direct-solved-state' if alpha==0 else 'hash-verified-completed-solver-state-interpolation') if '_solver_states' in receipt else 'hash-verified-completed-output-state-interpolation',world_sha256=manifest['world_sha256'],fixture=manifest['fixture'])


def inspect_cache(root, frame):
    manifest, world, states, _ = _read_cache(root)
    require(type(frame) is int and 0 <= frame < len(states['samples']),
            'PHYSICS_CACHE_SAMPLE', 'Inspection accepts only an existing integer output frame.', category='invalid-input')
    value = deepcopy(states['samples'][frame])
    t = Fraction(frame, 1)/_ratio(world['fps'])
    value.update(time=dict(numerator=t.numerator, denominator=t.denominator),
                 sampling=timing_clock(world)['sample_brackets'][frame],
                 source='hash-verified-sample-cache-replay', world_sha256=manifest['world_sha256'],
                 fixture=manifest['fixture'], contacts='not-exposed-by-Blender-adapter' if manifest['backend']['id']=='blender' else 'available-in-contacts.json')
    return value


def export_animation(root, destination, *, format='native-transform-json'):
    require(format == 'native-transform-json', 'PHYSICS_EXPORT_UNSUPPORTED',
            'Only exact sampled native-transform-json export is implemented.', category='capability-gap')
    manifest, world, states, _ = _read_cache(root)
    destination = _new_destination(destination)
    stage = Path(tempfile.mkdtemp(prefix='.'+destination.name+'.physics-', dir=destination.parent))
    try:
        payload = dict(schema_version=1, kind='hatch-physics-transform-animation',
            source_world_sha256=manifest['world_sha256'], source='hash-bound-cache-samples',
            execution_evidence='retained unsigned solver receipt; not a cryptographic identity attestation',
            coordinate_system=world['coordinate_system'], fps=world['fps'], frame_range=world['frame_range'],
            interpolation='none-exact-samples-only', samples=states['samples'], fixture=manifest['fixture'],
            review_status='unreviewed')
        data = canonical(payload)
        _write(stage/'transforms.json', data)
        _write(stage/'export.json', canonical(dict(format=format, path='transforms.json', sha256=digest(data), bytes=len(data),
                                                 world_sha256=manifest['world_sha256'], fixture=manifest['fixture'], review_status='unreviewed')))
        _publish(stage, destination)
        return dict(status='exported', destination=str(destination), sha256=digest(data), format=format,
                    sample_count=len(states['samples']), fixture=manifest['fixture'], review_status='unreviewed')
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def demonstrate(destination,backend):
    from physics_backend_example import build_inputs
    world,payloads=build_inputs(backend=backend.backend_id)
    destination=_new_destination(destination)
    stage=Path(tempfile.mkdtemp(prefix='.'+destination.name+'.physics-',dir=destination.parent))
    prepared=None
    try:
        for path,payload in payloads.items():_write(stage/'sources'/path,payload)
        _write(stage/'physics-world.json',canonical(world))
        prepared=backend.prepare(world,payloads)
        result=backend.simulate(prepared,verify_consumption=backend.backend_id=='blender')
        cache_report=backend.bake(result,stage/'cache')
        export_report=backend.export_animation(stage/'cache',stage/'animation')
        source=parse((prepared.root/'states.json').read_bytes())
        heights=[next(body for body in source['samples'][frame]['bodies'] if body['id']=='ball')['translation'][1] for frame in (0,12,48)]
        freefall_error=abs(heights[1]-(3-.5*9.81*.5**2))
        resting_error=abs(heights[2]-.5)
        require(heights[0]>heights[1] and freefall_error<=.02 and resting_error<=.03,
                'PHYSICS_DEMO_NUMERICS','Actual demonstration failed its independent gravity/resting-height tolerances.',category='numerical-instability')
        report=dict(status='executed-and-verified',fixture=True,review_status='unreviewed',backend=result.backend,
            source_files=len(payloads),world_sha256=result.world_sha256,
            native_source='Scene3D contract/hash closure plus genuine decoded OBJ meshes',
            simulation_usage=result.usage,cache=verify_cache(stage/'cache'),cache_manifest_sha256=cache_report['cache_sha256'],
            animation={**export_report,'destination':'animation'},
            numerical=dict(frames=[0,12,48],actual_heights_m=heights,continuous_freefall_error_m=freefall_error,
                           freefall_tolerance_m=.02,resting_height_error_m=resting_error,resting_tolerance_m=.03),
            native_restart='unsupported; clean restart only',visual_review='not-performed')
        if (prepared.root/'native-consumption.json').is_file():
            report['native_consumption_control']=parse((prepared.root/'native-consumption.json').read_bytes())
        _write(stage/'acceptance.json',canonical(report))
        _publish(stage,destination)
        return dict(status='executed-and-verified',destination=str(destination),world_sha256=result.world_sha256,
                    fixture=True,review_status='unreviewed',sample_count=world['frame_range']['end'])
    finally:
        if prepared is not None:backend.cleanup(prepared)
        if stage.exists():shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    probe = commands.add_parser('probe', help='Discover backend; --smoke actually executes/bakes/reopens physics')
    probe.add_argument('--blender')
    probe.add_argument('--smoke', action='store_true')
    probe.add_argument('--backend', choices=['blender','pybullet'], default='blender')
    probe.add_argument('--python', help='isolated optional PyBullet worker interpreter')
    demo=commands.add_parser('demo',help='Generate genuine native meshes and execute/bake/export a small real simulation')
    demo.add_argument('destination',type=Path)
    demo.add_argument('--backend',choices=['blender','pybullet'],default='blender')
    demo.add_argument('--blender')
    demo.add_argument('--python')
    verify = commands.add_parser('verify')
    verify.add_argument('cache', type=Path)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('cache', type=Path)
    inspect.add_argument('--frame', type=int, required=True)
    seek=commands.add_parser('seek',help='Interpolate verified completed samples at exact rational seconds')
    seek.add_argument('cache',type=Path)
    seek.add_argument('--time',type=Fraction,required=True)
    export = commands.add_parser('export')
    export.add_argument('cache', type=Path)
    export.add_argument('destination', type=Path)
    args = parser.parse_args()
    if args.command == 'probe':
        backend=BlenderBackend(args.blender) if args.backend=='blender' else PyBulletBackend(args.python)
        result = backend.probe(smoke=args.smoke).as_dict()
        print(json.dumps(result,indent=2,allow_nan=False))
        if args.smoke and result['state']!='available':
            raise SystemExit(2)
        return
    elif args.command=='demo':
        backend=BlenderBackend(args.blender) if args.backend=='blender' else PyBulletBackend(args.python)
        result=demonstrate(args.destination,backend)
    elif args.command == 'verify':
        result = verify_cache(args.cache)
    elif args.command == 'seek':
        result=inspect_time(args.cache,args.time)
    elif args.command == 'inspect':
        result = inspect_cache(args.cache, args.frame)
    else:
        result = export_animation(args.cache, args.destination)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (BackendError, RigError, OSError, ValueError) as exc:
        print(json.dumps(exc.report if hasattr(exc, 'report') else dict(status='failed', message=str(exc))), file=sys.stderr)
        raise SystemExit(2)
