"""Actual Windows sharing, exact-handle publication, SQLite and reparse tests."""
import os
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import atomic_windows as windows
import atomic_publish as publication


class ActualWindowsPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not publication.windows_private_stage_supported():
            raise RuntimeError('actual Windows publication gate requires Windows APIs')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def junction(self, link, target):
        result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_path_writes_and_by_handle_exclusive_publication(self):
        with publication.windows_private_stage(self.root) as stage:
            (stage.path / 'verified').write_bytes(b'actual verified bytes')
            (stage.path / 'nested').mkdir()
            (stage.path / 'nested/original').write_bytes(b'original')
            stage.publish_to(self.root / 'result')
        self.assertEqual((self.root / 'result/verified').read_bytes(), b'actual verified bytes')
        self.assertEqual((self.root / 'result/nested/original').read_bytes(), b'original')
        with self.assertRaises(ValueError): stage.publish_to(self.root / 'again')

    def test_child_pins_reuse_live_payload_ancestors_and_close_before_publish(self):
        with windows.private_stage(self.root) as stage:
            with stage.pin_child('receipts', create=True) as child:
                with child.pin_child('nested', create=True) as nested:
                    (nested.path / 'verified').write_bytes(b'actual nested bytes')
                    with self.assertRaises(ValueError): stage.publish_to(self.root / 'result')
                    with self.assertRaises(OSError): child.path.rename(stage.path / 'replacement')
            with self.assertRaises(ValueError):
                with child.pin_child('late', create=True): self.fail('expired child admitted')
            stage.publish_to(self.root / 'result')
        self.assertEqual((self.root / 'result/receipts/nested/verified').read_bytes(), b'actual nested bytes')

    def test_registered_child_path_pinning_does_not_reopen_delete_held_payload(self):
        with windows.private_stage(self.root) as stage:
            (stage.path / 'existing').mkdir()
            with windows.pinned_directory(stage.path / 'existing') as child:
                (child.path / 'verified').write_bytes(b'actual verified bytes')
                with self.assertRaises(ValueError): stage.publish_to(self.root / 'result')
            stage.publish_to(self.root / 'result')
        self.assertEqual((self.root / 'result/existing/verified').read_bytes(), b'actual verified bytes')

    def test_child_pinning_rejects_junction_without_touching_foreign_target(self):
        foreign = self.root / 'foreign'; foreign.mkdir(); (foreign / 'original').write_bytes(b'user data')
        with windows.private_stage(self.root) as stage:
            self.junction(stage.path / 'link', foreign)
            with self.assertRaises(ValueError):
                with stage.pin_child('link'): self.fail('junction child admitted')
            for name in ('../escape', 'x/y', 'x:stream'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    with stage.pin_child(name, create=True): self.fail('unsafe child admitted')
        self.assertEqual((foreign / 'original').read_bytes(), b'user data')

    def test_private_wrapper_has_protected_owner_and_system_acl(self):
        with windows.private_stage(self.root) as stage:
            environment = dict(os.environ, HATCH_STAGE_TEST_PATH=str(stage.path.parent))
            command = ("$acl=Get-Acl -LiteralPath $env:HATCH_STAGE_TEST_PATH; "
                       "[pscustomobject]@{protected=$acl.AreAccessRulesProtected; "
                       "owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value; "
                       "user=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                       "identities=@($acl.GetAccessRules($true,$true,"
                       "[System.Security.Principal.SecurityIdentifier]) | "
                       "ForEach-Object {$_.IdentityReference.Value})} | ConvertTo-Json -Compress")
            result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
                                    env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['protected'])
            self.assertEqual(report['owner'], report['user'])
            self.assertEqual(set(report['identities']), {report['user'], 'S-1-5-18'})

    def test_acquisition_replacement_with_foreign_acl_is_rejected_before_payload_io(self):
        original_mkdir = windows._mkdir_private
        replacements = []
        def substitute_before_open(path):
            original_mkdir(path)
            path.rename(self.root / 'original-owned-wrapper')
            path.mkdir()
            (path / 'foreign').write_bytes(b'unrelated original')
            changed = subprocess.run(['icacls', str(path), '/grant', '*S-1-1-0:(OI)(CI)F'],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
            replacements.append(path)
        with mock.patch.object(windows, '_mkdir_private', side_effect=substitute_before_open):
            with self.assertRaises(ValueError):
                with windows.private_stage(self.root): self.fail('foreign ACL stage yielded')
        self.assertEqual(len(replacements), 1)
        self.assertEqual((replacements[0] / 'foreign').read_bytes(), b'unrelated original')
        self.assertFalse((replacements[0] / 'payload').exists())
        self.assertEqual(list((self.root / 'original-owned-wrapper').iterdir()), [])

    def test_payload_namespace_substitution_is_denied_before_publication(self):
        with windows.private_stage(self.root) as stage:
            (stage.path / 'verified').write_bytes(b'verified')
            with self.assertRaises(OSError):
                stage.path.rename(self.root / 'original-payload')
            stage.publish_to(self.root / 'result')
        self.assertEqual((self.root / 'result/verified').read_bytes(), b'verified')
        self.assertFalse((self.root / 'original-payload').exists())

    def test_wrapper_and_all_owned_ancestors_remain_pinned(self):
        ancestor = self.root / 'ancestor'; parent = ancestor / 'parent'; parent.mkdir(parents=True)
        with windows.private_stage(parent) as stage:
            for source, target in ((stage.path.parent, parent / 'renamed'),
                                   (parent, ancestor / 'replacement'),
                                   (ancestor, self.root / 'replacement')):
                with self.subTest(source=source), self.assertRaises(OSError): source.rename(target)
            (stage.path / 'verified').write_bytes(b'verified')
            stage.publish_to(parent / 'result')
        self.assertEqual((parent / 'result/verified').read_bytes(), b'verified')

    def test_existing_immutable_destination_is_never_replaced(self):
        target = self.root / 'result'; target.mkdir(); (target / 'original').write_bytes(b'user data')
        with windows.private_stage(self.root) as stage:
            (stage.path / 'new').write_bytes(b'new data')
            with self.assertRaises(OSError): stage.publish_to(target)
        self.assertEqual((target / 'original').read_bytes(), b'user data')
        self.assertFalse((target / 'new').exists())

    def test_sqlite_creation_and_reopen_use_ordinary_pinned_paths(self):
        with windows.private_stage(self.root) as stage:
            database = sqlite3.connect(stage.path / 'jobs.sqlite')
            try:
                database.execute('create table records(value text)')
                database.execute('insert into records values (?)', ('verified',))
                database.commit()
            finally: database.close()
            stage.publish_to(self.root / 'store')
        database = sqlite3.connect(self.root / 'store/jobs.sqlite')
        try: self.assertEqual(database.execute('select value from records').fetchall(), [('verified',)])
        finally: database.close()

    def test_error_cleanup_and_diagnostic_retention(self):
        original = RuntimeError('cancelled')
        with self.assertRaises(RuntimeError) as caught:
            with windows.private_stage(self.root) as stage:
                (stage.path / 'owned').write_bytes(b'diagnostic'); wrapper = stage.path.parent
                raise original
        self.assertIs(caught.exception, original)
        self.assertEqual(list(wrapper.iterdir()), [])
        with self.assertRaises(RuntimeError):
            with windows.private_stage(self.root, cleanup_on_error=False) as stage:
                (stage.path / 'owned').write_bytes(b'diagnostic'); retained = stage.path
                raise RuntimeError('retain')
        self.assertEqual((retained / 'owned').read_bytes(), b'diagnostic')

    def test_reparse_ancestor_is_rejected_without_touching_target(self):
        target = self.root / 'foreign'; target.mkdir(); (target / 'original').write_bytes(b'user bytes')
        link = self.root / 'link'; self.junction(link, target)
        with self.assertRaises(ValueError):
            with windows.private_stage(link): self.fail('reparse ancestor admitted')
        self.assertEqual([p.name for p in target.iterdir()], ['original'])
        self.assertEqual((target / 'original').read_bytes(), b'user bytes')

    def test_cleanup_deletes_junction_itself_and_preserves_foreign_target(self):
        foreign = self.root / 'foreign'; foreign.mkdir(); (foreign / 'original').write_bytes(b'user bytes')
        with windows.private_stage(self.root) as stage:
            wrapper = stage.path.parent
            self.junction(stage.path / 'link', foreign)
            (stage.path / 'nested').mkdir()
            (stage.path / 'nested/owned').write_bytes(b'owned')
        self.assertEqual(list(wrapper.iterdir()), [])
        self.assertEqual((foreign / 'original').read_bytes(), b'user bytes')

    def test_unicode_destination_uses_exact_utf16_byte_length(self):
        destination = self.root / 'result-\U0001f408-\u00e9'
        with windows.private_stage(self.root) as stage:
            (stage.path / 'verified').write_bytes(b'original')
            stage.publish_to(destination)
        self.assertEqual((destination / 'verified').read_bytes(), b'original')

    def test_invalid_names_and_drive_relative_paths_fail_closed(self):
        for path in ('C:relative', self.root / '..' / 'escape'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                with windows.private_stage(path): self.fail('unsafe path admitted')
        for options in ({'prefix': '../escape'}, {'cleanup': 1}, {'cleanup_on_error': None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                with windows.private_stage(self.root, **options): self.fail('invalid policy admitted')
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__': unittest.main()
