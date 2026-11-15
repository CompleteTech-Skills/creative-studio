"""Focused tests; the delivery runner explicitly enumerates individual methods."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import model3d
import model3d_contracts
import model3d_discovery_cli as discovery


class DiscoveryTests(unittest.TestCase):
    def test_capabilities_and_limits_reuse(self):
        self.assertIs(discovery.capabilities, model3d.capabilities)
        self.assertIs(discovery.limits()['limits'], model3d_contracts.LIMITS)
        self.assertEqual(discovery.capabilities(), model3d.capabilities())
        self.assertEqual(discovery.limits()['limits'], discovery.capabilities()['limits'])

    def test_optional_package_absence(self):
        with mock.patch('importlib.metadata.version', side_effect=model3d.importlib.metadata.PackageNotFoundError), \
             mock.patch('importlib.util.find_spec', return_value=None):
            result = discovery.doctor()
        self.assertEqual(set(result['packages']), {'numpy', 'Pillow', 'jsonschema'})
        self.assertTrue(all(p == {'installed': False, 'version': None} for p in result['packages'].values()))
        self.assertIn('does not prove readiness', result['status'])

    def test_provider_and_format_scope(self):
        self.assertEqual(discovery.providers()['providers'], [])
        self.assertFalse(discovery.providers()['execution_integrated'])
        result = discovery.formats()
        self.assertEqual(result['inspection_inputs'], {'contract': ['json'], 'geometry': ['glb', 'gltf', 'obj'], 'source_images': ['png', 'jpeg', 'webp']})
        for field in ('model3d_cli_creation_formats', 'model3d_cli_export_formats', 'native_write_formats'):
            self.assertEqual(result[field], [])

    def test_cli_json_and_no_cwd_mutation(self):
        functions = {name: getattr(discovery, name) for name in ('doctor', 'capabilities', 'providers', 'formats', 'limits')}
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                for name, function in functions.items():
                    with self.subTest(command=name), contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(discovery.main([name]), 0)
                        self.assertEqual(json.loads(output.getvalue()), function())
                        self.assertEqual(json.loads(output.getvalue())['schema_version'], 1)
                self.assertEqual(list(Path('.').iterdir()), [])
            finally:
                os.chdir(previous)

    def test_help(self):
        for args in (['--help'], *([name, '--help'] for name in ('doctor', 'capabilities', 'providers', 'formats', 'limits'))):
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as exit_status:
                    discovery.main(args)
                self.assertEqual(exit_status.exception.code, 0)
                self.assertIn('usage:', output.getvalue())

    def test_executable_cli(self):
        import subprocess
        import sys
        script = Path(discovery.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            for name in ('doctor', 'capabilities', 'providers', 'formats', 'limits'):
                with self.subTest(command=name):
                    result = subprocess.run([sys.executable, '-B', str(script), name], cwd=directory,
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['schema_version'], 1)
                    self.assertEqual(result.stderr, '')
            self.assertEqual(list(Path(directory).iterdir()), [])
