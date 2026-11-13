from copy import deepcopy
from contextlib import contextmanager
import os
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from rig_test_helpers import fixture
import rig_package
from rig_contracts import validate_contract
from rig_corrections import apply_rig_corrections
from rig_errors import RigError,atomic_json,job_lock,read_json,sha256,timestamp
from rig_jobs import run_step
from rig_models import load_rig,rig_signature
from rig_package import package_rig,restore_rig
from rig_review import apply_review
from rig_validation import REVIEW_DIMENSIONS,validate_rig
from rig_states import map_states


class RigWorkflowTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.rig=fixture(self.temp.name)
    def correction(self,edits):
        r=self.rig;return dict(schema_version=1,kind='hatch-3d-rig-correction',rig_id=r.manifest['id'],rig_version=r.manifest['version'],
            expected_sha256=rig_signature(r.manifest),author='unit fixture',reason='Explicit test correction',created_at=timestamp(),edits=edits)
    def test_source_bytes_identity_and_fixture_cannot_silently_drift(self):
        r=self.rig;self.assertTrue(r.manifest['source']['fixture']);self.assertEqual(sha256(r.root/r.manifest['source']['path']),r.manifest['source']['sha256'])
        (r.root/'identity.json').write_text('{}')
        with self.assertRaises(RigError):load_rig(r.root)
    def test_strict_schema_rejects_unknown_and_nonfinite(self):
        for mutate in (lambda v:v.update(extra=True),lambda v:v['skeleton']['bones'][0]['head'].__setitem__(0,float('nan'))):
            value=deepcopy(self.rig.manifest);mutate(value)
            with self.assertRaises(RigError):validate_contract(value,'Rig')
    def test_structural_validation_requires_real_skin_and_motion(self):
        self.assertTrue(validate_rig(self.rig,require_animation=True)['ok']);self.rig.weights={}
        self.assertFalse(validate_rig(self.rig,require_animation=True)['ok'])
    def test_bone_correction_invalidates_dependents_and_retains_original(self):
        before=sha256(self.rig.root/self.rig.manifest['source']['path'])
        correction=self.correction([dict(operation='bone-update',target='b1',value={'head':[.1,1,0]})])
        updated,report=apply_rig_corrections(self.rig,correction)
        self.assertFalse(updated.weights);self.assertFalse(updated.clips);self.assertIn('skins',report['invalidated'])
        self.assertEqual(sha256(updated.root/updated.manifest['source']['path']),before);self.assertTrue(list((updated.root/'rig/revisions').glob('*.json')))
        with self.assertRaises(RigError):apply_rig_corrections(updated,correction)
    def test_rename_remaps_hierarchy_and_ik(self):
        correction=self.correction([dict(operation='bone-update',target='b1',value={'id':'elbow'})])
        updated,_=apply_rig_corrections(self.rig,correction,persist=False)
        self.assertIn('elbow',updated.manifest['controllers']['ik'][0]['bones']);self.assertEqual(updated.skeleton['bones'][-1]['parent'],'elbow')
    def test_bad_edit_is_transactional(self):
        before=sha256(self.rig.root/'rig.json')
        with self.assertRaises(RigError):apply_rig_corrections(self.rig,self.correction([dict(operation='bone-update',target='b1',value={'parent':'b2'})]))
        self.assertEqual(before,sha256(self.rig.root/'rig.json'))
    def test_weight_edit_locks_vertex_without_removing_clips(self):
        updated,_=apply_rig_corrections(self.rig,self.correction([dict(operation='weights',target='strip',value={'assignments':{'0':{'b1':1.}}})]))
        self.assertIn(0,updated.weights['strip'].locked);self.assertIn('bend',updated.clips)
    def test_job_cache_bound_to_artifacts_and_single_writer(self):
        root=self.rig.root;calls=[]
        def action(r):calls.append(1);atomic_json(r.root/'validation/check.json',{'ok':True});return {'artifact_paths':['validation/check.json']}
        self.assertEqual(run_step(root,'probe',{},action,status='structurally-valid')['status'],'succeeded')
        self.assertEqual(run_step(root,'probe',{},action,status='structurally-valid')['status'],'cached');self.assertEqual(len(calls),1)
        (root/'validation/check.json').write_text('{}');run_step(root,'probe',{},action,status='structurally-valid');self.assertEqual(len(calls),2)
        with job_lock(root):
            with self.assertRaises(RigError):run_step(root,'other',{},action,status='structurally-valid')
    def test_failed_attempts_are_bounded_and_error_preserved(self):
        def action(r):raise RigError('TEST_FAILURE','Retained failure')
        for _ in range(3):
            with self.assertRaises(RigError):run_step(self.rig.root,'failure',{},action,status='structurally-valid')
        with self.assertRaises(RigError) as error:run_step(self.rig.root,'failure',{},action,status='structurally-valid')
        self.assertEqual(error.exception.report['code'],'RIG_ATTEMPTS');self.assertEqual(read_json(self.rig.root/'job.json')['steps']['failure']['attempts'],3)
        self.assertTrue((self.rig.root/'rig.json').is_file())
    def test_archive_restore_relocates_and_rejects_traversal(self):
        path=Path(self.temp.name)/'rig.zip';package_rig(self.rig.root,path)
        out=Path(self.temp.name)/'relocated';restore_rig(path,out);self.assertEqual(rig_signature(load_rig(out).manifest),rig_signature(self.rig.manifest))
        with zipfile.ZipFile(Path(self.temp.name)/'bad.zip','w') as z:z.writestr('../escape','bad')
        with self.assertRaises(RigError):restore_rig(Path(self.temp.name)/'bad.zip',Path(self.temp.name)/'bad')
        self.assertFalse((Path(self.temp.name)/'escape').exists())
    def test_package_preserves_existing_destination(self):
        destination = Path(self.temp.name) / 'existing.zip'
        destination.write_bytes(b'user-owned archive')
        before = destination.stat()
        with self.assertRaises(RigError) as error:
            package_rig(self.rig.root, destination)
        self.assertEqual(error.exception.report['code'], 'RIG_ARCHIVE_EXISTS')
        self.assertEqual(destination.read_bytes(), b'user-owned archive')
        self.assertEqual(destination.stat().st_ino, before.st_ino)

    def test_package_exclusive_publication_preserves_racing_file(self):
        destination = Path(self.temp.name) / 'racing.zip'
        original = rig_package._rename_no_replace
        def race(stage, target):
            if Path(target) == destination:
                target.write_bytes(b'concurrent writer')
            return original(stage, target)
        with patch.object(rig_package, '_rename_no_replace', side_effect=race):
            with self.assertRaises(FileExistsError):
                package_rig(self.rig.root, destination)
        self.assertEqual(destination.read_bytes(), b'concurrent writer')
        self.assertFalse(list(Path(self.temp.name).glob('.rig-package-*')))

    def test_package_holds_run_writer_lock(self):
        with job_lock(self.rig.root):
            with self.assertRaises(RigError) as error:
                package_rig(self.rig.root, Path(self.temp.name) / 'busy.zip')
        self.assertEqual(error.exception.report['code'], 'RIG_BUSY')

    def test_package_rejects_source_change_after_inventory(self):
        extra = self.rig.root / 'extra.bin'
        extra.write_bytes(b'original')
        original = rig_package._write_package_entry
        def changed(archive, root, entry):
            if entry['path'] == 'extra.bin':
                extra.write_bytes(b'modified')
            return original(archive, root, entry)
        destination = Path(self.temp.name) / 'changed.zip'
        with patch.object(rig_package, '_write_package_entry', side_effect=changed):
            with self.assertRaises(RigError) as error:
                package_rig(self.rig.root, destination)
        self.assertEqual(error.exception.report['code'], 'RIG_ARCHIVE_CHANGED')
        self.assertFalse(destination.exists())
        self.assertFalse(list(Path(self.temp.name).glob('.rig-package-*')))

    def test_package_rejects_new_file_during_capture(self):
        original = rig_package._write_package_entry
        def changed(archive, root, entry):
            result = original(archive, root, entry)
            (root / 'racing-addition.bin').write_bytes(b'new')
            return result
        destination = Path(self.temp.name) / 'changed.zip'
        with patch.object(rig_package, '_write_package_entry', side_effect=changed):
            with self.assertRaises(RigError) as error:
                package_rig(self.rig.root, destination)
        self.assertEqual(error.exception.report['code'], 'RIG_ARCHIVE_CHANGED')
        self.assertFalse(destination.exists())

    def test_package_streams_payloads_and_is_deterministic(self):
        extra = self.rig.root / 'large.bin'
        extra.write_bytes(b'bounded streaming payload' * 100000)
        original = Path.read_bytes
        def read(path):
            self.assertNotEqual(path, extra, 'Archive payload must be streamed')
            return original(path)
        first, second = (Path(self.temp.name) / name for name in ('one.zip', 'two.zip'))
        with patch.object(Path, 'read_bytes', read):
            package_rig(self.rig.root, first)
            package_rig(self.rig.root, second)
        self.assertEqual(sha256(first), sha256(second))
        restored = Path(self.temp.name) / 'streamed'
        restore_rig(first, restored)
        self.assertEqual(sha256(extra), sha256(restored / 'large.bin'))

    def test_package_rejects_special_files_without_blocking(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('POSIX named pipes are unavailable')
        os.mkfifo(self.rig.root / 'fifo')
        with self.assertRaises(RigError) as error:
            package_rig(self.rig.root, Path(self.temp.name) / 'pipe.zip')
        self.assertEqual(error.exception.report['code'], 'RIG_MISSING_ASSET')

    def test_restore_rejects_malformed_inventory_with_structured_error(self):
        values = [[], {'schema_version': True, 'kind': 'hatch-3d-rig-package',
                       'rig_sha256': '0' * 64, 'files': []},
                  {'schema_version': 1, 'kind': 'hatch-3d-rig-package',
                   'rig_sha256': '0' * 64, 'files': [None]},
                  {'schema_version': 1, 'kind': 'hatch-3d-rig-package',
                   'rig_sha256': '0' * 64, 'files': [{'path': 1, 'sha256': '0' * 64, 'bytes': 0}]}]
        for index, value in enumerate(values):
            archive = Path(self.temp.name) / ('bad-inventory-%s.zip' % index)
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('inventory.json', json.dumps(value))
            with self.subTest(value=value), self.assertRaises(RigError) as error:
                restore_rig(archive, Path(self.temp.name) / ('bad-restore-%s' % index))
            self.assertEqual(error.exception.report['code'], 'RIG_ARCHIVE_INVENTORY')

    def test_package_directory_scan_stops_at_budget(self):
        root = Path(self.temp.name) / 'many-files'
        root.mkdir()
        for index in range(100):
            (root / str(index)).write_bytes(b'')
        original = os.scandir
        consumed = []
        @contextmanager
        def counted(path):
            with original(path) as entries:
                def iterate():
                    for entry in entries:
                        consumed.append(entry.name)
                        yield entry
                yield iterate()
        limits = dict(self.rig.manifest['limits'], archive_files=2)
        with patch.object(rig_package.os, 'scandir', counted):
            with self.assertRaises(RigError) as error:
                rig_package._package_entries(root, limits)
        self.assertEqual(error.exception.report['code'], 'RIG_ARCHIVE_LIMIT')
        self.assertLessEqual(len(consumed), 3)

    def test_restore_rejects_aliased_inventory_name(self):
        archive = Path(self.temp.name) / 'aliased-inventory.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('INVENTORY.JSON', '{}')
        with self.assertRaises(RigError) as error:
            restore_rig(archive, Path(self.temp.name) / 'aliased-restore')
        self.assertEqual(error.exception.report['code'], 'RIG_ARCHIVE_INVENTORY')

    def test_restore_bounds_inventory_before_decompression(self):
        archive = Path(self.temp.name) / 'oversized-inventory.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr('inventory.json', b' ' * 65)
        with patch.object(rig_package, 'MAX_JSON_BYTES', 64):
            with self.assertRaises(RigError) as error:
                restore_rig(archive, Path(self.temp.name) / 'too-large')
        self.assertEqual(error.exception.report['code'], 'RIG_ARCHIVE_INVENTORY')

    def test_repeated_demo_verification_preserves_retained_archives(self):
        import verify_rig_demos
        root = Path(self.temp.name) / 'demo-verification'
        root.mkdir()
        for name in ('humanoid-retarget', 'quadruped-retarget'):
            fixture(root, name=name)
        packages = root / 'packages'
        packages.mkdir()
        retained = packages / 'humanoid-retarget.zip'
        retained.write_bytes(b'previous retained archive')
        with patch.object(verify_rig_demos, 'ARCHETYPES', ()):
            first = verify_rig_demos.verify(root, archives=True)
            second = verify_rig_demos.verify(root, archives=True)
        self.assertEqual(retained.read_bytes(), b'previous retained archive')
        first_paths = [item['archive']['path'] for item in first['fixtures']]
        second_paths = [item['archive']['path'] for item in second['fixtures']]
        self.assertTrue(set(first_paths).isdisjoint(second_paths))
        for report in (first, second):
            for item in report['fixtures']:
                path = root / item['archive']['path']
                self.assertEqual(sha256(path), item['archive']['sha256'])
                self.assertTrue(item['archive']['restored_and_evaluated'])

    def test_restore_rejects_dangling_destination_symlink_before_extraction(self):
        home = Path(self.temp.name)
        archive = home / 'rig.zip'
        package_rig(self.rig.root, archive)
        destination = home / 'restored'
        target = home / 'missing-target'
        try:
            destination.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows symlink creation privilege is unavailable: ' + str(error))
            raise
        before = destination.lstat()
        linked_target = destination.readlink()

        with patch.object(rig_package.tempfile, 'mkdtemp', wraps=tempfile.mkdtemp) as create_stage:
            with self.assertRaises(RigError) as error:
                restore_rig(archive, destination)
        self.assertEqual(error.exception.report['code'], 'RIG_RESTORE_EXISTS')
        create_stage.assert_not_called()
        self.assertTrue(destination.is_symlink())
        after = destination.lstat()
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertEqual(destination.readlink(), linked_target)
        self.assertFalse(target.exists())
        self.assertFalse(list(home.glob('.rig-restore-*')))
    def test_restore_preserves_empty_destination_racing_publication(self):
        home = Path(self.temp.name)
        archive = home / 'rig.zip'
        package_rig(self.rig.root, archive)
        destination = home / 'restored'
        unrelated = home / '.rig-restore-unrelated'
        unrelated.mkdir()
        (unrelated / 'keep.txt').write_bytes(b'Independent staging directory')
        original_publish = rig_package._rename_no_replace
        staged, created = [], []

        def race(stage, target):
            staged.append(stage)
            target.mkdir()
            status = target.stat()
            created.append((status.st_dev, status.st_ino))
            return original_publish(stage, target)

        with patch.object(rig_package, '_rename_no_replace', side_effect=race):
            with self.assertRaises(FileExistsError):
                restore_rig(archive, destination)
        self.assertEqual(len(created), 1)
        status = destination.stat()
        self.assertEqual((status.st_dev, status.st_ino), created[0])
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(staged[0].exists())
        self.assertEqual(list(home.glob('.rig-restore-*')), [unrelated])
        self.assertEqual((unrelated / 'keep.txt').read_bytes(), b'Independent staging directory')
    def test_duplicate_archive_names_and_tamper_are_rejected(self):
        path=Path(self.temp.name)/'rig.zip';package_rig(self.rig.root,path)
        with zipfile.ZipFile(path,'a') as z:z.writestr('extra.txt','not in inventory')
        with self.assertRaises(RigError):restore_rig(path,Path(self.temp.name)/'bad')
    def test_synthetic_receipt_cannot_approve_and_stale_evidence_fails(self):
        r=self.rig;atomic_json(r.root/'validation/evidence.json',{'rendered':False})
        receipt=dict(schema_version=1,kind='hatch-3d-rig-review',rig_id=r.manifest['id'],rig_version=r.manifest['version'],expected_sha256=rig_signature(r.manifest),
            author='synthetic fixture',reviewer_kind='synthetic-test',created_at=timestamp(),fixture=True,
            evidence=[dict(path='validation/evidence.json',sha256=sha256(r.root/'validation/evidence.json'))],
            dimensions={k:dict(verdict='approved',note='Synthetic test, not a real visual review.') for k in REVIEW_DIMENSIONS})
        with self.assertRaises(RigError):apply_review(r,receipt)
        receipt['dimensions']={k:dict(verdict='unreviewed',note='Pending human inspection.') for k in REVIEW_DIMENSIONS}
        self.assertEqual(apply_review(r,receipt)['status'],'pending')
        (r.root/'validation/evidence.json').write_text('{}')
        with self.assertRaises(RigError):apply_review(r,receipt)
    def test_state_catalog_is_selective_and_missing_capability_is_explicit(self):
        result=map_states(self.rig,recipes={'waving':'bend','static-neutral':'bend'},selected=['waving','static-neutral','flying'])
        self.assertEqual(result['total_catalog_states'],416);self.assertFalse(result['all_416_animated'])
        self.assertEqual(result['mapping']['waving']['status'],'requires-rig-capability')
        self.assertEqual(result['mapping']['static-neutral']['status'],'needs-review')
