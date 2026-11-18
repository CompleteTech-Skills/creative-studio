"""Optional, source-bound instance foreground proposals; never anatomical approval.

Inference runs in a bounded child process. Model files must already be provisioned;
no import, doctor or inference operation downloads code or weights.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from PIL import Image, ImageDraw
from asset_contracts import MAX_SOURCE_BYTES
from atomic_publish import pinned_directory, private_stage, _rename_no_replace
from rig_errors import directory_capability, normalize_root
from character_decomposition import create_decomposition
from decomposition_analysis import _record_bytes, _record_json as read_json
from decomposition_masks import binary, count, intersect, union, mask_to_runs
from decomposition_spec import (ASSET, BOOLEAN, CONFIDENCE, HASH, NOTE, TEXT, array,
                                obj, safe_path, validate_structure)
from extract_strip_frames import atomic_json

MODEL_NAME = 'maskrcnn_resnet50_fpn_coco-bf2d0c1e.pth'
MODEL_SHA256 = 'bf2d0c1efbc936eeee2bc95a48e80ebc86b891f61b0106485937fc29f9315fc0'
MODEL_URL = 'https://download.pytorch.org/models/' + MODEL_NAME
MAX_CANDIDATES = 32
MAX_PIXELS = 1_500_000
VERSIONS = {'torch': '2.10.0', 'torchvision': '0.25.0'}
CANDIDATE = obj({'id': TEXT, 'category': TEXT, 'confidence': CONFIDENCE,
                 'mask': ASSET, 'bbox': array({'type': 'integer', 'minimum': 0}, 4, 4),
                 'pixels': {'type': 'integer', 'minimum': 1}})
RECEIPT = obj({'schema_version': {'const': 1}, 'kind': {'const': 'foreground-proposals'},
    'source': obj({'asset': ASSET, 'width': {'type': 'integer', 'minimum': 1},
                   'height': {'type': 'integer', 'minimum': 1}, 'origin': TEXT, 'fixture': BOOLEAN}),
    'backend': obj({'name': {'const': 'torchvision-maskrcnn-coco-v1'}, 'weights_sha256': HASH,
                    'torch': TEXT, 'torchvision': TEXT, 'execution': {'const': 'local'}}),
    'parameters': obj({'score_threshold': CONFIDENCE, 'mask_threshold': {'const': .5},
                        'threads': {'enum': [1, 2]}, 'max_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300},
                        'max_pixels': {'const': MAX_PIXELS}}),
    'elapsed_seconds': {'type': 'number', 'minimum': 0},
    'candidates': array(CANDIDATE, MAX_CANDIDATES), 'overlap': ASSET,
    'overlay': ASSET, 'uncertainty': array(TEXT, 16, 1),
    'anatomy_recognized': {'const': False}, 'review_status': {'const': 'pending'}})

# Optional on historical v1 receipts; current workers retain actual runtime counts.
RECEIPT['properties']['runtime'] = obj({'torch_threads': {'enum': [1, 2]},
    'torch_interop_threads': {'const': 1}})


def _safe_sha(path, maximum=MAX_SOURCE_BYTES):
    return hashlib.sha256(_record_bytes(path, maximum)).hexdigest()


def _verified_image(path, expected_hash=None):
    """Hash and decode the same bounded admitted bytes, never reopening a path."""
    path = Path(path)
    if path.suffix.lower() not in ('.png', '.webp', '.jpg', '.jpeg'):
        raise ValueError('unsupported foreground image type')
    data = _record_bytes(path, MAX_SOURCE_BYTES)
    if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError('proposal artifact hash mismatch: ' + str(path))
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('PNG', 'WEBP', 'JPEG') or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('foreground requires a single PNG/WebP/JPEG image')
            if image.width * image.height > MAX_PIXELS:
                raise ValueError('source exceeds foreground pixel bound')
            return image.copy()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('invalid bounded foreground image: ' + str(exc)) from exc


def _weight(path):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 200_000_000:
        raise ValueError('weights must be a regular local file below 200 MB')
    if _safe_sha(path, 200_000_000) != MODEL_SHA256:
        raise ValueError('weights are not the pinned official COCO Mask R-CNN checkpoint')
    return path.resolve()


def capabilities(weights=None):
    """Cheap discovery: no model import, network, or inference."""
    reasons, versions = [], {}
    for name, version in VERSIONS.items():
        try:
            versions[name] = importlib.metadata.version(name)
            if versions[name].split('+')[0] != version:
                reasons.append(f'{name} {version} required, found {versions[name]}')
        except importlib.metadata.PackageNotFoundError:
            reasons.append(name + ' is not installed')
    if weights is None:
        reasons.append('supply explicitly provisioned local weights')
    else:
        try:
            _weight(weights)
        except (ValueError, OSError) as exc:
            reasons.append(str(exc))
    return {'available': not reasons, 'backend': 'torchvision-maskrcnn-coco-v1',
            'versions': versions, 'unavailable_reasons': reasons,
            'categories': 'COCO object categories only; no body parts, fantasy or robot taxonomy',
            'network_calls': False, 'anatomy_recognized': False}


def _asset(root, relative):
    return {'path': relative, 'sha256': _safe_sha(safe_path(root, relative))}


def _mask(root, descriptor, size):
    path = safe_path(root, descriptor['path'])
    image = _verified_image(path, descriptor['sha256'])
    if image.mode != 'L' or image.size != size or any(image.histogram()[1:255]):
        raise ValueError('proposal masks must be binary source-sized grayscale PNGs')
    return image



def load(root):
    root = normalize_root(root)
    receipt = read_json(safe_path(root, 'foreground-proposals.json'))
    validate_structure(receipt, RECEIPT, 'foreground proposal receipt')
    if not .05 <= receipt['parameters']['score_threshold'] <= .99:
        raise ValueError('invalid recorded score threshold')
    if receipt['elapsed_seconds'] > receipt['parameters']['max_seconds'] + 5:
        raise ValueError('recorded inference exceeds its runtime budget')
    if 'runtime' in receipt and receipt['runtime']['torch_threads'] != receipt['parameters']['threads']:
        raise ValueError('actual worker thread count differs from recorded parameters')
    source = receipt['source']; size = (source['width'], source['height'])
    if size[0] * size[1] > MAX_PIXELS:
        raise ValueError('source exceeds foreground resource limit')
    if receipt['backend']['weights_sha256'] != MODEL_SHA256:
        raise ValueError('unrecognized foreground model identity')
    for name, expected in VERSIONS.items():
        if receipt['backend'][name].split('+')[0] != expected:
            raise ValueError('unrecognized foreground runtime identity')
    source_path = safe_path(root, source['asset']['path'])
    image = _verified_image(source_path, source['asset']['sha256'])
    if image.size != size:
        raise ValueError('source size mismatch')
    support = binary(image.convert('RGBA').getchannel('A'))
    occupied = Image.new('L', size); overlaps = Image.new('L', size)
    ids = set()
    for index, candidate in enumerate(receipt['candidates'], 1):
        if candidate['id'] != f'candidate-{index}' or candidate['id'] in ids:
            raise ValueError('invalid or duplicate candidate identity')
        ids.add(candidate['id'])
        if candidate['confidence'] < receipt['parameters']['score_threshold']:
            raise ValueError('candidate below recorded score threshold')
        mask = _mask(root, candidate['mask'], size)
        if count(intersect(mask, support)) != count(mask):
            raise ValueError('candidate extends outside source alpha')
        if candidate['pixels'] != count(mask) or candidate['bbox'] != list(mask.getbbox() or []):
            raise ValueError('candidate geometry disagrees with mask')
        overlaps = union(overlaps, intersect(occupied, mask)); occupied = union(occupied, mask)
    overlap = _mask(root, receipt['overlap'], size)
    if overlap.tobytes() != overlaps.tobytes():
        raise ValueError('overlap mask disagrees with independent candidate masks')
    overlay = _verified_image(safe_path(root, receipt['overlay']['path']), receipt['overlay']['sha256'])
    if overlay.size != size:
        raise ValueError('overlay size mismatch')
    return receipt


def _worker(stage, weights, threshold):
    for variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[variable] = '1'
    import torch
    import torchvision
    from torchvision.models.detection import maskrcnn_resnet50_fpn, MaskRCNN_ResNet50_FPN_Weights
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    source = read_json(stage / 'input.json')
    rgba = _verified_image(stage / source['asset']['path'], source['asset']['sha256']).convert('RGBA')
    rgb = rgba.convert('RGB')
    _weight(weights)
    model = maskrcnn_resnet50_fpn(weights=None, weights_backbone=None,
                                   box_detections_per_img=MAX_CANDIDATES)
    checkpoint = _record_bytes(weights, 200_000_000)
    if hashlib.sha256(checkpoint).hexdigest() != MODEL_SHA256:
        raise ValueError('checkpoint changed before loading')
    model.load_state_dict(torch.load(io.BytesIO(checkpoint), map_location='cpu', weights_only=True))
    del checkpoint
    model.eval()
    tensor = torchvision.transforms.functional.pil_to_tensor(rgb).float() / 255
    with torch.inference_mode():
        prediction = model([tensor])[0]
    support = binary(rgba.getchannel('A'))
    occupied = Image.new('L', rgba.size); overlaps = Image.new('L', rgba.size)
    overlay = rgba.copy()
    candidates = []
    classes = MaskRCNN_ResNet50_FPN_Weights.COCO_V1.meta['categories']
    for score, label, values in zip(prediction['scores'], prediction['labels'], prediction['masks']):
        score = float(score)
        if score < threshold:
            continue
        # Torchvision projects model masks back into the original input dimensions.
        mask = Image.fromarray((values[0].cpu().numpy() >= .5).astype('uint8') * 255)
        if mask.size != rgba.size:
            raise ValueError('backend failed native-source mask projection')
        mask = intersect(mask, support)
        if not count(mask):
            continue
        identifier = f'candidate-{len(candidates)+1}'
        relative = 'masks/' + identifier + '.png'; mask.save(stage / relative)
        box = list(mask.getbbox())
        category = classes[int(label)]
        candidates.append({'id': identifier, 'category': category, 'confidence': score,
                           'mask': _asset(stage, relative), 'bbox': box, 'pixels': count(mask)})
        overlaps = union(overlaps, intersect(occupied, mask)); occupied = union(occupied, mask)
        tint = Image.new('RGBA', rgba.size, (255, 0, 255, 80))
        overlay = Image.alpha_composite(overlay, Image.composite(tint, Image.new('RGBA', rgba.size), mask))
        draw = ImageDraw.Draw(overlay)
        draw.rectangle(box, outline=(255, 0, 255, 255), width=2)
        draw.text((box[0], box[1]), identifier + ': ' + category, fill=(255, 0, 255, 255))
    overlaps.save(stage / 'masks/overlap.png'); overlay.save(stage / 'overlay.png')
    atomic_json(stage / 'output.json', {'candidates': candidates, 'overlap': _asset(stage, 'masks/overlap.png'),
                                        'overlay': _asset(stage, 'overlay.png'),
                                        'runtime': {'torch_threads': torch.get_num_threads(), 'torch_interop_threads': torch.get_num_interop_threads()}})


def propose(source, destination, *, weights, enabled=False, origin, fixture=False,
            score_threshold=.7, max_seconds=120):
    if enabled is not True:
        raise ValueError('foreground inference requires explicit enabled=True (--enable-analysis)')
    if type(score_threshold) not in (float, int) or not math.isfinite(score_threshold) or not .05 <= score_threshold <= .99:
        raise ValueError('score threshold must be finite between .05 and .99')
    if type(max_seconds) is not int or not 1 <= max_seconds <= 300:
        raise ValueError('runtime limit must be an integer between 1 and 300 seconds')
    if type(fixture) is not bool or not isinstance(origin, str) or not origin.strip():
        raise ValueError('provide source attribution and an explicit fixture boolean')
    backend_capability = capabilities(weights)
    if not backend_capability['available']:
        raise ValueError('foreground backend unavailable: ' + '; '.join(backend_capability['unavailable_reasons']))
    weights = _weight(weights)
    source = Path(source).expanduser().resolve()
    width, height = _verified_image(source).size
    if width * height > MAX_PIXELS:
        raise ValueError('source exceeds foreground resource limit')
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('proposal destination must be new')
    try:
        with pinned_directory(destination.parent) as parent_fd, private_stage(parent_fd, prefix='.foreground-') as (_, stage_fd), directory_capability(stage_fd) as capability:
            stage = capability.path / 'payload'
            stage.mkdir()
            (stage / 'source').mkdir(); (stage / 'masks').mkdir()
            source_hash = _safe_sha(source); relative = 'source/original' + source.suffix.lower()
            (stage / relative).write_bytes(_record_bytes(source, MAX_SOURCE_BYTES))
            if _safe_sha(source) != source_hash or _safe_sha(stage / relative) != source_hash:
                raise ValueError('source changed during snapshot')
            source_record = {'asset': _asset(stage, relative), 'width': width, 'height': height,
                             'origin': origin, 'fixture': fixture}
            atomic_json(stage / 'input.json', source_record)
            started = time.monotonic()
            with (stage / 'worker.log').open('wb') as log:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '_worker',
                    str(stage), str(weights), str(score_threshold), str(stage_fd)], stdout=log, stderr=subprocess.STDOUT,
                    timeout=max_seconds, check=False, pass_fds=(stage_fd,))
            if result.returncode:
                raise ValueError('foreground inference failed; no proposal published')
            _weight(weights)
            output = read_json(stage / 'output.json')
            receipt = {'schema_version': 1, 'kind': 'foreground-proposals', 'source': source_record,
                'backend': {'name': 'torchvision-maskrcnn-coco-v1', 'weights_sha256': MODEL_SHA256,
                    'execution': 'local', **backend_capability['versions']},
                'parameters': {'score_threshold': score_threshold, 'mask_threshold': .5, 'threads': 1, 'max_seconds': max_seconds, 'max_pixels': MAX_PIXELS},
                'elapsed_seconds': time.monotonic()-started, **output,
                'uncertainty': ['Object classes are model proposals, not semantic review or anatomy.',
                    'Candidate masks may omit thin, disconnected, cropped or unfamiliar foreground.',
                    'Overlaps remain ambiguous; no candidate owns overlapping pixels automatically.',
                    'Internal model resizing does not alter original pixels or source-sized output masks.'],
                'anatomy_recognized': False, 'review_status': 'pending'}
            atomic_json(stage / 'foreground-proposals.json', receipt)
            load(stage)
            _rename_no_replace('payload', destination.name, source_dir_fd=stage_fd, destination_dir_fd=parent_fd)
            return receipt
    except subprocess.TimeoutExpired as exc:
        raise ValueError('foreground runtime limit reached; manual selection remains available') from exc


def select(root, identifiers, destination, *, name=None):
    """Explicitly select candidate union, preserving alpha and unassigned semantics."""
    root = Path(root).resolve()
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('decomposition destination must be new')
    with pinned_directory(destination.parent) as parent_fd, private_stage(parent_fd, prefix='.selection-') as (_, stage_fd), directory_capability(stage_fd) as capability:
        stage = capability.path
        snapshot = stage / 'snapshot'; snapshot.mkdir()
        receipt_bytes = _record_bytes(safe_path(root, 'foreground-proposals.json'), MAX_SOURCE_BYTES)
        (snapshot / 'foreground-proposals.json').write_bytes(receipt_bytes)
        receipt = read_json(snapshot / 'foreground-proposals.json')
        validate_structure(receipt, RECEIPT, 'foreground proposal receipt')
        source = receipt['source']
        paths = {source['asset']['path'], receipt['overlap']['path'], receipt['overlay']['path']}
        paths.update(c['mask']['path'] for c in receipt['candidates'])
        for relative in sorted(paths):
            target = safe_path(snapshot, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_record_bytes(safe_path(root, relative), MAX_SOURCE_BYTES))
        receipt = load(snapshot)
        if (not isinstance(identifiers, list) or not identifiers or
                any(not isinstance(item, str) for item in identifiers) or len(identifiers) != len(set(identifiers))):
            raise ValueError('select one or more unique candidate IDs')
        by_id = {c['id']: c for c in receipt['candidates']}
        if any(identifier not in by_id for identifier in identifiers):
            raise ValueError('unknown foreground candidate')
        source = receipt['source']; size = (source['width'], source['height'])
        selected = Image.new('L', size)
        for identifier in identifiers:
            selected = union(selected, _mask(snapshot, by_id[identifier]['mask'], size))
        receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
        annotation = {'schema_version': 1, 'source_sha256': source['asset']['sha256'],
            'author': 'Explicit foreground candidate selection',
            'note': 'Only foreground selected; semantic components remain unassigned.', 'components': [],
            'extensions': {'foreground_analysis': {'receipt': 'foreground-analysis/foreground-proposals.json',
                'receipt_sha256': receipt_hash, 'selected_ids': identifiers}}}
        run = stage / 'run'
        result = create_decomposition(safe_path(snapshot, source['asset']['path']), run,
            annotations=annotation, foreground_mask={'runs': mask_to_runs(selected)},
            name=name, origin=source['origin'], fixture=source['fixture'])
        kept = run / 'foreground-analysis'
        snapshot.rename(kept)
        if _safe_sha(kept / 'foreground-proposals.json') != receipt_hash or load(kept) != receipt:
            raise ValueError('proposal snapshot changed during selection')
        verified = Image.new('L', size)
        for identifier in identifiers:
            verified = union(verified, _mask(kept, by_id[identifier]['mask'], size))
        if verified.tobytes() != selected.tobytes():
            raise ValueError('proposal masks changed during selection')
        _rename_no_replace('run', destination.name, source_dir_fd=stage_fd, destination_dir_fd=parent_fd)
        return result



def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == '_worker':
        with directory_capability(int(args[4])) as capability:
            _worker(capability.path / 'payload', Path(args[2]), float(args[3]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    doctor = sub.add_parser('doctor'); doctor.add_argument('--weights')
    propose_parser = sub.add_parser('propose')
    propose_parser.add_argument('source'); propose_parser.add_argument('destination')
    propose_parser.add_argument('--weights', required=True); propose_parser.add_argument('--origin', required=True)
    propose_parser.add_argument('--enable-analysis', action='store_true'); propose_parser.add_argument('--fixture', action='store_true')
    propose_parser.add_argument('--score-threshold', type=float, default=.7)
    propose_parser.add_argument('--max-seconds', type=int, default=120)
    choose = sub.add_parser('select'); choose.add_argument('proposals'); choose.add_argument('destination')
    choose.add_argument('--candidate', action='append', required=True); choose.add_argument('--name')
    arguments = parser.parse_args(args)
    try:
        if arguments.command == 'doctor': result = capabilities(arguments.weights)
        elif arguments.command == 'propose':
            result = propose(arguments.source, arguments.destination, weights=arguments.weights,
                enabled=arguments.enable_analysis, origin=arguments.origin, fixture=arguments.fixture,
                score_threshold=arguments.score_threshold, max_seconds=arguments.max_seconds)
        else: result = select(arguments.proposals, arguments.candidate, arguments.destination, name=arguments.name)
        print(json.dumps(result, indent=2)); return 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

if __name__ == '__main__':
    raise SystemExit(main())
