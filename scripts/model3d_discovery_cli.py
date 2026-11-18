"""Read-only discovery for the bounded Model3D contract and payload inspector."""
from __future__ import annotations

import argparse
import json

from model3d import capabilities
from model3d_contracts import LIMITS


def doctor():
    """Report package discovery, not imports or operational readiness."""
    snapshot = capabilities()
    return {
        'schema_version': 1,
        'scope': 'Model3D contract validation and bounded payload/geometry inspection',
        'packages': snapshot['packages'],
        'package_roles': {
            'jsonschema': 'contract validation',
            'Pillow': 'source image and geometry inspection',
            'numpy': 'geometry inspection',
        },
        'status': 'package discovery only; availability does not prove readiness',
        'checks': 'module specifications and distribution metadata only',
    }


def providers():
    """Describe the current absence of a Model3D provider registry/execution."""
    return {'schema_version': 1, 'registry_integrated': False,
            'execution_integrated': False, 'providers': [],
            'scope': 'Model3D; no credentials or external services inspected'}


def formats():
    """Describe only formats evidenced by the current inspector."""
    return {
        'schema_version': 1,
        'scope': 'Model3D contract and referenced payload inspection',
        'inspection_inputs': {
            'contract': ['json'],
            'geometry': ['glb', 'gltf', 'obj'],
            'source_images': ['png', 'jpeg', 'webp'],
        },
        'constraints': 'Declared, hash-bound payloads and validator/importer restrictions apply; not arbitrary file inspection.',
        'model3d_cli_creation_formats': [],
        'model3d_cli_export_formats': [],
        'native_write_formats': [],
        'inventory_scope': 'No complete shared format inventory API; contract declarations do not imply supported output operations.',
    }


def limits():
    """Return canonical static contract/verifier ceilings."""
    return {'schema_version': 1, 'scope': 'static contract and verifier/inspection limits',
            'limits': LIMITS,
            'note': 'Individual documents and inspection backends may impose lower limits; these are not generation or scheduler budgets.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    functions = {'doctor': doctor, 'capabilities': capabilities, 'providers': providers,
                 'formats': formats, 'limits': limits}
    for name, function in functions.items():
        commands.add_parser(name, help=function.__doc__, description=function.__doc__)
    args = parser.parse_args(argv)
    print(json.dumps(functions[args.command](), indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
