"""Reverse character generation: observed pixels, semantic annotations and review.

This module never calls a model. Optional semantic analysis and hidden artwork
are externally acquired artifacts with explicit receipts, not imaginary Python
model APIs. The automatic fallback preserves unresolved foreground for editing.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat, UnidentifiedImageError

from asset_contracts import check_image, hash_object, sha, validate_identity, MAX_SOURCE_BYTES
from decomposition_masks import (MAX_COMPONENTS, MAX_MASK_PIXELS, MAX_TOTAL_RUNS, apply_mask, binary, count,
    decode_mask, identify_foreground, intersect, mask_to_runs, palette, quality, subtract,
    union, character_candidates)
from decomposition_spec import (ALGORITHM, ANNOTATION_SCHEMA, MANIFEST_SCHEMA, component_signature,
    check_slug, safe_path, validate_relations, validate_structure)
from extraction_annotations import rectangle
from extract_strip_frames import atomic_json

MAX_JSON_BYTES = 64 * 1024 * 1024


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError('decomposition JSON exceeds 64 MiB resource bound')
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate JSON key: ' + key)
            out[key] = value
        return out
    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON number')))
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid decomposition JSON: ' + str(exc)) from exc


def _pending():
    return {'status': 'pending', 'author': '', 'note': '', 'signature': None}


def _image(root, descriptor):
    path = safe_path(root, descriptor['path'])
    check_image(path)
    if sha(path) != descriptor['sha256']:
        raise ValueError('decomposition asset hash mismatch: ' + descriptor['path'])
    with Image.open(path) as image:
        return image.copy()


def _save_image(root, relative, image):
    path = safe_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format='PNG')
    return {'path': relative, 'sha256': sha(path)}


def _content_image(root, folder, identifier, image):
    # Address both size/mode and source bytes; no prior repair output is overwritten.
    content_hash = hash_object({'mode': image.mode, 'size': list(image.size),
        'pixels': __import__('hashlib').sha256(image.tobytes()).hexdigest()})
    relative = f'{folder}/{identifier}-{content_hash[:24]}.png'
    path = safe_path(root, relative)
    if path.exists():
        with Image.open(path) as prior:
            if prior.mode != image.mode or prior.size != image.size or prior.tobytes() != image.tobytes():
                raise ValueError('content-addressed raster conflict: ' + relative)
        return {'path': relative, 'sha256': sha(path)}
    return _save_image(root, relative, image)


def _annotation(value, source_hash, image, base):
    if value is None:
        result = {'schema_version': 1, 'source_sha256': source_hash, 'author': 'Hatch Pet deterministic foreground analysis',
            'note': 'No semantic analyzer was called. Identify and review components using source-bound annotations.',
            'orientation': 'unknown', 'components': [], 'extensions': {}}
    else:
        result = read_json(value) if isinstance(value, (str, Path)) else copy.deepcopy(value)
    validate_structure(result, ANNOTATION_SCHEMA, 'decomposition annotation')
    if result['source_sha256'] != source_hash:
        raise ValueError('annotation must bind the exact original source sha256')
    result.setdefault('orientation', 'unknown')
    result.setdefault('extensions', {})
    validate_relations(result['components'], image.size)
    used_runs = 0
    for index, component in enumerate(result['components']):
        component.setdefault('parent_id', None)
        component.setdefault('z_index', index)
        component.setdefault('side', 'unknown')
        component.setdefault('pivot', None)
        component.setdefault('attachments', [])
        component.setdefault('confidence', .5)
        component.setdefault('occlusion', {'status': 'unknown', 'occluded_by': [], 'note': 'Hidden geometry is not observable in this view.'})
        component.setdefault('notes', [])
        mask = decode_mask(component['mask'], image.size, base, image)
        if not count(mask):
            raise ValueError('component mask is empty: ' + component['id'])
        component['mask'] = {'runs': mask_to_runs(mask, MAX_TOTAL_RUNS - used_runs)}
        used_runs += len(component['mask']['runs'])
    return result


def _selection_mask(size, selected):
    if selected is None:
        return Image.new('L', size, 255)
    rectangle(selected, size)
    mask = Image.new('L', size)
    x0, y0, x1, y1 = selected
    ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    return mask


def _extract(root, source, annotation, foreground_mask, previous=None, corrected=False):
    """Turn proposed semantic masks into disjoint visible source ownership."""
    old = {c['id']: c for c in (previous or {}).get('components', [])}
    occupied = Image.new('L', source.size)
    overlap_mask = Image.new('L', source.size)
    normalized = []
    components = []
    masks = {}
    used_runs = 0
    raw_records = sorted(annotation['components'], key=lambda c: (c['z_index'], c['id']), reverse=True)
    for supplied in raw_records:
        record = copy.deepcopy(supplied)
        proposed = intersect(decode_mask(record['mask'], source.size), foreground_mask)
        shared = intersect(proposed, occupied)
        overlap_mask = union(overlap_mask, shared)
        visible = subtract(proposed, occupied)
        occluded_by = [identifier for identifier, mask in masks.items() if count(intersect(proposed, mask))]
        if not count(visible):
            # Completely hidden components cannot be claimed as extracted observed artwork.
            raise ValueError('component has no supported visible pixels: ' + record['id'] + '; remove the hidden-only proposal or provide visible geometry')
        if count(shared):
            record['occlusion']['status'] = 'partial'
            record['occlusion']['occluded_by'] = sorted(set(record['occlusion']['occluded_by']) | set(occluded_by))
            record['notes'].append(f'{count(shared)} proposed mask pixels overlapped front layers; frontmost ownership applied, review segmentation.')
        record['mask'] = {'runs': mask_to_runs(visible, MAX_TOTAL_RUNS - used_runs)}
        used_runs += len(record['mask']['runs'])
        normalized.append(record)
        masks[record['id']] = visible
        occupied = union(occupied, visible)
    remaining = subtract(foreground_mask, occupied)
    if count(remaining):
        identifier = 'unassigned'
        while identifier in {r['id'] for r in normalized}:
            identifier += '-foreground'
        if len(normalized) >= MAX_COMPONENTS:
            raise ValueError('component limit leaves no room for unassigned foreground; complete mask coverage first')
        normalized.append({'id': identifier, 'name': 'Unassigned foreground', 'category': 'unassigned',
            'parent_id': None, 'z_index': -100000, 'side': 'unknown', 'mask': {'runs': mask_to_runs(remaining, MAX_TOTAL_RUNS - used_runs)},
            'pivot': None, 'attachments': [], 'confidence': 0.0,
            'occlusion': {'status': 'unknown', 'occluded_by': [], 'note': 'Semantic identity has not been assigned.'},
            'notes': ['These are real source pixels awaiting semantic identification, not an identified body part.']})
        masks[identifier] = remaining
    if not normalized:
        raise ValueError('no visible character components')
    if len(normalized) * source.width * source.height > MAX_MASK_PIXELS:
        raise ValueError('decomposition masks exceed working-set bound')
    if sum(len(c['mask']['runs']) for c in normalized) > MAX_TOTAL_RUNS:
        raise ValueError('decomposition aggregate mask-run resource bound exceeded')
    normalized.sort(key=lambda c: (c['z_index'], c['id']))
    for record in normalized:
        visible = masks[record['id']]
        bbox = list(visible.getbbox())
        rgba = apply_mask(source, visible).crop(bbox)
        bucket = re.sub('[^a-z0-9-]+', '-', record['category'].lower()).strip('-')[:64] or 'component'
        try:
            check_slug(bucket)
        except ValueError:
            bucket = 'category-' + bucket
        asset = _content_image(root, 'components/' + bucket, record['id'], rgba)
        mask_asset = _content_image(root, 'masks', record['id'], visible)
        record_signature = hash_object(record)
        provenance = {'kind': 'manually-corrected' if corrected else 'segmented-observed',
            'source_sha256': annotation['source_sha256'], 'annotation_sha256': record_signature,
            'orientation': annotation['orientation'], 'method': ALGORITHM, 'author': annotation['author'], 'note': annotation['note']}
        prior = old.get(record['id'])
        if (prior and prior['provenance']['annotation_sha256'] == record_signature and prior['asset'] == asset and
                prior['mask'] == mask_asset and prior['provenance']['orientation'] == annotation['orientation']):
            provenance = copy.deepcopy(prior['provenance'])
        uncertainties = list(record['notes'])
        if record['confidence'] < .8:
            uncertainties.append('Semantic confidence below 0.8; inspect and correct this component.')
        if record['side'] in ('left', 'right') and annotation['orientation'] == 'unknown':
            uncertainties.append('Anatomical side supplied but character orientation is unknown.')
        if record['occlusion']['status'] != 'visible':
            uncertainties.append('Hidden geometry is incomplete or unknown; faithful extraction contains only visible pixels.')
        if record['pivot'] is None:
            uncertainties.append('No approved pivot; the component is not ready for articulated motion.')
        if count(visible) == (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]):
            uncertainties.append('Mask fills its bounding rectangle; confirm this is an actual rectangular feature, especially for pixel art.')
        if provenance.get('reconstruction'):
            uncertainties.append('Hidden artwork was inferred and needs independent visual review; observed asset remains unchanged.')
        component = {key: copy.deepcopy(record[key]) for key in ('id', 'name', 'category', 'parent_id', 'z_index',
            'side', 'pivot', 'attachments', 'confidence', 'occlusion')}
        component.update(bbox=bbox, asset=asset, mask=mask_asset, uncertainty=uncertainties,
            transform={'position': bbox[:2], 'rotation': 0, 'scale': [1, 1], 'local_origin': bbox[:2]},
            provenance=provenance, review=_pending())
        if prior and component_signature(prior) == component_signature(component):
            component['review'] = copy.deepcopy(prior['review'])
        components.append(component)
    annotation['components'] = normalized
    return components, {'proposal_overlap_pixels': count(overlap_mask),
                        'unassigned_foreground_pixels': count(remaining),
                        'ownership_rule': 'frontmost-z-then-id; source alpha applied once'}


def _aggregate(manifest):
    values = [c['review']['status'] for c in manifest['components']]
    unresolved = any(c['category'] == 'unassigned' for c in manifest['components'])
    foreground_pending = manifest['foreground']['method'] == 'unresolved-complex-background'
    pending = values.count('pending')
    status = 'rejected' if 'rejected' in values else ('pending' if pending or unresolved or foreground_pending or
        not manifest['analysis']['selection_resolved'] else 'approved')
    return {'status': status, 'pending': pending, 'approved': values.count('approved'), 'rejected': values.count('rejected')}


def _rig(components):
    by_id = {c['id']: c for c in components}
    suggestions = {}
    for component in components:
        parent = by_id.get(component['parent_id'])
        if component['pivot'] is None and parent:
            box, pbox = component['bbox'], parent['bbox']
            pc = [(pbox[0] + pbox[2]) / 2, (pbox[1] + pbox[3]) / 2]
            suggestions[component['id']] = {'point': [max(box[0], min(box[2], pc[0])), max(box[1], min(box[3], pc[1]))],
                'confidence': .2, 'method': 'nearest-parent-bounds suggestion; not an anatomical joint detection'}
    return {'animation_ready': False, 'status': 'partial' if any(c['pivot'] is not None for c in components) else 'unrigged',
        'missing_pivots': [c['id'] for c in components if c['pivot'] is None], 'suggested_pivots': suggestions,
        'note': 'Single-view cutout geometry. Animation readiness requires inspected joints, separation, occluded geometry and target-specific motion validation.'}


def recompose(root, manifest=None, inferred=False, visible_ids=None):
    manifest = manifest or load_decomposition(root)
    size = (manifest['canvas']['width'], manifest['canvas']['height'])
    out = Image.new('RGBA', size)
    selected = set(visible_ids) if visible_ids is not None else None
    if selected is not None and selected - {c['id'] for c in manifest['components']}:
        raise ValueError('unknown component in recomposition selection')
    for component in sorted(manifest['components'], key=lambda c: (c['z_index'], c['id'])):
        if selected is not None and component['id'] not in selected:
            continue
        reconstruction = component['provenance'].get('reconstruction')
        if inferred and reconstruction:
            asset = _image(root, reconstruction['asset']).convert('RGBA')
            position = reconstruction['bbox'][:2]
        else:
            asset = _image(root, component['asset']).convert('RGBA')
            position = component['bbox'][:2]
        out.alpha_composite(asset, dest=tuple(position))
    return out


def _validation_signature(manifest):
    return hash_object({'algorithm': ALGORITHM, 'source': manifest['source'], 'canvas': manifest['canvas'],
        'foreground': manifest['foreground'], 'components': [component_signature(c) for c in manifest['components']]})


def validate_decomposition(root, manifest=None, write=True):
    root = Path(root)
    manifest = manifest or load_decomposition(root)
    size = (manifest['canvas']['width'], manifest['canvas']['height'])
    expected = _image(root, manifest['foreground']).convert('RGBA')
    original = _image(root, {'path': manifest['source']['normalized_path'], 'sha256': manifest['source']['normalized_sha256']}).convert('RGBA')
    foreground_mask = _image(root, manifest['foreground']['mask']).convert('L')
    errors, warnings, clipped = [], [], []
    archived_source = safe_path(root, manifest['source']['path'])
    check_image(archived_source)
    with Image.open(archived_source) as archived:
        if (archived.format != manifest['source']['format'] or archived.mode != manifest['source']['original_mode'] or
                archived.size != size or archived.getexif().get(274) != manifest['source']['exif_orientation'] or
                archived.convert('RGBA').tobytes() != original.tobytes()):
            errors.append('Normalized original pixels or metadata differ from the archived source image.')
    annotation = read_json(safe_path(root, manifest['annotation']['path']))
    validate_structure(annotation, ANNOTATION_SCHEMA, 'current annotation')
    annotated = {c['id']: c for c in annotation['components']}
    if (annotation['source_sha256'] != manifest['source']['sha256'] or
            set(annotated) != {c['id'] for c in manifest['components']} or
            annotation.get('orientation') != manifest['character']['orientation']):
        errors.append('Semantic manifest differs from its source-bound canonical annotation.')
    if expected.size != size or original.size != size or foreground_mask.size != size:
        raise ValueError('source/foreground dimensions disagree with decomposition canvas')
    if manifest['quality'] != quality(original, expected, manifest['canvas']['pixel_art']):
        errors.append('Source quality measurements differ from the original image and selected foreground.')
    if manifest['rig'] != _rig(manifest['components']):
        errors.append('Rig summary differs from current component geometry and pivots.')
    if any(foreground_mask.histogram()[1:255]):
        raise ValueError('foreground ownership mask is not binary')
    if apply_mask(original, foreground_mask).tobytes() != expected.tobytes():
        errors.append('Foreground differs from masked original pixels.')
    occupied, overlapping = Image.new('L', size), Image.new('L', size)
    unassigned = 0
    generated_count = 0
    for component in manifest['components']:
        mask = _image(root, component['mask'])
        asset = _image(root, component['asset'])
        box = component['bbox']
        rectangle(box, size)
        record = annotated.get(component['id'])
        semantic_fields = ('id', 'name', 'category', 'parent_id', 'z_index', 'side', 'pivot', 'attachments', 'confidence', 'occlusion')
        if (record is None or any(record.get(key) != component[key] for key in semantic_fields) or
                hash_object(record) != component['provenance']['annotation_sha256']):
            errors.append('Component semantic metadata disagrees with canonical annotation: ' + component['id'])
        if mask.mode != 'L' or mask.size != size or any(mask.histogram()[1:255]):
            raise ValueError('component requires a full-canvas binary L mask: ' + component['id'])
        if record is not None and decode_mask(record['mask'], size, root).tobytes() != mask.tobytes():
            errors.append('Component mask differs from canonical annotation: ' + component['id'])
        if mask.getbbox() is None or list(mask.getbbox()) != box or asset.size != (box[2] - box[0], box[3] - box[1]):
            errors.append('Component clipping or bounding box mismatch: ' + component['id'])
            clipped.append(component['id'])
            continue
        if asset.mode != 'RGBA':
            errors.append('Component asset does not preserve RGBA: ' + component['id'])
        if component['transform'] != {'position': box[:2], 'rotation': 0, 'scale': [1, 1], 'local_origin': box[:2]}:
            errors.append('Faithful component transform differs from source-space placement: ' + component['id'])
        if count(subtract(mask, foreground_mask)):
            errors.append('Component contains pixels outside selected source foreground: ' + component['id'])
        if apply_mask(original, mask).crop(box).tobytes() != asset.convert('RGBA').tobytes():
            errors.append('Component changes observed source appearance: ' + component['id'])
        if component['category'] == 'unassigned':
            unassigned += count(mask)
        overlapping = union(overlapping, intersect(occupied, mask))
        occupied = union(occupied, mask)
        if (component['provenance']['source_sha256'] != manifest['source']['sha256'] or
                component['provenance']['orientation'] != manifest['character']['orientation']):
            errors.append('Component provenance names another source: ' + component['id'])
        if component['provenance'].get('reconstruction'):
            reconstruction = component['provenance']['reconstruction']
            if manifest['mode'] != 'assisted':
                errors.append('Inferred content requires an explicitly assisted-mode decomposition.')
            generated = _image(root, reconstruction['asset']).convert('RGBA')
            inferred_mask = _image(root, reconstruction['inferred_mask']).convert('L')
            allowed_mask = _image(root, reconstruction['allowed_mask']).convert('L')
            if inferred_mask.size != size or any(inferred_mask.histogram()[1:255]):
                raise ValueError('reconstruction inferred mask is invalid')
            if allowed_mask.size != size or any(allowed_mask.histogram()[1:255]) or count(subtract(inferred_mask, allowed_mask)):
                errors.append('Inferred pixels exceed explicitly permitted hidden mask: ' + component['id'])
            front = Image.new('L', size)
            for other in manifest['components']:
                if (other['z_index'], other['id']) > (component['z_index'], component['id']):
                    front = union(front, _image(root, other['mask']).convert('L'))
            front = intersect(front, expected.getchannel('A').point(lambda value: 255 if value == 255 else 0))
            if count(subtract(inferred_mask, front)) or count(intersect(inferred_mask, mask)):
                errors.append('Inferred pixels must remain behind opaque front layers and outside observed component pixels: ' + component['id'])
            raw_output = _image(root, reconstruction['original_output'])
            if raw_output.size != size or raw_output.mode != 'RGBA':
                raise ValueError('original reconstruction output dimensions or mode changed')
            completed = apply_mask(raw_output, inferred_mask)
            completed.alpha_composite(asset.convert('RGBA'), tuple(box[:2]))
            rbox = reconstruction['bbox']
            rectangle(rbox, size)
            if (completed.getchannel('A').getbbox() != tuple(rbox) or completed.crop(rbox).tobytes() != generated.tobytes() or
                    reconstruction['observed_asset'] != component['asset'] or reconstruction['source_sha256'] != manifest['source']['sha256'] or
                    reconstruction['fixture'] is not manifest['source']['fixture']):
                errors.append('Reconstructed layer differs from observed anchors, inferred mask, or recorded source: ' + component['id'])
            for path, expected_hash in reconstruction['input_hashes'].items():
                if sha(safe_path(root, path)) != expected_hash:
                    errors.append('Reconstruction grounding input changed: ' + component['id'])
            receipt = read_json(safe_path(root, reconstruction['receipt']['path']))
            if (receipt.get('source_sha256') != manifest['source']['sha256'] or receipt.get('component_id') != component['id'] or
                    receipt.get('token') != reconstruction['token'] or receipt.get('input_hashes') != reconstruction['input_hashes'] or
                    receipt.get('fixture') is not manifest['source']['fixture'] or
                    receipt.get('generation_skill') != ('$fixture' if manifest['source']['fixture'] else '$imagegen') or
                    receipt.get('model_call') != reconstruction['model_call'] or receipt.get('note') != reconstruction['note']):
                errors.append('Reconstruction receipt no longer matches original grounding: ' + component['id'])
            generated_count += count(inferred_mask)
    actual = recompose(root, manifest)
    difference = ImageChops.difference(expected, actual)
    maximum = max(channel[1] for channel in difference.getextrema())
    channel_square_sum = sum(sum(i * i * n for i, n in enumerate(channel.histogram())) for channel in difference.split())
    differing = binary(ImageChops.lighter(ImageChops.lighter(difference.getchannel('R'), difference.getchannel('G')),
                        ImageChops.lighter(difference.getchannel('B'), difference.getchannel('A'))))
    support = binary(expected.getchannel('A'))
    total = count(support)
    gaps = count(subtract(support, occupied))
    overlap = count(overlapping)
    coverage = count(intersect(support, occupied)) / total if total else 0.0
    alpha_ok = expected.getchannel('A').tobytes() == actual.getchannel('A').tobytes()
    if maximum or gaps or overlap or not alpha_ok or not total:
        errors.append('Faithful reconstruction must reproduce every selected foreground RGBA pixel exactly once.')
    if generated_count:
        assisted = recompose(root, manifest, inferred=True)
        if assisted.tobytes() != expected.tobytes():
            errors.append('Hidden reconstruction changes visible-source fidelity; repair the inferred layer or occlusion order.')
    boundary = subtract(support.filter(ImageFilter.MaxFilter(3)), support.filter(ImageFilter.MinFilter(3)))
    edge_errors = count(intersect(differing, binary(boundary)))
    if unassigned:
        warnings.append(f'{unassigned} foreground pixels remain semantically unassigned.')
    warnings.extend(manifest['foreground']['warnings'])
    warnings.extend(manifest['analysis']['warnings'])
    if any(c['confidence'] < .8 for c in manifest['components']):
        warnings.append('Low-confidence semantic regions require visual review; exact reconstruction is not semantic evidence.')
    if any(c['occlusion']['status'] != 'visible' for c in manifest['components']):
        warnings.append('Some hidden component geometry is partial or unknown.')
    semantic_status = _aggregate(manifest)['status']
    if semantic_status != 'approved':
        warnings.append('This decomposition is not semantically approved.')
    result = {'schema_version': 1, 'algorithm': ALGORITHM, 'ok': not errors,
        'faithful_fidelity_passed': not errors, 'semantic_status': semantic_status,
        'input_signature': _validation_signature(manifest),
        'reconstruction_error': {'max_channel_error': maximum,
            'rmse': math.sqrt(channel_square_sum / (size[0] * size[1] * 4)), 'different_pixels': count(differing)},
        'source_pixel_coverage': coverage, 'foreground_pixels': total,
        'unassigned_foreground_pixels': unassigned, 'unexpected_overlap_pixels': overlap, 'gap_pixels': gaps,
        'alpha_preserved': alpha_ok, 'component_clipping': clipped, 'edge_artifacts': edge_errors,
        'generated_content_pixels': generated_count, 'errors': errors, 'warnings': list(dict.fromkeys(warnings)),
        'tolerances': {'max_channel_error': 0, 'minimum_coverage': 1, 'maximum_overlap_pixels': 0}}
    if write:
        manifest['validation'] = result
        save_manifest(root, manifest, validate=False)
    return result


def _checker(image):
    backdrop = Image.new('RGBA', image.size, '#eceff2')
    draw = ImageDraw.Draw(backdrop)
    for y in range(0, image.height, 12):
        for x in range(0, image.width, 12):
            if (x // 12 + y // 12) % 2:
                draw.rectangle((x, y, x + 11, y + 11), fill='#cbd2d9')
    return Image.alpha_composite(backdrop, image)


def _previews(root, manifest):
    original = _image(root, {'path': manifest['source']['normalized_path'], 'sha256': manifest['source']['normalized_sha256']}).convert('RGBA')
    foreground = _image(root, manifest['foreground']).convert('RGBA')
    reconstructed = recompose(root, manifest)
    _save_image(root, 'previews/reconstructed.png', reconstructed)
    _save_image(root, 'previews/transparent.png', foreground)
    _save_image(root, 'previews/overlay.png', Image.blend(_checker(foreground), _checker(reconstructed), .5))
    diff = ImageChops.difference(foreground, reconstructed)
    _save_image(root, 'previews/pixel-difference.png', diff.convert('RGB').point(lambda x: min(255, x * 8)))
    size = original.size
    # Comparison stays bounded for very tall/wide source images; full originals are retained.
    panels = []
    for image in (original, foreground, reconstructed):
        thumb = _checker(image)
        thumb.thumbnail((640, 640), Image.Resampling.NEAREST if manifest['canvas']['pixel_art'] else Image.Resampling.LANCZOS)
        panels.append(thumb)
    comparison = Image.new('RGB', (sum(p.width for p in panels), max(p.height for p in panels) + 24), '#edf1f5')
    draw = ImageDraw.Draw(comparison)
    x = 0
    for panel, title in zip(panels, ('Original image', 'Selected foreground', 'Reconstruction')):
        draw.text((x + 4, 4), title, fill='#263747')
        comparison.paste(panel, (x, 24))
        x += panel.width
    _save_image(root, 'previews/comparison.png', comparison)
    boxes, coverage = _checker(foreground), Image.new('RGBA', size)
    draw = ImageDraw.Draw(boxes)
    tiles = []
    for index, component in enumerate(manifest['components']):
        color = ((53 + index * 67) % 230 + 25, (91 + index * 31) % 230 + 25, (31 + index * 47) % 230 + 25, 170)
        x0, y0, x1, y1 = component['bbox']
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=color[:3], width=1)
        draw.text((x0, max(0, y0 - 11)), component['id'][:32], fill=color[:3])
        mask = _image(root, component['mask']).convert('L')
        coverage.paste(Image.new('RGBA', size, color), (0, 0), mask)
        asset = _image(root, component['asset']).convert('RGBA')
        asset.thumbnail((144, 144), Image.Resampling.NEAREST if manifest['canvas']['pixel_art'] else Image.Resampling.LANCZOS)
        tile = Image.new('RGBA', (176, 182), '#f0f3f7')
        tile.alpha_composite(_checker(asset), ((176 - asset.width) // 2, 4))
        text_draw = ImageDraw.Draw(tile)
        text_draw.text((4, 150), component['id'][:27], fill='#24374a')
        text_draw.text((4, 164), component['category'][:27], fill='#415568')
        tiles.append(tile)
    _save_image(root, 'previews/bounding-boxes.png', boxes)
    _save_image(root, 'previews/mask-coverage.png', coverage)
    columns = min(6, len(tiles))
    sheet = Image.new('RGBA', (columns * 176, math.ceil(len(tiles) / columns) * 182), '#dce4ec')
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * 176, (index // columns) * 182))
    _save_image(root, 'previews/contact-sheet.png', sheet)


def save_manifest(root, manifest, validate=True, transactional=True):
    """Refresh evidence and atomically publish metadata without inventing approvals.

    Existing runs stage their manifest and sidecars together. Internal callers
    already building a staged run disable the additional copy explicitly.
    """
    root = Path(root).resolve()
    if transactional and (root / 'decomposition.json').exists():
        recover_decomposition(root)
        from decomposition_package import _inventory
        old_files = _inventory(root, exclude_receipt=False)
        stage = Path(tempfile.mkdtemp(prefix='.' + root.name + '-decomposition-edit-', dir=root.parent))
        journal = root.parent / ('.' + root.name + '.decomposition-transaction.json')
        try:
            shutil.copytree(root, stage, dirs_exist_ok=True)
            save_manifest(stage, manifest, validate=validate, transactional=False)
            load_decomposition(stage)
            if _inventory(stage, exclude_receipt=False) != old_files:
                _promote_decomposition(root, stage, old_files)
            return manifest
        finally:
            if stage.exists() and not journal.exists():
                shutil.rmtree(stage)
    for component in manifest['components']:
        review = component['review']
        if review['status'] in ('approved', 'rejected'):
            if (not review['author'].strip() or not review['note'].strip() or
                    review['signature'] != component_signature(component)):
                raise ValueError('component review is stale or missing author/note: ' + component['id'])
        if component['category'] == 'unassigned' and review['status'] == 'approved':
            raise ValueError('unassigned foreground cannot be semantically approved')
        reconstruction = component['provenance'].get('reconstruction')
        if reconstruction and review['status'] == 'approved' and reconstruction['status'] != 'approved':
            raise ValueError('inferred reconstruction requires explicit approval before approving its component')
    manifest['review'] = _aggregate(manifest)
    if validate:
        manifest['validation'] = validate_decomposition(root, manifest, write=False)
    if not manifest['validation']['ok']:
        raise ValueError('decomposition validation failed: ' + '; '.join(manifest['validation']['errors']))
    from decomposition_reuse import derive_identity
    identity = derive_identity(manifest)
    for relative in ('decomposition.json', 'identity.json', 'validation.json', 'provenance.json'):
        safe_path(root, relative)
    atomic_json(root / 'identity.json', identity)
    manifest['identity'] = {'path': 'identity.json', 'sha256': sha(root / 'identity.json')}
    manifest['validation']['semantic_status'] = manifest['review']['status']
    atomic_json(root / 'validation.json', manifest['validation'])
    provenance = {'schema_version': 1, 'kind': 'character-decomposition', 'algorithm': ALGORITHM,
        'source': manifest['source'], 'annotation': manifest['annotation'],
        'components': {c['id']: c['provenance'] for c in manifest['components']},
        'model_calls_performed_by_controller': 0, 'fixture': manifest['source']['fixture']}
    atomic_json(root / 'provenance.json', provenance)
    manifest['artifacts'] = {p.relative_to(root).as_posix(): sha(p) for folder in ('previews',) for p in sorted((root / folder).glob('*.png'))}
    manifest['artifacts'].update({name: sha(root / name) for name in ('validation.json', 'provenance.json', 'analysis-request.json') if (root / name).exists()})
    validate_structure(manifest, MANIFEST_SCHEMA)
    validate_relations(manifest['components'], (manifest['canvas']['width'], manifest['canvas']['height']))
    atomic_json(root / 'decomposition.json', manifest)
    return manifest


def _load_checked(root, verify):
    manifest = read_json(root / 'decomposition.json')
    validate_structure(manifest, MANIFEST_SCHEMA)
    check_slug(manifest['character']['id'])
    size = (manifest['canvas']['width'], manifest['canvas']['height'])
    validate_relations(manifest['components'], size)
    source = manifest['source']
    descriptors = [{'path': source['path'], 'sha256': source['sha256']},
        {'path': source['normalized_path'], 'sha256': source['normalized_sha256']}, manifest['foreground'],
        manifest['foreground']['mask'], manifest['annotation'], manifest['identity']]
    for component in manifest['components']:
        descriptors.extend((component['asset'], component['mask']))
        review = component['review']
        if review['status'] in ('approved', 'rejected') and (review['signature'] != component_signature(component) or
                not review['author'].strip() or not review['note'].strip()):
            raise ValueError('stale component review signature: ' + component['id'])
        if component['category'] == 'unassigned' and review['status'] == 'approved':
            raise ValueError('unassigned foreground cannot be semantically approved')
        reconstruction = component['provenance'].get('reconstruction')
        if reconstruction and review['status'] == 'approved' and reconstruction['status'] != 'approved':
            raise ValueError('inferred reconstruction approval is missing or stale')
        rectangle(component['bbox'], size)
        if component['provenance'].get('reconstruction'):
            reconstruction = component['provenance']['reconstruction']
            for key in ('asset', 'inferred_mask', 'allowed_mask', 'receipt', 'original_output', 'observed_asset'):
                descriptors.append(reconstruction[key])
    descriptors += [{'path': name, 'sha256': value} for name, value in manifest['artifacts'].items()]
    for descriptor in descriptors:
        path = safe_path(root, descriptor['path'])
        if verify and (not path.is_file() or sha(path) != descriptor['sha256']):
            raise ValueError('decomposition artifact hash mismatch: ' + descriptor['path'])
    if manifest['review'] != _aggregate(manifest):
        raise ValueError('decomposition review aggregate is stale')
    if manifest['validation']['input_signature'] != _validation_signature(manifest):
        raise ValueError('decomposition validation signature is stale')
    if verify:
        annotation = read_json(safe_path(root, manifest['annotation']['path']))
        validate_structure(annotation, ANNOTATION_SCHEMA, 'current annotation')
        if annotation['source_sha256'] != source['sha256']:
            raise ValueError('current annotation names another source')
        identity = read_json(safe_path(root, manifest['identity']['path']))
        directions = identity.get('directions', {}) if isinstance(identity, dict) else {}
        validate_identity(identity, {'states': [{'id': name} for name in directions]})
        calculated = validate_decomposition(root, manifest, write=False)
        if calculated != manifest['validation']:
            raise ValueError('decomposition validation results are stale or falsified')
        if not calculated['ok']:
            raise ValueError('decomposition fidelity validation failed: ' + '; '.join(calculated['errors']))
    return manifest


def load_decomposition(root, verify=True):
    root = Path(root).resolve()
    return _load_checked(root, verify)


def _analysis_request(root, manifest):
    request = {'schema_version': 1, 'kind': 'semantic-analysis-request', 'source_sha256': manifest['source']['sha256'],
        'source': {'path': 'original.png', 'sha256': manifest['source']['normalized_sha256']},
        'foreground': manifest['foreground'], 'canvas': manifest['canvas'],
        'semantic_model_called': manifest['analysis']['semantic_model_called'], 'status': 'awaiting-source-bound-annotations',
        'instructions': 'Inspect the original and foreground. Identify only visible semantic parts appropriate to this character. '
            'Use anatomical left/right relative to its stated orientation, or unknown. Return source-bound annotations with '
            'binary masks, polygons or color/seed selectors. Group disconnected pixels when they belong to one logical feature. '
            'Record confidence, occlusion and ambiguity. Never label generated or hidden pixels as observed. '
            'Optional analysis uses decompose-analyze --analysis-backend with explicit enablement; the local review desk remains the complete manual route.',
        'annotation_schema': 'profiles/decomposition-annotations.schema.json',
        'optional_capability': 'An assistant with image understanding may inspect these images and author the annotation file. '
            'Any raster generation/edit uses the installed imagegen skill and its real interface, followed by explicit reconstruction ingestion.'}
    atomic_json(root / 'analysis-request.json', request)


def _build(root, source, source_info, annotation, foreground_mask, foreground_info, *, previous=None,
           selection=None, pixel_art=False, name=None, character_id=None, mode='faithful', corrected=False):
    foreground = apply_mask(source, foreground_mask)
    components, diagnostics = _extract(root, source, annotation, foreground_mask, previous, corrected)
    annotation['foreground_mask'] = {'runs': mask_to_runs(foreground_mask,
        MAX_TOTAL_RUNS - sum(len(c['mask']['runs']) for c in annotation['components']))}
    atomic_json(root / 'annotations/current.json', annotation)
    foreground_asset = _save_image(root, 'foreground.png', foreground)
    foreground_asset.update(mask=_content_image(root, 'masks', 'foreground', foreground_mask), **foreground_info)
    candidates = character_candidates(foreground)
    annotations_are_semantic = any(c['category'] != 'unassigned' for c in components)
    suspected = len(candidates) > 1
    analysis_warnings = []
    if suspected:
        analysis_warnings.append('Multiple substantial disconnected regions may be separate characters or detached character parts; inspect explicit selection/grouping.')
    if not annotations_are_semantic:
        analysis_warnings.append('No semantic component annotations supplied. Foreground is retained for identification and correction.')
    if source_info['exif_orientation'] not in (None, 1):
        analysis_warnings.append('EXIF orientation is recorded without rotating source-space pixels; inspect orientation before labeling sides.')
    extensions = copy.deepcopy(annotation.get('extensions', {}))
    extensions['palette'] = palette(foreground)
    extensions['segmentation_diagnostics'] = diagnostics
    manifest = {'schema_version': 1, 'kind': 'character-decomposition', 'revision': (previous or {}).get('revision', 0) + 1,
        'mode': mode, 'character': {'id': character_id or (previous or {}).get('character', {}).get('id', 'character-' + source_info['sha256'][:12]),
            'name': name or (previous or {}).get('character', {}).get('name', 'Imported character'), 'orientation': annotation['orientation']},
        'source': source_info, 'canvas': {'width': source.width, 'height': source.height, 'pixel_art': pixel_art},
        'foreground': foreground_asset, 'components': components,
        'annotation': {'path': 'annotations/current.json', 'sha256': sha(root / 'annotations/current.json')},
        'analysis': {'mode': 'model-assisted' if 'decomposition_analysis' in annotation.get('extensions', {}) else ('annotation-assisted' if annotations_are_semantic else 'manual-pending'),
            'semantic_model_called': bool(annotation.get('extensions', {}).get('decomposition_analysis', {}).get('evidence', {}).get('execution') == 'local'), 'candidates': candidates, 'multiple_characters_suspected': suspected,
            'selection': selection, 'selection_resolved': bool(selection is not None or annotations_are_semantic or not suspected),
            'warnings': analysis_warnings},
        'quality': quality(source, foreground, pixel_art), 'rig': _rig(components),
        'extensions': extensions, 'artifacts': {}}
    check_slug(manifest['character']['id'])
    from decomposition_reuse import derive_identity
    atomic_json(root / 'identity.json', derive_identity(manifest))
    manifest['identity'] = {'path': 'identity.json', 'sha256': sha(root / 'identity.json')}
    _analysis_request(root, manifest)
    _previews(root, manifest)
    save_manifest(root, manifest, transactional=False)
    if not manifest['validation']['ok']:
        raise ValueError('decomposition failed fidelity checks: ' + '; '.join(manifest['validation']['errors']))
    return manifest


def create_decomposition(source, run_dir, annotations=None, background='auto', foreground_mask=None,
                         selection=None, name=None, character_id=None, pixel_art=False,
                         origin='User supplied image', fixture=False, mode='faithful', chroma_key=None, key_threshold=0):
    source_path, root = Path(source).expanduser().resolve(), Path(run_dir).expanduser().resolve()
    if root.exists():
        raise ValueError('decomposition run directory must be new; review/rebuild or resume the existing package')
    if type(pixel_art) is not bool or type(fixture) is not bool or mode not in ('faithful', 'assisted'):
        raise ValueError('invalid decomposition pixel-art, fixture, or mode value')
    if background not in ('auto', 'alpha', 'keep', 'solid', 'chroma'):
        raise ValueError('unsupported decomposition background mode')
    if not isinstance(origin, str) or not origin.strip():
        raise ValueError('source origin/attribution must be nonempty text')
    if not source_path.is_file():
        raise ValueError('source image does not exist or is not a file')
    try:
        check_image(source_path)
        with Image.open(source_path) as original:
            info = {'format': original.format, 'original_mode': original.mode, 'exif_orientation': original.getexif().get(274)}
            image = original.convert('RGBA')
            icc = original.info.get('icc_profile')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('invalid character source image: ' + str(exc)) from exc
    if not image.getchannel('A').getbbox():
        raise ValueError('empty transparent character source')
    original_hash = sha(source_path)
    base = Path(annotations).resolve().parent if isinstance(annotations, (str, Path)) else source_path.parent
    annotation = _annotation(annotations, original_hash, image, base)
    if annotation.get('extensions', {}).get('fixture') is True and not fixture:
        raise ValueError('fixture annotation must use --fixture-mode; never relabel synthetic artwork')
    supplied = foreground_mask if foreground_mask is not None else annotation.get('foreground_mask')
    if isinstance(supplied, (str, Path)):
        supplied = {'path': str(Path(supplied).resolve())}
    mask, foreground_info = identify_foreground(image, background, supplied, base, chroma_key, key_threshold)
    mask = intersect(mask, _selection_mask(image.size, selection))
    if not count(mask):
        raise ValueError('selection contains no foreground pixels')
    if selection is not None and annotation['components']:
        annotation['components'] = [c for c in annotation['components'] if count(intersect(decode_mask(c['mask'], image.size), mask))]
        kept = {c['id'] for c in annotation['components']}
        for component in annotation['components']:
            if component['parent_id'] not in kept:
                component['parent_id'] = None
            component['attachments'] = [a for a in component['attachments'] if a['target_id'] is None or a['target_id'] in kept]
            component['occlusion']['occluded_by'] = [i for i in component['occlusion']['occluded_by'] if i in kept]
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + root.name + '-decompose-', dir=root.parent))
    try:
        source_rel = 'source/original' + source_path.suffix.lower()
        safe_path(stage, source_rel).parent.mkdir(parents=True)
        shutil.copyfile(source_path, stage / source_rel)
        # Preserve original compression/metadata bytes and a decoded RGBA canvas independently.
        image.save(stage / 'original.png', format='PNG', **({'icc_profile': icc} if icc else {}))
        if sha(stage / source_rel) != original_hash or sha(source_path) != original_hash:
            raise ValueError('source image changed during decomposition')
        source_info = {**info, 'path': source_rel, 'sha256': original_hash, 'normalized_path': 'original.png',
            'normalized_sha256': sha(stage / 'original.png'), 'origin': origin, 'fixture': fixture,
            'original_name': source_path.name}
        result = _build(stage, image, source_info, annotation, mask, foreground_info, selection=selection,
            pixel_art=pixel_art, name=name, character_id=character_id, mode=mode)
        load_decomposition(stage)
        if root.exists():
            raise ValueError('decomposition destination appeared during preparation')
        os.replace(stage, root)
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _promote_decomposition(root, stage, old_files):
    """Publish an already validated stage only if the original snapshot is intact."""
    from decomposition_package import _inventory
    if _inventory(root, exclude_receipt=False) != old_files:
        raise ValueError('decomposition changed during preparation; preserve the current run')
    new_files = _inventory(stage, exclude_receipt=False)
    backup = root.parent / ('.' + root.name + '-decomposition-backup-' + uuid.uuid4().hex)
    journal = root.parent / ('.' + root.name + '.decomposition-transaction.json')
    if journal.exists():
        raise ValueError('decomposition has an unreconciled recovery journal')
    atomic_json(journal, {'stage': str(stage), 'backup': str(backup),
        'old_manifest_sha256': old_files['decomposition.json'], 'new_manifest_sha256': new_files['decomposition.json'],
        'old_files': old_files, 'new_files': new_files})
    recover_decomposition(root)


def recover_decomposition(root):
    """Finish a journaled local repair after interruption, without dropping originals."""
    root = Path(root).resolve()
    journal = root.parent / ('.' + root.name + '.decomposition-transaction.json')
    if not journal.exists():
        return
    record = read_json(journal)
    from asset_contracts import strict_keys
    from decomposition_package import _inventory
    strict_keys(record, {'stage', 'backup', 'old_manifest_sha256', 'new_manifest_sha256', 'old_files', 'new_files'},
                'decomposition recovery journal')
    if (not isinstance(record.get('old_files'), dict) or not isinstance(record.get('new_files'), dict) or
            record['old_files'].get('decomposition.json') != record.get('old_manifest_sha256') or
            record['new_files'].get('decomposition.json') != record.get('new_manifest_sha256')):
        raise ValueError('decomposition recovery requires exact old/new file inventories')
    paths = []
    for key, prefix in (('stage', '.' + root.name + '-decomposition-edit-'), ('backup', '.' + root.name + '-decomposition-backup-')):
        value = record.get(key)
        if not isinstance(value, str):
            raise ValueError('invalid decomposition recovery journal')
        path = Path(value)
        if path.is_symlink() or path.parent.resolve() != root.parent or not path.name.startswith(prefix):
            raise ValueError('unsafe decomposition recovery path')
        paths.append(path)
    stage, backup = paths
    if stage.exists() and _inventory(stage, exclude_receipt=False) != record['new_files']:
        raise ValueError('decomposition recovery stage contains changed or unrelated files')
    if backup.exists() and _inventory(backup, exclude_receipt=False) != record['old_files']:
        raise ValueError('decomposition recovery backup contains changed or unrelated files; preserve it')
    if root.exists():
        current_hash = sha(root / 'decomposition.json')
        if current_hash == record['new_manifest_sha256']:
            if _inventory(root, exclude_receipt=False) != record['new_files']:
                raise ValueError('decomposition recovered root contains changed or unrelated files')
            if stage.exists():
                raise ValueError('ambiguous decomposition recovery: new root and stage both exist')
        elif current_hash == record['old_manifest_sha256'] and stage.exists() and not backup.exists():
            if _inventory(root, exclude_receipt=False) != record['old_files']:
                raise ValueError('decomposition recovery original contains changed or unrelated files')
            if sha(stage / 'decomposition.json') != record['new_manifest_sha256']:
                raise ValueError('decomposition recovery stage changed')
            os.replace(root, backup)
            os.replace(stage, root)
        else:
            raise ValueError('decomposition recovery found unrelated changed files; preserve and inspect them')
    elif stage.exists() and backup.exists() and sha(stage / 'decomposition.json') == record['new_manifest_sha256'] and sha(backup / 'decomposition.json') == record['old_manifest_sha256']:
        os.replace(stage, root)
    else:
        raise ValueError('incomplete decomposition recovery; preserved journal requires inspection')
    load_decomposition(root)
    if backup.exists():
        shutil.rmtree(backup)
    journal.unlink()


def rebuild_from_annotation(root, annotation, reason='Component correction', author='Local reviewer'):
    """Repair observed components transactionally; preserve unaffected hashes and approvals."""
    root = Path(root).resolve()
    recover_decomposition(root)
    previous = load_decomposition(root)
    from decomposition_package import _inventory
    old_files = _inventory(root, exclude_receipt=False)  # Refuse unsafe or excessive repair trees before copying.
    original = _image(root, {'path': previous['source']['normalized_path'], 'sha256': previous['source']['normalized_sha256']}).convert('RGBA')
    base = Path(annotation).resolve().parent if isinstance(annotation, (str, Path)) else root
    normalized = _annotation(annotation, previous['source']['sha256'], original, base)
    normalized['author'], normalized['note'] = author, reason
    validate_structure(normalized, ANNOTATION_SCHEMA, 'corrected annotation')
    supplied = normalized.get('foreground_mask')
    old_mask = _image(root, previous['foreground']['mask']).convert('L')
    mask = intersect(decode_mask(supplied, original.size, base, original), binary(original.getchannel('A'))) if supplied else old_mask
    if not count(mask):
        raise ValueError('corrected foreground is empty')
    info = {key: copy.deepcopy(previous['foreground'][key]) for key in ('method', 'confidence', 'review_required', 'warnings')}
    if mask.tobytes() != old_mask.tobytes():
        info = {'method': 'manually-corrected-mask', 'confidence': 1.0, 'review_required': True,
                'warnings': ['Foreground mask was manually corrected; review source membership and matte edges.']}
    stage = Path(tempfile.mkdtemp(prefix='.' + root.name + '-decomposition-edit-', dir=root.parent))
    journal = root.parent / ('.' + root.name + '.decomposition-transaction.json')
    try:
        shutil.copytree(root, stage, dirs_exist_ok=True)
        history = stage / 'annotations/history' / (f'{previous["revision"]:06d}-' + sha(root / 'decomposition.json')[:12])
        history.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(root / 'decomposition.json', history / 'decomposition.json')
        shutil.copyfile(root / 'annotations/current.json', history / 'annotation.json')
        result = _build(stage, original, previous['source'], normalized, mask, info, previous=previous,
            selection=previous['analysis']['selection'], pixel_art=previous['canvas']['pixel_art'],
            name=previous['character']['name'], character_id=previous['character']['id'], mode=previous['mode'], corrected=True)
        load_decomposition(stage)
        _promote_decomposition(root, stage, old_files)
        return result
    finally:
        if stage.exists() and not journal.exists():
            shutil.rmtree(stage)
