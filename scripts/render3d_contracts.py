"""Additive Render3D contracts and exact clocks; no renderer is imported here.

Documents describe requests and evidence. Schema validity never proves execution,
image correctness, hardware availability, or production visual approval.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Protocol
import math
import unicodedata

from rig_contracts import COORDINATES, arr, obj, ref
from rig_errors import RigError, decode_json, finite_json, object_hash, portable_name

VERSION = '1.0.0'
MAX_FILES = 4096
MAX_FILE_BYTES = 256 * 1024**2
MAX_TOTAL_BYTES = 1024**3
ERRORS = ('invalid-input', 'missing-asset', 'stale-asset', 'unsupported-feature',
          'unsupported-backend', 'unsupported-device', 'device-initialization',
          'gpu-out-of-memory', 'system-memory', 'storage-exhaustion', 'timeout',
          'worker-crash', 'cancelled', 'corrupt-output', 'incomplete-output',
          'stale-cache', 'verification-failed', 'external-tool', 'ownership')


class Render3DError(ValueError):
    def __init__(self, category, message, *, path='', remediation='Correct the indicated input and retry.'):
        self.report = dict(code='RENDER3D_' + category.upper().replace('-', '_'),
                           category=category, message=str(message)[:2048], path=str(path)[:1024],
                           remediation=remediation)
        super().__init__(self.report['message'])


def require(condition, category, message, **details):
    if not condition:
        raise Render3DError(category, message, **details)


@lru_cache(maxsize=1)
def definitions():
    end = r'(?![\s\S])'
    identifier = dict(type='string', pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}' + end)
    digest = dict(type='string', pattern=r'^[0-9a-f]{64}' + end)
    version = dict(type='string', pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)' + end)
    text = dict(type='string', minLength=1, maxLength=2048)
    path = dict(type='string', minLength=1, maxLength=1024)
    count = dict(type='integer', minimum=0, maximum=2**31-1)
    positive = dict(type='integer', minimum=1, maximum=2**31-1)
    seconds = dict(type='number', minimum=0, maximum=1e12)
    nullable = lambda value: {'anyOf': [value, {'type': 'null'}]}
    rate = obj(dict(numerator=positive, denominator=positive))
    time = obj(dict(numerator=count, denominator=positive))
    file = obj(dict(path=path, sha256=digest, bytes=dict(type='integer', minimum=0, maximum=MAX_FILE_BYTES)))
    identity = obj(dict(id=identifier, version=version))
    bounds = obj(dict(start=count, end=positive))
    passes = arr({'enum': ['combined', 'depth', 'normal', 'albedo']}, 1, 4)
    passes['uniqueItems'] = True
    output = obj(dict(format={'enum': ['png', 'exr']}, bit_depth={'enum': [8, 16, 32]},
                      passes=passes, alpha={'enum': ['opaque', 'straight']},
                      color_space={'enum': ['srgb', 'linear']}))
    resources = obj(dict(memory_bytes=dict(type='integer', minimum=1, maximum=2**40),
                         storage_bytes=dict(type='integer', minimum=1, maximum=2**40),
                         timeout_seconds=dict(type='integer', minimum=1, maximum=86400),
                         workers=dict(type='integer', minimum=1, maximum=64),
                         max_frames=dict(type='integer', minimum=1, maximum=100000)))
    settings = obj(dict(engine={'enum': ['cycles', 'eevee']},
                        device_policy={'enum': ['cpu', 'gpu-required', 'gpu-preferred']},
                        device=nullable(identifier), allow_cpu_fallback={'type': 'boolean'},
                        resolution=arr(dict(type='integer', minimum=1, maximum=16384), 2, 2),
                        samples=dict(type='integer', minimum=1, maximum=65536),
                        seed=count, denoise={'type': 'boolean'}, output=output,
                        exposure=dict(type='number', minimum=-32, maximum=32),
                        view_transform={'enum': ['standard', 'agx']},
                        resources=resources))
    backend = obj(dict(id=identifier, adapter_version=version, application_version=text,
                       executable_sha256=digest, worker_sha256=digest, platform=text,
                       engine={'enum': ['cycles', 'eevee']}, device=identifier))
    diagnostic = obj(dict(code=text, category={'enum': list(ERRORS)}, message=text,
                          path=dict(type='string', maxLength=1024), remediation=text))
    verifier_identity = obj(dict(id=identifier, version=version, code_sha256=digest, platform=text))
    verification = obj(dict(status={'enum': ['not-run', 'passed', 'failed']},
                            verifier=nullable(verifier_identity), evidence=nullable(file)))
    image = obj(dict(frame=count, file=file, format={'enum': ['png', 'exr']},
                     bit_depth={'enum': [8, 16, 32]}, pass_name={'enum': ['combined', 'depth', 'normal', 'albedo']},
                     resolution=arr(positive, 2, 2)))
    doc = lambda kind, fields: obj(dict(schema_version={'const': 1}, kind={'const': 'render3d-' + kind}, **fields))
    snapshot = doc('snapshot', dict(adapter=identity, source=identity,
                     coordinate_system={'const': COORDINATES}, entrypoint=path,
                     files=arr(file, 1, MAX_FILES), total_bytes=dict(type='integer', minimum=1, maximum=MAX_TOTAL_BYTES),
                     fixture={'type': 'boolean'}, scope={'enum': ['native-rig-preview', 'scene3d']},
                     snapshot_sha256=digest))
    request = doc('request', dict(id=identifier, project=identity, scene=identity, shot=identity,
                   mode={'const': '3d'}, snapshot=file, fps=rate, frame_range=bounds,
                   source_start=time, playback_rate=rate, camera=identifier, settings=settings,
                   priority=dict(type='integer', minimum=0, maximum=100),
                   max_attempts=dict(type='integer', minimum=1, maximum=16),
                   cache_policy={'enum': ['verify-reuse', 'refresh']},
                   destination=obj(dict(path=path, owner_token=identifier))))
    frame = doc('frame', dict(index=count, local_index=count, presentation_time=time,
                 source_time=time, camera=identifier, snapshot_sha256=digest, settings_sha256=digest,
                 dependencies=arr(digest, 1, MAX_FILES), output=output, destination=path, cache_key=digest))
    counts = obj({name: count for name in ('requested', 'queued', 'active', 'completed', 'failed')})
    job = doc('job', dict(id=identifier, request_sha256=digest, snapshot_sha256=digest,
               backend=nullable(backend), destination_owner=identifier,
               submitted_at=text, updated_at=text, worker=nullable(identifier), heartbeat_at=nullable(text),
               attempt=dict(type='integer', minimum=0, maximum=16), counts=counts,
               state={'enum': ['queued', 'running', 'complete', 'partial', 'failed', 'cancelled', 'interrupted']},
               cancellation_requested={'type': 'boolean'}, manifest=nullable(file), error=nullable(diagnostic)))
    usage = obj(dict(elapsed_seconds=nullable(seconds), peak_memory_bytes=nullable(count),
                     output_bytes=nullable(count), measurement={'enum': ['measured', 'estimated', 'unavailable']}))
    result = doc('result', dict(job=identifier, request_sha256=digest, snapshot_sha256=digest,
                  backend=backend, status={'enum': ['complete', 'partial', 'failed', 'cancelled']},
                  requested_frames=arr(count, 1, 100000), successful=arr(count, 0, 100000),
                  failed=arr(count, 0, 100000), skipped=arr(count, 0, 100000), reused=arr(count, 0, 100000),
                  images=arr(image, 0, 400000), diagnostics=arr(diagnostic, 0, 1024),
                  usage=usage, logs=arr(file, 0, 1024), manifest=nullable(file), verification=verification))
    capabilities = doc('capabilities', dict(adapter=identity, backend=nullable(backend),
        discovery={'enum': ['available', 'unavailable', 'untested']},
        initialization={'enum': ['passed', 'failed', 'not-run']},
        execution={'enum': ['passed', 'failed', 'not-run']},
        evidence=arr(file, 0, 128), engines=arr({'enum': ['cycles', 'eevee']}, 0, 2),
        features=arr(identifier, 0, 128), diagnostics=arr(diagnostic, 0, 128)))
    return dict(FileRef=file, Identity=identity, Rate=rate, Time=time, FrameRange=bounds,
                RenderSettings=settings, BackendIdentity=backend, Diagnostic=diagnostic,
                VerifierIdentity=verifier_identity, Verification=verification, RenderSnapshot=snapshot, RenderRequest=request,
                FrameSpec=frame, RenderJob=job, RenderResult=result, BackendCapabilities=capabilities)


KINDS = dict(snapshot='RenderSnapshot', request='RenderRequest', frame='FrameSpec',
             job='RenderJob', result='RenderResult', capabilities='BackendCapabilities')


def _reachable(root):
    found, pending = set(), [root]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        for item in _walk(definitions()[name]):
            if '$ref' in item:
                pending.append(item['$ref'].removeprefix('#/$defs/'))
    return {name: deepcopy(definitions()[name]) for name in sorted(found)}


def schemas():
    return {f'render3d-{name}.schema.json': dict(
        **{'$schema': 'https://json-schema.org/draft/2020-12/schema', '$defs': _reachable(kind),
           '$ref': '#/$defs/' + kind}, title='Render3D ' + kind + ' v1') for name, kind in KINDS.items()}


@lru_cache(maxsize=16)
def _validator(kind):
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise Render3DError('unsupported-feature', 'Install jsonschema>=4 to validate Render3D contracts.') from exc
    return Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/' + kind})


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def validate(value, kind=None):
    """Validate intent/evidence shape and relationships, without trusting files."""
    try:
        finite_json(value)
        require(isinstance(value, dict), 'invalid-input', 'Render3D documents must be objects.')
        if kind is None:
            require(isinstance(value.get('kind'), str), 'invalid-input', 'Contract kind must be a string.')
            kind = KINDS.get(value['kind'].removeprefix('render3d-'))
        require(kind in definitions(), 'invalid-input', 'Unknown Render3D contract.')
        error = next(iter(_validator(kind).iter_errors(value)), None)
        require(error is None, 'invalid-input', error.message if error else '')
        for item in _walk(value):
            for key, child in item.items():
                key.encode('utf-8')
                if isinstance(child, str):
                    child.encode('utf-8')
            if 'path' in item and item.get('path'):
                portable_name(item['path'])
            for key in ('entrypoint', 'destination'):
                if isinstance(item.get(key), str):
                    portable_name(item[key])
            if set(item) == {'start', 'end'}:
                require(item['end'] > item['start'], 'invalid-input', 'Frame ranges are nonempty and half-open.')
            if set(item) == {'numerator', 'denominator'}:
                require(math.gcd(item['numerator'], item['denominator']) == 1,
                        'invalid-input', 'Rational numbers must be reduced.')
    except (RigError, UnicodeError) as exc:
        raise Render3DError('invalid-input', str(exc)) from exc
    if kind == 'RenderSettings':
        _settings(value)
    elif kind == 'RenderRequest':
        _settings(value['settings'])
        require(value['frame_range']['end'] - value['frame_range']['start'] <= value['settings']['resources']['max_frames'],
                'invalid-input', 'Frame request exceeds the configured frame budget.')
    elif kind == 'RenderSnapshot':
        paths = [f['path'] for f in value['files']]
        require(paths == sorted(paths) and len(set(paths)) == len(paths), 'invalid-input', 'Snapshot inventory must be sorted and unique.')
        aliases = {}
        for path in paths:
            for n in range(1, len(path.split('/')) + 1):
                part = '/'.join(path.split('/')[:n]); key = unicodedata.normalize('NFC', part).casefold()
                require(key not in aliases or aliases[key] == part, 'invalid-input', 'Snapshot paths alias across platforms.')
                aliases[key] = part
        require(value['entrypoint'] in paths, 'missing-asset', 'Snapshot entrypoint is missing.')
        require(sum(f['bytes'] for f in value['files']) == value['total_bytes'], 'invalid-input', 'Snapshot byte count disagrees.')
        require(snapshot_hash(value) == value['snapshot_sha256'], 'stale-asset', 'Snapshot identity differs from its contents.')
    elif kind == 'RenderJob':
        c = value['counts']
        require(c['requested'] > 0, 'invalid-input', 'Render jobs require at least one frame.')
        require(c['requested'] == sum(c[k] for k in ('queued', 'active', 'completed', 'failed')),
                'invalid-input', 'Job frame counts do not partition the request.')
        if value['state'] == 'complete':
            require(c['completed'] == c['requested'] and value['manifest'] is not None and value['backend'] is not None,
                    'incomplete-output', 'Complete jobs require all frames and an actual backend/manifest.')
    elif kind == 'RenderResult':
        requested = value['requested_frames']; categories = [value[k] for k in ('successful', 'failed', 'skipped', 'reused')]
        flat = sum(categories, [])
        require(len(set(requested)) == len(requested) and len(set(flat)) == len(flat) and set(flat) == set(requested),
                'invalid-input', 'Result frame categories must uniquely partition the request.')
        successful = set(value['successful']) | set(value['reused'])
        require(all(image['frame'] in successful for image in value['images']), 'invalid-input', 'Images must belong to successful/reused frames.')
        keys = [(i['frame'], i['pass_name']) for i in value['images']]
        require(len(set(keys)) == len(keys), 'invalid-input', 'Duplicate frame/pass image record.')
        if value['status'] == 'complete':
            require(not value['failed'] and not value['skipped'] and successful == set(requested)
                    and {i['frame'] for i in value['images'] if i['pass_name'] == 'combined'} == set(requested)
                    and value['manifest'] is not None and value['verification']['status'] == 'passed',
                    'incomplete-output', 'Complete results require independently verified combined images for every frame.')
        for image in value['images']:
            require(image['bit_depth'] in ((8, 16) if image['format'] == 'png' else (16, 32)),
                    'invalid-input', 'Image bit depth is incompatible with its format.')
        _verification(value['verification'])
    elif kind == 'FrameSpec':
        require(value['cache_key'] == frame_hash(value), 'stale-cache', 'Frame cache identity changed.')
    elif kind == 'BackendCapabilities':
        if value['initialization'] == 'passed':
            require(value['discovery'] == 'available' and value['backend'] is not None,
                    'invalid-input', 'Initialized capabilities require a discovered backend identity.')
        if value['execution'] == 'passed':
            require(value['initialization'] == 'passed' and value['discovery'] == 'available'
                    and value['backend'] is not None and value['evidence'], 'invalid-input', 'Execution claims require prior stages and evidence.')
    elif kind == 'Verification':
        _verification(value)
    return value


def _verification(value):
    if value['status'] != 'not-run':
        require(value['verifier'] is not None and value['evidence'] is not None,
                'verification-failed', 'A verification verdict requires verifier identity and evidence.')


def _settings(value):
    output = value['output']
    require(not (value['device_policy'] == 'gpu-required' and value['allow_cpu_fallback']),
            'invalid-input', 'GPU-required cannot permit CPU fallback.')
    require(value['device_policy'] != 'cpu' or value['device'] in (None, 'cpu'),
            'invalid-input', 'CPU policy cannot select a GPU.')
    if output['format'] == 'png':
        require(output['bit_depth'] in (8, 16) and output['color_space'] == 'srgb'
                and output['passes'] == ['combined'], 'unsupported-feature', 'PNG supports sRGB combined output at 8/16 bits.')
    else:
        require(output['bit_depth'] in (16, 32) and output['color_space'] == 'linear',
                'unsupported-feature', 'EXR supports linear output at 16/32 bits.')
    require(output['color_space'] != 'linear' or value['view_transform'] == 'standard',
            'unsupported-feature', 'Scene-linear masters cannot bake a display view transform.')


def load(path, kind=None):
    try:
        path = Path(path)
        require(path.stat().st_size <= 32 * 1024**2, 'invalid-input', 'JSON exceeds 32 MiB.')
        return validate(decode_json(path.read_bytes()), kind)
    except (OSError, RigError) as exc:
        raise Render3DError('invalid-input', str(exc), path=path) from exc


def snapshot_hash(value):
    return object_hash({k: v for k, v in value.items() if k != 'snapshot_sha256'})


def rational(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def frame_times(request, index):
    validate(request, 'RenderRequest')
    span = request['frame_range']
    require(type(index) is int and span['start'] <= index < span['end'], 'invalid-input', 'Frame index lies outside the half-open request.')
    fps = Fraction(**request['fps']); local = index - span['start']
    return (Fraction(index, 1) / fps,
            Fraction(**request['source_start']) + Fraction(local, 1) / fps * Fraction(**request['playback_rate']))


def frame_hash(value):
    return object_hash({k: v for k, v in value.items() if k not in ('cache_key', 'destination')})


def make_frame_spec(request, index, snapshot_path, *, adapter=None):
    """Plan from a request-bound exact snapshot file and verified source closure."""
    from rig_errors import sha256
    from render3d_snapshot import verify_snapshot, NativeRigAdapter
    validate(request, 'RenderRequest')
    snapshot_path = Path(snapshot_path)
    require(not snapshot_path.is_symlink() and snapshot_path.is_file(),
            'missing-asset', 'Expected a regular snapshot file.')
    reference = request['snapshot']
    require(snapshot_path.stat().st_size == reference['bytes']
            and sha256(snapshot_path) == reference['sha256'],
            'stale-asset', 'Snapshot file differs from the request-bound bytes.')
    snapshot = load(snapshot_path, 'RenderSnapshot')
    require(snapshot_path.name == 'snapshot.json', 'invalid-input', 'Use the frozen snapshot.json entrypoint.')
    if adapter is None:
        require(snapshot['scope'] == 'native-rig-preview', 'unsupported-feature',
                'Scene3D source planning requires its explicit owner adapter.')
        adapter = NativeRigAdapter()
    snapshot = verify_snapshot(snapshot_path.parent, adapter)
    presentation, source = frame_times(request, index)
    frame = dict(schema_version=1, kind='render3d-frame', index=index,
                 local_index=index - request['frame_range']['start'],
                 presentation_time=rational(presentation), source_time=rational(source),
                 camera=request['camera'], snapshot_sha256=snapshot['snapshot_sha256'],
                 settings_sha256=object_hash(request['settings']),
                 dependencies=[item['sha256'] for item in snapshot['files']],
                 output=deepcopy(request['settings']['output']),
                 destination=request['destination']['path'])
    frame['cache_key'] = frame_hash(frame)
    return validate(frame, 'FrameSpec')


def blender_frame(time, fps):
    """Canonical zero seconds maps to Blender frame one; retain exact subframe."""
    require(isinstance(time, Fraction) and time >= 0 and isinstance(fps, Fraction) and fps > 0,
            'invalid-input', 'Use nonnegative exact time and a positive rational rate.')
    position = 1 + time * fps
    integer = position.numerator // position.denominator
    return integer, position - integer


def canonical_to_blender(matrix):
    """Reuse the native rig coordinate authority; loaded only when needed."""
    import numpy as np
    from rig_math import BLENDER_FROM_CANONICAL
    try:
        matrix = np.asarray(matrix, dtype=float)
    except (TypeError, ValueError) as exc:
        raise Render3DError('invalid-input', 'Expected a numeric 4x4 matrix.') from exc
    require(matrix.shape == (4, 4) and np.isfinite(matrix).all()
            and np.allclose(matrix[3], [0, 0, 0, 1]), 'invalid-input', 'Expected a finite affine 4x4 matrix.')
    return BLENDER_FROM_CANONICAL @ matrix @ BLENDER_FROM_CANONICAL.T


class RenderBackend(Protocol):
    """Implementations validate every message; none may self-approve pixels."""
    def capabilities(self) -> dict: ...
    def probe(self, configuration: dict) -> dict: ...
    def validate(self, snapshot: dict, settings: dict) -> dict: ...
    def prepare(self, snapshot: dict, settings: dict) -> dict: ...
    def render_frames(self, request: dict) -> dict: ...
    def cancel(self, worker_id: str) -> None: ...
    def inspect_version(self) -> dict: ...


class ArtifactVerifier(Protocol):
    """Must decode files and bind its report to snapshot/backend/settings hashes."""
    def verify(self, request: dict, result: dict, root: Path) -> dict: ...
