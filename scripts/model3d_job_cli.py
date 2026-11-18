"""Local Model3D job lifecycle; currently executes the native primitive engine."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from rig_errors import RigError, object_hash, read_json, require, sha256
from model3d_jobs import (archive_job, cancel_job, clear_cancellation, create_job,
                          restore_job, run_job, status)


def native_primitive(context):
    """Real bounded geometry through the native mesh/export owners."""
    from model3d_primitives import primitive
    from model3d_interchange import export_mesh
    from rig_errors import atomic_json
    require(context.request['engine'] == 'native-primitive', 'MODEL3D_JOB_ENGINE', 'Unsupported local engine.')
    parameters = context.request['parameters']
    require(set(parameters) == {'kind', 'parameters', 'name', 'normal_mode', 'format'},
            'MODEL3D_JOB_PARAMETERS', 'Native primitive parameters must be explicit.')
    context.check()
    item = primitive(parameters['kind'], parameters['parameters'], name=parameters['name'], normal_mode=parameters['normal_mode'])
    context.check()
    result = export_mesh(item.geometry(), context.output/'model', format=parameters['format'], editable=item)
    context.check()
    atomic_json(context.output/'acceptance.json', dict(schema_version=1, fixture=True,
                engine='native-primitive', export=result, review_state='visually-unreviewed',
                provider_calls=0, request_sha256=object_hash(context.request)))


def handler_version():
    root = Path(__file__).parent
    return object_hash({path.name:sha256(path) for path in sorted(root.glob('*.py'))
                        if path.name.startswith(('model3d_', 'rig_'))})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create'); create.add_argument('--request', required=True)
    create.add_argument('--budgets'); create.add_argument('--root', required=True)
    for name in ('status', 'run', 'cancel', 'clear-cancellation', 'archive'):
        command = commands.add_parser(name); command.add_argument('--root', required=True)
        if name == 'run': command.add_argument('--retry', action='store_true')
        if name == 'archive': command.add_argument('--destination', required=True)
    restore = commands.add_parser('restore'); restore.add_argument('--archive', required=True); restore.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'create':
            result = create_job(args.root, read_json(args.request), budgets=read_json(args.budgets) if args.budgets else None)
        elif args.command == 'run': result = run_job(args.root, native_primitive, handler_version=handler_version(), retry=args.retry)
        elif args.command == 'status': result = status(args.root)
        elif args.command == 'cancel': result = cancel_job(args.root)
        elif args.command == 'clear-cancellation': result = clear_cancellation(args.root)
        elif args.command == 'archive': result = archive_job(args.root, args.destination)
        else: result = restore_job(args.archive, args.destination)
        print(json.dumps(result, indent=2, allow_nan=False)); return 0
    except (RigError, OSError, ValueError) as exc:
        error = exc.report if isinstance(exc, RigError) else dict(code='MODEL3D_JOB_IO', message=str(exc))
        print(json.dumps(dict(ok=False, error=error), allow_nan=False), file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
