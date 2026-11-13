"""Production command adapters for the separately verified, zero-audio workflow.

This module does not encode, render, publish or schedule independently: each
operation delegates to its existing authority. Optional authorities are loaded
only by the command that requires them.
"""
from __future__ import annotations
import importlib
from pathlib import Path

COMMANDS = ('export-video', 'export-sequence', 'verify-export', 'export-capabilities',
            'export-batch', 'export-status', 'export-resume', 'export-cancel')


class ExportBlocked(ValueError):
    code = 'EXPORT_DEPENDENCY_UNAVAILABLE'


def _module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise ExportBlocked('Required export authority/dependency is unavailable: '+str(exc.name or name)) from exc


def _read(path):
    from production_contracts import read_json
    return read_json(path)


def add_parsers(command):
    for name in COMMANDS:
        child = command(name, False)
        if name == 'export-capabilities':
            continue
        if name in ('export-status', 'export-resume', 'export-cancel'):
            child.add_argument('--job-root', required=True)
            continue
        if name == 'export-batch':
            child.add_argument('--jobs', required=True, help='JSON array of existing job roots')
            continue
        if name == 'verify-export':
            child.add_argument('--artifact', required=True)
            child.add_argument('--request', required=True)
            child.add_argument('--root', required=True)
            child.add_argument('--require-silent', action='store_true', required=True)
            continue
        child.add_argument('--destination', required=(name == 'export-sequence'))
        child.add_argument('--root')
        if name == 'export-sequence':
            child.add_argument('--request', required=True, help='Canonical sequence request JSON')
            child.add_argument('--format', choices=('png', 'exr'), required=True)
            continue
        source = child.add_mutually_exclusive_group()
        source.add_argument('--request', help='Canonical silent-video request JSON')
        source.add_argument('--timeline', help='Verified timeline directory')
        source.add_argument('--render-dir', help='Complete legacy production render directory')
        child.add_argument('--silent', action='store_true', required=True)
        child.add_argument('--profile', choices=('h264-mp4', 'vp9-webm', 'av1-webm'))
        child.add_argument('--quality', choices=('draft', 'preview', 'standard', 'high', 'master'))
        child.add_argument('--job-root', help='Create and execute a persistent resumable job')
        child.add_argument('--resume', action='store_true')
        child.add_argument('--resolution', help='Explicit WIDTHxHEIGHT')
        child.add_argument('--fps', help='Exact integer or P/Q frame rate')
        child.add_argument('--fit', choices=('preserve', 'letterbox', 'fill', 'stretch'))
        child.add_argument('--fps-policy', choices=('preserve', 'nearest'))
        child.add_argument('--prepared-dir', help='New immutable converted-frame directory')


def dispatch(args):
    name = args.command
    if name == 'export-capabilities':
        media = _module('silent_video')
        result = media.capabilities()
        try:
            result['jobs'] = _module('silent_jobs').worker_capability()
        except ExportBlocked as exc:
            result['jobs'] = {'status': 'blocked', 'reason': str(exc)}
        return result
    if name in ('export-status', 'export-resume', 'export-cancel'):
        jobs = _module('silent_jobs')
        return getattr(jobs, name.removeprefix('export-'))(args.job_root)
    if name == 'export-batch':
        roots = _read(args.jobs)
        if (not isinstance(roots, list) or not 1 <= len(roots) <= 16 or
                any(not isinstance(root, str) or not root for root in roots) or
                len(set(roots)) != len(roots)):
            raise ValueError('batch requires 1..16 distinct existing job roots')
        return _module('silent_jobs').run_batch(roots)
    if name == 'verify-export':
        return _module('silent_video').verify_export(args.artifact, _read(args.request), root=args.root)
    if name == 'export-sequence':
        if not args.root:
            raise ValueError('--root is required for a sequence request')
        request = _read(args.request)
        sequence = _module('silent_sequence')
        from production_contracts import _structural
        _structural(request, sequence.schemas()['request'], 'sequence request')
        if request['source']['output']['format'] != args.format:
            raise ValueError('--format must match the preserved source; conversion is not implicit')
        return sequence.export_sequence(request['source'], args.root, args.destination,
                                        provenance=request['provenance'])
    media = _module('silent_video')
    conversion = any((args.resolution, args.fps, args.fit, args.fps_policy, args.prepared_dir))
    preparation = None
    if args.resume:
        if not args.job_root:
            raise ValueError('--resume requires --job-root')
        if any((args.request, args.timeline, args.render_dir, args.root, args.destination, args.profile, args.quality, conversion)):
            raise ValueError('--resume uses the captured immutable request; source/destination/profile overrides are forbidden')
        return _module('silent_jobs').resume(args.job_root)
    if not args.destination or not any((args.request, args.timeline, args.render_dir)):
        raise ValueError('export-video requires --destination and one source: --request, --timeline or --render-dir')
    if args.request:
        request = _read(args.request)
        media.validate_request(request)
        if not args.root:
            raise ValueError('--root is required with --request')
        if args.profile and args.profile != request['profile']:
            raise ValueError('--profile cannot override a captured request')
        if args.quality or conversion:
            raise ValueError('quality/conversion flags cannot override a captured request')
        root = args.root
    else:
        if args.root:
            raise ValueError('--root cannot override --timeline or --render-dir')
        if args.timeline:
            if args.quality or args.job_root or conversion:
                raise ValueError('timeline quality/jobs require captured timeline integration; use a verified explicit request')
            return _module('silent_timeline').encode(args.timeline, args.destination, args.profile or 'h264-mp4')
        else:
            root = args.render_dir
            source = media.source_from_production(root)
        if conversion:
            if not args.prepared_dir:
                raise ValueError('explicit conversion requires a new --prepared-dir')
            import re
            from fractions import Fraction
            width, height = source['width'], source['height']
            if args.resolution:
                match = re.fullmatch(r'([0-9]{1,4})x([0-9]{1,4})', args.resolution)
                if not match or not args.fit:
                    raise ValueError('--resolution requires WIDTHxHEIGHT and an explicit --fit')
                width, height = map(int, match.groups())
            rate = source['fps']
            if args.fps:
                if not re.fullmatch(r'[0-9]{1,6}(?:/[0-9]{1,5})?', args.fps):
                    raise ValueError('--fps requires an exact positive integer or P/Q')
                value = Fraction(args.fps)
                rate = {'numerator': value.numerator, 'denominator': value.denominator}
                if rate != source['fps'] and args.fps_policy != 'nearest':
                    raise ValueError('changed FPS requires explicit --fps-policy nearest')
            transform = _module('silent_transform')
            settings = transform.profile(width,height,rate,fit=args.fit or 'preserve',
                                         fps_policy=args.fps_policy or 'preserve')
            preparation = transform.transform(source,root,settings,args.prepared_dir)
            transform.verify(args.prepared_dir,source_root=root)
            source, root = preparation['source'], args.prepared_dir
        profile = args.profile or 'h264-mp4'
        encoding = None
        if args.quality:
            profiles = _module('silent_profiles')
            encoding = profiles.quality_settings(media.PROFILES[profile], args.quality, fps=source['fps'])
        request = media.make_request(source, profile, encoding=encoding)
    if args.job_root:
        jobs = _module('silent_jobs')
        jobs.create(request, root, args.job_root, args.destination)
        return jobs.run(args.job_root)
    receipt = media.encode(request, root, args.destination)
    if preparation is not None:
        return {'schema_version': 1, 'kind': 'silent-prepared-video-export',
                'preparation': preparation, 'request': request, 'verification': receipt,
                'visual_review': 'not-performed'}
    return receipt


def prepare_preview(source, root, destination, *, width=320):
    """Create an immutable explicit SDR thumbnail-size source, without encoding.

    The canonical transform retains every frame and rational FPS. It records
    aspect-preserving letterboxing onto an explicit black opaque background.
    """
    media = _module('silent_video')
    transform = _module('silent_transform')
    media.validate_source(source)
    if type(width) is not int or not 2 <= width <= 4096 or width % 2:
        raise ValueError('preview width must be an even integer in 2..4096')
    height = max(2, 2 * round(source['height'] * width / source['width'] / 2))
    settings = transform.profile(width, height, source['fps'], fit='letterbox',
                                 fps_policy='preserve', background=[0, 0, 0])
    report = transform.transform(source, root, settings, destination)
    # Reopen the delivered transform through its owner before advertising it.
    transform.verify(destination, source_root=root)
    return {'source': report['source'], 'root': str(Path(destination).absolute()),
            'provenance': {'kind': 'silent-preview-source',
                           'input_source_sha256': media._digest(source),
                           'transform_sha256': media._digest(report)},
            'visual_review': 'not-performed'}


def preview(source, root, destination, prepared_destination, *, width=320):
    """Prepare explicit scaled frames and encode a separately verified preview."""
    prepared = prepare_preview(source, root, prepared_destination, width=width)
    media = _module('silent_video')
    request = media.make_request(prepared['source'])
    receipt = media.encode(request, prepared['root'], destination)
    return {'schema_version': 1, 'kind': 'silent-preview-export',
            'prepared': prepared, 'request': request, 'verification': receipt,
            'visual_review': 'not-performed'}


def review_assets(source, root, destination, *, indices=None):
    """Publish actual source-derived poster and bounded shot thumbnails.

    No arbitrary filenames, source rewrites, synthetic artwork, semantic review
    or movie verification are implied. Outputs retain their selected frame hashes.
    """
    import hashlib
    import io
    import json
    from PIL import Image, ImageOps
    from atomic_publish import pinned_directory, private_stage, _rename_no_replace
    media = _module('silent_video')
    media.validate_source(source)
    indices = [0] if indices is None else indices
    if (not isinstance(indices, (list, tuple)) or not 1 <= len(indices) <= 64 or
            any(type(index) is not int or not 0 <= index < len(source['frames']) for index in indices) or
            len(set(indices)) != len(indices)):
        raise ValueError('review assets require 1..64 distinct valid source indices')
    destination = media._destination(destination)
    with pinned_directory(destination.parent) as parent_fd:
        with private_stage(parent_fd, prefix='.silent-review-') as (private, wrapper_fd):
            stage = private/'payload'; stage.mkdir(mode=0o700)
            columns = min(4, len(indices))
            contact = Image.new('RGB', (columns*320, ((len(indices)+columns-1)//columns)*180), (0,0,0))
            bindings = []
            artifacts = []
            def put(name, payload):
                (stage/name).write_bytes(payload)
                artifacts.append({'path': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
            try:
                for offset, index in enumerate(indices):
                    frame = source['frames'][index]
                    data = media._read_frame(root, frame['path'])
                    if len(data) != frame['bytes'] or hashlib.sha256(data).hexdigest() != frame['sha256']:
                        raise ValueError('review source frame changed')
                    with media._image(data, (source['width'],source['height'])) as image:
                        thumbnail = ImageOps.contain(image, (320,180), Image.Resampling.LANCZOS)
                        contact.paste(thumbnail, ((offset%columns)*320+(320-thumbnail.width)//2,
                                                 (offset//columns)*180+(180-thumbnail.height)//2))
                        buffer = io.BytesIO(); thumbnail.save(buffer, format='PNG'); thumbnail.close()
                    put(f'thumbnail-{index:06d}.png', buffer.getvalue())
                    if offset == 0:
                        put('poster.png', data)
                    bindings.append({'index': index, 'source_sha256': frame['sha256'], 'source_path': frame['path']})
                buffer = io.BytesIO(); contact.save(buffer, format='PNG'); put('contact-sheet.png', buffer.getvalue())
            finally:
                contact.close()
            manifest = {'schema_version': 1, 'kind': 'silent-review-assets',
                        'source_sha256': media._digest(source), 'frames': bindings,
                        'artifacts': artifacts, 'visual_review': 'not-performed'}
            (stage/'review-assets.json').write_text(json.dumps(manifest,indent=2,allow_nan=False)+'\n',encoding='utf-8')
            _rename_no_replace('payload', destination.name, source_dir_fd=wrapper_fd, destination_dir_fd=parent_fd)
    return manifest
