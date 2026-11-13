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
import tempfile

from PIL import Image
from atomic_publish import _rename_no_replace
import silent_video as video


def composite_srgb(image, background):
    """Flatten straight RGBA8 using sRGB EOTF, linear over, inverse EOTF.

    Row blocks bound temporary floating-point arrays independently of film size.
    Alpha is linear coverage, never passed through the sRGB transfer function.
    """
    import numpy as np
    if (not isinstance(background, list) or len(background) != 3 or
            any(type(value) is not int or not 0 <= value <= 255 for value in background)):
        raise ValueError('explicit background must be three 8-bit sRGB integers')
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


def prepare(request, result, render_root, destination, *, snapshot_root, expected_backend, background=None, cancel=None):
    """Return an admitted source and retained real-owner verification evidence.

    The observed backend identity must come from the renderer caller, rather
    than silently trusting the result's own backend claim. Verification checks
    snapshot/request closure and every image through the canonical owner.
    """
    if cancel is not None and cancel():
        raise ValueError('silent source preparation cancelled')
    resolution = request.get('settings', {}).get('resolution') if isinstance(request, dict) else None
    if (not isinstance(resolution, list) or len(resolution) != 2 or
            any(type(value) is not int or not 2 <= value <= 4096 or value % 2 for value in resolution) or
            resolution[0] * resolution[1] > video.MAX_PIXELS):
        raise ValueError('native source dimensions exceed silent export limits')
    try:
        from render3d_verify import verify_result
    except ImportError as exc:
        raise ValueError('canonical Render3D verifier is unavailable') from exc
    request, result = copy.deepcopy(request), copy.deepcopy(result)
    verification = verify_result(request, result, render_root,
                                 snapshot_root=snapshot_root, expected_backend=expected_backend)
    if cancel is not None and cancel():
        raise ValueError('silent source preparation cancelled')
    output = request['settings']['output']
    if output['format'] != 'png' or output['bit_depth'] != 8 or output['color_space'] != 'srgb':
        raise ValueError('silent SDR adapter requires actual sRGB PNG8; EXR/HDR needs an explicit separate transform')
    if background is not None:
        # Validate even for an empty/malformed inventory before publication.
        probe = composite_srgb(Image.new('RGBA', (1, 1)), background)
        probe.close()
    combined = sorted((item for item in result['images'] if item['pass_name'] == 'combined'), key=lambda item: item['frame'])
    if not combined or len(combined) > video.MAX_FRAMES:
        raise ValueError('complete bounded combined image inventory required')
    destination = video._destination(destination)
    with video._pin_directory(destination.parent) as parent_fd:
        with tempfile.TemporaryDirectory(prefix='.silent-render3d-', dir=Path('/proc/self/fd') / str(parent_fd)) as tmp:
            stage = Path(tmp)
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
            (stage / 'source.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
            if cancel is not None and cancel():
                raise ValueError('silent source preparation cancelled before publication')
            _rename_no_replace(stage.name, destination.name, source_dir_fd=parent_fd, destination_dir_fd=parent_fd)
    return report
