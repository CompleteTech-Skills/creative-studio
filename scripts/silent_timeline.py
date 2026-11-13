"""Exact, source-bound cuts for the shared silent-media encoder.

This adapter copies already-rendered PNG bytes. It never renders geometry,
changes FPS, flattens alpha, assigns color transforms or supplies visual review.
"""
from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from atomic_publish import _rename_no_replace
from production_contracts import read_json, MAX_MANIFEST_BYTES
from production_render import fps_fraction
import silent_video as video

MAX_SHOTS = 1024
REFERENCE = video._obj({'id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                        'version': {'type': 'string', 'minLength': 1, 'maxLength': 128}})
CONTEXT = {'anyOf': [{'type': 'null'}, video._obj({name: REFERENCE for name in ('project', 'scene', 'shot')})]}


def schema():
    ratio = video._obj({'numerator': video._int(0, 10**12), 'denominator': video._int(1, 10**12)})
    identity = {'type': 'string', 'pattern': '[A-Za-z0-9][A-Za-z0-9_.-]{0,127}'}
    result = video._obj({'schema_version': {'const': 1}, 'kind': {'const': 'silent-timeline'},
        'source': video.SOURCE,
        'shots': {'type': 'array', 'minItems': 1, 'maxItems': MAX_SHOTS, 'items': video._obj({
            'id': identity, 'context': CONTEXT, 'source': video.SOURCE, 'source_sha256': video.SHA,
            'start': video._int(0, video.MAX_FRAMES - 1), 'end': video._int(1, video.MAX_FRAMES),
            'output_start': video._int(0, video.MAX_FRAMES - 1), 'output_end': video._int(1, video.MAX_FRAMES)})},
        'mapping': {'type': 'array', 'minItems': 1, 'maxItems': video.MAX_FRAMES, 'items': video._obj({
            'index': video._int(0, video.MAX_FRAMES - 1), 'shot_id': identity,
            'source_index': video._int(0, video.MAX_FRAMES - 1), 'source_sha256': video.SHA, 'pts': ratio})},
        'duration': ratio, 'audio_policy': {'const': 'forbidden'}, 'visual_review': {'const': 'not-performed'}})
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'title': 'Silent timeline v1', **copy.deepcopy(result)}


def _ratio(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError('shot identity must be a bounded portable identifier')
    return value


def _prepare(shots):
    if not isinstance(shots, list) or not 1 <= len(shots) <= MAX_SHOTS:
        raise ValueError('timeline requires bounded ordered shots')
    planned, identities, total, total_bytes, baseline = [], set(), 0, 0, None
    input_count, input_bytes = 0, 0
    for item in shots:
        if (not isinstance(item, dict) or not {'id', 'root', 'source', 'start', 'end'} <= set(item) or
                set(item) - {'id', 'root', 'source', 'start', 'end', 'context'}):
            raise ValueError('shot requires id, root, source and half-open start/end')
        context = copy.deepcopy(item.get('context'))
        video._structural(context, CONTEXT, 'shot context')
        identity = _identity(item['id'])
        if identity in identities:
            raise ValueError('shot identities must be unique')
        identities.add(identity)
        source = copy.deepcopy(video.validate_source(item['source']))
        input_count += len(source['frames'])
        input_bytes += sum(frame['bytes'] for frame in source['frames'])
        if input_count > video.MAX_FRAMES or input_bytes > video.MAX_SOURCE_BYTES:
            raise ValueError('aggregate input inventories exceed frame/byte budget')
        start, end = item['start'], item['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source['frames']):
            raise ValueError('invalid half-open source range')
        signature = (source['width'], source['height'], fps_fraction(source['fps']),
                     source['format'], source['precision'], source['alpha'], source['color'])
        if baseline is not None and baseline != signature:
            raise ValueError('timeline sources require matching FPS, dimensions, precision, alpha and color; convert explicitly first')
        baseline = signature
        total += end - start
        total_bytes += sum(frame['bytes'] for frame in source['frames'][start:end])
        if total > video.MAX_FRAMES or total_bytes > video.MAX_SOURCE_BYTES:
            raise ValueError('timeline exceeds bounded frame/byte budget')
        planned.append({**item, 'source': source, 'context': context})
    return planned, baseline, total_bytes


def assemble(shots, destination, *, cancel=None, destination_dir_fd=None):
    """Publish a complete immutable timeline directory, or nothing.

    Each shot supplies an admitted source and its read-only root. Every input
    inventory is revalidated, including frames outside its selected range.
    Output mapping is deterministic and exact for rational source FPS.
    """
    planned, signature, total_bytes = _prepare(shots)
    with video.destination_directory(destination, destination_dir_fd=destination_dir_fd) as (destination_name, parent_fd):
        storage = os.fstatvfs(parent_fd)
        if storage.f_bavail * storage.f_frsize < total_bytes + 1024 * 1024:
            raise ValueError('insufficient free storage for timeline')
        with video._private_stage(parent_fd, prefix='.silent-timeline-') as (private_path, wrapper_fd):
            stage = private_path / 'payload'
            stage.mkdir(mode=0o700)
            frames = stage / 'frames'
            frames.mkdir()
            inventory, mapping, retained = [], [], []
            fps = signature[2]
            for shot in planned:
                output_start = len(inventory)
                source = shot['source']
                for frame in source['frames']:
                    if cancel is not None and cancel():
                        raise ValueError('timeline assembly cancelled')
                    data = video._read_frame(shot['root'], frame['path'])
                    if len(data) != frame['bytes'] or hashlib.sha256(data).hexdigest() != frame['sha256']:
                        raise ValueError('missing or stale source frame: ' + frame['path'])
                    image = video._image(data, (source['width'], source['height']))
                    image.close()
                    if shot['start'] <= frame['index'] < shot['end']:
                        index = len(inventory)
                        relative = 'frames/%06d.png' % index
                        (stage / relative).write_bytes(data)
                        inventory.append({**frame, 'index': index, 'path': relative})
                        mapping.append({'index': index, 'shot_id': shot['id'], 'source_index': frame['index'],
                                        'source_sha256': frame['sha256'], 'pts': _ratio(Fraction(index, 1) / fps)})
                retained.append({'id': shot['id'], 'context': shot['context'], 'source': source, 'source_sha256': video._digest(source),
                                 'start': shot['start'], 'end': shot['end'],
                                 'output_start': output_start, 'output_end': len(inventory)})
            output_source = {**planned[0]['source'], 'origin': 'assembled', 'fps': _ratio(fps), 'frames': inventory}
            video.validate_source(output_source)
            manifest = {'schema_version': 1, 'kind': 'silent-timeline', 'source': output_source,
                        'shots': retained, 'mapping': mapping, 'duration': _ratio(Fraction(len(inventory), 1) / fps),
                        'audio_policy': 'forbidden', 'visual_review': 'not-performed'}
            encoded = (json.dumps(manifest, indent=2, allow_nan=False) + '\n').encode('utf-8')
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise ValueError('timeline manifest exceeds byte budget')
            (stage / 'timeline.json').write_bytes(encoded)
            validate(stage)
            if cancel is not None and cancel():
                raise ValueError('timeline assembly cancelled before publication')
            _rename_no_replace('payload', destination_name, source_dir_fd=wrapper_fd, destination_dir_fd=parent_fd)
    return manifest


def validate(root):
    """Revalidate every delivered byte and the retained source-to-output map."""
    root = Path(root)
    # Use a pinned regular-file descriptor, so replacing the manifest by a
    # symlink after the portable path check cannot redirect the JSON read.
    with video._open_source(root, 'timeline.json') as stream:
        if os.fstat(stream.fileno()).st_size > MAX_MANIFEST_BYTES:
            raise ValueError('timeline manifest exceeds byte budget')
        manifest = read_json(Path('/proc/self/fd') / str(stream.fileno()))
    validate_document(manifest)
    video._snapshot(video.make_request(manifest['source']), root, None)
    actual_names = {path.name for path in (root / 'frames').iterdir()}
    if actual_names != {'%06d.png' % i for i in range(len(manifest['source']['frames']))}:
        raise ValueError('unexpected or missing frame in timeline')
    return manifest


def validate_document(manifest):
    """Validate the self-contained source/map relationships without file I/O."""
    video._structural(manifest, schema(), 'silent timeline')
    keys = {'schema_version', 'kind', 'source', 'shots', 'mapping', 'duration', 'audio_policy', 'visual_review'}
    if (not isinstance(manifest, dict) or set(manifest) != keys or type(manifest['schema_version']) is not int or
            manifest['schema_version'] != 1 or manifest['kind'] != 'silent-timeline' or
            manifest['audio_policy'] != 'forbidden' or manifest['visual_review'] != 'not-performed'):
        raise ValueError('invalid timeline manifest')
    source = video.validate_source(manifest['source'])
    if source['origin'] != 'assembled':
        raise ValueError('timeline must retain assembled provenance')
    shots = manifest['shots']
    if not isinstance(shots, list) or not 1 <= len(shots) <= MAX_SHOTS:
        raise ValueError('invalid timeline shots')
    arguments = []
    for shot in shots:
        if not isinstance(shot, dict) or set(shot) != {'id', 'context', 'source', 'source_sha256', 'start', 'end', 'output_start', 'output_end'}:
            raise ValueError('invalid retained shot')
        if shot['source_sha256'] != video._digest(shot['source']):
            raise ValueError('retained source identity mismatch')
        arguments.append({key: shot[key] for key in ('id', 'context', 'source', 'start', 'end')} | {'root': None})
    planned, signature, _ = _prepare(arguments)
    expected_map, index = [], 0
    for shot, retained in zip(planned, shots):
        if type(retained['output_start']) is not int or retained['output_start'] != index:
            raise ValueError('noncontiguous shot output range')
        for frame in shot['source']['frames'][shot['start']:shot['end']]:
            if index >= len(source['frames']):
                raise ValueError('incomplete timeline inventory')
            expected_frame = {**frame, 'index': index, 'path': 'frames/%06d.png' % index}
            if source['frames'][index] != expected_frame:
                raise ValueError('timeline frame differs from retained source')
            expected_map.append({'index': index, 'shot_id': shot['id'], 'source_index': frame['index'],
                                 'source_sha256': frame['sha256'], 'pts': _ratio(Fraction(index, 1) / signature[2])})
            index += 1
        if type(retained['output_end']) is not int or retained['output_end'] != index:
            raise ValueError('noncontiguous shot output range')
    expected_header = {**planned[0]['source'], 'origin': 'assembled', 'fps': _ratio(signature[2]), 'frames': source['frames']}
    if (source != expected_header or index != len(source['frames']) or video._digest(manifest['mapping']) != video._digest(expected_map) or
            video._digest(manifest['duration']) != video._digest(_ratio(Fraction(index, 1) / signature[2]))):
        raise ValueError('timeline mapping, format or duration mismatch')
    return manifest


def encode(root, destination, profile='h264-mp4', **options):
    """Encode a timeline with a self-contained hash-bound shot-map receipt.

    The normalized manifest is captured after relationship validation. Encoding
    rechecks every source byte; later edits to original metadata cannot alter
    the retained mapping or its identity in this returned envelope.
    """
    manifest = copy.deepcopy(validate(root))
    request = video.make_request(manifest['source'], profile)
    receipt = video.encode(request, root, destination, **options)
    return {'schema_version': 1, 'kind': 'silent-timeline-export',
            'timeline_sha256': video._digest(manifest), 'timeline': manifest,
            'request': request, 'verification': receipt, 'visual_review': 'not-performed'}


def verify_export(artifact, envelope, *, root=None, **options):
    """Independently recheck movie bytes and retained timeline/request binding."""
    keys = {'schema_version', 'kind', 'timeline_sha256', 'timeline', 'request', 'verification', 'visual_review'}
    if (not isinstance(envelope, dict) or set(envelope) != keys or type(envelope['schema_version']) is not int or
            envelope['schema_version'] != 1 or envelope['kind'] != 'silent-timeline-export' or
            envelope['visual_review'] != 'not-performed'):
        raise ValueError('invalid timeline export envelope')
    manifest = validate_document(envelope['timeline'])
    if envelope['timeline_sha256'] != video._digest(manifest):
        raise ValueError('timeline export identity mismatch')
    request = video.validate_request(envelope['request'])
    if video._digest(request['source']) != video._digest(manifest['source']):
        raise ValueError('timeline export request differs from mapped source')
    if root is not None and video._digest(validate(root)) != envelope['timeline_sha256']:
        raise ValueError('current timeline differs from export')
    video.validate_receipt(envelope['verification'], artifact, request, root=root, **options)
    return {'verified': True, 'timeline_sha256': envelope['timeline_sha256'],
            'artifact_sha256': envelope['verification']['artifact_sha256'], 'visual_review': 'not-performed'}
