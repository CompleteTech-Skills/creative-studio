"""Final Director delivery over authoritative native jobs and silent exports.

This adapter binds the native job store's completed publications to the final
720-frame contract, then delegates encoding and independent decoding to
SilentVideo. Worker claims and execution stay with their authoritative owners.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
import unicodedata

from rig_errors import RigError, decode_json, object_hash, require, safe_path, portable_name

FRAME_COUNT = 720
FRAME_RATE = Fraction(24)
MINIMUM_RESOLUTION = (1280, 720)
PROFILES = ('h264-mp4', 'vp9-webm')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_BINDING_FIELDS = {'job', 'job_revision', 'request_sha256', 'backend_sha256',
                   'directory', 'snapshot_directory'}


def _need(condition, message, **details):
    require(condition, 'DIRECTOR_FINAL_DELIVERY', message,
            stage='director-final-delivery', **details)


def validate_final_requests(requests):
    """Validate final timing/format intent; this never proves rendered output."""
    from render3d_contracts import validate
    _need(isinstance(requests, list) and 5 <= len(requests) <= FRAME_COUNT,
          'The final sequence requires at least five complete shot requests.')
    expected_start = 0
    project = snapshot = resolution = output = engine = None
    shots, destinations = set(), set()
    for request in requests:
        validate(request, 'RenderRequest')
        span = request['frame_range']
        _need(span['start'] == expected_start and span['end'] <= FRAME_COUNT,
              'Final shot ranges must cover the global timeline without gaps or overlaps.')
        expected_start = span['end']
        _need(Fraction(**request['fps']) == FRAME_RATE,
              'Final production must use exactly 24 frames per second.')
        size = request['settings']['resolution']
        _need(size[0] >= MINIMUM_RESOLUTION[0] and size[1] >= MINIMUM_RESOLUTION[1]
              and all(value % 2 == 0 for value in size),
              'Final masters require even dimensions of at least 1280 by 720.')
        selected_output = request['settings']['output']
        _need(selected_output == dict(format='png', bit_depth=8, passes=['combined'],
                                     alpha='opaque', color_space='srgb'),
              'This delivery profile requires opaque 8-bit sRGB PNG masters.')
        project = project or request['project']
        snapshot = snapshot or request['snapshot']
        resolution = resolution or size
        output = output or selected_output
        engine = engine or request['settings']['engine']
        _need(request['project'] == project and request['snapshot'] == snapshot,
              'Every shot must use the same exact project and frozen source closure.')
        _need(size == resolution and selected_output == output,
              'Final shot dimensions and output format must remain consistent.')
        _need(request['settings']['engine'] == engine,
              'This final Blender-manifest adapter requires one explicitly selected engine.')
        identity = (request['shot']['id'], request['shot']['version'])
        _need(identity not in shots, 'A final shot identity/version may appear only once.')
        shots.add(identity)
        destination = unicodedata.normalize('NFC', request['destination']['path']).casefold()
        _need(destination not in destinations, 'Final shot destinations must be distinct.')
        destinations.add(destination)
    _need(expected_start == FRAME_COUNT, 'Final production requires exactly 720 frames.')
    return dict(frame_count=FRAME_COUNT, fps=dict(numerator=24, denominator=1),
                duration=dict(numerator=30, denominator=1), resolution=deepcopy(resolution),
                project=deepcopy(project), snapshot=deepcopy(snapshot),
                shot_count=len(requests), execution_verified=False)


def validate_production_selection(requests, report):
    """Match the narrative owner's explicit production subset, not proof shots.

    This is a selection check only. Production entry points obtain ``report``
    from the native narrative verifier; passing a dict here grants no authority.
    """
    plan = validate_final_requests(requests)
    _need(isinstance(report, dict) and type(report.get('schema_version')) is int and report.get('schema_version') == 1 and
          report.get('kind') == 'director-narrative-acceptance' and
          report.get('ready_for_final_render') is True and report.get('frames') == FRAME_COUNT,
          'The native narrative acceptance is incomplete or unsupported.')
    try:
        rate = Fraction(**report['fps'])
        size = report['resolution']
        selected = report['production_shots']
        valid_size = (isinstance(size, list) and len(size) == 2 and
                      all(type(value) is int and value > 0 for value in size))
        _need(rate == FRAME_RATE and valid_size and
              plan['resolution'][0] >= size[0] and plan['resolution'][1] >= size[1] and
              Fraction(*plan['resolution']) == Fraction(*size),
              'Final rendering changes the accepted narrative frame rate, resolution or framing aspect.')
        _need(isinstance(selected, list) and len(selected) == len(requests),
              'Select only the narrative owner\'s ordered production shots.')
        for request, shot in zip(requests, selected):
            _need(isinstance(shot, dict) and shot['start'] == request['frame_range']['start'] and
                  shot['end'] == request['frame_range']['end'] and
                  {key: shot['scene'][key] for key in ('id', 'version')} == request['scene'] and
                  {key: shot['shot'][key] for key in ('id', 'version')} == request['shot'],
                  'A render request differs from the explicit accepted production shot or timing.')
    except (KeyError, TypeError, ZeroDivisionError) as exc:
        raise RigError('DIRECTOR_FINAL_DELIVERY', 'Malformed narrative production selection.',
                       stage='director-final-delivery') from exc
    return plan


def verify_final_narrative(snapshot_root, narrative):
    """Delegate full source/proof validation without rerendering prior previews."""
    _need(isinstance(narrative, dict) and set(narrative) == {'evidence_root', 'proof_reference'},
          'Final rendering requires the exact native narrative acceptance reference.')
    try:
        from director_narrative import verify_narrative_acceptance
    except ImportError as exc:
        raise RigError('DIRECTOR_FINAL_NARRATIVE_DEPENDENCY',
                       'The authoritative native narrative acceptance verifier is unavailable.',
                       stage='director-final-delivery') from exc
    report = verify_narrative_acceptance(snapshot_root, narrative['evidence_root'],
                                         narrative['proof_reference'])
    _, reference = _read_json(snapshot_root, 'project.json')
    _need(isinstance(report, dict) and report.get('project_sha256') == reference['sha256'],
          'Narrative evidence is not bound to the actual frozen project bytes.')
    return report


def preflight_final_render(snapshot_root, requests, *, narrative):
    """Verify narrative/source selection before the scheduler starts 720 frames.

    Capability, budget, worker and resource authorization remain ExecutionService
    and JobStore decisions. This return value is source preflight evidence only.
    """
    from render3d_contracts import make_frame_spec
    snapshot_root = Path(snapshot_root)
    report = verify_final_narrative(snapshot_root, narrative)
    plan = validate_production_selection(requests, report)
    for request in requests:
        for index in (request['frame_range']['start'], request['frame_range']['end']-1):
            make_frame_spec(request, index, snapshot_root/'snapshot.json')
    return dict(schema_version=1, kind='director-final-source-preflight',
                source_validation='passed', plan=plan,
                narrative_acceptance=deepcopy(narrative['proof_reference']),
                execution_authorization='requires-authoritative-scheduler-admission')


def _read_json(root, relative, *, expected=None):
    """Read one bounded no-follow native JSON file and preserve its exact hash."""
    from render3d_contracts import open_regular
    maximum = 32*1024**2
    if expected is not None:
        _need(isinstance(expected, dict) and set(expected) == {'path', 'sha256', 'bytes'} and
              expected['path'] == relative and type(expected['bytes']) is int and
              0 < expected['bytes'] <= maximum and isinstance(expected['sha256'], str) and
              _HASH.fullmatch(expected['sha256']) is not None,
              'An exact bounded native evidence reference is required.')
        maximum = expected['bytes']
    path = safe_path(root, relative, exists=True)
    with open_regular(path, maximum) as stream:
        raw = stream.read(maximum+1)
    _need(len(raw) <= maximum, 'Native delivery document exceeds its byte bound.')
    reference = dict(path=relative, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    if expected is not None:
        _need(reference == expected, 'Native evidence bytes differ from their exact reference.')
    return decode_json(raw, max_bytes=32*1024**2), reference


def _directory(root, relative):
    # safe_path(exists=True) deliberately admits regular files only. Directory
    # bindings use its same no-follow traversal and a separate type check.
    path = safe_path(root, relative)
    _need(path.is_dir() and not path.is_symlink(), 'Native evidence directory is missing or unsafe.')
    return path


def _validate_binding(binding):
    _need(isinstance(binding, dict) and set(binding) == _BINDING_FIELDS,
          'Use an exact native job/revision/request/backend/materialization binding.')
    for name in ('job', 'request_sha256', 'backend_sha256'):
        _need(isinstance(binding[name], str) and _HASH.fullmatch(binding[name]) is not None,
              'Native job bindings require canonical SHA-256 values.')
    _need(type(binding['job_revision']) is int and binding['job_revision'] > 0,
          'A completed native job revision is required.')
    for name in ('directory', 'snapshot_directory'):
        _need(isinstance(binding[name], str) and 0 < len(binding[name]) <= 1024,
              'Native directory bindings must be bounded portable relative paths.')
        portable_name(binding[name])


def _binding_state(store, binding):
    _validate_binding(binding)
    state = store.status(binding['job'], event_limit=10000)
    _need(state['state'] == 'complete' and state['revision'] == binding['job_revision'],
          'The native job is incomplete or differs from its pinned revision.')
    _need(object_hash(state['request']) == binding['request_sha256'] and
          object_hash(state['backend']) == binding['backend_sha256'],
          'Native request/backend differs from the admitted job binding.')
    _need(object_hash(dict(request=state['request'], backend=state['backend'])) == binding['job'],
          'Native render job identity differs from its authoritative inputs.')
    span = state['request']['frame_range']
    frames = state['frames']
    _need([entry['frame'] for entry in frames] == list(range(span['start'], span['end']))
          and all(entry['state'] == 'complete' for entry in frames),
          'The native job has incomplete, duplicated or missing frame states.')
    _need(all(type(entry['attempts']) is int and entry['attempts'] >= 1 for entry in frames),
          'Every completed frame must retain its native execution/reuse attempts.')
    return state


def _live_native_guard(store, bindings):
    """Read small native rows atomically without polling full frame/event history.

    JobStore remains the sole owner of these rows. This is the same borrowed
    read-transaction boundary used by the existing Director graph adapter.
    """
    with store._transaction() as db:
        for binding in bindings:
            row = db.execute('SELECT revision,state,request,backend FROM jobs WHERE id=?',
                             (binding['job'],)).fetchone()
            _need(row is not None and row['state'] == 'complete' and
                  row['revision'] == binding['job_revision'],
                  'The native job changed during final media processing.')
            _need(object_hash(decode_json(row['request'])) == binding['request_sha256'] and
                  object_hash(decode_json(row['backend'])) == binding['backend_sha256'],
                  'The native render inputs changed during final media processing.')


def _successful_usage(root, result, request, snapshot_root, backend):
    """Measure only retained successful workers; failed-attempt usage is unknown."""
    from render3d_contracts import make_frame_spec, validate, frame_times, rational
    span = request['frame_range']
    expected = {f'provenance/{index:08d}.json': index
                for index in range(span['start'], span['end'])}
    logs = result['logs']
    _need(len(logs) == len(expected) and {ref['path'] for ref in logs} == set(expected),
          'Every final frame requires its exact native cache provenance.')
    elapsed, peaks, unknown_elapsed, unknown_peak = [], [], 0, 0
    for ref in logs:
        record, _ = _read_json(root, ref['path'], expected=ref)
        index = expected[ref['path']]
        spec = make_frame_spec(request, index, snapshot_root/'snapshot.json')
        _need(isinstance(record, dict) and record.get('version') == 1 and
              record.get('backend') == backend,
              'Unsupported or substituted native frame provenance.')
        observed_spec = record.get('spec', {})
        _need({k: v for k, v in observed_spec.items() if k != 'destination'} ==
              {k: v for k, v in spec.items() if k != 'destination'} and
              record.get('cache_key') == object_hash(dict(frame=spec['cache_key'], backend=backend)),
              'Cached worker provenance differs from the exact global frame plan.')
        original = validate(record.get('request'), 'RenderRequest')
        current = deepcopy(request)
        current['frame_range'] = dict(start=index, end=index+1)
        current['source_start'] = rational(frame_times(request, index)[1])
        fields = ('project', 'scene', 'shot', 'snapshot', 'fps', 'source_start',
                  'playback_rate', 'camera', 'settings', 'frame_range')
        _need(all(original[name] == current[name] for name in fields),
              'Cached worker request differs from current native frame inputs.')
        worker = validate(record.get('result'), 'RenderResult')
        _need(worker['request_sha256'] == object_hash(original) and
              worker['backend'] == backend and worker['status'] == 'complete' and
              worker['snapshot_sha256'] == spec['snapshot_sha256'] and
              worker['successful'] == [index] and
              [image['file']['sha256'] for image in worker['images']] ==
              [image['file']['sha256'] for image in result['images'] if image['frame'] == index],
              'Successful worker provenance does not bind the delivered pixels.')
        usage = worker['usage']
        seconds, peak = usage['elapsed_seconds'], usage['peak_memory_bytes']
        if seconds is None:
            unknown_elapsed += 1
        else:
            elapsed.append(seconds)
        if peak is None:
            unknown_peak += 1
        else:
            peaks.append(peak)
    return dict(known_successful_worker_elapsed_seconds=math.fsum(elapsed) if elapsed else None,
                observed_successful_worker_peak_memory_bytes=max(peaks) if peaks else None,
                unknown_successful_worker_elapsed_count=unknown_elapsed,
                unknown_successful_worker_peak_memory_count=unknown_peak,
                failed_attempt_resource_usage=None,
                scope='Original successful cached workers; includes reused historical results, excludes unknown failed-attempt consumption.')


def verify_native_materialization(store, root, binding, *, required_scope=None):
    """Bind a native publication event to freshly decoded bytes at any frame range.

    This stateless Director adapter composes JobStore and RenderArtifactVerifier;
    it creates no execution or review authority. Other Director QA consumers may
    use it for real control frames without constructing a final 720-frame film.
    """
    from render3d_jobs import JobStore
    from render3d_contracts import validate
    from render3d_verify import RenderArtifactVerifier
    _need(isinstance(store, JobStore), 'Use the authoritative native JobStore.')
    _need(required_scope in (None, 'scene3d', 'native-rig-preview'), 'Unknown native snapshot scope.')
    state = _binding_state(store, binding)
    request, backend = state['request'], state['backend']
    directory = _directory(root, binding['directory'])
    snapshot_root = _directory(root, binding['snapshot_directory'])
    snapshot, _ = _read_json(snapshot_root, 'snapshot.json',
        expected={**request['snapshot'], 'path': 'snapshot.json'})
    validate(snapshot, 'RenderSnapshot')
    _need(required_scope is None or snapshot['scope'] == required_scope,
          'Native snapshot has the wrong production scope.')
    result, result_ref = _read_json(directory, 'result.json')
    validate(result, 'RenderResult')
    _need(result['job'] == binding['job'] and result['status'] == 'complete',
          'Delivered output is not the bound completed native job.')
    matching = []
    for event in state['events']:
        if event['event'] == 'delivery-published':
            detail = decode_json(event['detail'], max_bytes=32768)
            _need(isinstance(detail, dict), 'Native publication event detail is malformed.')
            if detail.get('result_sha256') == result_ref['sha256']:
                matching.append(event)
    _need(matching, 'Output bytes have no matching authoritative native publication event.')
    report = RenderArtifactVerifier(snapshot_root, backend).verify(request, result, directory)
    _binding_state(store, binding)
    return dict(binding=deepcopy(binding), state=state, request=request, backend=backend,
                result=result, result_reference=result_ref, verification=report,
                publication_event=matching[-1]['seq'], output_root=directory,
                snapshot_root=snapshot_root, snapshot_scope=snapshot['scope'])


def assemble_final_frames(store, root, bindings, *, narrative=None):
    """Reverify completed native publications and decode all 720 actual masters.

    Bindings come from the authoritative execution graph, not an output's claimed
    verified flag. A result file alone can never establish native completion.
    """
    from render3d_jobs import JobStore
    from silent_video import source_from_blender_manifest
    _need(isinstance(store, JobStore), 'Use the authoritative native JobStore.')
    _need(isinstance(narrative, dict) and set(narrative) == {'evidence_root', 'proof_reference'},
          'Final delivery requires exact native narrative acceptance evidence.')
    _need(isinstance(bindings, list) and 5 <= len(bindings) <= FRAME_COUNT,
          'Bind at least five native final jobs.')
    for binding in bindings:
        _validate_binding(binding)
    bindings = deepcopy(bindings)
    root = Path(root)
    states = [_binding_state(store, binding) for binding in bindings]
    plan = validate_final_requests([state['request'] for state in states])
    first_snapshot = _directory(root, bindings[0]['snapshot_directory'])
    narrative_report = verify_final_narrative(first_snapshot, narrative)
    validate_production_selection([state['request'] for state in states], narrative_report)
    _need(len({binding['job'] for binding in bindings}) == len(bindings),
          'One native job cannot implement multiple final shots.')
    directories = [unicodedata.normalize('NFC', binding['directory']).casefold() for binding in bindings]
    _need(len(set(directories)) == len(directories), 'Final native output directories alias each other.')
    for index, directory in enumerate(directories):
        path = PurePosixPath(directory)
        for other in directories[index+1:]:
            _need(not path.is_relative_to(other) and not PurePosixPath(other).is_relative_to(path),
                  'Final native output ownership must not overlap.')
        for binding in bindings:
            snapshot = PurePosixPath(unicodedata.normalize('NFC', binding['snapshot_directory']).casefold())
            _need(not snapshot.is_relative_to(path) and not path.is_relative_to(snapshot),
                  'Native outputs must remain separate from immutable snapshot directories.')
    frames, reports, native, usage = [], [], [], []
    for binding, state in zip(bindings, states):
        request, backend = state['request'], state['backend']
        _need(backend['id'] == 'blender', 'Final production requires the authoritative Blender backend.')
        evidence = verify_native_materialization(store, root, binding, required_scope='scene3d')
        directory, snapshot_root = evidence['output_root'], evidence['snapshot_root']
        result, result_ref = evidence['result'], evidence['result_reference']
        reports.append(evidence['verification'])
        usage.append(_successful_usage(directory, result, request, snapshot_root, backend))
        for image in sorted(result['images'], key=lambda item: item['frame']):
            _need(image['format'] == 'png' and image['pass_name'] == 'combined',
                  'Unexpected final master output.')
            ref = image['file']
            frames.append(dict(index=image['frame'], path=binding['directory']+'/'+ref['path'],
                               sha256=ref['sha256'], bytes=ref['bytes']))
        native.append(dict(job=binding['job'], revision=binding['job_revision'],
            request_sha256=binding['request_sha256'], backend=backend,
            result_sha256=result_ref['sha256'], result_reference=result_ref,
            publication_event=evidence['publication_event'],
            attempts=sum(frame['attempts'] for frame in state['frames']),
            retries=sum(max(0, frame['attempts']-1) for frame in state['frames']),
            recorded_cache_reuse_events=sum(event['event'] == 'cache-reused' for event in state['events']),
            rendered_range=deepcopy(request['frame_range']), camera=request['camera']))
    _need([entry['index'] for entry in frames] == list(range(FRAME_COUNT)),
          'Final master sequence must retain all global frame indices 0 through 719.')
    source = source_from_blender_manifest(root, dict(schema_version=1, kind='blender-frame-manifest',
        engine='CYCLES' if states[0]['backend']['engine'] == 'cycles' else 'BLENDER_EEVEE_NEXT',
        fps=dict(numerator=24, denominator=1), frames=frames))
    # Capture/decoding does not authorize a later changed native job revision.
    for binding in bindings:
        _binding_state(store, binding)
    return dict(schema_version=1, kind='director-final-render-admission', plan=plan,
                source=source, native_jobs=native, image_verification=reports,
                successful_worker_usage=usage, visual_review='not-performed',
                narrative_acceptance=deepcopy(narrative['proof_reference']),
                native_execution_verified=True, semantic_scene_approval=False)


def _recheck_delivery_sources(store, root, bindings, admission, narrative):
    """Recheck mutable namespaces after encoding, without rerendering frames."""
    from render3d_contracts import make_frame_spec
    for binding, expected in zip(bindings, admission['native_jobs']):
        state = _binding_state(store, binding)
        directory = _directory(root, binding['directory'])
        _read_json(directory, 'result.json', expected=expected['result_reference'])
        snapshot = _directory(root, binding['snapshot_directory'])
        make_frame_spec(state['request'], state['request']['frame_range']['start'], snapshot/'snapshot.json')
    snapshot = _directory(root, bindings[0]['snapshot_directory'])
    report = verify_final_narrative(snapshot, narrative)
    validate_production_selection([store.status(binding['job'], event_limit=0)['request']
                                   for binding in bindings], report)


def encode_final_media(store, root, bindings, destinations, *, narrative=None, retained_receipts=None,
                       receipt_root=None,
                       ffmpeg=None, ffprobe=None, timeout_seconds=3600, cancel=None):
    """Encode and independently decode MP4 and WebM; preserve verified masters.

    Existing output requires a trusted externally pinned receipt file reference
    (path/SHA256/bytes), read from receipt_root or root. Every reuse is reverified.
    A failed second format does not label the production complete.
    The caller's ExecutionService owns persistence, checkpointing and publication
    of this returned result; this function creates no controller or status store.
    """
    from silent_video import make_request, encode, validate_request, validate_receipt
    _need(isinstance(destinations, dict) and set(destinations) == set(PROFILES),
          'Both explicit MP4 and WebM destinations are required.')
    _need(type(timeout_seconds) is int and 1 <= timeout_seconds <= 3600,
          'Use a bounded encoding timeout between one and 3600 seconds.')
    _need(retained_receipts is None or isinstance(retained_receipts, dict)
          and set(retained_receipts) <= set(PROFILES), 'Unknown retained export profile.')
    bindings = deepcopy(bindings)
    admission = assemble_final_frames(store, root, bindings, narrative=narrative)
    paths = {profile: Path(path).absolute() for profile, path in destinations.items()}
    _need(len(set(paths.values())) == len(PROFILES), 'Final media destinations must differ.')
    protected = [_directory(root, binding[field]).resolve()
                 for binding in bindings for field in ('directory', 'snapshot_directory')]
    protected.append(store.root.resolve())
    _need(all(not path.resolve().is_relative_to(directory)
              for path in paths.values() for directory in protected),
          'Media publication must not write inside native jobs, snapshots or master directories.')
    receipts, requests, reused = {}, {}, []
    def guarded_cancel():
        cancelled = bool(cancel is not None and cancel())
        _live_native_guard(store, bindings)
        return cancelled
    for profile in PROFILES:
        _need(not guarded_cancel(), 'Final delivery was canceled.')
        request = make_request(admission['source'], profile)
        request['limits']['timeout_seconds'] = timeout_seconds
        validate_request(request)
        path = paths[profile]
        if path.exists() or path.is_symlink():
            old_ref = (retained_receipts or {}).get(profile)
            _need(isinstance(old_ref, dict) and isinstance(old_ref.get('path'), str),
                  'Existing media requires its trusted retained export receipt reference.')
            old, _ = _read_json(root if receipt_root is None else receipt_root,
                                old_ref['path'], expected=old_ref)
            verification = validate_receipt(old, path, request, root=root, ffmpeg=ffmpeg, ffprobe=ffprobe)
            receipt = deepcopy(old)
            reused.append(profile)
        else:
            receipt = encode(request, root, path, ffmpeg=ffmpeg, ffprobe=ffprobe, cancel=guarded_cancel)
            verification = validate_receipt(receipt, path, request, root=root, ffmpeg=ffmpeg, ffprobe=ffprobe)
        _need(verification['frame_count'] == FRAME_COUNT and
              Fraction(**verification['fps']) == FRAME_RATE and
              verification['video_stream_count'] == 1 and
              verification['audio_stream_count'] == verification['other_stream_count'] == 0,
              'The published media does not contain exactly 720 silent video frames.')
        _need(abs(Fraction(**verification['duration']) - 30) <=
              Fraction(**verification['timebase_tolerance']), 'Published media duration differs from 30 seconds.')
        _need(not guarded_cancel(), 'Final native state changed or delivery was canceled.')
        _recheck_delivery_sources(store, root, bindings, admission, narrative)
        receipts[profile], requests[profile] = receipt, request
    return dict(schema_version=1, kind='director-final-media', render_admission=admission,
                requests=requests, receipts=receipts, reused_profiles=reused,
                frame_count=FRAME_COUNT, audio_stream_count=0,
                visual_review='not-performed', semantic_scene_approval=False)
