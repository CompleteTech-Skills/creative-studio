"""Adapt independently verified renderer-owned PNGs into silent-media sources.

No renderer is implemented here. Transparent display-encoded masters require
an explicit sRGB background, composited in linear light; originals stay intact.
"""
from __future__ import annotations
import copy
import hashlib
import io
import json
import os
from pathlib import Path

from PIL import Image
from atomic_publish import _rename_no_replace
from production_contracts import MAX_MANIFEST_BYTES
import silent_video as video


def _background(background):
    if (not isinstance(background, list) or len(background) != 3 or
            any(type(value) is not int or not 0 <= value <= 255 for value in background)):
        raise ValueError('explicit background must be three 8-bit sRGB integers')


def _preflight(request, result):
    if not isinstance(request, dict) or not isinstance(request.get('settings'), dict):
        raise ValueError('native request settings required')
    settings = request['settings']
    resolution = settings.get('resolution')
    if (not isinstance(resolution, list) or len(resolution) != 2 or
            any(type(value) is not int or not 2 <= value <= 4096 or value % 2 for value in resolution) or
            resolution[0] * resolution[1] > video.MAX_PIXELS):
        raise ValueError('native source dimensions exceed silent export limits')
    output = settings.get('output')
    if (not isinstance(output, dict) or output.get('format') != 'png' or
            type(output.get('bit_depth')) is not int or output['bit_depth'] != 8 or
            output.get('color_space') != 'srgb' or output.get('passes') != ['combined'] or
            output.get('alpha') not in ('straight', 'opaque')):
        raise ValueError('silent SDR adapter requires actual sRGB PNG8 combined; EXR/HDR needs an explicit separate transform')
    video.fps_fraction(request.get('fps'))
    frame_range = request.get('frame_range')
    if (not isinstance(frame_range, dict) or set(frame_range) != {'start', 'end'} or
            any(type(frame_range[key]) is not int for key in ('start', 'end')) or
            not 0 <= frame_range['start'] < frame_range['end'] <= 100000 or
            frame_range['end'] - frame_range['start'] > video.MAX_FRAMES):
        raise ValueError('native frame range exceeds silent export limits')
    images = result.get('images') if isinstance(result, dict) else None
    count = frame_range['end'] - frame_range['start']
    if not isinstance(images, list) or len(images) != count:
        raise ValueError('complete bounded combined image inventory required')
    file_schema = video._obj({key: video.FILE['properties'][key] for key in ('path', 'sha256', 'bytes')})
    seen, total = set(), 0
    for item in images:
        if (not isinstance(item, dict) or item.get('pass_name') != 'combined' or
                type(item.get('frame')) is not int or item['frame'] in seen or
                not frame_range['start'] <= item['frame'] < frame_range['end']):
            raise ValueError('invalid native frame inventory')
        seen.add(item['frame'])
        video._structural(item.get('file'), file_schema, 'native source file')
        if (item['file']['path'] != 'frames/%06d.png' % item['frame'] or
                item.get('format') != 'png' or type(item.get('bit_depth')) is not int or
                item['bit_depth'] != 8 or item.get('resolution') != resolution):
            raise ValueError('native image declaration differs from supported source')
        total += item['file']['bytes']
        if total > video.MAX_SOURCE_BYTES:
            raise ValueError('native source exceeds aggregate byte budget')


def composite_srgb(image, background):
    """Flatten straight RGBA8 using sRGB EOTF, linear over, inverse EOTF.

    Row blocks bound temporary floating-point arrays independently of film size.
    Alpha is linear coverage, never passed through the sRGB transfer function.
    """
    _background(background)
    import numpy as np
    rgba = image.convert('RGBA')
    result = Image.new('RGB', rgba.size)
    values = np.arange(256, dtype=np.float64) / 255
    linear = np.where(values <= .04045, values / 12.92, ((values + .055) / 1.055) ** 2.4)
    background_linear = linear[np.asarray(background)]
    for start in range(0, rgba.height, 32):
        end = min(start + 32, rgba.height)
        pixels = np.asarray(rgba.crop((0, start, rgba.width, end)))
        alpha = pixels[:, :, 3:4].astype(np.float64) / 255
        rgb = linear[pixels[:, :, :3]] * alpha + background_linear * (1 - alpha)
        encoded = np.where(rgb <= .0031308, 12.92 * rgb, 1.055 * rgb ** (1 / 2.4) - .055)
        encoded = np.clip(np.floor(encoded * 255 + .5), 0, 255).astype(np.uint8)
        result.paste(Image.fromarray(encoded), (0, start))
    return result


def prepare(request, result, render_root, destination, *, snapshot_root, expected_backend, background=None, cancel=None, destination_dir_fd=None):
    """Return an admitted source and retained real-owner verification evidence.

    The observed backend identity must come from the renderer caller, rather
    than silently trusting the result's own backend claim. Verification checks
    snapshot/request closure and every image through the canonical owner.
    """
    if cancel is not None and cancel():
        raise ValueError('silent source preparation cancelled')
    _preflight(request, result)
    if background is not None:
        _background(background)
    try:
        from render3d_verify import verify_result
    except ImportError as exc:
        raise ValueError('canonical Render3D verifier is unavailable') from exc
    with video.destination_directory(destination, destination_dir_fd=destination_dir_fd) as (destination_name, parent_fd):
        request, result = copy.deepcopy(request), copy.deepcopy(result)
        _preflight(request, result)
        verification = verify_result(request, result, render_root,
                                     snapshot_root=snapshot_root, expected_backend=expected_backend)
        if cancel is not None and cancel():
            raise ValueError('silent source preparation cancelled')
        output = request['settings']['output']
        if output['format'] != 'png' or output['bit_depth'] != 8 or output['color_space'] != 'srgb':
            raise ValueError('silent SDR adapter requires actual sRGB PNG8; EXR/HDR needs an explicit separate transform')
        combined = sorted((item for item in result['images'] if item['pass_name'] == 'combined'), key=lambda item: item['frame'])
        if not combined or len(combined) > video.MAX_FRAMES:
            raise ValueError('complete bounded combined image inventory required')
        with video._private_stage(parent_fd, prefix='.silent-render3d-') as (private_path, wrapper_fd):
            stage = private_path / 'payload'
            stage.mkdir(mode=0o700)
            (stage / 'frames').mkdir()
            mapping, paths, total = [], [], 0
            for index, item in enumerate(combined):
                if cancel is not None and cancel():
                    raise ValueError('silent source preparation cancelled')
                reference = item['file']
                data = video._read_frame(render_root, reference['path'])
                if len(data) != reference['bytes'] or hashlib.sha256(data).hexdigest() != reference['sha256']:
                    raise ValueError('native render changed after independent verification')
                if background is None:
                    image = video._image(data)
                    image.close()
                else:
                    # Owner verification established actual PNG8, dimensions,
                    # channels and straight/opaque alpha for these exact bytes.
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                        converted = composite_srgb(image, background)
                    buffer = io.BytesIO()
                    converted.save(buffer, format='PNG')
                    converted.close()
                    data = buffer.getvalue()
                total += len(data)
                if len(data) > video.MAX_FRAME_BYTES or total > video.MAX_SOURCE_BYTES:
                    raise ValueError('prepared source exceeds frame/aggregate byte budget')
                path = 'frames/%06d.png' % index
                (stage / path).write_bytes(data)
                paths.append(path)
                mapping.append({'index': index, 'native_frame': item['frame'], 'native_file': copy.deepcopy(reference),
                                'output_sha256': hashlib.sha256(data).hexdigest()})
            source = video.build_source(stage, paths, request['fps'], origin='blender3d')
            report = {'schema_version': 1, 'kind': 'silent-render3d-source', 'source': source,
                      'request': request, 'result': result, 'verification': verification,
                      'source_mapping': mapping, 'transform': {'kind': 'none' if background is None else 'srgb-linear-over',
                          'background_srgb8': background, 'alpha': 'straight-coverage', 'output_alpha': 'opaque'},
                      'visual_review': 'not-performed'}
            encoded = (json.dumps(report, indent=2, allow_nan=False) + '\n').encode('utf-8')
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise ValueError('prepared source manifest exceeds byte budget')
            (stage / 'source.json').write_bytes(encoded)
            if cancel is not None and cancel():
                raise ValueError('silent source preparation cancelled before publication')
            with video._open_source(stage, 'source.json') as stream:
                if stream.read(len(encoded) + 1) != encoded:
                    raise ValueError('prepared source manifest changed before publication')
            if {path.name for path in stage.iterdir()} != {'frames', 'source.json'}:
                raise ValueError('unexpected prepared source payload inventory')
            verify_result(request, result, render_root,
                          snapshot_root=snapshot_root, expected_backend=expected_backend)
            video._snapshot(video.make_request(source), stage, None)
            if {path.name for path in (stage / 'frames').iterdir()} != {'%06d.png' % i for i in range(len(paths))}:
                raise ValueError('unexpected prepared source frame inventory')
            _rename_no_replace('payload', destination_name, source_dir_fd=wrapper_fd, destination_dir_fd=parent_fd)
    return report
