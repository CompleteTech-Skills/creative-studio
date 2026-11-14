"""Optional source-bound analysis jobs; proposals never constitute semantic approval.

External responses are attributed assertions, not authenticated provider execution.
The local OpenCV adapter executes fixed face/eye cascades without network access.
"""
from __future__ import annotations

import argparse
import copy
import json
import hashlib
import io
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from asset_contracts import hash_object, MAX_SOURCE_BYTES
from character_decomposition import (_annotation, load_decomposition, MAX_JSON_BYTES,
                                     rebuild_from_annotation)
from decomposition_spec import (ANNOTATION_SCHEMA, HASH, TEXT, NOTE, BOOLEAN,
    array, obj, validate_structure, safe_path)
from extract_strip_frames import atomic_json

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4_000_000
MAX_JOBS = 64
MODEL_FILES = ('haarcascade_frontalface_default.xml', 'haarcascade_eye.xml')
MODEL = obj({'provider': TEXT, 'name': TEXT, 'version': TEXT,
             'files': array(obj({'name': TEXT, 'sha256': HASH}), 32),
             'identity_evidence': TEXT})
PARAM_VALUE = {'anyOf': [NOTE, {'type':'number'}, BOOLEAN, {'type':'null'}, array(NOTE,32)]}
PARAMETERS = {'type':'object', 'maxProperties':32, 'additionalProperties':PARAM_VALUE}
LOCAL_PARAMETERS = obj({'scale_factor': {'type':'number','minimum':1.01,'maximum':2},
                        'min_neighbors': {'type':'integer','minimum':1,'maximum':20}})
REQUEST = obj({'base_manifest_sha256': HASH, 'schema_version': {'const': 1}, 'kind': {'const': 'decomposition-analysis-request'},
    'source': obj({'path': TEXT, 'sha256': HASH, 'width': {'type': 'integer', 'minimum': 1},
                   'height': {'type': 'integer', 'minimum': 1}}),
    'references': array(obj({'role': {'enum': ['foreground', 'reference']}, 'path': TEXT, 'sha256': HASH}), 16),
    'taxonomy': array(TEXT, 128, 1), 'backend': {'enum': ['opencv-haar', 'clipseg', 'external']},
    'model': {'anyOf': [MODEL, {'type': 'null'}]}, 'prompt': NOTE,
    'parameters': PARAMETERS,
    'policy': obj({'enabled': {'const': True}, 'network_allowed': {'const': False},
                   'max_cost': {'const': 0}, 'max_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 120},
                   'max_pixels': {'type': 'integer', 'minimum': 1, 'maximum': MAX_PIXELS}})})
STATE = obj({'status': {'enum': ['ready','awaiting-external','running','interrupted','failed','cancelled','proposed','unsupported','applied']},
    'attempts': {'type': 'integer', 'minimum': 0, 'maximum': 3}, 'request_sha256': HASH,
    'applied_revision': {'anyOf': [{'type': 'integer', 'minimum': 1}, {'type': 'null'}]},
    'error': {'anyOf': [TEXT, {'type': 'null'}]}, 'response_sha256': HASH},
    ['status','attempts','request_sha256','applied_revision','error'])
RESPONSE = obj({'schema_version': {'const': 1}, 'kind': {'const': 'decomposition-analysis-response'},
    'request_sha256': HASH, 'source_sha256': HASH,
    'status': {'enum': ['proposed', 'unsupported', 'failed']}, 'model': MODEL,
    'annotation': ANNOTATION_SCHEMA,
    'evidence': obj({'execution': {'enum': ['local', 'external-assertion']},
        'call_id': {'anyOf': [TEXT, {'type': 'null'}]}, 'unavailable_reason': NOTE,
        'elapsed_seconds': {'anyOf': [{'type': 'number', 'minimum': 0}, {'type': 'null'}]},
        'cost': {'anyOf': [{'const': 0}, {'type': 'null'}]}, 'note': TEXT}),
    'errors': array(TEXT, 128)})


def _windows_record_descriptor(path):
    """Open a disk file's own handle, rejecting Windows reparse points/devices."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class FileInformation(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME),
            ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
            ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
            ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
            ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    kind = kernel.GetFileType
    kind.argtypes, kind.restype = [wintypes.HANDLE], wintypes.DWORD
    information = kernel.GetFileInformationByHandle
    information.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    information.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT.
    # Excluding write/delete sharing keeps the admitted handle stable while read.
    handle = create(os.fspath(path), 0x80000000, 0x1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    transferred = False
    try:
        if kind(handle) != 1:  # FILE_TYPE_DISK; no pipe/device handle is read.
            raise ValueError('analysis record must be a regular disk file: ' + str(path))
        details = FileInformation()
        if not information(handle, ctypes.byref(details)):
            raise ctypes.WinError(ctypes.get_last_error())
        if details.attributes & (0x400 | 0x10):  # REPARSE_POINT or DIRECTORY.
            raise ValueError('analysis record must be a regular file without reparse points: ' + str(path))
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        transferred = True  # The CRT descriptor owns/ultimately closes the handle.
        return descriptor
    finally:
        if not transferred:
            close(handle)


_WINDOWS_RECORD_IO = sys.platform == 'win32'


def _record_bytes(path, maximum=MAX_RESPONSE_BYTES):
    """Read admitted regular bytes; analysis JSON retains its default8MiB quota."""
    if type(maximum) is not int or not 1 <= maximum <= 200_000_000:
        raise ValueError('invalid regular-file byte bound')
    try:
        if _WINDOWS_RECORD_IO:
            descriptor = _windows_record_descriptor(path)
        else:
            if not all(hasattr(os, flag) for flag in ('O_NOFOLLOW', 'O_NONBLOCK')):
                raise ValueError('safe analysis record reads require no-follow/nonblocking platform support')
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError('analysis record cannot be opened safely: ' + str(path)) from exc
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError('analysis record must be a bounded regular file: ' + str(path))
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('analysis record exceeds byte quota: ' + str(path))
        return data


def _record_sha(path, maximum=MAX_RESPONSE_BYTES):
    return hashlib.sha256(_record_bytes(path, maximum)).hexdigest()


def _record_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    try:
        return json.loads(_record_bytes(path).decode('utf-8'), object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON number')))
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid analysis JSON: ' + str(exc)) from exc


def schemas():
    return {f'decomposition-analysis-{name}.schema.json': {
        '$schema': 'https://json-schema.org/draft/2020-12/schema', **copy.deepcopy(value)}
        for name, value in [('request', REQUEST), ('response', RESPONSE), ('state', STATE)]}


def _capture_model():
    """Capture the exact bounded XML bytes used to identify and load a model."""
    try:
        import cv2
    except ImportError as exc:
        raise ValueError('OpenCV analysis is unavailable; install optional requirements-analysis.txt') from exc
    if (not callable(getattr(cv2, 'FileStorage', None)) or
            not callable(getattr(cv2, 'CascadeClassifier', None)) or
            not callable(getattr(cv2.CascadeClassifier, 'read', None)) or
            not all(type(getattr(cv2, name, None)) is int for name in
                    ('FILE_STORAGE_READ', 'FILE_STORAGE_MEMORY'))):
        raise ValueError('OpenCV analysis requires in-memory cascade loading support')
    from rig_errors import open_payload
    root = Path(cv2.data.haarcascades).resolve()
    captured = {}
    for name in MODEL_FILES:
        with open_payload(root, name) as (stream, info):
            if info.st_size > MAX_RESPONSE_BYTES:
                raise ValueError('OpenCV cascade exceeds the 8 MiB model byte quota')
            payload = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError('OpenCV cascade exceeds the 8 MiB model byte quota')
        captured[name] = payload
    model = {'provider': 'local-opencv', 'name': 'opencv-haar-face-eye', 'version': cv2.__version__,
             'files': [{'name': name, 'sha256': hashlib.sha256(captured[name]).hexdigest()}
                       for name in MODEL_FILES],
             'identity_evidence': 'Installed OpenCV version and exact cascade bytes; not a remote call.'}
    return model, captured


def _model():
    return _capture_model()[0]


def _classifier_from_xml(cv2, payload, name):
    """Load only captured bytes; never pass a model filename to OpenCV."""
    if type(payload) is not bytes or not payload or len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError('OpenCV cascade requires bounded immutable XML bytes')
    try:
        storage = cv2.FileStorage(payload.decode('utf-8'),
                                 cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
        try:
            if not storage.isOpened():
                raise ValueError('captured XML storage did not open')
            classifier = cv2.CascadeClassifier()
            if not classifier.read(storage.getFirstTopLevelNode()) or classifier.empty():
                raise ValueError('captured XML is not a supported cascade')
            return classifier
        finally:
            storage.release()
    except Exception as exc:
        raise ValueError('OpenCV cannot load captured cascade ' + name +
                         ' in memory (' + type(exc).__name__ + ')') from None


def _local_model(backend):
    if backend == 'clipseg':
        from decomposition_clipseg import identity
        return identity()
    return _model()


def capabilities():
    result = {'schema_version': 1, 'default': 'disabled', 'external': {
        'available': True, 'execution': 'verified structured-response ingest only; no automatic remote calls'},
        'opencv-haar': {'available': False,
        'limitations': 'Frontal photographic human face and eye regions only; coarse rectangles, not pixel segmentation, anatomy, identity or approval.'}}
    try:
        result['opencv-haar'].update(available=True, model=_model())
    except (ImportError, ValueError, AttributeError) as exc:
        result['opencv-haar']['unavailable_reason'] = str(exc)
    result['clipseg'] = {'available': False, 'accepted_for_automatic_decomposition': False,
        'quality_status': 'Experimental: initial real-image visibility evaluation failed; masks and labels require review.',
        'limitations': 'Text-conditioned part-mask proposals only; uncalibrated pixel scores and possible absent-part false positives require semantic review.'}
    try:
        result['clipseg'].update(available=True, model=_local_model('clipseg'))
    except (ImportError, ValueError, OSError) as exc:
        result['clipseg']['unavailable_reason'] = str(exc)
    return result


def _job(root, identifier):
    if not isinstance(identifier, str) or len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('analysis job must be its full lowercase SHA-256')
    path = safe_path(root, 'analysis-jobs/' + identifier)
    if path.exists():
        entries = list(path.iterdir())
        if len(entries) > 32:
            raise ValueError('analysis job file quota exceeded')
        if any(p.is_symlink() for p in entries):
            raise ValueError('analysis job contains an unsafe symbolic link')
        for name in ('request.json','state.json','response.json','worker-response.json','retry.json'):
            file = path / name
            if file.exists() and (not file.is_file() or file.stat().st_size > MAX_RESPONSE_BYTES):
                raise ValueError('analysis metadata file exceeds bounds')
    return path


def provenance(root, annotation, source_hash):
    """Verify the reserved lineage extension against retained immutable records.

    This is byte-bound local provenance, not cryptographic provider identity.
    Arbitrary imported extension data cannot itself assert a local model call.
    """
    record = annotation.get('extensions', {}).get('decomposition_analysis')
    if record is None:
        return None
    spec = obj({'job': HASH, 'response_sha256': HASH, 'model': MODEL,
                'evidence': RESPONSE['properties']['evidence'], 'semantic_approval': {'const': False}})
    validate_structure(record, spec, 'analysis lineage')
    job = _job(root, record['job'])
    request = _record_json(job / 'request.json')
    response = _record_json(job / 'response.json')
    state = _record_json(job / 'state.json')
    validate_structure(request, REQUEST, 'analysis lineage request')
    validate_structure(response, RESPONSE, 'analysis lineage response')
    validate_structure(state, STATE, 'analysis lineage state')
    digest = record['job']
    if (hash_object(request) != digest or response['request_sha256'] != digest or
            state['request_sha256'] != digest or request['source']['sha256'] != source_hash or
            response['source_sha256'] != source_hash or response['annotation']['source_sha256'] != source_hash or
            _record_sha(job / 'response.json') != record['response_sha256'] or
            state.get('response_sha256') != record['response_sha256'] or
            record['model'] != response['model'] or record['evidence'] != response['evidence'] or
            response['status'] != 'proposed' or state['status'] not in ('proposed', 'applied')):
        raise ValueError('analysis lineage disagrees with retained source-bound job')
    if request['backend'] != 'external':
        if record['evidence']['execution'] != 'local' or request['model'] != record['model']:
            raise ValueError('local analysis lineage disagrees with selected backend/model')
    elif record['evidence']['execution'] != 'external-assertion':
        raise ValueError('external analysis lineage cannot assert local execution')
    return record


def _source_bytes(root, reference):
    """Hash bounded bytes from the canonical admitted regular-file descriptor."""
    from rig_errors import open_payload
    # Decomposition callers historically accept ordinary OS root aliases.
    # Resolve that adapter boundary before using the stricter native reader.
    physical_root = Path(root).resolve()
    with open_payload(physical_root, reference['path']) as (stream, _):
        if os.fstat(stream.fileno()).st_size > MAX_SOURCE_BYTES:
            raise ValueError('analysis source/reference exceeds byte quota')
        payload = stream.read(MAX_SOURCE_BYTES + 1)
        if len(payload) > MAX_SOURCE_BYTES:
            raise ValueError('analysis source/reference exceeds byte quota')
    if hashlib.sha256(payload).hexdigest() != reference['sha256']:
        raise ValueError('analysis source/reference bytes changed')
    return payload


def _source_image(root, request, *, mode='RGB'):
    """Decode the exact admitted/hash-verified source bytes, without reopening."""
    from PIL import Image
    payload = _source_bytes(root, request['source'])
    with Image.open(io.BytesIO(payload)) as image:
        expected = (request['source']['width'], request['source']['height'])
        if image.size != expected or image.width * image.height > request['policy']['max_pixels']:
            raise ValueError('analysis source dimensions changed or exceed pixel quota')
        return image.convert(mode)


def _source(root, request):
    validate_structure(request, REQUEST, 'analysis request')
    if request['backend'] == 'opencv-haar':
        validate_structure(request['parameters'], LOCAL_PARAMETERS, 'OpenCV parameters')
    elif request['backend'] == 'clipseg':
        from decomposition_clipseg import validate_options
        validate_options(request)
    manifest = load_decomposition(root)
    source = request['source']
    if (manifest['source']['sha256'] != source['sha256'] or
            [manifest['canvas']['width'], manifest['canvas']['height']] != [source['width'], source['height']] or
            manifest['source']['path'] != source['path']):
        raise ValueError('stale analysis source')
    if source['width'] * source['height'] > request['policy']['max_pixels']:
        raise ValueError('analysis source exceeds pixel quota')
    for ref in [source, *request['references']]:
        _source_bytes(root, ref)
    return manifest


def prepare(root, backend, *, enabled=False, taxonomy=None, prompt='', max_seconds=None,
            scale_factor=1.1, min_neighbors=5, parameters=None, expected_model=None):
    root = Path(root)
    if not enabled:
        raise ValueError('analysis is disabled; explicit --enable-analysis is required')
    manifest = load_decomposition(root)
    if max_seconds is None:
        max_seconds = 120 if backend == 'clipseg' else 30
    model = _local_model(backend) if backend != 'external' else expected_model
    if backend == 'clipseg':
        from decomposition_clipseg import DEFAULT_PARAMETERS, DEFAULT_TAXONOMY
        taxonomy = DEFAULT_TAXONOMY if taxonomy is None else taxonomy
        parameters = dict(DEFAULT_PARAMETERS) if parameters is None else parameters
    request = {'base_manifest_sha256': _record_sha(root / 'decomposition.json', MAX_JSON_BYTES), 'schema_version': 1, 'kind': 'decomposition-analysis-request',
        'source': {key: manifest['source'][key] for key in ('path', 'sha256')},
        'references': [{'role': 'foreground', **manifest['foreground']['mask']}],
        'taxonomy': taxonomy or ['face/region', 'face/eye-region'], 'backend': backend,
        'model': model, 'prompt': prompt,
        'parameters': parameters if parameters is not None else ({'scale_factor': scale_factor, 'min_neighbors': min_neighbors} if backend == 'opencv-haar' else {}),
        'policy': {'enabled': True, 'network_allowed': False, 'max_cost': 0,
                   'max_seconds': max_seconds, 'max_pixels': MAX_PIXELS}}
    request['source'].update({key: manifest['canvas'][key] for key in ('width', 'height')})
    _source(root, request)
    if backend == 'opencv-haar' and (prompt or any(x not in ('face/region', 'face/eye-region') for x in request['taxonomy'])):
        raise ValueError('OpenCV supports only fixed face/eye taxonomy and no text prompt')
    # Applying this exact analysis changes the manifest revision. Repeating the
    # high-level analyze command must still reuse that accepted result instead
    # of spending another detector/provider call on its own output revision.
    annotation = _record_json(safe_path(root, manifest['annotation']['path']))
    previous_id = annotation.get('extensions', {}).get('decomposition_analysis', {}).get('job')
    if previous_id:
        previous_job = _job(root, previous_id)
        previous_request = _record_json(previous_job / 'request.json')
        comparable = lambda value: {key: item for key, item in value.items() if key != 'base_manifest_sha256'}
        if comparable(previous_request) == comparable(request):
            prior = status(root, previous_id)
            if prior['status'] == 'applied':
                return prior
    identifier = hash_object(request)
    job = _job(root, identifier)
    if job.exists():
        if _record_json(job / 'request.json') != request:
            raise ValueError('analysis request collision')
        return status(root, identifier)
    state = {'status': 'awaiting-external' if backend == 'external' else 'ready',
             'attempts': 0, 'request_sha256': identifier, 'applied_revision': None, 'error': None}
    return _publish_prepared_job(root, identifier, request, state)


def _publish_prepared_job(root, identifier, request, state):
    """Publish small job records through the canonical owned staging authority.

    Empty outer wrappers are intentionally retained by that authority. Keep them
    outside analysis-jobs so they do not consume the existing job quota, and cap
    their count separately, including wrappers left by interrupted attempts.
    """
    from contextlib import ExitStack
    from itertools import islice
    from atomic_publish import pinned_directory_fd, private_stage_fd, rename_no_replace_at, windows_private_stage
    root = Path(root).resolve()  # Preserve the decomposition OS-root alias adapter.
    payloads = {name: (json.dumps(value, indent=2, allow_nan=False) + '\n').encode('utf-8')
                for name, value in (('request.json', request), ('state.json', state))}
    if any(len(value) > MAX_RESPONSE_BYTES for value in payloads.values()):
        raise ValueError('analysis job records exceed byte quota')
    def full(directory, limit):
        # Read at most the admission limit, even in an already crowded directory.
        with os.scandir(directory) as entries:
            return sum(1 for _ in islice(entries, limit)) >= limit
    if os.name == 'nt':
        from atomic_windows import pinned_directory
        with pinned_directory(root) as parent:
            if _record_sha(parent.path / 'decomposition.json', MAX_JSON_BYTES) != request['base_manifest_sha256']:
                raise ValueError('decomposition changed before analysis job publication')
            with ExitStack() as stack:
                pins = []
                for name in ('analysis-jobs', '.analysis-staging'):
                    (parent.path / name).mkdir(exist_ok=True)
                    pins.append(stack.enter_context(parent.pin_child(name)))
                jobs, staging = pins
                if full(jobs.path, MAX_JOBS):
                    raise ValueError('analysis job quota exceeded')
                if full(staging.path, MAX_JOBS * 4):
                    raise ValueError('analysis staging quota exceeded')
                with windows_private_stage(staging.path, prefix='.analysis-') as stage:
                    # The managed stage and all ancestors deny namespace changes.
                    # No descendant directory pins remain when the held payload publishes.
                    for name, payload in payloads.items():
                        with (stage.path / name).open('xb') as stream:
                            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                    stage.publish_to(jobs.path / identifier)
                return status(root, identifier)
    with pinned_directory_fd(root) as parent, ExitStack() as stack:
        descriptor = os.open('decomposition.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
                raise ValueError('analysis manifest must be a bounded regular file')
            selected = stream.read(MAX_JSON_BYTES + 1)
            if len(selected) > MAX_JSON_BYTES or hashlib.sha256(selected).hexdigest() != request['base_manifest_sha256']:
                raise ValueError('decomposition changed before analysis job publication')
        children = []
        for name in ('analysis-jobs', '.analysis-staging'):
            try: os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError: pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, child)
            children.append(child)
        jobs, staging = children
        if full(jobs, MAX_JOBS):
            raise ValueError('analysis job quota exceeded')
        if full(staging, MAX_JOBS * 4):
            raise ValueError('analysis staging quota exceeded')
        def check_namespace():
            for current, held in ((root.stat(), os.fstat(parent)),
                                  (os.stat('analysis-jobs', dir_fd=parent, follow_symlinks=False), os.fstat(jobs)),
                                  (os.stat('.analysis-staging', dir_fd=parent, follow_symlinks=False), os.fstat(staging))):
                if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                    raise ValueError('analysis job publication directory changed')
        with private_stage_fd(staging, prefix='.analysis-') as stage:
            os.mkdir('payload', mode=0o700, dir_fd=stage)
            payload_fd = os.open('payload', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=stage)
            try:
                for name, payload in payloads.items():
                    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=payload_fd)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                os.fsync(payload_fd)
                check_namespace()
                rename_no_replace_at(stage, 'payload', jobs, identifier)
                os.fsync(jobs)
                check_namespace()
            finally:
                os.close(payload_fd)
        result = status(root, identifier)
        check_namespace()
        return result


def _recover_retry(root, identifier):
    """Roll forward an explicitly requested retry after any interrupted step."""
    job = _job(root, identifier)
    journal = job / 'retry.json'
    if not journal.exists():
        return
    transaction = _record_json(journal)
    names = ('worker.log', 'cancel', 'response.json', 'worker-response.json')
    validate_structure(transaction, obj({'previous': STATE, 'next': STATE,
        'files': {'type': 'object', 'additionalProperties': False,
                  'properties': {name: HASH for name in names}}}), 'analysis retry journal')
    previous, following = transaction['previous'], transaction['next']
    request = _record_json(job / 'request.json')
    validate_structure(request, REQUEST, 'analysis retry request')
    if hash_object(request) != identifier:
        raise ValueError('analysis retry request changed')
    expected = {key: previous[key] for key in ('attempts','request_sha256','applied_revision','error')}
    expected.update(status='ready' if request['backend'] != 'external' else 'awaiting-external', error=None)
    if (previous['request_sha256'] != identifier or following != expected or
            (previous['status'] not in ('failed', 'cancelled', 'interrupted') and 'cancel' not in transaction['files']) or previous['attempts'] >= 3):
        raise ValueError('invalid analysis retry transition')
    history = safe_path(root, 'analysis-jobs/' + identifier + '/attempt-' + str(previous['attempts']))
    if _record_json(safe_path(history, 'state.json')) != previous:
        raise ValueError('retry history state changed')
    current = _record_json(job / 'state.json')
    if current not in (previous, following):
        raise ValueError('retry live state changed')
    # The complete old attempt is durable before deleting any live artifact.
    for name, digest in transaction['files'].items():
        retained = safe_path(history, name)
        if not retained.is_file() or _record_sha(retained) != digest:
            raise ValueError('retry history artifact changed')
        live = job / name
        if live.exists():
            if _record_sha(live) != digest:
                raise ValueError('retry live artifact changed')
            live.unlink()
    atomic_json(job / 'state.json', following)
    journal.unlink()


def status(root, identifier):
    job = _job(root, identifier)
    _recover_retry(root, identifier)
    request = _record_json(job / 'request.json')
    if hash_object(request) != identifier:
        raise ValueError('analysis request changed')
    _source(root, request)
    state = _record_json(job / 'state.json')
    validate_structure(state, STATE, 'analysis state')
    if state['request_sha256'] != identifier:
        raise ValueError('analysis state/request mismatch')
    if 'response_sha256' in state and _record_sha(job / 'response.json') != state['response_sha256']:
        raise ValueError('analysis response changed')
    if (job / 'cancel').exists() and state['status'] not in ('running', 'applied'):
        state = {**state, 'status': 'cancelled'}
    return {'job': identifier, **state, 'request': request,
            'response_available': (job / 'response.json').is_file()}


def cancel(root, identifier):
    job = _job(root, identifier)
    # A separate immutable marker allows cancellation while the controller lock
    # is held by a worker. No state or decomposition artifact is overwritten.
    if not job.is_dir():
        raise ValueError('analysis job not found')
    marker = safe_path(root, 'analysis-jobs/' + identifier + '/cancel')
    try:
        with marker.open('xb') as stream:
            stream.write(b'cancel\n')
    except FileExistsError:
        if marker.is_symlink():
            raise ValueError('unsafe cancellation marker')
    return {'job': identifier, 'cancellation_requested': True}


def _validated_response(root, identifier, response):
    validate_structure(response, RESPONSE, 'analysis response')
    job = _job(root, identifier)
    request = _record_json(job / 'request.json')
    _source(root, request)
    if hash_object(request) != identifier or response['request_sha256'] != identifier or response['source_sha256'] != request['source']['sha256']:
        raise ValueError('response binds a different request/source')
    local = request['backend'] != 'external'
    if local and (response['model'] != request['model'] or response['evidence']['execution'] != 'local'):
        raise ValueError('local response model/execution mismatch')
    if request['model'] is not None and response['model'] != request['model']:
        raise ValueError('response does not match the selected model')
    if not local and response['evidence']['execution'] != 'external-assertion':
        raise ValueError('external execution is only an attributed assertion')
    if response['evidence']['call_id'] is None and not response['evidence']['unavailable_reason'].strip():
        raise ValueError('missing call identity requires an unavailable reason')
    if response['evidence']['cost'] not in (None, 0):
        raise ValueError('paid analysis is not enabled')
    annotation = response['annotation']
    if response['status'] != 'proposed' and annotation['components']:
        raise ValueError('unsuccessful response cannot contain proposals')
    if response['status'] == 'proposed' and not annotation['components']:
        raise ValueError('proposed response must contain components')
    for component in annotation['components']:
        if component['category'] not in request['taxonomy']:
            raise ValueError('component outside requested taxonomy')
        # No external mask file reads: provider-neutral portable primitives only.
        if 'path' in component['mask'] or 'colors' in component['mask']:
            raise ValueError('analysis responses require inline runs or polygons')
    if 'foreground_mask' in annotation:
        raise ValueError('analysis cannot replace foreground; use its independent owner')
    image = _source_image(root, request, mode='RGBA')
    normalized = _annotation(annotation, request['source']['sha256'], image, root)
    return normalized


def ingest(root, identifier, response_path):
    job = _job(root, identifier)
    status(root, identifier)
    path = Path(response_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESPONSE_BYTES:
        raise ValueError('analysis response must be a bounded regular JSON file')
    request = _record_json(job / 'request.json')
    if request['backend'] != 'external' and path.resolve() != (job / 'worker-response.json').resolve():
        raise ValueError('local responses must come from the fixed worker; use external backend for imports')
    response = _record_json(path)
    _validated_response(root, identifier, response)
    state = _record_json(job / 'state.json')
    if state['status'] in ('cancelled', 'applied') or (job / 'cancel').exists():
        raise ValueError('analysis job is cancelled or already applied')
    if (job / 'response.json').exists():
        if _record_json(job / 'response.json') != response:
            raise ValueError('analysis response is immutable')
    else:
        if request['backend'] == 'external':
            if state['attempts'] >= 3:
                raise ValueError('analysis retry quota exhausted')
            state['attempts'] += 1
        atomic_json(job / 'response.json', response)
    state.update(status=response['status'], response_sha256=_record_sha(job / 'response.json'))
    atomic_json(job / 'state.json', state)
    return status(root, identifier)


def run(root, identifier):
    job = _job(root, identifier)
    info = status(root, identifier)
    request = info['request']
    if request['backend'] == 'external':
        raise ValueError('external backend requires attributed response ingest; no remote dispatch')
    state = _record_json(job / 'state.json')
    if state['status'] in ('proposed', 'unsupported', 'applied'):
        _validated_response(root, identifier, _record_json(job / 'response.json'))
        return info
    if (job / 'cancel').exists():
        state.update(status='cancelled', error='Cancelled before dispatch')
        atomic_json(job / 'state.json', state)
        return status(root, identifier)
    if state['status'] in ('running', 'interrupted'):
        # A completed child may have survived a controller crash. Ingest its
        # exact result; otherwise never repeat an uncertain execution.
        if (job / 'worker-response.json').is_file():
            try:
                return ingest(root, identifier, job / 'worker-response.json')
            except ValueError:
                state.update(status='failed', error='Interrupted worker response is invalid; preserved for inspection')
                atomic_json(job / 'state.json', state)
                return status(root, identifier)
        state.update(status='interrupted', error='Execution outcome unknown; resume never redispatches')
        atomic_json(job / 'state.json', state)
        return status(root, identifier)
    if state['status'] != 'ready':
        return info
    if _local_model(request['backend']) != request['model']:
        raise ValueError('local model changed after request')
    if state['attempts'] >= 3:
        raise ValueError('analysis retry quota exhausted')
    state.update(status='running', attempts=state['attempts']+1)
    atomic_json(job / 'state.json', state)
    started = time.monotonic()
    log_path = safe_path(root, 'analysis-jobs/' + identifier + '/worker.log')
    with log_path.open('xb') as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_worker',
            '--root', str(Path(root).resolve()), '--job', identifier], stdin=subprocess.DEVNULL,
            stdout=log, stderr=log)
        try:
            while process.poll() is None:
                if (job / 'cancel').exists() or time.monotonic() - started > request['policy']['max_seconds'] or log_path.stat().st_size > 65536:
                    process.kill()
                    process.wait()
                    state.update(status='cancelled' if (job / 'cancel').exists() else 'failed',
                                 error='Cancelled or runtime/log quota exceeded')
                    atomic_json(job / 'state.json', state)
                    return status(root, identifier)
                time.sleep(.05)
            if process.returncode or log_path.stat().st_size > 65536 or time.monotonic() - started > request['policy']['max_seconds']:
                raise ValueError('local analyzer failed; see bounded worker.log')
            return ingest(root, identifier, job / 'worker-response.json')
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            state.update(status='cancelled' if (job / 'cancel').exists() else 'failed', error='Analyzer failed; no automatic retry')
            atomic_json(job / 'state.json', state)
            raise


def apply(root, identifier):
    job = _job(root, identifier)
    info = status(root, identifier)
    response = _record_json(job / 'response.json')
    normalized = _validated_response(root, identifier, response)
    if info['status'] == 'applied':
        return info
    if info['status'] != 'proposed' or (job / 'cancel').exists():
        raise ValueError('only uncancelled proposed analysis may be applied')
    evidence = {'job': identifier, 'response_sha256': _record_sha(job / 'response.json'),
                'model': response['model'], 'evidence': response['evidence'],
                'semantic_approval': False}
    normalized['extensions']['decomposition_analysis'] = evidence
    current = load_decomposition(root)
    # Recover a crash after the atomic decomposition replacement but before the
    # job state acknowledgement without creating another revision.
    current_annotation = _record_json(safe_path(root, current['annotation']['path']))
    if current_annotation.get('extensions', {}).get('decomposition_analysis') != evidence:
        if _record_sha(Path(root) / 'decomposition.json', MAX_JSON_BYTES) != info['request']['base_manifest_sha256']:
            raise ValueError('decomposition changed since analysis request; preserve reviewer edits')
        current = rebuild_from_annotation(root, normalized,
            reason='Automatic source-bound proposals; semantic review remains pending.',
            author=normalized['author'])
    state = _record_json(job / 'state.json')
    state.update(status='applied', applied_revision=current['revision'])
    atomic_json(job / 'state.json', state)
    return status(root, identifier)


def retry(root, identifier):
    job = _job(root, identifier)
    info = status(root, identifier)
    if info['status'] not in ('failed', 'cancelled', 'interrupted') or info['attempts'] >= 3:
        raise ValueError('retry requires a failed/interrupted/cancelled job below attempt quota')
    if (job / 'worker-response.json').exists():
        try:
            _validated_response(root, identifier, _record_json(job / 'worker-response.json'))
        except ValueError:
            pass  # Preserve invalid output in the explicit retry's attempt history.
        else:
            if info['status'] == 'interrupted':
                raise ValueError('completed worker response must be recovered before retry')
    history = safe_path(root, 'analysis-jobs/' + identifier + '/attempt-' + str(info['attempts']))
    history.mkdir(exist_ok=True)
    previous = _record_json(job / 'state.json')
    files = {}
    for name in ('state.json','worker.log','cancel','response.json','worker-response.json'):
        path = safe_path(root, 'analysis-jobs/' + identifier + '/' + name)
        target = safe_path(root, 'analysis-jobs/' + identifier + '/' + history.name + '/' + name)
        if path.exists():
            if target.exists() and _record_sha(path) != _record_sha(target):
                raise ValueError('retry history conflict')
            if not target.exists():
                descriptor, temp_name = tempfile.mkstemp(prefix='.record-', dir=history)
                temporary = Path(temp_name)
                try:
                    with open(descriptor, 'wb', closefd=True) as output:
                        output.write(_record_bytes(path))
                        output.flush()
                        os.fsync(output.fileno())
                    from atomic_publish import _rename_no_replace
                    _rename_no_replace(temporary, target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            if name != 'state.json':
                files[name] = _record_sha(path)
    state = {key: info[key] for key in ('attempts','request_sha256','applied_revision','error')}
    state.update(status='ready' if info['request']['backend'] != 'external' else 'awaiting-external', error=None)
    atomic_json(job / 'retry.json', {'previous': previous, 'next': state, 'files': files})
    _recover_retry(root, identifier)
    return status(root, identifier)


def _worker(root, identifier):
    request = _record_json(_job(root, identifier) / 'request.json')
    if request['backend'] == 'clipseg':
        return _clipseg_worker(root, identifier, request)
    import cv2
    import numpy as np
    from PIL import Image
    root = Path(root)
    job = _job(root, identifier)
    request = _record_json(job / 'request.json')
    _source(root, request)
    model, captured = _capture_model()
    if model != request['model']:
        raise ValueError('local cascade identity changed')
    started = time.monotonic()
    cv2.setNumThreads(1)
    rgb = np.asarray(_source_image(root, request))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    face, eyes = [_classifier_from_xml(cv2, captured[name], name) for name in MODEL_FILES]
    parts = []
    def add(identifier, name, category, box, parent=None):
        x,y,w,h = map(int, box)
        parts.append({'id': identifier, 'name': name, 'category': category, 'parent_id': parent,
            'mask': {'polygons': [[[x,y],[x+w-1,y],[x+w-1,y+h-1],[x,y+h-1]]]},
            'confidence': 0.5, 'notes': ['Uncalibrated detector confidence is unknown; 0.5 is a conservative proposal placeholder, not a probability.',
            'Coarse detector rectangle includes background/other features; requires mask correction and semantic review.']})
    params = request['parameters']
    boxes = sorted(face.detectMultiScale(gray, scaleFactor=params['scale_factor'], minNeighbors=params['min_neighbors'], minSize=(24,24)), key=lambda r: tuple(map(int,r)))
    for i, (x,y,w,h) in enumerate(boxes[:32]):
        ident = f'face-region-{i+1}'
        if 'face/region' in request['taxonomy']:
            add(ident, f'Face region {i+1}', 'face/region', (x,y,w,h))
        eye_boxes = sorted(eyes.detectMultiScale(gray[y:y+h,x:x+w], scaleFactor=params['scale_factor'], minNeighbors=params['min_neighbors'], minSize=(8,8)), key=lambda r: tuple(map(int,r)))
        if 'face/eye-region' in request['taxonomy']:
            for j, (ex,ey,ew,eh) in enumerate(eye_boxes[:2]):
                add(f'eye-region-{i+1}-{j+1}', f'Eye region {i+1}.{j+1}', 'face/eye-region', (x+ex,y+ey,ew,eh), ident if 'face/region' in request['taxonomy'] else None)
    response = {'schema_version': 1, 'kind': 'decomposition-analysis-response',
        'request_sha256': identifier, 'source_sha256': request['source']['sha256'],
        'status': 'proposed' if parts else 'unsupported', 'model': model,
        'annotation': {'schema_version': 1, 'source_sha256': request['source']['sha256'],
            'author': 'Local OpenCV Haar detector', 'note': 'Actual local inference; coarse human face/eye regions only, not general character segmentation.', 'components': parts},
        'evidence': {'execution': 'local', 'call_id': None,
            'unavailable_reason': 'Local cascade inference does not issue provider call IDs.',
            'elapsed_seconds': time.monotonic()-started, 'cost': 0,
            'note': 'Executed fixed installed cascades on original source RGB; detector rectangles are proposals.'},
        'errors': [] if parts else ['No supported frontal human face detected. Manual decomposition remains available.']}
    _validated_response(root, identifier, response)
    atomic_json(job / 'worker-response.json', response)


def _clipseg_worker(root, identifier, request):
    from decomposition_clipseg import proposals
    root = Path(root)
    _source(root, request)
    started = time.monotonic()
    parts, diagnostics, model, runtime = proposals(root, request)
    response = {'schema_version': 1, 'kind': 'decomposition-analysis-response',
        'request_sha256': identifier, 'source_sha256': request['source']['sha256'],
        'status': 'proposed' if parts else 'unsupported', 'model': model,
        'annotation': {'schema_version': 1, 'source_sha256': request['source']['sha256'],
            'author': 'Local pinned CLIPSeg model',
            'note': 'Actual text-conditioned membership proposals; semantic correctness, anatomy presence and side remain uncertain.',
            'orientation': 'unknown', 'components': parts,
            'extensions': {'clipseg_diagnostics': diagnostics, 'clipseg_runtime': runtime}},
        'evidence': {'execution': 'local', 'call_id': None,
            'unavailable_reason': 'Local inference does not issue provider call IDs.',
            'elapsed_seconds': time.monotonic() - started, 'cost': 0,
            'note': 'Executed pinned local safetensors and builtin Transformers classes. Original pixels unchanged; all labels require review.'},
        'errors': [] if parts else ['No category survived the configured model thresholds; this does not prove anatomy absent. Manual annotations remain available.']}
    _validated_response(root, identifier, response)
    atomic_json(_job(root, identifier) / 'worker-response.json', response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['capabilities','prepare','run','status','cancel','ingest','apply','retry','_worker'])
    parser.add_argument('--root', type=Path)
    parser.add_argument('--job')
    parser.add_argument('--backend', choices=['opencv-haar','clipseg','external'], default='opencv-haar')
    parser.add_argument('--enable-analysis', action='store_true')
    parser.add_argument('--response', type=Path)
    parser.add_argument('--taxonomy', action='append')
    args = parser.parse_args(argv)
    try:
        if args.command == 'capabilities': result = capabilities()
        elif args.root is None: raise ValueError('--root is required')
        elif args.command == 'cancel': result = cancel(args.root, args.job)
        elif args.command == '_worker': _worker(args.root, args.job); return
        else:
            from hatch import ownership
            with ownership(args.root):
                if args.command == 'prepare': result = prepare(args.root, args.backend, enabled=args.enable_analysis, taxonomy=args.taxonomy)
                elif args.command == 'ingest': result = ingest(args.root,args.job,args.response)
                else: result = globals()[args.command](args.root,args.job)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
