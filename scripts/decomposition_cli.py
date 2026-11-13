"""Character decomposition commands in the existing locked Hatch controller."""
from __future__ import annotations

import copy
from pathlib import Path
import shutil
import tempfile
import os

from asset_contracts import sha
from character_decomposition import (create_decomposition, load_decomposition, read_json,
    recompose, recover_decomposition, rebuild_from_annotation, validate_decomposition)

COMMANDS = ('decompose', 'decompose-status', 'decompose-analyze', 'decompose-annotate',
    'decompose-analysis-status', 'decompose-analysis-run', 'decompose-analysis-ingest',
    'decompose-analysis-apply', 'decompose-analysis-retry', 'decompose-validate', 'decompose-recompose', 'decompose-review-ui', 'decompose-review',
    'decompose-restore', 'decompose-export', 'decompose-import', 'decompose-prepare',
    'decompose-reconstruction-request', 'decompose-reconstruction-ingest',
    'decompose-reconstruction-fail', 'decompose-edit')


def add_arguments(parser):
    for flag in ('annotations', 'foreground-mask', 'origin', 'select', 'hidden-mask', 'author',
                 'operation', 'operation-note', 'color', 'offset'):
        parser.add_argument('--' + flag)
    parser.add_argument('--analysis-backend', choices=('opencv-haar', 'clipseg', 'external'), help='Optional real analysis: fixed local face/eye detector, pinned text-conditioned masks, or attributed external result; disabled by default')
    parser.add_argument('--enable-analysis', action='store_true', help='Explicitly permit local no-cost image analysis; never grants semantic approval')
    parser.add_argument('--analysis-taxonomy', action='append', help='Allowed proposed semantic category; repeat for several')
    parser.add_argument('--component', action='append', default=[])
    parser.add_argument('--decomposition-mode', choices=('faithful', 'assisted'), default='faithful')
    parser.add_argument('--review-revision', type=int)
    parser.add_argument('--rotation', type=float, default=0.0)
    parser.add_argument('--scale', type=float, default=1.0)
    parser.add_argument('--z-index', type=int)
    for flag in ('pixel-art', 'allow-reconstruction', 'include-inferred', 'require-approved'):
        parser.add_argument('--' + flag, action='store_true')


def _require(args, *names):
    for name in names:
        value = getattr(args, name, None)
        if value is None or value == '' or value == []:
            raise ValueError(args.command + ' requires --' + name.replace('_', '-'))


def _numbers(value, count, label, kind=int):
    if value is None:
        return None
    try:
        parsed = [kind(x.strip()) for x in value.split(',')]
        if len(parsed) != count:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise ValueError(label + f' requires {count} comma-separated coordinates') from None


def _one_component(args):
    _require(args, 'component')
    if len(args.component) != 1:
        raise ValueError(args.command + ' requires exactly one --component')
    return args.component[0]


def _status(root, manifest):
    return {'run': str(root), 'character': manifest['character'], 'revision': manifest['revision'],
        'mode': manifest['mode'], 'source_sha256': manifest['source']['sha256'],
        'fixture': manifest['source']['fixture'], 'analysis': manifest['analysis'],
        'components': [{'id': c['id'], 'category': c['category'], 'review': c['review']['status'],
                        'bbox': c['bbox'], 'provenance': c['provenance']['kind']} for c in manifest['components']],
        'review': manifest['review'], 'validation': manifest['validation'],
        'animation_ready': manifest['rig']['animation_ready'],
        'next_step': 'Inspect the decomposition review desk; correct and approve semantic components.' if manifest['review']['status'] != 'approved'
            else 'Export the decomposition or prepare a forward run; new poses still require new artwork.'}


def _export_recomposition(root, manifest, destination, include_inferred=False, selected=None):
    out = Path(destination).resolve()
    if out.exists() or out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError('recomposition destination must be a new directory outside the decomposition')
    if include_inferred and manifest['mode'] != 'assisted':
        raise ValueError('--include-inferred requires an assisted-mode decomposition')
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.decomposition-preview-', dir=out.parent))
    try:
        image = recompose(root, manifest, inferred=include_inferred, visible_ids=selected or None)
        image.save(stage / 'reconstructed.png')
        for name in ('comparison.png', 'contact-sheet.png', 'pixel-difference.png', 'mask-coverage.png', 'bounding-boxes.png'):
            shutil.copyfile(root / 'previews' / name, stage / name)
        from extract_strip_frames import atomic_json
        receipt = {'schema_version': 1, 'source_sha256': manifest['source']['sha256'],
            'decomposition_sha256': sha(root / 'decomposition.json'), 'include_inferred': include_inferred,
            'visible_components': selected or [c['id'] for c in manifest['components']],
            'preview_sha256': sha(stage / 'reconstructed.png'), 'review_status': manifest['review']['status'],
            'fixture': manifest['source']['fixture'],
            'comparison_scope': 'Other diagnostics compare the complete faithful source foreground; selected/inferred output is named reconstructed.png.'}
        atomic_json(stage / 'recomposition.json', receipt)
        if out.exists():
            raise ValueError('recomposition destination appeared during export')
        os.replace(stage, out)
        return {'recomposition': str(out / 'reconstructed.png'), 'receipt': str(out / 'recomposition.json'), **receipt}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def dispatch(root, args):
    recover_decomposition(root)
    command = args.command
    if command == 'decompose':
        _require(args, 'source')
        value = create_decomposition(args.source, root, annotations=args.annotations, background=args.background or 'auto',
            foreground_mask=args.foreground_mask, selection=_numbers(args.select, 4, '--select'),
            name=args.pet_name or args.display_name or None, character_id=args.pet_id, pixel_art=args.pixel_art,
            origin=args.origin or 'User supplied image; attribution not provided', fixture=args.fixture_mode,
            mode=args.decomposition_mode, chroma_key=args.chroma_key, key_threshold=args.key_threshold or 0)
        return _status(root, value)
    if command == 'decompose-import':
        _require(args, 'source')
        from decomposition_package import import_decomposition
        return import_decomposition(args.source, root)
    manifest = load_decomposition(root)
    if command == 'decompose-status':
        result = _status(root, manifest)
        if (root / 'reconstruction/jobs.json').exists():
            from decomposition_assisted import _jobs
            result['reconstruction_handoffs'] = _jobs(root)
        return result
    if command == 'decompose-analyze':
        if args.analysis_backend:
            from decomposition_analysis import prepare, run
            prepared = prepare(root, args.analysis_backend, enabled=args.enable_analysis, taxonomy=args.analysis_taxonomy)
            return run(root, prepared['job']) if args.analysis_backend != 'external' else prepared
        return read_json(root / 'analysis-request.json')
    if command.startswith('decompose-analysis-'):
        _require(args, 'job')
        import decomposition_analysis as analysis
        action = command.removeprefix('decompose-analysis-')
        if action == 'ingest':
            _require(args, 'receipt')
            return analysis.ingest(root, args.job, args.receipt)
        return getattr(analysis, action)(root, args.job)
    if command == 'decompose-annotate':
        _require(args, 'annotations', 'author', 'note')
        value = rebuild_from_annotation(root, args.annotations, reason=args.note, author=args.author)
        return _status(root, value)
    if command == 'decompose-validate':
        return validate_decomposition(root, manifest)
    if command == 'decompose-recompose':
        _require(args, 'destination')
        return _export_recomposition(root, manifest, args.destination, args.include_inferred, args.component)
    if command == 'decompose-review-ui':
        _require(args, 'destination')
        from decomposition_review import build_review
        return build_review(root, args.destination)
    if command == 'decompose-review':
        _require(args, 'receipt')
        from decomposition_review import apply_review
        return apply_review(root, args.receipt)
    if command == 'decompose-restore':
        _require(args, 'review_revision', 'author', 'note')
        from decomposition_review import restore_revision
        return restore_revision(root, args.review_revision, author=args.author, note=args.note)
    if command == 'decompose-export':
        _require(args, 'destination')
        from decomposition_package import export_decomposition
        return export_decomposition(root, args.destination, require_approved=args.require_approved)
    if command == 'decompose-prepare':
        _require(args, 'destination')
        from decomposition_reuse import prepare_from_decomposition
        return prepare_from_decomposition(root, args.destination, args, args.component, args.operation, args.operation_note or '')
    if command == 'decompose-reconstruction-request':
        _require(args, 'note')
        from decomposition_assisted import request_reconstruction
        return request_reconstruction(root, _one_component(args), args.note, args.hidden_mask, args.allow_reconstruction)
    if command == 'decompose-reconstruction-ingest':
        _require(args, 'source', 'receipt')
        from decomposition_assisted import ingest_reconstruction
        return ingest_reconstruction(root, _one_component(args), args.source, args.receipt, args.allow_reconstruction)
    if command == 'decompose-reconstruction-fail':
        _require(args, 'token', 'note')
        from decomposition_assisted import fail_reconstruction
        return fail_reconstruction(root, args.token, args.note)
    if command == 'decompose-edit':
        _require(args, 'destination', 'component', 'operation')
        from decomposition_layers import edit_layers
        return edit_layers(root, args.destination, args.component, args.operation, color=args.color,
            offset=_numbers(args.offset, 2, '--offset'), rotation=args.rotation, scale=args.scale, z_index=args.z_index)
    raise ValueError('unsupported decomposition command')
