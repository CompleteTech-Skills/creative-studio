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
import re
import subprocess
import sys
import time
from PIL import Image, ImageDraw

import decomposition_package as package
from asset_contracts import MAX_SOURCE_BYTES
from atomic_publish import pinned_directory, private_stage, private_stage_supported, _rename_no_replace
from character_decomposition import (
    create_decomposition, load_decomposition, read_json as read_run_json, save_manifest,
)
from decomposition_analysis import (
    MAX_RESPONSE_BYTES, _parse_record_json, _record_bytes, _record_json as read_json,
)
from decomposition_masks import MAX_TOTAL_RUNS, binary, count, intersect, mask_to_runs, union
from decomposition_spec import (ASSET, BOOLEAN, CONFIDENCE, HASH, TEXT, array,
                                obj, safe_path, validate_structure)
from extract_strip_frames import atomic_json
from rig_errors import directory_capability, normalize_root

MODEL_NAME = 'maskrcnn_resnet50_fpn_coco-bf2d0c1e.pth'
MODEL_SHA256 = 'bf2d0c1efbc936eeee2bc95a48e80ebc86b891f61b0106485937fc29f9315fc0'
MODEL_URL = 'https://download.pytorch.org/models/' + MODEL_NAME
MAX_CANDIDATES = 32
MAX_PIXELS = 1_500_000
VERSIONS = {'torch': '2.10.0', 'torchvision': '0.25.0'}
IMAGE_SUFFIXES = {'.png', '.webp', '.jpg', '.jpeg'}
RECEIPT_NAME = 'foreground-proposals.json'
RETAINED_DIRECTORY = 'foreground-analysis'
RETAINED_RECEIPT = f'{RETAINED_DIRECTORY}/{RECEIPT_NAME}'
OVERLAP_NOTE = 'Selected pixels shared with another candidate; not owned by this selection.'

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


def _verified_image(path, expected_hash=None, *, png_only=False):
    """Hash and decode the same bounded admitted bytes, never reopening a path."""
    path = Path(path)
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError('unsupported foreground image type')
    data = _record_bytes(path, MAX_SOURCE_BYTES)
    if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError('proposal artifact hash mismatch: ' + str(path))
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('PNG', 'WEBP', 'JPEG') or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('foreground requires a single PNG/WebP/JPEG image')
            if png_only and (path.suffix != '.png' or image.format != 'PNG'):
                raise ValueError('foreground artifact must be encoded as PNG')
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
    if not private_stage_supported():
        reasons.append('publication requires Linux descriptor and procfs support')
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
    image = _verified_image(path, descriptor['sha256'], png_only=True)
    if image.mode != 'L' or image.size != size or any(image.histogram()[1:255]):
        raise ValueError('proposal masks must be binary source-sized grayscale PNGs')
    return image



def _receipt_assets(receipt):
    # Fixed role names also keep every path distinct under transport case folding.
    validate_structure(receipt, RECEIPT, 'foreground proposal receipt')
    source = receipt['source']['asset']
    if source['path'] not in {'source/original' + suffix for suffix in IMAGE_SUFFIXES}:
        raise ValueError('foreground receipt has a noncanonical source path')
    assets = [source]
    for index, candidate in enumerate(receipt['candidates'], 1):
        identifier = f'candidate-{index}'
        if candidate['id'] != identifier:
            raise ValueError('invalid or duplicate candidate identity')
        if candidate['mask']['path'] != f'masks/{identifier}.png':
            raise ValueError('foreground receipt has a noncanonical candidate mask path')
        assets.append(candidate['mask'])
    for role, expected in (('overlap', 'masks/overlap.png'), ('overlay', 'overlay.png')):
        if receipt[role]['path'] != expected:
            raise ValueError(f'foreground receipt has a noncanonical {role} path')
        assets.append(receipt[role])
    return assets


def load(root):
    root = normalize_root(root)
    receipt_bytes = _record_bytes(safe_path(root, RECEIPT_NAME), MAX_RESPONSE_BYTES)
    return _validate_receipt(root, receipt_bytes)


def _validate_receipt(root, receipt_bytes):
    receipt = _parse_record_json(receipt_bytes)
    _receipt_assets(receipt)
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
    for candidate in receipt['candidates']:
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
    overlay = _verified_image(safe_path(root, receipt['overlay']['path']), receipt['overlay']['sha256'], png_only=True)
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
    # Inference alone uses an opaque white matte, so hidden RGB cannot influence
    # detections and fractional alpha contributes its visible composited color.
    rgb = Image.alpha_composite(Image.new('RGBA', rgba.size, (255, 255, 255, 255)), rgba).convert('RGB')
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


def _selected_geometry(root, receipt, identifiers):
    by_id = {candidate['id']: candidate for candidate in receipt['candidates']}
    if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= MAX_CANDIDATES or
            any(not isinstance(item, str) for item in identifiers) or len(identifiers) != len(set(identifiers))):
        raise ValueError('select one or more unique candidate IDs')
    if any(identifier not in by_id for identifier in identifiers):
        raise ValueError('unknown foreground candidate')
    source = receipt['source']
    size = (source['width'], source['height'])
    selected = Image.new('L', size)
    for identifier in identifiers:
        selected = union(selected, _mask(root, by_id[identifier]['mask'], size))
    return selected, intersect(selected, _mask(root, receipt['overlap'], size))


def _ambiguity(overlap, descriptor):
    return {'status': 'unresolved', 'pixels': count(overlap), 'runs': mask_to_runs(overlap),
            'overlap_mask': {'path': f"{RETAINED_DIRECTORY}/{descriptor['path']}",
                             'sha256': descriptor['sha256']},
            'note': OVERLAP_NOTE}


SELECTION = obj({'receipt': {'const': RETAINED_RECEIPT}, 'receipt_sha256': HASH,
                 'selected_ids': array(TEXT, MAX_CANDIDATES, 1), 'selected_mask': ASSET,
                 'ambiguous_overlap': obj({'status': {'const': 'unresolved'},
                     'pixels': {'type': 'integer', 'minimum': 0, 'maximum': MAX_PIXELS},
                     'runs': array(array({'type': 'integer', 'minimum': 0}, 3, 3), MAX_TOTAL_RUNS),
                     'overlap_mask': ASSET, 'note': {'const': OVERLAP_NOTE}})})


def verify_selection(root, manifest, annotation):
    """Verify the retained instance decision separately from current semantic review."""
    missing = object()
    record = annotation.get('extensions', {}).get('foreground_analysis', missing)
    manifest_record = manifest.get('extensions', {}).get('foreground_analysis', missing)
    retained = root / RETAINED_DIRECTORY
    if record is missing and manifest_record is missing and not (retained.exists() or retained.is_symlink()):
        return
    if record is missing or manifest_record is missing or record != manifest_record:
        raise ValueError('foreground selection lineage disagrees with canonical annotation')
    validate_structure(record, SELECTION, 'foreground selection')
    receipt_path = safe_path(root, RETAINED_RECEIPT)
    receipt_bytes = _record_bytes(receipt_path, MAX_RESPONSE_BYTES)
    if hashlib.sha256(receipt_bytes).hexdigest() != record['receipt_sha256']:
        raise ValueError('foreground receipt digest differs from the canonical selection')
    receipt = _validate_receipt(retained, receipt_bytes)
    source = receipt['source']
    if (source['asset']['sha256'] != manifest['source']['sha256'] or
            (source['width'], source['height']) != (manifest['canvas']['width'], manifest['canvas']['height']) or
            any(source[key] != manifest['source'][key] for key in ('origin', 'fixture'))):
        raise ValueError('foreground receipt source disagrees with canonical source')
    selected, ambiguous = _selected_geometry(retained, receipt, record['selected_ids'])
    selected_mask = record['selected_mask']
    path = selected_mask['path']
    if not re.fullmatch(r'masks/foreground-[0-9a-f]{24}\.png', path):
        raise ValueError('foreground selection mask must be a canonical foreground artifact')
    if _mask(root, selected_mask, selected.size).tobytes() != selected.tobytes():
        raise ValueError('foreground selection geometry disagrees with retained candidates')
    if (manifest['revision'] == 1 or manifest['foreground']['method'] != 'manually-corrected-mask') and (
            manifest['foreground']['mask'] != selected_mask):
        raise ValueError('canonical foreground mask disagrees with retained selection')
    if _ambiguity(ambiguous, receipt['overlap']) != record['ambiguous_overlap']:
        raise ValueError('foreground overlap provenance disagrees with retained candidates')


def _capture_snapshot(root, snapshot):
    receipt_bytes = _record_bytes(safe_path(root, RECEIPT_NAME), min(MAX_RESPONSE_BYTES, package.MAX_PACKAGE_BYTES))
    assets = _receipt_assets(_parse_record_json(receipt_bytes))
    remaining = package.MAX_PACKAGE_BYTES - len(receipt_bytes)
    for asset in assets:
        if remaining <= 0:
            raise ValueError('foreground snapshot exceeds decomposition package size bound')
        relative = asset['path']
        try:
            data = _record_bytes(safe_path(root, relative), min(MAX_SOURCE_BYTES, remaining))
        except ValueError as exc:
            if remaining < MAX_SOURCE_BYTES:
                raise ValueError('foreground artifact exceeds remaining package budget or cannot be read safely') from exc
            raise
        if hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('proposal artifact hash mismatch: ' + relative)
        target = safe_path(snapshot, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        remaining -= len(data)
    (snapshot / RECEIPT_NAME).write_bytes(receipt_bytes)
    return _validate_receipt(snapshot, receipt_bytes), hashlib.sha256(receipt_bytes).hexdigest()


def select(root, identifiers, destination, *, name=None):
    """Explicitly select candidate union, preserving alpha and unassigned semantics."""
    root = Path(root).resolve()
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('decomposition destination must be new')
    with pinned_directory(destination.parent) as parent_fd, private_stage(parent_fd, prefix='.selection-') as (_, stage_fd), directory_capability(stage_fd) as capability:
        stage = capability.path
        snapshot = stage / 'snapshot'
        snapshot.mkdir()
        receipt, receipt_hash = _capture_snapshot(root, snapshot)
        selected, ambiguous = _selected_geometry(snapshot, receipt, identifiers)
        source = receipt['source']
        ambiguous_pixels = count(ambiguous)
        annotation = {'schema_version': 1, 'source_sha256': source['asset']['sha256'],
            'author': 'Explicit foreground candidate selection',
            'note': 'Only foreground selected; semantic components remain unassigned.' + (
                f' {ambiguous_pixels} selected pixels overlap other candidates and remain ambiguous.'
                if ambiguous_pixels else ''),
            'components': []}
        run = stage / 'run'
        result = create_decomposition(safe_path(snapshot, source['asset']['path']), run,
            annotations=annotation, foreground_mask={'runs': mask_to_runs(selected)},
            name=name, origin=source['origin'], fixture=source['fixture'])
        # Build the ordinary run first, then attach and verify lineage while
        # the complete run is still inside the pinned private staging directory.
        snapshot.rename(run / RETAINED_DIRECTORY)
        record = {'receipt': RETAINED_RECEIPT, 'receipt_sha256': receipt_hash,
                  'selected_ids': identifiers, 'selected_mask': result['foreground']['mask'],
                  'ambiguous_overlap': _ambiguity(ambiguous, receipt['overlap'])}
        annotation_path = safe_path(run, result['annotation']['path'])
        canonical = read_run_json(annotation_path)
        canonical['extensions']['foreground_analysis'] = record
        atomic_json(annotation_path, canonical)
        result['annotation']['sha256'] = _safe_sha(annotation_path)
        result['extensions']['foreground_analysis'] = record
        save_manifest(run, result, transactional=False)
        package._inventory(run)
        result = load_decomposition(run)
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
