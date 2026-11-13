"""Actual CLI parsing/admission and authority routing; no media mocks prove encoding."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import production
import silent_cli

class SilentCLI(unittest.TestCase):
    def test_all_commands_registered_without_project(self):
        cases = [['export-capabilities'], ['export-status', '--job-root','job'],
                 ['export-resume','--job-root','job'], ['export-cancel','--job-root','job'],
                 ['export-batch','--jobs','batch.json'],
                 ['export-sequence','--request','r.json','--root','r','--format','exr','--destination','d'],
                 ['verify-export','--artifact','movie','--request','r.json','--root','r','--require-silent'],
                 ['export-video','--render-dir','r','--destination','d','--silent']]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(production.build_parser().parse_args(case).command, case[0])
    def test_silent_policy_is_required_at_parser_boundary(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            production.build_parser().parse_args(['export-video','--render-dir','r','--destination','d'])
    def test_source_selection_is_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            production.build_parser().parse_args(['export-video','--request','r','--timeline','t','--destination','d','--silent'])
    def test_missing_optional_authority_is_explicit_blocked_json(self):
        original = silent_cli.importlib.import_module
        def load(name):
            if name == 'silent_jobs':
                exc = ModuleNotFoundError(name); exc.name=name; raise exc
            return original(name)
        output=io.StringIO()
        with patch.object(silent_cli.importlib,'import_module',side_effect=load), contextlib.redirect_stderr(output):
            status=production.main(['export-status','--job-root','j'])
        self.assertEqual(status,2)
        self.assertEqual(json.loads(output.getvalue())['code'],'EXPORT_DEPENDENCY_UNAVAILABLE')
    def test_batch_input_is_bounded_before_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'batch.json'; path.write_text(json.dumps(['same','same']))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(production.main(['export-batch','--jobs',str(path)]),2)
    def test_output_receipt_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'receipt.json'; path.write_bytes(b'keep')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(production.main(['export-capabilities','--output',str(path)]),2)
            self.assertEqual(path.read_bytes(),b'keep')

if __name__=='__main__':unittest.main()
