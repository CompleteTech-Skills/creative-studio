"""Explicit video-only export. Legacy production_render.mux_video stays audiovisual.

This module owns silent media admission, encoding and independent verification;
renderers supply exact PNG inventories and never supply arbitrary FFmpeg flags.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from PIL import Image
from atomic_publish import _rename_no_replace
from production_contracts import _structural, safe_path, read_json
from production_render import fps_fraction, file_hash

MAX_FRAMES = 18000
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_FRAME_BYTES = 64 * 1024**2
MAX_PIXELS = 4096 * 2160
DEFAULT_LIMITS = {'timeout_seconds': 300, 'max_output_bytes': 512 * 1024**2,
                  'max_log_bytes': 4 * 1024**2}
PROFILES = {
    'h264-mp4': {'container': 'mp4', 'codec': 'h264', 'encoder': 'libx264',
                 'extension': '.mp4', 'pixel_format': 'yuv420p', 'alpha': 'opaque',
                 'color': 'untagged-sdr', 'precision': 8, 'crf': 18, 'preset': 'veryfast'},
    'vp9-webm': {'container': 'webm', 'codec': 'vp9', 'encoder': 'libvpx-vp9',
                 'extension': '.webm', 'pixel_format': 'yuv420p', 'alpha': 'opaque',
                 'color': 'untagged-sdr', 'precision': 8, 'crf': 30, 'preset': 'good'},
}


def _obj(properties):
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties, 'required': list(properties)}


def _int(low, high):
    return {'type': 'integer', 'minimum': low, 'maximum': high}


SHA = {'type': 'string', 'pattern': '[0-9a-f]{64}'}
FPS = _obj({'numerator': _int(1, 240000), 'denominator': _int(1, 10000)})
FILE = _obj({'index': _int(0, MAX_FRAMES-1), 'path': {'type': 'string', 'minLength': 1, 'maxLength': 1024},
             'sha256': SHA, 'bytes': _int(1, MAX_FRAME_BYTES)})
SOURCE = _obj({'schema_version': {'const': 1}, 'kind': {'const': 'silent-frame-source'},
               'origin': {'enum': ['synthetic', 'production2d', 'blender3d']},
               'format': {'const': 'png'}, 'precision': {'const': 8},
               'alpha': {'const': 'opaque'}, 'color': {'const': 'untagged-sdr'},
               'width': _int(2, 4096), 'height': _int(2, 4096), 'fps': FPS,
               'frames': {'type': 'array', 'minItems': 1, 'maxItems': MAX_FRAMES, 'items': FILE}})
LIMITS = _obj({'timeout_seconds': _int(1, 3600), 'max_output_bytes': _int(1024, 2*1024**3),
               'max_log_bytes': _int(1024, 64*1024**2)})
REQUEST = _obj({'schema_version': {'const': 1}, 'kind': {'const': 'silent-video-request'},
                'source': SOURCE, 'profile': {'enum': list(PROFILES)},
                'audio': _obj({'policy': {'const': 'forbidden'}}), 'limits': LIMITS})


def schemas():
    """Standalone strict schemas. Semantic/path/hash checks remain mandatory."""
    result = {'source': SOURCE, 'request': REQUEST, 'profile': {'enum': list(PROFILES.values())}}
    raw = _obj({'command': {'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': {'type': 'string', 'maxLength': 4096}},
                'exit_status': {'const': 0}, 'stdout': {'type': 'string', 'maxLength': 64*1024**2},
                'stderr': {'type': 'string', 'maxLength': 64*1024**2}})
    fraction = _obj({'numerator': _int(0, 10**12), 'denominator': _int(1, 10**12)})
    result['artifact'] = _obj({'schema_version': {'const': 1}, 'kind': {'const': 'silent-video-verification'},
        'verified': {'const': True}, 'artifact_sha256': SHA, 'artifact_bytes': _int(1, 2*1024**3),
        'source_manifest_sha256': SHA, 'request_sha256': SHA, 'video_stream_count': {'const': 1},
        'audio_stream_count': {'const': 0}, 'other_stream_count': {'const': 0},
        'frame_count': _int(1, MAX_FRAMES), 'fps': FPS, 'duration': fraction,
        'timebase_tolerance': fraction, 'profile': {'enum': list(PROFILES)},
        'source_pixel_comparison': {'type': 'array', 'maxItems': 3, 'items': _obj({'index': _int(0, MAX_FRAMES-1), 'rgb_psnr_db': {'type': 'number', 'minimum': 18, 'maximum': 100}, 'minimum_psnr_db': {'const': 18.0}})},
        'probe': raw, 'timeline': raw, 'decode': raw, 'visual_review': {'const': 'not-performed'}})
    result['job'] = _obj({'schema_version': {'const': 1}, 'kind': {'const': 'silent-video-job'},
        'request_sha256': SHA, 'source_manifest_sha256': SHA,
        'status': {'enum': ['queued', 'running', 'verifying', 'complete', 'failed', 'cancelled']},
        'artifact_sha256': {'type': 'string', 'pattern': '(?:[0-9a-f]{64})?'},
        'error': {'type': 'string', 'maxLength': 4096}})
    result['capability'] = _obj({'schema_version': {'const': 1}, 'kind': {'const': 'silent-profile-capability'},
        'profile': {'enum': list(PROFILES)}, 'status': {'enum': ['supported', 'unsupported', 'unavailable']},
        'platform': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'max_width': _int(2, 4096), 'max_height': _int(2, 4096), 'max_pixels': _int(4, MAX_PIXELS),
        'estimated_source_bytes': _int(0, MAX_SOURCE_BYTES), 'estimated_output_bytes': _int(0, 2*1024**3)})
    return {name: {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                   'title': 'Silent video '+name+' v1', **copy.deepcopy(value)}
            for name, value in result.items()}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _path(root, relative):
    path = safe_path(root, relative)
    # Cross-platform alias and control-character rejection before native I/O.
    for part in relative.split('/'):
        if (part[-1:] in (' ', '.') or any(ord(c) < 32 for c in part) or
                re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part)):
            raise ValueError('nonportable frame path')
    return path


def _open_source(root, relative):
    """Walk directory descriptors without following replaceable symlinks."""
    path = _path(root, relative)
    if os.name == 'posix' and hasattr(os, 'O_NOFOLLOW'):
        fd = os.open(Path(root).resolve(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            parts = relative.split('/')
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result)
            raise ValueError('frame source must be a regular file')
        return os.fdopen(result, 'rb')
    stream = path.open('rb')
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or path.is_symlink():
        stream.close()
        raise ValueError('frame source must be a regular file')
    return stream


def _read_frame(root, relative):
    try:
        with _open_source(root, relative) as stream:
            data = stream.read(MAX_FRAME_BYTES+1)
    except OSError as exc:
        raise ValueError('frame cannot be safely read: '+str(relative)) from exc
    if not 0 < len(data) <= MAX_FRAME_BYTES:
        raise ValueError('frame exceeds encoded byte limit')
    return data


def _image_unchecked(data, expected=None):
    import io
    with Image.open(io.BytesIO(data)) as im:
        if im.format != 'PNG' or im.mode not in ('RGB', 'RGBA'):
            raise ValueError('only 8-bit RGB/RGBA PNG masters are currently encodable')
        width, height = im.size
        if width < 2 or height < 2 or width > 4096 or height > 4096 or width*height > MAX_PIXELS:
            raise ValueError('frame dimensions exceed supported bounds')
        if width % 2 or height % 2:
            raise ValueError('yuv420p requires even dimensions; source pixels are never silently padded')
        if expected is not None and im.size != expected:
            raise ValueError('frame dimensions differ from source inventory')
        im.load()
        if im.mode == 'RGBA' and im.getchannel('A').getextrema() != (255, 255):
            raise ValueError('opaque profile rejects nonopaque source alpha; composite explicitly first')
        return im.convert('RGB')


def _image(data, expected=None):
    try:
        return _image_unchecked(data, expected)
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('invalid or unsafe PNG frame') from exc


def validate_source(source):
    try:
        _structural(source, SOURCE, 'silent source')
    except (OverflowError, RecursionError) as exc:
        raise ValueError('source exceeds numeric/nesting limits') from exc
    fps_fraction(source['fps'])
    if source['width']*source['height'] > MAX_PIXELS or source['width'] % 2 or source['height'] % 2:
        raise ValueError('unsupported dimensions')
    if [x['index'] for x in source['frames']] != list(range(len(source['frames']))):
        raise ValueError('frame indices must be contiguous and ordered')
    aliases = set()
    total = 0
    import unicodedata
    for frame in source['frames']:
        # Validate syntax without requiring an on-disk root.
        name = frame['path']
        for part in name.split('/'):
            if not part or part[-1:] in (' ', '.') or any(ord(c) < 32 for c in part) or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part):
                raise ValueError('nonportable frame path')
        if ('\\' in name or ':' in name or name.startswith('/') or
                any(p in ('', '.', '..') for p in name.split('/'))):
            raise ValueError('unsafe source path')
        key = unicodedata.normalize('NFC', name).casefold()
        if key in aliases:
            raise ValueError('duplicate/aliased frame path')
        aliases.add(key)
        total += frame['bytes']
    if total > MAX_SOURCE_BYTES:
        raise ValueError('source inventory exceeds byte budget')
    return source


def build_source(root, frame_paths, fps, origin='synthetic'):
    if not isinstance(frame_paths, (list, tuple)) or not 1 <= len(frame_paths) <= MAX_FRAMES:
        raise ValueError('bounded ordered frame paths required')
    frames, size, total = [], None, 0
    for index, relative in enumerate(frame_paths):
        data = _read_frame(root, relative)
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            raise ValueError('source inventory exceeds byte budget')
        im = _image(data, size)
        size = im.size
        im.close()
        frames.append({'index': index, 'path': relative, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    source = {'schema_version': 1, 'kind': 'silent-frame-source', 'origin': origin,
              'format': 'png', 'precision': 8, 'alpha': 'opaque', 'color': 'untagged-sdr',
              'width': size[0], 'height': size[1], 'fps': copy.deepcopy(fps), 'frames': frames}
    return validate_source(source)


def source_from_production(root):
    """Adapt complete existing shot/scene output, preserving its original WAV."""
    from production_render import validate_render
    validate_render(root)
    manifest = read_json(Path(root)/'render-manifest.json')
    source = build_source(root, [x['path'] for x in manifest['frames']], manifest['fps'], 'production2d')
    if any(a['sha256'] != b['sha256'] for a, b in zip(source['frames'], manifest['frames'])):
        raise ValueError('production source changed during admission')
    return source


def source_from_blender_manifest(root, manifest):
    """Admit a renderer-owned PNG inventory; origin is a claim, not visual approval.

    EXR/HDR/alpha contracts belong to later #229/#235/#237 and fail explicitly.
    """
    expected = {'schema_version', 'kind', 'engine', 'fps', 'frames'}
    if (not isinstance(manifest, dict) or set(manifest) != expected or
            manifest['schema_version'] != 1 or manifest['kind'] != 'blender-frame-manifest' or
            manifest['engine'] not in ('CYCLES', 'BLENDER_EEVEE_NEXT') or
            not isinstance(manifest['frames'], list)):
        raise ValueError('unsupported Blender frame manifest')
    for item in manifest['frames']:
        _structural(item, FILE, 'Blender frame')
    source = build_source(root, [x['path'] for x in manifest['frames']], manifest['fps'], 'blender3d')
    if source['frames'] != manifest['frames']:
        raise ValueError('Blender frame inventory differs from actual bytes/order')
    return source


def make_request(source, profile='h264-mp4'):
    return validate_request({'schema_version': 1, 'kind': 'silent-video-request',
                             'source': copy.deepcopy(source), 'profile': profile,
                             'audio': {'policy': 'forbidden'}, 'limits': dict(DEFAULT_LIMITS)})


def validate_request(request):
    try:
        _structural(request, REQUEST, 'silent request')
    except (OverflowError, RecursionError) as exc:
        raise ValueError('request exceeds numeric/nesting limits') from exc
    validate_source(request['source'])
    return request


def _binary(name, override=None):
    candidate = override if override is not None else shutil.which(name)
    if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(name+' unavailable; no codec or audiovisual fallback is permitted')
    return str(Path(candidate).resolve())


def _run(command, limits, *, cancel=None, output=None, pass_fds=()):
    """Bound both process duration and captured evidence, including fast exits."""
    start = time.monotonic()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, pass_fds=pass_fds)
        try:
            while True:
                status = process.poll()
                if cancel is not None and cancel():
                    raise ValueError('silent export cancelled')
                if time.monotonic()-start > limits['timeout_seconds']:
                    raise ValueError('media command exceeded timeout')
                if any(os.fstat(stream.fileno()).st_size > limits['max_log_bytes'] for stream in (stdout, stderr)):
                    raise ValueError('media command exceeded evidence byte budget')
                if output is not None and output.exists() and output.stat().st_size > limits['max_output_bytes']:
                    raise ValueError('encoded output exceeds byte budget')
                if status is not None:
                    break
                time.sleep(.02)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        stdout.seek(0); stderr.seek(0)
        out = stdout.read().decode('utf-8', 'strict')
        err = stderr.read().decode('utf-8', 'replace')
    if status:
        raise ValueError('media command failed ('+str(status)+'): '+err[-2000:])
    return {'command': command, 'exit_status': status, 'stdout': out, 'stderr': err}


def capabilities(*, ffmpeg=None, ffprobe=None):
    result = {'schema_version': 1, 'kind': 'silent-video-capabilities', 'platform': os.name,
              'profiles': {}, 'audio_policy': 'forbidden'}
    try:
        encoder, probe = _binary('ffmpeg', ffmpeg), _binary('ffprobe', ffprobe)
        evidence = {name: _run([binary, '-version'], DEFAULT_LIMITS)
                    for name, binary in [('ffmpeg', encoder), ('ffprobe', probe)]}
        lists = {flag: _run([encoder, '-hide_banner', flag], DEFAULT_LIMITS)['stdout']
                 for flag in ['-encoders', '-muxers', '-pix_fmts']}
    except (ValueError, OSError) as exc:
        result.update(status='unavailable', reason=str(exc))
        return result
    publication_supported = secure_publication_supported()
    for name, profile in PROFILES.items():
        supported = (publication_supported and re.search(r'\b'+re.escape(profile['encoder'])+r'\b', lists['-encoders']) and
                     re.search(r'\b'+profile['container']+r'\b', lists['-muxers']) and
                     re.search(r'\byuv420p\b', lists['-pix_fmts']))
        result['profiles'][name] = {**profile, 'status': 'supported' if supported else 'unsupported'}
    result.update(status='available', evidence=evidence, raw_capabilities=lists,
                  secure_publication_supported=publication_supported)
    return result


def _snapshot(request, root, directory):
    source = request['source']
    for frame in source['frames']:
        data = _read_frame(root, frame['path'])
        if len(data) != frame['bytes'] or hashlib.sha256(data).hexdigest() != frame['sha256']:
            raise ValueError('source frame missing/stale: '+frame['path'])
        im = _image(data, (source['width'], source['height']))
        im.close()
        if directory is not None:
            (directory / ('%06d.png' % frame['index'])).write_bytes(data)


def _fraction(value, label):
    try:
        if not isinstance(value, str):
            raise ValueError()
        result = Fraction(value)
        if result <= 0:
            raise ValueError()
        return result
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        raise ValueError('invalid '+label+' in probe') from exc


def _verify_snapshot(path, request, root=None, *, ffmpeg=None, ffprobe=None, cancel=None, diagnostics=None):
    """Probe ALL streams, enumerate presentation timestamps and strictly decode."""
    validate_request(request)
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= request['limits']['max_output_bytes']:
        raise ValueError('artifact is missing, unsafe or exceeds byte budget')
    before = file_hash(path)
    profile, source, limits = PROFILES[request['profile']], request['source'], request['limits']
    encoder, probe = _binary('ffmpeg', ffmpeg), _binary('ffprobe', ffprobe)
    inspected = _run([probe, '-v', 'error', '-count_frames', '-show_streams', '-show_format',
                      '-of', 'json', str(path.resolve())], limits, cancel=cancel)
    try:
        data = json.loads(inspected['stdout'])
        streams = data['streams']
        if not isinstance(streams, list) or any(not isinstance(x, dict) for x in streams):
            raise ValueError('malformed stream list')
        if diagnostics is not None:
            diagnostics.update(video_stream_count=sum(x.get('codec_type') == 'video' for x in streams),
                               audio_stream_count=sum(x.get('codec_type') == 'audio' for x in streams),
                               other_stream_count=sum(x.get('codec_type') not in ('video', 'audio') for x in streams),
                               probe=inspected)
        if not isinstance(streams, list) or len(streams) != 1 or streams[0].get('codec_type') != 'video':
            raise ValueError('silent artifact requires exactly one video stream and zero audio/other streams')
        video = streams[0]
        expected_count = len(source['frames'])
        if (video.get('codec_name') != profile['codec'] or video.get('pix_fmt') != profile['pixel_format'] or
                video.get('width') != source['width'] or video.get('height') != source['height'] or
                int(video.get('nb_read_frames', -1)) != expected_count):
            raise ValueError('encoded codec/pixel format/dimensions/frame count mismatch')
        if video.get('sample_aspect_ratio') != '1:1':
            raise ValueError('encoded sample aspect ratio mismatch')
        dar = Fraction(source['width'], source['height'])
        if Fraction(video.get('display_aspect_ratio', '0:1').replace(':', '/')) != dar:
            raise ValueError('encoded display aspect ratio mismatch')
        if video.get('color_range', 'unknown') not in ('unknown', 'tv'):
            raise ValueError('unexpected color range')
        for key in ('color_space', 'color_transfer', 'color_primaries'):
            if video.get(key, 'unknown') not in ('unknown', 'unspecified'):
                raise ValueError('unexpected color properties for untagged SDR profile')
        formats = set(data['format']['format_name'].split(','))
        if profile['container'] not in formats:
            raise ValueError('encoded container mismatch')
        fps = fps_fraction(source['fps'])
        if _fraction(video['avg_frame_rate'], 'frame rate') != fps:
            raise ValueError('encoded rational frame rate mismatch')
        tick = _fraction(video['time_base'], 'time base')
        duration = Fraction(data['format']['duration'])
        if abs(duration - Fraction(expected_count, 1)/fps) > 2*tick:
            raise ValueError('encoded presentation duration mismatch')
    except (KeyError, TypeError, ZeroDivisionError, json.JSONDecodeError) as exc:
        raise ValueError('malformed/incomplete ffprobe evidence') from exc
    timeline = _run([probe, '-v', 'error', '-select_streams', 'v:0', '-show_frames',
                     '-show_entries', 'frame=best_effort_timestamp,pkt_duration', '-of', 'json', str(path.resolve())],
                    limits, cancel=cancel)
    try:
        frames = json.loads(timeline['stdout'])['frames']
        if len(frames) != expected_count:
            raise ValueError('decoded frame enumeration mismatch')
        times = [int(frame['best_effort_timestamp'])*tick for frame in frames]
        for i, stamp in enumerate(times):
            if abs(stamp-Fraction(i, 1)/fps) > tick or i and stamp <= times[i-1]:
                raise ValueError('missing/duplicate/out-of-order presentation timestamp')
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('malformed frame timestamp evidence') from exc
    decoded = _run([encoder, '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
                    '-err_detect', 'explode', '-i', str(path.resolve()), '-map', '0:v:0',
                    '-an', '-sn', '-dn', '-f', 'null', '-'], limits, cancel=cancel)
    if decoded['stderr'].strip():
        raise ValueError('strict decode reported errors: '+decoded['stderr'][-2000:])
    pixel_checks = []
    if root is not None:
        _snapshot(request, root, None)
        from PIL import ImageChops, ImageStat
        with tempfile.TemporaryDirectory(prefix='silent-pixels-') as tmp:
            for index in sorted({0, expected_count//2, expected_count-1}):
                output = Path(tmp)/('decoded-%d.png' % index)
                _run([encoder, '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
                      '-i', str(path.resolve()), '-map', '0:v:0', '-an', '-sn', '-dn',
                      '-vf', 'select=eq(n\\,%d)' % index, '-frames:v', '1', '-threads', '1',
                      str(output)], limits, cancel=cancel, output=output)
                expected = _image(_read_frame(root, source['frames'][index]['path']),
                                  (source['width'], source['height']))
                with Image.open(output) as decoded_frame:
                    actual = decoded_frame.convert('RGB')
                    difference = ImageChops.difference(expected, actual)
                    mse = sum(ImageStat.Stat(difference).sum2)/(source['width']*source['height']*3)
                    psnr = 100.0 if mse == 0 else 10*math.log10(255**2/mse)
                    if psnr < 18.0:
                        raise ValueError('decoded representative pixels differ from source')
                    pixel_checks.append({'index': index, 'rgb_psnr_db': psnr, 'minimum_psnr_db': 18.0})
                    actual.close(); difference.close()
                expected.close()
    if before != file_hash(path):
        raise ValueError('artifact changed during independent verification')
    return {'schema_version': 1, 'kind': 'silent-video-verification', 'verified': True,
            'artifact_sha256': before, 'artifact_bytes': path.stat().st_size,
            'source_manifest_sha256': _digest(source), 'request_sha256': _digest(request),
            'video_stream_count': 1, 'audio_stream_count': 0, 'other_stream_count': 0,
            'frame_count': expected_count, 'fps': source['fps'],
            'duration': {'numerator': duration.numerator, 'denominator': duration.denominator},
            'timebase_tolerance': {'numerator': (2*tick).numerator, 'denominator': (2*tick).denominator},
            'profile': request['profile'], 'source_pixel_comparison': pixel_checks, 'probe': inspected, 'timeline': timeline, 'decode': decoded,
            'visual_review': 'not-performed'}


def verify_export(path, request, root=None, *, ffmpeg=None, ffprobe=None, cancel=None):
    """Verify a private exact-byte snapshot, then recheck the caller's artifact.

    This prevents an untrusted producer switching a path between probe/decode
    subprocesses. The receipt identifies bytes, never mutable pathname identity.
    """
    validate_request(request)
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError('artifact is missing or a symlink')
    limit = request['limits']['max_output_bytes']
    with tempfile.TemporaryDirectory(prefix='silent-verify-') as tmp:
        snapshot = Path(tmp)/('artifact'+PROFILES[request['profile']]['extension'])
        digest = hashlib.sha256()
        count = 0
        with _open_source(path.parent, path.name) as source, snapshot.open('xb') as output:
            for chunk in iter(lambda: source.read(1024*1024), b''):
                count += len(chunk)
                if count > limit:
                    raise ValueError('artifact exceeds byte budget')
                output.write(chunk)
                digest.update(chunk)
        diagnostics = {'schema_version': 1, 'verified': False, 'artifact_sha256': digest.hexdigest(),
                       'request_sha256': _digest(request), 'source_manifest_sha256': _digest(request['source'])}
        try:
            result = _verify_snapshot(snapshot, request, root=root, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                      cancel=cancel, diagnostics=diagnostics)
        except ValueError as exc:
            exc.evidence = diagnostics
            raise
        checked = hashlib.sha256()
        checked_bytes = 0
        with _open_source(path.parent, path.name) as original:
            for chunk in iter(lambda: original.read(1024*1024), b''):
                checked_bytes += len(chunk)
                if checked_bytes > limit:
                    raise ValueError('artifact changed beyond byte budget')
                checked.update(chunk)
        if digest.hexdigest() != result['artifact_sha256'] or checked.hexdigest() != result['artifact_sha256']:
            raise ValueError('artifact changed during independent verification')
        return result


def validate_receipt(receipt, path, request, root=None, *, ffmpeg=None, ffprobe=None):
    """Never trust a claimed verified flag; re-probe the actual artifact."""
    contract = copy.deepcopy(schemas()['artifact'])
    contract['properties']['encode'] = contract['properties']['probe']
    contract['properties']['path'] = {'type': 'string', 'minLength': 1, 'maxLength': 1024}
    _structural(receipt, contract, 'silent receipt')
    actual = verify_export(path, request, root=root, ffmpeg=ffmpeg, ffprobe=ffprobe)
    for key in ('artifact_sha256', 'artifact_bytes', 'source_manifest_sha256', 'request_sha256',
                'video_stream_count', 'audio_stream_count', 'other_stream_count', 'frame_count',
                'fps', 'duration', 'timebase_tolerance', 'profile'):
        if receipt[key] != actual[key]:
            raise ValueError('corrupt/stale silent receipt: '+key)
    old_probe, new_probe = json.loads(receipt['probe']['stdout']), json.loads(actual['probe']['stdout'])
    for data in (old_probe, new_probe):
        data['format'].pop('filename', None)
    if old_probe != new_probe or receipt['timeline']['stdout'] != actual['timeline']['stdout']:
        raise ValueError('corrupt silent verification evidence')
    if any(receipt[key]['exit_status'] != 0 or receipt[key]['stderr'] for key in ('probe', 'timeline', 'decode')):
        raise ValueError('failed command evidence cannot verify a receipt')
    return actual


def _destination(path):
    path = Path(path).absolute()
    for ancestor in [path, *path.parents]:
        if ancestor.is_symlink():
            raise ValueError('destination must not traverse symlinks')
    if path.exists() or not path.parent.is_dir():
        raise ValueError('destination must be new and have an existing parent')
    return path


def secure_publication_supported():
    # The current subprocess path adapter uses Linux procfs and inherited FDs.
    # The shared primitive also supports macOS dirfds, but no untested fdescfs
    # subprocess adapter is advertised as working.
    return sys.platform.startswith('linux') and Path('/proc/self/fd').is_dir()


@contextmanager
def _pin_directory(path):
    if not secure_publication_supported():
        raise ValueError('secure silent encoding currently requires Linux procfs; verification remains portable')
    path = Path(path).absolute()
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = current
        yield descriptor
    finally:
        os.close(descriptor)



def _encode_pinned(request, root, destination, parent_fd, *, ffmpeg=None, ffprobe=None, cancel=None, progress=None):
    validate_request(request)
    request = copy.deepcopy(request)
    destination = _destination(destination)
    profile = PROFILES[request['profile']]
    if destination.suffix != profile['extension']:
        raise ValueError('destination extension differs from explicit profile')
    if cancel is not None and cancel():
        raise ValueError('silent export cancelled')
    caps = capabilities(ffmpeg=ffmpeg, ffprobe=ffprobe)
    if caps['status'] != 'available' or caps['profiles'][request['profile']]['status'] != 'supported':
        raise ValueError('requested media profile unavailable/unsupported; no fallback')
    if shutil.disk_usage(destination.parent).free < sum(x['bytes'] for x in request['source']['frames']) + request['limits']['max_output_bytes']:
        raise ValueError('insufficient free storage for bounded snapshot and output')
    with tempfile.TemporaryDirectory(prefix='.silent-export-', dir=Path('/proc/self/fd')/str(parent_fd)) as tmp:
        stage = Path(tmp)
        inputs = stage/'frames'; inputs.mkdir()
        _snapshot(request, root, inputs)
        output = stage/('output.partial'+profile['extension'])
        fps = fps_fraction(request['source']['fps'])
        command = [_binary('ffmpeg', ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                   '-framerate', str(fps), '-start_number', '0', '-i', str(inputs/'%06d.png'),
                   '-map', '0:v:0', '-an', '-sn', '-dn', '-map_metadata', '-1', '-map_chapters', '-1',
                   '-frames:v', str(len(request['source']['frames'])), '-c:v', profile['encoder'],
                   '-pix_fmt', profile['pixel_format'], '-vf', 'setsar=1', '-crf', str(profile['crf']),
                   '-threads', '1']
        if request['profile'] == 'h264-mp4':
            command += ['-preset', profile['preset'], '-g', str(max(1, round(fps*2))), '-movflags', '+faststart']
        else:
            command += ['-deadline', 'good', '-b:v', '0']
        command += [str(output)]
        if progress is not None:
            progress({'phase': 'encode', 'total_frames': len(request['source']['frames'])})
        execution = _run(command, request['limits'], cancel=cancel, output=output, pass_fds=(parent_fd,))
        receipt = verify_export(output, request, root=root, ffmpeg=ffmpeg, ffprobe=ffprobe, cancel=cancel)
        if cancel is not None and cancel():
            raise ValueError('silent export cancelled before publication')
        # Never replace a file or even an empty directory created by another writer.
        # Source and destination basenames resolve relative to pinned directory
        # descriptors, never a path a concurrent writer can replace by a symlink.
        source_fd = os.open(stage.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            _rename_no_replace(output.name, destination.name,
                               source_dir_fd=source_fd, destination_dir_fd=parent_fd)
        finally:
            os.close(source_fd)
        receipt['encode'] = execution
        receipt['path'] = destination.name
        if progress is not None:
            progress({'phase': 'complete', 'frame_count': receipt['frame_count']})
        return receipt


def encode(request, root, destination, *, ffmpeg=None, ffprobe=None, cancel=None, progress=None):
    validate_request(request)
    destination = _destination(destination)
    with _pin_directory(destination.parent) as parent_fd:
        return _encode_pinned(request, root, destination, parent_fd, ffmpeg=ffmpeg,
                              ffprobe=ffprobe, cancel=cancel, progress=progress)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('capabilities')
    verify = sub.add_parser('verify-export')
    verify.add_argument('artifact'); verify.add_argument('--request', required=True)
    verify.add_argument('--require-silent', action='store_true', required=True)
    verify.add_argument('--root')
    export = sub.add_parser('encode')
    export.add_argument('--request', required=True); export.add_argument('--root', required=True)
    export.add_argument('--destination', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'capabilities': result = capabilities()
        elif args.command == 'verify-export': result = verify_export(args.artifact, read_json(args.request), root=args.root)
        else: result = encode(read_json(args.request), args.root, args.destination)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except (ValueError, OSError) as exc:
        print(json.dumps({**getattr(exc, 'evidence', {}), 'schema_version': 1, 'verified': False, 'error': str(exc)}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
