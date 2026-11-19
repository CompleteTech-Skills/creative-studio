"""Regression checks for the retained render91 audit evidence and its validator.

These are evidence-consistency checks only; they do not establish rendering.
"""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

EVIDENCE = Path(__file__).resolve().parent / 'results' / 'render91-audit-20261113'


def load_validator():
    spec = importlib.util.spec_from_file_location('render91_audit_validate', EVIDENCE / 'validate.py')
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


class Render91AuditEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.validate = load_validator()
        self.audit = json.loads((EVIDENCE / 'audit.json').read_text(encoding='utf-8'))

    def test_default_emit_prints_and_leaves_retained_receipt_unchanged(self):
        receipt = EVIDENCE / 'audit-validation.json'
        before = receipt.read_bytes()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.validate.emit({'kind': 'probe'})
        self.assertEqual(json.loads(stdout.getvalue()), {'kind': 'probe'})
        self.assertEqual(receipt.read_bytes(), before)

    def test_output_writes_new_path_but_refuses_retained_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'fresh.json'
            self.validate.emit({'kind': 'probe'}, str(target))
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'kind': 'probe'})
        for name in ('audit-validation.json', 'audit-validation-219.json'):
            before = (EVIDENCE / name).read_bytes()
            with self.assertRaises(FileExistsError):
                self.validate.emit({'kind': 'probe'}, str(EVIDENCE / name))
            self.assertEqual((EVIDENCE / name).read_bytes(), before)

    def test_output_preserves_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'existing.json'
            target.write_bytes(b'existing contents\n')
            with self.assertRaises(FileExistsError):
                self.validate.emit({'kind': 'probe'}, target)
            self.assertEqual(target.read_bytes(), b'existing contents\n')

    def test_job_binding_addresses_resolve_without_the_unmerged_revision(self):
        native = self.audit['native_job_equivalent']
        with mock.patch.object(self.validate, 'optional_gitfile', return_value=None):
            payload, cross_checked = self.validate.job_binding_source(native)
        self.assertFalse(cross_checked)
        tree = ast.parse(payload)
        methods = {(c.name, m.name) for c in tree.body if isinstance(c, ast.ClassDef)
                   for m in c.body if isinstance(m, ast.FunctionDef)}
        for ref in native['tests']:
            path, address = ref.split(':')
            self.assertEqual(path, native['retained_test_source']['source_path'])
            self.assertIn(tuple(address.split('.')), methods)

    def test_job_binding_source_rejects_tampered_copy_or_git_mismatch(self):
        native = json.loads(json.dumps(self.audit['native_job_equivalent']))
        with mock.patch.object(self.validate, 'optional_gitfile', return_value=b'different'):
            with self.assertRaises(AssertionError):
                self.validate.job_binding_source(native)
        native['retained_test_source']['sha256'] = '0' * 64
        with mock.patch.object(self.validate, 'optional_gitfile', return_value=None):
            with self.assertRaises(AssertionError):
                self.validate.job_binding_source(native)

    def test_present_revision_without_claimed_job_test_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['git', 'init', '--quiet', directory], check=True)
            git = ['git', '-C', directory, '-c', 'user.name=Synthetic fixture',
                   '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false']
            empty_tree = subprocess.check_output([*git, 'mktree'], input=b'').decode('ascii').strip()
            revision = subprocess.check_output([*git, 'commit-tree', empty_tree, '-m', 'Synthetic fixture'])
            native = dict(self.audit['native_job_equivalent'], revision=revision.decode('ascii').strip())
            with mock.patch.object(self.validate, 'repo', Path(directory)):
                with self.assertRaisesRegex(AssertionError, 'Present job revision lacks'):
                    self.validate.job_binding_source(native)

    def test_recorded_runs_reject_corruption_and_false_agreement(self):
        workflow = json.loads((EVIDENCE / 'source-workflow.json').read_bytes())
        names = (
            'legacy-baseline.log', 'hosted-core317.json',
            'native-capture.stdout.json', 'native-relocated-verify.stdout.json',
            'native-source-snapshot/snapshot.json', 'scene-capture.stdout.json',
            'scene-verify.stdout.json', 'scene-source-snapshot/snapshot.json',
        )
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(self.validate, 'out', Path(directory)):
            for name in names:
                destination = Path(directory) / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((EVIDENCE / name).read_bytes())
            self.validate.verify_recorded_runs(self.audit, workflow)
            for name, invalid in (('legacy-baseline.log', b'FAILED\n'), ('hosted-core317.json', b'{}')):
                with self.subTest(name=name):
                    destination = Path(directory) / name
                    original = destination.read_bytes()
                    destination.write_bytes(invalid)
                    with self.assertRaisesRegex(AssertionError, 'digest differs'):
                        self.validate.verify_recorded_runs(self.audit, workflow)
                    destination.write_bytes(original)
            workflow['commands'][0]['returncode'] = 1
            with self.assertRaisesRegex(AssertionError, 'successful commands'):
                self.validate.verify_recorded_runs(self.audit, workflow)
            workflow['commands'][0]['returncode'] = 0
            workflow['native_capture_relocated_verify_equal'] = False
            with self.assertRaisesRegex(AssertionError, 'receipts differ'):
                self.validate.verify_recorded_runs(self.audit, workflow)
            workflow['native_capture_relocated_verify_equal'] = True
            (Path(directory) / 'native-relocated-verify.stdout.json').write_bytes(b'{}')
            with self.assertRaisesRegex(AssertionError, 'receipts differ'):
                self.validate.verify_recorded_runs(self.audit, workflow)

    def test_redacted_review_matches_recorded_redaction_hash(self):
        review = self.audit['foundation_review']
        payload = (EVIDENCE / review['report']).read_bytes()
        self.assertEqual(len(payload), review['report_redaction']['bytes'])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), review['report_redaction']['sha256'])

    def test_optimized_validator_rejects_retained_and_git_mismatches(self):
        path = EVIDENCE / 'validate.py'
        namespace = {'__name__': 'optimized_audit_validator', '__file__': str(path)}
        exec(compile(path.read_bytes(), str(path), 'exec', optimize=1), namespace)
        native = json.loads(json.dumps(self.audit['native_job_equivalent']))
        with mock.patch.dict(namespace, {'optional_gitfile': lambda *_: b'different'}):
            with self.assertRaises(AssertionError):
                namespace['job_binding_source'](native)
        native['retained_test_source']['sha256'] = '0' * 64
        with mock.patch.dict(namespace, {'optional_gitfile': lambda *_: None}):
            with self.assertRaises(AssertionError):
                namespace['job_binding_source'](native)

    def test_evidence_contains_no_absolute_local_home_paths(self):
        for path in sorted(EVIDENCE.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                text = path.read_bytes().decode('utf-8', 'replace')
                for marker in ('/' + 'home/', '/' + 'root/'):
                    self.assertNotIn(marker, text, str(path.relative_to(EVIDENCE)))


if __name__ == '__main__':
    unittest.main()
