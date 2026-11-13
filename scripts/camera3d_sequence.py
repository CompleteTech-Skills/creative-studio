"""Exact Camera3D editorial sampling and linear-light PNG transitions.

This module never changes the legacy production v1 assembler. Its input is the
existing hash-verified Camera3D registry; rendered frame providers remain owned
by native render backends. It combines actual returned pixels, not camera poses.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import tempfile

from camera3d_contracts import canonical_bytes, load_registry, validate

MAX_PIXELS = 1_048_576


def _get(documents, reference, kind=None):
    key = reference['kind'], reference['id'], reference['version']
    try:
        value = documents[key]
    except KeyError as exc:
        raise ValueError('camera3d unresolved exact reference: ' + str(key)) from exc
    validate(value, kind)
    if (value['kind'], value['id'], value['version']) != key:
        raise ValueError('camera3d document identity differs from its reference')
    return value


@dataclass(frozen=True)
class FrameSource:
    shot: dict
    local_frame: int


class CameraSequence:
    """Immutable editorial plan; random sampling has no playback history."""

    def __init__(self, storyboard, documents, *, catalog=None):
        validate(storyboard, 'storyboard')
        if catalog is not None:
            validate(catalog, 'catalog')
        self.storyboard = deepcopy(storyboard)
        self._shots = []
        self._starts = []
        end = 0
        previous_duration = 0
        previous_overlap = 0
        seen = set()
        for pointer in storyboard['shots']:
            key = pointer['id'], pointer['version']
            if key in seen:
                raise ValueError('camera3d sequence repeats a shot')
            seen.add(key)
            shot = _get(documents, pointer, 'shot')['shot']
            if catalog is not None and shot['render_mode'] == '3d':
                binding = _get(documents, shot['binding'], 'binding')
                if binding['camera'] not in catalog['enabled']:
                    raise ValueError('camera3d shot camera version is disabled or absent from the catalog')
            transition = _get(documents, shot['transition'], 'transition')
            overlap = transition['duration_frames']
            if (shot['fps'] != storyboard['fps'] or shot['start_frame'] != end - overlap
                    or overlap > min(previous_duration, shot['duration_frames'])):
                raise ValueError('camera3d sequence has inconsistent exact overlap timing')
            if previous_overlap + overlap > previous_duration:
                raise ValueError('camera3d sequence forbids ambiguous three-shot overlap')
            self._shots.append((deepcopy(pointer), deepcopy(shot), deepcopy(transition)))
            self._starts.append(shot['start_frame'])
            end = shot['start_frame'] + shot['duration_frames']
            previous_duration, previous_overlap = shot['duration_frames'], overlap
        if end != storyboard['total_frames']:
            raise ValueError('camera3d sequence total differs from its storyboard')
        self.total_frames = end
        self.fps = deepcopy(storyboard['fps'])
        self.sha256 = hashlib.sha256(canonical_bytes(dict(storyboard=self.storyboard, shots=self._shots))).hexdigest()

    @classmethod
    def load(cls, root, registry, storyboard_reference, *, catalog_reference=None):
        """Resolve all exact source bytes through the authoritative registry loader."""
        documents = load_registry(root, registry)
        catalog = None if catalog_reference is None else _get(documents, catalog_reference, 'catalog')
        return cls(_get(documents, storyboard_reference, 'storyboard'), documents, catalog=catalog)

    def sample(self, frame):
        if type(frame) is not int or not 0 <= frame < self.total_frames:
            raise ValueError('camera3d display frame must be within the half-open sequence')
        index = bisect_right(self._starts, frame) - 1
        pointer, shot, transition = self._shots[index]
        local = frame - shot['start_frame']
        sources = [FrameSource(deepcopy(pointer), local)]
        overlap = transition['duration_frames']
        if index and local < overlap:
            prior_pointer, prior_shot, _ = self._shots[index - 1]
            sources.insert(0, FrameSource(deepcopy(prior_pointer), frame - prior_shot['start_frame']))
        return dict(frame=frame, sources=sources,
                    transition=deepcopy(transition) if len(sources) == 2 else None,
                    transition_frame=local if len(sources) == 2 else None)

    def composite(self, frame, frame_provider, *, max_pixels=MAX_PIXELS):
        """Call provider(shot_reference, local_frame) for each exact rendered view."""
        sample = self.sample(frame)
        images = [frame_provider(source.shot, source.local_frame) for source in sample['sources']]
        if len(images) == 1:
            _image(images[0], max_pixels)
            return images[0].copy()
        return composite_transition(images[0], images[1], sample['transition'],
                                    sample['transition_frame'], max_pixels=max_pixels)


def transition_progress(transition, frame):
    validate(transition, 'transition')
    count = transition['duration_frames']
    if count == 0 or type(frame) is not int or not 0 <= frame < count:
        raise ValueError('camera3d transition frame must be within a positive overlap')
    progress = Fraction(2 * frame + 1, 2 * count)
    if transition['curve'] == 'smoothstep':
        progress = progress * progress * (3 - 2 * progress)
    return progress


def _image(image, max_pixels):
    from PIL import Image
    if type(max_pixels) is not int or not 1 <= max_pixels <= 16_777_216:
        raise ValueError('camera3d pixel budget must be an integer in 1..16777216')
    if not isinstance(image, Image.Image) or image.mode != 'RGBA':
        raise ValueError('camera3d transition needs decoded straight RGBA8 images')
    if min(image.size) < 1 or image.width * image.height > max_pixels:
        raise ValueError('camera3d transition exceeds its pixel budget')


def _decode_srgb(value):
    import numpy as np
    return np.where(value <= .04045, value / 12.92, ((value + .055) / 1.055) ** 2.4)


def _encode_srgb(value):
    import numpy as np
    return np.where(value <= .0031308, 12.92 * value, 1.055 * np.maximum(value, 0) ** (1 / 2.4) - .055)


def composite_transition(outgoing, incoming, transition, frame, *, max_pixels=MAX_PIXELS):
    """Blend two actual sRGB straight-RGBA8 views in linear premultiplied light.

    Transition samples use pixel/temporal centers. A one-frame overlap is its
    midpoint; no duplicated endpoint or terminal frame is introduced. Wipe
    direction names the entering edge. Fades travel via the configured straight
    sRGB solid color, preserving its alpha rather than inventing opaque black.
    """
    import numpy as np
    from PIL import Image
    _image(outgoing, max_pixels); _image(incoming, max_pixels)
    if outgoing.size != incoming.size:
        raise ValueError('camera3d transition view dimensions must match')
    progress = float(transition_progress(transition, frame))
    arrays = [np.asarray(image, dtype=np.float32) / 255 for image in (outgoing, incoming)]
    for array in arrays:
        array[..., :3] = _decode_srgb(array[..., :3]) * array[..., 3:4]
    left, right = arrays
    if transition['type'] == 'fade':
        solid = np.asarray(transition['color_rgba'], dtype=np.float32)
        solid[:3] = _decode_srgb(solid[:3]) * solid[3]
        if progress <= .5:
            result = left * (1 - 2 * progress) + solid * (2 * progress)
        else:
            result = solid * (2 - 2 * progress) + right * (2 * progress - 1)
    elif transition['type'] == 'wipe':
        if transition['direction'] in ('left', 'right'):
            centers = (np.arange(outgoing.width, dtype=np.float32) + .5) / outgoing.width
            if transition['direction'] == 'right':
                centers = 1 - centers
            mask = (centers < progress)[None, :, None]
        else:
            centers = (np.arange(outgoing.height, dtype=np.float32) + .5) / outgoing.height
            if transition['direction'] == 'down':
                centers = 1 - centers
            mask = (centers < progress)[:, None, None]
        result = np.where(mask, right, left)
    else:
        result = left * (1 - progress) + right * progress
    alpha = result[..., 3:4]
    rgb = np.divide(result[..., :3], alpha, out=np.zeros_like(result[..., :3]), where=alpha > 0)
    result = np.concatenate([_encode_srgb(rgb), alpha], axis=-1)
    return Image.fromarray(np.rint(np.clip(result, 0, 1) * 255).astype(np.uint8), 'RGBA')


def read_rendered_png(path, *, max_pixels=MAX_PIXELS):
    """Decode a bounded regular PNG snapshot, preserving its exact source hash."""
    from PIL import Image
    from asset_library import read_source_bytes
    data = read_source_bytes(path, 64 * 1024 * 1024)
    if not data.endswith(b'IEND\xaeB`\x82'):
        raise ValueError('camera3d rendered PNG has no exact end marker')
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'PNG' or image.width * image.height > max_pixels:
            raise ValueError('camera3d rendered input format or pixel budget is invalid')
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        result = image.convert('RGBA')
        result.load()
    _image(result, max_pixels)
    return result, hashlib.sha256(data).hexdigest()


def write_transition_preview(outgoing_path, incoming_path, destination, *, duration_frames=8):
    """Publish an exact, inspectable PNG preview from two actual rendered views.

    Sources are supplied explicitly and their bytes are hashed; this function
    does not claim that it rendered geometry. The renderer owns that provenance.
    Existing destinations survive even a racing writer through exclusive rename.
    """
    from atomic_publish import _rename_no_replace
    from PIL import Image
    import hatch
    if type(duration_frames) is not int or not 1 <= duration_frames <= 32:
        raise ValueError('camera3d preview duration must be 1..32 frames')
    outgoing, outgoing_sha = read_rendered_png(outgoing_path)
    incoming, incoming_sha = read_rendered_png(incoming_path)
    if outgoing.size != incoming.size:
        raise ValueError('camera3d rendered input dimensions must match')
    if outgoing.width * outgoing.height * duration_frames * 3 > 64 * 1024 * 1024:
        raise ValueError('camera3d preview total pixel budget exceeded')
    destination = Path(destination).absolute()
    if any(parent.is_symlink() for parent in (destination, *destination.parents)):
        raise ValueError('camera3d preview destination cannot pass through a symlink')
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('camera3d preview needs an existing parent and a new destination')
    with tempfile.TemporaryDirectory(prefix='.camera3d-preview-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'payload'; stage.mkdir()
        records = []
        thumbnails = []
        for kind in ('dissolve', 'fade', 'wipe'):
            transition = dict(schema_version=1, kind='camera3d-transition', id=kind, version='1.0.0',
                              type=kind, duration_frames=duration_frames, overlap='tail-head',
                              color_rgba=[0, 0, 0, 1], direction='left', curve='linear')
            for frame in range(duration_frames):
                image = composite_transition(outgoing, incoming, transition, frame)
                path = stage / f'{kind}-{frame:03d}.png'
                image.save(path, format='PNG')
                records.append(dict(path=path.name, transition=kind, frame=frame,
                                    sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
                if frame in (0, duration_frames // 2, duration_frames - 1):
                    thumbnail = image.copy(); thumbnail.thumbnail((256, 256))
                    thumbnails.append(thumbnail)
        sheet = Image.new('RGBA', (256 * 3, 256 * 3), (64, 64, 64, 255))
        for index, image in enumerate(thumbnails[:9]):
            sheet.alpha_composite(image, ((index % 3) * 256, (index // 3) * 256))
        sheet_path = stage / 'contact-sheet.png'; sheet.save(sheet_path)
        manifest = dict(schema_version=1, kind='camera3d-transition-preview',
                        color='linear-srgb premultiplied blending; straight sRGB RGBA8 PNG output',
                        algorithm='camera3d-transition-v1', geometry_rendered_here=False,
                        source_sha256=[outgoing_sha, incoming_sha], source_dimensions=list(outgoing.size),
                        duration_frames=duration_frames, frames=records,
                        contact_sheet_sha256=hashlib.sha256(sheet_path.read_bytes()).hexdigest())
        (stage / 'manifest.json').write_bytes(canonical_bytes(manifest))
        with hatch.ownership(destination, resolve_leaf=False):
            _rename_no_replace(stage, destination)
    return manifest


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outgoing', required=True, type=Path)
    parser.add_argument('--incoming', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--duration-frames', type=int, default=8)
    args = parser.parse_args()
    try:
        result = write_transition_preview(args.outgoing, args.incoming, args.destination,
                                          duration_frames=args.duration_frames)
    except (ValueError, OSError) as exc:
        print(json.dumps(dict(ok=False, error=str(exc))))
        return 2
    print(json.dumps(dict(ok=True, destination=str(args.destination), manifest=result)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
