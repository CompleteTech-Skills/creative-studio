#!/usr/bin/env python3
"""Production CLI for persistent casts, performance clips, scenes and exact evidence."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import hatch
import production_workflow as workflow


def _root(value):
    path = Path(value).resolve()
    if path.is_dir():
        return path
    if path.name != 'project.json':
        raise ValueError('--project must identify a project directory or project.json')
    return path.parent


def _indices(value):
    if value is None:
        return None
    try:
        result = [int(item) for item in value.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError('frames must be comma-separated integer indices') from exc
    if not result or len(result) != len(set(result)) or any(item < 0 for item in result):
        raise argparse.ArgumentTypeError('frames must be distinct nonnegative indices')
    return result


def _target(args):
    fields = {key: getattr(args, key, None) for key in
              ('kind', 'character', 'clip', 'version', 'component', 'scene', 'shot')}
    if args.target:
        if any(value is not None for value in fields.values()):
            raise ValueError('--target cannot be combined with individual review target flags')
        target = hatch.read(args.target)
        workflow._target_key(target)
        return target
    kind = fields['kind']
    if kind is None:
        if fields['component']:
            kind = 'component'
        elif fields['character']:
            kind = 'character'
        elif fields['clip']:
            kind = 'clip'
        elif fields['scene'] or fields['shot']:
            kind = 'shot'
        else:
            raise ValueError('a review target is required')
    allowed = {'character': ('character', 'version'), 'component': ('character', 'version', 'component'),
               'clip': ('clip', 'version'), 'shot': ('scene', 'shot')}[kind]
    if any(value is not None and key not in (*allowed, 'kind') for key, value in fields.items()):
        raise ValueError('review target flags are ambiguous or do not belong to the selected kind')
    if any(not fields[key] for key in allowed):
        raise ValueError(kind + ' review requires ' + ', '.join('--' + key for key in allowed))
    names = {'character': 'character_id', 'clip': 'clip_id', 'component': 'component_id',
             'scene': 'scene_id', 'shot': 'shot_id', 'version': 'version'}
    return {'kind': kind, **{names[key]: fields[key] for key in allowed}}


def _create(args):
    from production_contracts import load_project
    destination = Path(args.destination).resolve()
    if destination.exists():
        raise ValueError('create destination must not exist; preserve the existing project')
    if args.from_project:
        source = _root(args.from_project)
        if args.id:
            raise ValueError('--from-project preserves project identity; do not provide --id')
        if destination.is_relative_to(source):
            raise ValueError('portable clone destination must be outside the source project')
        with tempfile.TemporaryDirectory(prefix='hatch-production-clone-') as temporary:
            archive = Path(temporary) / 'project.zip'
            workflow.archive_project(source, archive)
            result = workflow.restore_project(archive, destination)
        return {**result, 'created': str(destination), 'identity_preserved': True}
    from production_fixture import build_fixture_project
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.hatch-production-create-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'project'
        build_fixture_project(stage)
        if args.id:
            project = hatch.read(stage / 'project.json')
            project['id'] = args.id
            hatch.atomic(stage / 'project.json', project)
        bundle = load_project(stage / 'project.json')
        os.replace(stage, destination)
    return {'created': str(destination), 'project_id': bundle['project']['id'],
            'fixture': True, 'visual_review_performed': False, 'provider_called': False}


def _progress(event):
    completed, total = event.get('completed', 0), event.get('total', 0)
    if completed == 1 or completed == total or completed % 120 == 0:
        print(json.dumps({'render_progress': event}), file=sys.stderr, flush=True)


def dispatch(args):
    from silent_cli import COMMANDS, dispatch as export_dispatch
    if args.command in COMMANDS:
        return export_dispatch(args)
    if args.command == 'create':
        return _create(args)
    if args.command == 'restore':
        return workflow.restore_project(args.source, args.destination)
    if args.command == 'storyboard':
        from production_render import contact_sheet
        return contact_sheet(args.render_dir, args.destination, frame_indices=args.frames,
                             columns=args.columns, cell_width=args.cell_width)
    if args.command == 'export-editor':
        from production_adapter import export_editor_handoff, validate_handoff, exercise_krita
        exported = export_editor_handoff(args.render_dir, args.destination, selected_frames=args.frames)
        verified = validate_handoff(args.destination)
        result = {'export': exported, 'adapter_validation': verified,
                  'visual_review_performed': False}
        if args.exercise_krita:
            result['consumer_exercise'] = exercise_krita(args.destination)
        return result
    root = _root(args.project)
    if args.command == 'validate':
        from production_contracts import load_project
        bundle = load_project(root / 'project.json', verify_hashes=not args.diagnostic,
                              allow_missing_assets=args.diagnostic)
        return {'valid': not bundle.get('stale'), 'project_id': bundle['project']['id'],
                'fixture': bundle['project']['fixture'], 'resources': bundle.get('resources', {}),
                'stale': bundle.get('stale', []), 'missing': bundle.get('missing', []),
                'characters': sorted(bundle['characters']), 'clips': sorted(bundle['clips']),
                'scenes': [scene['id'] for scene in bundle['scenes']], 'shots': sorted(bundle['shots']),
                'visual_review_performed': False, 'provider_called': False}
    if args.command == 'coverage':
        return workflow.coverage_report(root, args.scene)
    if args.command == 'snapshot':
        return workflow.dependency_snapshot(root, args.scene)
    if args.command == 'impact':
        return workflow.impact_report(root, args.receipt, args.scene)
    if args.command == 'review-template':
        return workflow.review_template(root, _target(args), args.dimension, args.render_manifest)
    if args.command == 'review':
        return workflow.record_review(root, args.receipt)
    if args.command == 'reviews':
        return workflow.review_status(root, _target(args))
    if args.command == 'archive':
        return workflow.archive_project(root, args.destination)
    if args.command == 'migrate':
        return workflow.migrate_project(root)
    if args.command == 'generation-status':
        return workflow.generation_status(root)
    if args.command == 'bind-generation':
        return workflow.bind_generation_run(root, args.binding, args.run_dir, hatch.read(args.requirements))
    if args.command == 'claim':
        return workflow.claim_generation(root, args.binding, args.job)
    if args.command == 'fail':
        return workflow.fail_generation(root, args.binding, args.job, args.token, args.note)
    if args.command == 'ingest':
        return workflow.ingest_generation(root, args.binding, args.job, args.source, args.token, args.note, args.receipt)
    if args.command == 'repair':
        return workflow.repair_generation(root, args.binding, args.job, args.note)
    if args.command == 'limits':
        return workflow.configure_generation_limits(root, max_calls=args.max_calls,
                    attempts_per_job=args.max_attempts, concurrency=args.concurrency)
    if args.command == 'import-component':
        return workflow.import_component(root, args.character, args.version, args.component, args.receipt, args.new_version)
    if args.command == 'adopt-generated-component':
        return workflow.adopt_generated_component(root, args.binding, args.job, args.character, args.version,
                                                  args.component, args.receipt, args.new_version)
    if args.command == 'render':
        from production_contracts import load_project
        from production_render import render_shot, render_scene, validate_render, mux_video
        bundle = load_project(root / 'project.json')
        if args.max_frames is not None and not args.shot:
            raise ValueError('--max-frames is supported only for an individual shot')
        kwargs = {'track_layers': not args.no_layers, 'progress': _progress}
        if args.shot:
            manifest = render_shot(bundle, args.shot, args.destination, max_frames=args.max_frames, **kwargs)
        else:
            manifest = render_scene(bundle, args.scene, args.destination, **kwargs)
        validation = validate_render(args.destination, require_complete=manifest['status'] == 'complete')
        result = {'manifest': str(Path(args.destination).resolve() / 'render-manifest.json'),
                  'status': manifest['status'], 'fixture': manifest['fixture'],
                  'frame_count': manifest['frame_count'], 'completed_frames': len(manifest['frames']),
                  'operation': manifest.get('operation', {}), 'validation': validation,
                  'visual_review_performed': False}
        if args.video:
            if manifest['status'] != 'complete':
                raise ValueError('--video requires a complete render; resume this shot first')
            result['video'] = mux_video(args.destination)
        return result
    raise ValueError('unsupported production command')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    def command(name, project=True):
        child = commands.add_parser(name)
        if project:
            child.add_argument('--project', required=True)
        child.add_argument('--output', help='New JSON report file; stdout prints only its absolute output path.')
        return child

    from silent_cli import add_parsers
    add_parsers(command)

    create = command('create', False)
    create.add_argument('--destination', required=True)
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument('--fixture', action='store_true')
    source.add_argument('--from-project')
    create.add_argument('--id')
    validate = command('validate')
    validate.add_argument('--diagnostic', action='store_true')
    for name in ('coverage', 'snapshot'):
        command(name).add_argument('--scene')
    impact = command('impact')
    impact.add_argument('--receipt', '--snapshot', dest='receipt', required=True)
    impact.add_argument('--scene')
    for name in ('review-template', 'reviews'):
        child = command(name)
        child.add_argument('--target', help='JSON target descriptor, exclusive with target flags')
        child.add_argument('--kind', choices=('character', 'component', 'clip', 'shot'))
        for field in ('character', 'clip', 'version', 'component', 'scene', 'shot'):
            child.add_argument('--' + field)
        if name == 'review-template':
            child.add_argument('--dimension', action='append', choices=workflow.REVIEW_DIMENSIONS)
            child.add_argument('--render-manifest')
    command('review').add_argument('--receipt', required=True)
    render = command('render')
    render.add_argument('--destination', required=True)
    target = render.add_mutually_exclusive_group(required=True)
    target.add_argument('--scene')
    target.add_argument('--shot')
    render.add_argument('--max-frames', type=int)
    render.add_argument('--no-layers', action='store_true')
    render.add_argument('--video', action='store_true')
    storyboard = command('storyboard', False)
    storyboard.add_argument('--render-dir', required=True)
    storyboard.add_argument('--destination', required=True)
    storyboard.add_argument('--frames', type=_indices)
    storyboard.add_argument('--columns', type=int, default=4)
    storyboard.add_argument('--cell-width', type=int, default=320)
    export = command('export-editor', False)
    export.add_argument('--render-dir', required=True)
    export.add_argument('--destination', required=True)
    export.add_argument('--frames', type=_indices)
    export.add_argument('--exercise-krita', action='store_true')
    command('archive').add_argument('--destination', required=True)
    restore = command('restore', False)
    restore.add_argument('--source', required=True)
    restore.add_argument('--destination', required=True)
    command('migrate')
    command('generation-status')
    binding = command('bind-generation')
    binding.add_argument('--binding', required=True)
    binding.add_argument('--run-dir', required=True)
    binding.add_argument('--requirements', required=True)
    for name in ('claim', 'fail', 'ingest', 'repair'):
        child = command(name)
        child.add_argument('--binding', required=True)
        child.add_argument('--job', required=True)
        if name in ('fail', 'ingest'):
            child.add_argument('--token', required=True)
        if name != 'claim':
            child.add_argument('--note', required=True)
        if name == 'ingest':
            child.add_argument('--source', required=True)
            child.add_argument('--receipt', required=True)
    limits = command('limits')
    limits.add_argument('--max-calls', type=int)
    limits.add_argument('--max-attempts', type=int)
    limits.add_argument('--concurrency', type=int)
    for name in ('import-component', 'adopt-generated-component'):
        child = command(name)
        for field in ('character', 'version', 'component', 'new-version', 'receipt'):
            child.add_argument('--' + field, required=True)
        if name == 'adopt-generated-component':
            child.add_argument('--binding', required=True)
            child.add_argument('--job', required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        output = Path(args.output).resolve() if args.output else None
        if output is not None and output.exists():
            raise ValueError('--output must be a new file; preserve the existing receipt')
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
        result = dispatch(args)
        if output is not None:
            hatch.atomic(output, result)
            print(json.dumps({'output': str(output)}))
        else:
            print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc), 'code': getattr(exc, 'code', 'PRODUCTION_COMMAND_FAILED'), 'command': args.command, 'provider_called': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
