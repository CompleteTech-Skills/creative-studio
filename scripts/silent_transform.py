"""Explicit bounded raster/FPS conversion for admitted silent-video PNG sources."""
from __future__ import annotations
import copy
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import shutil

from PIL import Image, __version__ as PILLOW_VERSION
from atomic_publish import _rename_no_replace, private_stage
from production_contracts import MAX_MANIFEST_BYTES, read_json
import silent_video as video


def _ratio(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


def _round(value):
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def profile(width, height, fps, *, fit='preserve', fps_policy='preserve', background=None):
    return validate_profile({'schema_version': 1, 'kind': 'silent-frame-profile',
        'width': width, 'height': height, 'fps': copy.deepcopy(fps), 'fit': fit,
        'fps_policy': fps_policy, 'background': [0, 0, 0] if background is None else copy.deepcopy(background)})


def profile_schema():
    return copy.deepcopy(video._obj({'schema_version': {'const': 1}, 'kind': {'const': 'silent-frame-profile'},
        'width': video._int(2, 4096), 'height': video._int(2, 4096), 'fps': video.FPS,
        'fit': {'enum': ['preserve', 'letterbox', 'fill', 'stretch']},
        'fps_policy': {'enum': ['preserve', 'nearest']},
        'background': {'type': 'array', 'minItems': 3, 'maxItems': 3, 'items': video._int(0, 255)}}))


def validate_profile(value):
    video._structural(value, profile_schema(), 'silent frame profile')
    video.fps_fraction(value['fps'])
    if value['width'] % 2 or value['height'] % 2 or value['width'] * value['height'] > video.MAX_PIXELS:
        raise ValueError('resolution exceeds even-dimension yuv420p limits')
    if value['fit'] != 'letterbox' and value['background'] != [0, 0, 0]:
        raise ValueError('background is meaningful only for explicit letterbox')
    return copy.deepcopy(value)


def schema():
    ratio = video._obj({'numerator': video._int(-10**12, 10**12), 'denominator': video._int(1, 10**12)})
    return copy.deepcopy(video._obj({'schema_version': {'const': 1}, 'kind': {'const': 'silent-frame-transform'},
        'input_source': video.SOURCE, 'input_source_sha256': video.SHA, 'profile': profile_schema(), 'profile_sha256': video.SHA,
        'source': video.SOURCE, 'mapping': {'type': 'array', 'minItems': 1, 'maxItems': video.MAX_FRAMES,
            'items': video._obj({'index': video._int(0, video.MAX_FRAMES - 1),
                'source_index': video._int(0, video.MAX_FRAMES - 1), 'pts': ratio})},
        'input_duration': ratio, 'output_duration': ratio, 'duration_delta': ratio,
        'implementation': video._obj({'id': {'const': 'pillow-lanczos-center'},
            'pillow_version': {'type': 'string', 'minLength': 1, 'maxLength': 64}, 'code_sha256': video.SHA}),
        'visual_review': {'const': 'not-performed'}}))


def plan(source, settings):
    source = video.validate_source(source)
    settings = validate_profile(settings)
    if settings['fit'] == 'preserve' and (source['width'], source['height']) != (settings['width'], settings['height']):
        raise ValueError('preserve policy requires identical source/output dimensions')
    source_fps, target_fps = video.fps_fraction(source['fps']), video.fps_fraction(settings['fps'])
    if settings['fps_policy'] == 'preserve' and source_fps != target_fps:
        raise ValueError('FPS changes require explicit nearest mapping policy')
    count = _round(Fraction(len(source['frames']), 1) * target_fps / source_fps)
    if not 1 <= count <= video.MAX_FRAMES:
        raise ValueError('requested FPS yields an empty or excessive output inventory')
    mapping = [{'index': index, 'source_index': min(len(source['frames']) - 1, _round(Fraction(index, 1) * source_fps / target_fps)),
                'pts': _ratio(Fraction(index, 1) / target_fps)} for index in range(count)]
    return mapping


def _raster(image, settings):
    size = (settings['width'], settings['height'])
    policy = settings['fit']
    if policy == 'preserve':
        return image.copy()
    if policy == 'stretch':
        return image.resize(size, Image.Resampling.LANCZOS)
    width, height = image.size
    if policy == 'letterbox':
        factor = min(Fraction(size[0], width), Fraction(size[1], height))
        fitted = image.resize((max(1, _round(width * factor)), max(1, _round(height * factor))), Image.Resampling.LANCZOS)
        result = Image.new('RGB', size, tuple(settings['background']))
        result.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
        fitted.close()
        return result
    # Crop in source coordinates before resampling; never create an enormous
    # intermediate image for extreme input/output aspect-ratio combinations.
    crop_width = min(Fraction(width), Fraction(height * size[0], size[1]))
    crop_height = min(Fraction(height), Fraction(width * size[1], size[0]))
    left, top = (width - crop_width) / 2, (height - crop_height) / 2
    return image.resize(size, Image.Resampling.LANCZOS,
                        box=tuple(float(v) for v in (left, top, left + crop_width, top + crop_height)))


def transform(source, root, settings, destination, *, cancel=None):
    source, settings = copy.deepcopy(video.validate_source(source)), validate_profile(settings)
    mapping = plan(source, settings)
    estimated = len(mapping) * (settings['width'] * settings['height'] * 4 + 1024)
    if estimated > video.MAX_SOURCE_BYTES:
        raise ValueError('conservative output work-byte estimate exceeds source budget')
    destination = video._destination(destination)
    if shutil.disk_usage(destination.parent).free < estimated + MAX_MANIFEST_BYTES:
        raise ValueError('insufficient free storage for bounded transform')
    video._snapshot(video.make_request(source), root, None)
    with video._pin_directory(destination.parent) as parent_fd:
        with private_stage(parent_fd, prefix='.silent-transform-') as (private_path, wrapper_fd):
            stage = private_path / 'payload'
            stage.mkdir(mode=0o700)
            (stage / 'frames').mkdir()
            inventory, total = [], 0
            for record in mapping:
                if cancel is not None and cancel():
                    raise ValueError('frame transform cancelled')
                original = source['frames'][record['source_index']]
                data = video._read_frame(root, original['path'])
                if len(data) != original['bytes'] or hashlib.sha256(data).hexdigest() != original['sha256']:
                    raise ValueError('source frame changed during transform')
                image = video._image(data, (source['width'], source['height']))
                if settings['fit'] != 'preserve':
                    output = _raster(image, settings)
                    buffer = io.BytesIO(); output.save(buffer, format='PNG'); output.close()
                    data = buffer.getvalue()
                image.close()
                total += len(data)
                if total > video.MAX_SOURCE_BYTES or len(data) > video.MAX_FRAME_BYTES:
                    raise ValueError('transformed output exceeds byte budget')
                name = 'frames/%06d.png' % record['index']
                (stage / name).write_bytes(data)
                inventory.append({'index': record['index'], 'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
            transformed = {**source, 'origin': 'transformed', 'width': settings['width'], 'height': settings['height'],
                           'fps': settings['fps'], 'frames': inventory}
            video.validate_source(transformed)
            video._snapshot(video.make_request(source), root, None)
            video._snapshot(video.make_request(transformed), stage, None)
            input_duration = Fraction(len(source['frames']), 1) / video.fps_fraction(source['fps'])
            output_duration = Fraction(len(mapping), 1) / video.fps_fraction(settings['fps'])
            report = {'schema_version': 1, 'kind': 'silent-frame-transform', 'input_source': source,
                'input_source_sha256': video._digest(source), 'profile': settings, 'profile_sha256': video._digest(settings),
                'source': transformed, 'mapping': mapping, 'input_duration': _ratio(input_duration),
                'output_duration': _ratio(output_duration), 'duration_delta': _ratio(output_duration - input_duration),
                'implementation': {'id': 'pillow-lanczos-center', 'pillow_version': PILLOW_VERSION,
                                   'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
                'visual_review': 'not-performed'}
            encoded = (json.dumps(report, indent=2, allow_nan=False) + '\n').encode()
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise ValueError('transform manifest exceeds byte budget')
            (stage / 'transform.json').write_bytes(encoded)
            if cancel is not None and cancel():
                raise ValueError('frame transform cancelled before publication')
            _rename_no_replace('payload', destination.name, source_dir_fd=wrapper_fd, destination_dir_fd=parent_fd)
    return report


def verify(root, *, source_root=None):
    root = Path(root)
    with video._open_source(root, 'transform.json') as stream:
        report = read_json(Path('/proc/self/fd') / str(stream.fileno()))
    video._structural(report, schema(), 'silent frame transform')
    expected_keys = {'schema_version','kind','input_source','input_source_sha256','profile','profile_sha256','source','mapping',
                     'input_duration','output_duration','duration_delta','implementation','visual_review'}
    if (not isinstance(report, dict) or set(report) != expected_keys or type(report['schema_version']) is not int or
            report['schema_version'] != 1 or report['kind'] != 'silent-frame-transform' or report['visual_review'] != 'not-performed'):
        raise ValueError('invalid frame transform manifest')
    original, settings = video.validate_source(report['input_source']), validate_profile(report['profile'])
    mapping, source = plan(original, settings), video.validate_source(report['source'])
    expected_source = {**original, 'origin':'transformed', 'width':settings['width'], 'height':settings['height'],
                       'fps':settings['fps'], 'frames':source['frames']}
    before = Fraction(len(original['frames']), 1) / video.fps_fraction(original['fps'])
    after = Fraction(len(mapping), 1) / video.fps_fraction(settings['fps'])
    if (report['input_source_sha256'] != video._digest(original) or report['profile_sha256'] != video._digest(settings) or
            video._digest(report['mapping']) != video._digest(mapping) or video._digest(source) != video._digest(expected_source) or
            len(source['frames']) != len(mapping) or report['input_duration'] != _ratio(before) or
            report['output_duration'] != _ratio(after) or report['duration_delta'] != _ratio(after - before)):
        raise ValueError('frame transform source, profile, mapping or duration mismatch')
    if [item['path'] for item in source['frames']] != ['frames/%06d.png' % i for i in range(len(mapping))]:
        raise ValueError('noncanonical transformed frame paths')
    video._snapshot(video.make_request(source), root, None)
    if {path.name for path in (root / 'frames').iterdir()} != {'%06d.png' % i for i in range(len(mapping))}:
        raise ValueError('unexpected transformed frame inventory')
    if source_root is not None:
        video._snapshot(video.make_request(original), source_root, None)
        for record in mapping:
            expected = video._image(video._read_frame(source_root, original['frames'][record['source_index']]['path']))
            transformed = _raster(expected, settings)
            actual = video._image(video._read_frame(root, source['frames'][record['index']]['path']))
            if actual.tobytes() != transformed.tobytes():
                raise ValueError('transformed pixels differ from declared source operation')
            expected.close(); transformed.close(); actual.close()
    return report
