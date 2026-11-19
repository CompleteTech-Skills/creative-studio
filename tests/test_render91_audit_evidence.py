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
import shutil
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
        names = ['legacy-baseline.log', 'hosted-core317.json',
                 *(row['path'] for row in workflow['artifacts'])]
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

    def test_recorded_snapshot_adapters_match_their_cli_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            out = shutil.copytree(EVIDENCE, Path(directory) / 'evidence')
            workflow = json.loads((out / 'source-workflow.json').read_bytes())
            exchanges = (
                ('native-source-snapshot', 'scene-source-snapshot'),
                ('native-capture.stdout.json', 'scene-capture.stdout.json'),
                ('native-relocated-verify.stdout.json', 'scene-verify.stdout.json'),
            )
            intermediate = out / 'swap'
            for left, right in exchanges:
                (out / left).rename(intermediate)
                (out / right).rename(out / left)
                intermediate.rename(out / right)
            for row in workflow['artifacts']:
                for left, right in exchanges:
                    if row['path'] == left or row['path'].startswith(left + '/'):
                        row['path'] = right + row['path'][len(left):]
                        break
                    if row['path'] == right or row['path'].startswith(right + '/'):
                        row['path'] = left + row['path'][len(right):]
                        break
            with mock.patch.object(self.validate, 'out', out):
                with self.assertRaisesRegex(AssertionError, 'adapter differs from recorded operation'):
                    self.validate.verify_recorded_runs(self.audit, workflow)

    def test_snapshot_members_must_match_the_workflow_inventory_and_bytes(self):
        for member in ('native-source-snapshot/source/render91-fixture.glb',
                       'scene-source-snapshot/triangle.obj'):
            for change in ('omit', 'rehash', 'retained-bytes', 'extra'):
                with self.subTest(member=member, change=change), tempfile.TemporaryDirectory() as directory:
                    out = shutil.copytree(EVIDENCE, Path(directory) / 'evidence')
                    workflow = json.loads((out / 'source-workflow.json').read_bytes())
                    row = next(row for row in workflow['artifacts'] if row['path'] == member)
                    if change == 'omit':
                        (out / member).unlink()
                        workflow['artifacts'].remove(row)
                    elif change == 'extra':
                        (out / member).with_suffix('.extra').write_bytes(b'unlisted')
                    else:
                        (out / member).write_bytes(b'changed')
                        if change == 'rehash':
                            row.update(bytes=7, sha256=hashlib.sha256(b'changed').hexdigest())
                    with mock.patch.object(self.validate, 'out', out), self.assertRaises(AssertionError):
                        self.validate.verify_recorded_runs(self.audit, workflow)

    def test_recorded_commands_bind_program_operation_options_and_paths(self):
        for index, position, replacement in (
                (0, None, ['unrelated-program']), (0, 1, 'other.py'), (0, 2, 'snapshot-scene'),
                (0, 3, '--destination'), (0, 4, 'other/source'), (1, 3, 'native-snapshot'),
                (2, 6, 'other/scene-snapshot'), (3, 3, 'other/scene-snapshot'),
                (3, 4, '--other'), (3, 5, 'native')):
            with self.subTest(index=index, position=position):
                workflow = json.loads((EVIDENCE / 'source-workflow.json').read_bytes())
                command = workflow['commands'][index]
                if position is None:
                    command['argv'] = replacement
                else:
                    command['argv'][position] = replacement
                with self.assertRaisesRegex(AssertionError, 'argv'):
                    self.validate.verify_recorded_runs(self.audit, workflow)

    def test_review_claims_must_agree_with_the_redacted_report(self):
        for key, value in (('focused_passed', 58), ('schema_math_passed', 177),
                           ('newly_spawned_whole_feature', 'FAIL'), ('schema_template_parity', 'FAIL'),
                           ('foundation_revision', '0' * 40)):
            with self.subTest(key=key):
                audit = dict(self.audit, foundation_review=dict(self.audit['foundation_review']))
                target = audit if key == 'foundation_revision' else audit['foundation_review']
                target[key] = value
                with self.assertRaisesRegex(AssertionError, 'review'):
                    self.validate.verify_foundation_review(audit)

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
