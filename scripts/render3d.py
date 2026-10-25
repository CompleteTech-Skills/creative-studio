#!/usr/bin/env python3
"""Inspect Render3D contracts and freeze native rig sources; does not render."""
import argparse
import json
from pathlib import Path

from render3d_contracts import Render3DError, load, make_frame_spec
from render3d_snapshot import NativeRigAdapter, create_snapshot, verify_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate')
    validate.add_argument('file', type=Path)
    snapshot = commands.add_parser('snapshot-rig')
    snapshot.add_argument('--source', type=Path, required=True)
    snapshot.add_argument('--destination', type=Path, required=True)
    verify = commands.add_parser('verify-snapshot')
    verify.add_argument('root', type=Path)
    frame = commands.add_parser('frame')
    frame.add_argument('--request', type=Path, required=True)
    frame.add_argument('--snapshot', type=Path, required=True)
    frame.add_argument('--index', type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            value = load(args.file)
            result = dict(valid=True, kind=value['kind'], evidence='contract-only')
        elif args.command == 'snapshot-rig':
            result = create_snapshot(args.source, args.destination, NativeRigAdapter())
        elif args.command == 'verify-snapshot':
            result = verify_snapshot(args.root, NativeRigAdapter())
        else:
            result = make_frame_spec(load(args.request), args.index, args.snapshot)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except Render3DError as exc:
        print(json.dumps(exc.report, indent=2))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
