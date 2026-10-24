"""Validate Model3D contracts and inspect real immutable asset payloads (read-only)."""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import sys

from model3d_contracts import LIMITS, VERSION
from rig_errors import RigError


def capabilities():
    packages = {}
    for distribution, module in [('numpy', 'numpy'), ('jsonschema', 'jsonschema'), ('Pillow', 'PIL')]:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = None
        packages[distribution] = dict(installed=importlib.util.find_spec(module) is not None, version=version)
    return dict(schema_version=1, implementation_version=VERSION, discovery_only=True,
                packages=packages, limits=LIMITS,
                implemented=['strict-contract-validation', 'hash-bound-payload-snapshot',
                             'dependency-closure-validation', 'existing-GLB-glTF-OBJ-inspection'],
                not_implemented=['model3d-generation', 'reconstruction', 'provider-execution',
                                 'model3d-job-scheduler', 'model3d-editing', 'model3d-review-approval'],
                geometry_authority='rig_import.load_character_model',
                legacy_2d_migration=False, network_calls=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('capabilities', help='Report installed packages without network or provider calls.')
    validate = commands.add_parser('validate', help='Validate contracts, exact payload closure and actual asset geometry.')
    validate.add_argument('--file', required=True, help='Model3D JSON document.')
    validate.add_argument('--root', help='Common root for every referenced path (default: manifest directory).')
    validate.add_argument('--structural-only', action='store_true', help='Do not read payloads; does NOT establish asset readiness.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'capabilities':
            result = capabilities()
        else:
            from model3d_validation import validate_file
            result = validate_file(args.file, args.root, structural_only=args.structural_only)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except RigError as exc:
        print(json.dumps(dict(ok=False, error=exc.report), indent=2), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps(dict(ok=False, error=dict(code='MODEL3D_IO', message=str(exc))), indent=2), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
