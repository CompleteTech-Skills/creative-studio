"""Exact private admission boundaries for legacy native and geometry readers."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rig_errors import RigError
from rig_models import load_rig
from rig_test_helpers import fixture
from scene3d_capture import capture_verified_files, capture_verified_native


class VerifiedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        (self.root/'geometry').mkdir()
        (self.root/'geometry/mesh.obj').write_bytes(b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        (self.root/'texture.png').write_bytes(b'exact texture bytes')
        self.inventory={p.relative_to(self.root).as_posix():dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),bytes=p.stat().st_size)
                        for p in self.root.rglob('*') if p.is_file()}

    def test_selected_closure_is_exact_and_private_until_context_exit(self):
        original=(self.root/'geometry/mesh.obj').read_bytes()
        with capture_verified_files(self.root,self.inventory) as captured:
            self.assertNotEqual(captured,self.root)
            self.assertEqual((captured/'geometry/mesh.obj').read_bytes(),original)
            (self.root/'geometry/mesh.obj').write_bytes(b'replaced caller owned bytes')
            self.assertEqual((captured/'geometry/mesh.obj').read_bytes(),original)
            self.assertEqual({p.relative_to(captured).as_posix() for p in captured.rglob('*') if p.is_file()},set(self.inventory))
        self.assertFalse(captured.exists())

    def test_generic_actor_manifest_is_loaded_only_from_private_subtree(self):
        rig=fixture(self.root,name='actors/hero')
        inventory={p.relative_to(self.root).as_posix():dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),bytes=p.stat().st_size)
                   for p in self.root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
        with capture_verified_native(self.root,'actors/hero/rig.json',inventory) as admitted:
            self.assertTrue((admitted/'rig.json').is_file())
            self.assertFalse((admitted.parent.parent/'texture.png').exists())
            observed=load_rig(admitted)
            self.assertEqual(observed.manifest,rig.manifest)
            self.assertEqual(observed.geometry.vertex_count,rig.geometry.vertex_count)
        self.assertFalse(admitted.exists())

    def test_hash_bytes_and_missing_file_fail_before_yield_without_publishing(self):
        for change in ('hash','bytes','missing'):
            inventory=deepcopy(self.inventory)
            if change=='hash':inventory['geometry/mesh.obj']['sha256']='0'*64
            elif change=='bytes':inventory['geometry/mesh.obj']['bytes']+=1
            else:inventory['missing.json']=dict(sha256='0'*64)
            with self.subTest(change=change), self.assertRaises(RigError):
                with capture_verified_files(self.root,inventory):self.fail('Unverified closure was yielded')

    def test_duplicate_case_directory_alias_traversal_and_unbound_manifest_fail(self):
        record=dict(path='geometry/mesh.obj',**self.inventory['geometry/mesh.obj'])
        for records in ([record,record], [record,dict(record,path='Geometry/mesh.obj')],
                        [record,dict(record,path='geometry')], [dict(record,path='../outside')],
                        [dict(record,sha256='bad')], []):
            with self.subTest(records=records),self.assertRaises(RigError):
                with capture_verified_files(self.root,records):self.fail('Invalid inventory yielded')
        with self.assertRaises(RigError):
            with capture_verified_native(self.root,'actors/missing/rig.json',self.inventory):self.fail('Unbound native manifest yielded')

    def test_source_and_inventory_mutation_after_first_read_cannot_replace_admitted_bytes(self):
        import scene3d_capture
        read=scene3d_capture.read_source_bytes
        records=[dict(path=p,**r) for p,r in self.inventory.items()]
        original=(self.root/'geometry/mesh.obj').read_bytes()
        def mutate(path,limit):
            payload=read(path,limit)
            if Path(path).name=='mesh.obj':
                Path(path).write_bytes(b'mutated source')
                records[0]['sha256']='0'*64
            return payload
        with patch.object(scene3d_capture,'read_source_bytes',mutate):
            with capture_verified_files(self.root,records) as captured:
                self.assertEqual((captured/'geometry/mesh.obj').read_bytes(),original)

    def test_file_count_and_aggregate_bounds_are_enforced(self):
        from rig_contracts import DEFAULT_LIMITS
        with patch.dict(DEFAULT_LIMITS,archive_files=1),self.assertRaises(RigError):
            with capture_verified_files(self.root,self.inventory):self.fail('Count budget ignored')
        with patch.dict(DEFAULT_LIMITS,dependency_bytes=1),self.assertRaises(RigError):
            with capture_verified_files(self.root,self.inventory):self.fail('Aggregate budget ignored')

    @unittest.skipUnless(hasattr(os,'symlink'),'Requires symbolic links')
    def test_symlink_file_or_ancestor_is_rejected(self):
        target=self.root/'geometry/mesh.obj'
        raw=target.read_bytes();target.unlink()
        try: target.symlink_to(self.root/'texture.png')
        except OSError as exc:
            if os.name == 'nt': self.skipTest('Windows symlink creation privilege is unavailable: '+str(exc))
            raise
        with self.assertRaises(RigError):
            with capture_verified_files(self.root,self.inventory):self.fail('Symlink yielded')
        target.unlink();target.write_bytes(raw)
        moved=self.root/'moved';(self.root/'geometry').rename(moved);(self.root/'geometry').symlink_to(moved,target_is_directory=True)
        with self.assertRaises(RigError):
            with capture_verified_files(self.root,self.inventory):self.fail('Symlink ancestor yielded')

    @unittest.skipUnless(hasattr(os,'mkfifo'),'Requires POSIX FIFO')
    def test_fifo_rejected_with_bounded_actual_subprocess(self):
        path=self.root/'texture.png';path.unlink();os.mkfifo(path)
        code='''import json,sys
from rig_errors import RigError
from scene3d_capture import capture_verified_files
try:
 with capture_verified_files(sys.argv[1],json.loads(sys.argv[2])): raise AssertionError('FIFO admitted')
except RigError: pass
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.root),json.dumps(self.inventory)],
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_exact_empty_evidence_survives_complete_project_capture_and_reopen(self):
        from scene3d_example import build_example
        from scene3d_contracts import canonical_bytes,load_project
        root=self.root/'project'
        bundle=build_example(root)
        (root/'empty.log').write_bytes(b'')
        project=deepcopy(bundle['project'])
        project['dependency_lock'].append(dict(id='empty-evidence',version='1.0.0',kind='evidence',
            path='empty.log',sha256=hashlib.sha256(b'').hexdigest()))
        (root/'project.json').write_bytes(canonical_bytes(project))
        verified=load_project(root)
        with capture_verified_files(root,verified['inventory']) as captured:
            self.assertEqual((captured/'empty.log').read_bytes(),b'')
            self.assertEqual(load_project(captured)['inventory'],verified['inventory'])
        from asset_library import read_source_bytes
        with self.assertRaises(RigError):read_source_bytes(root/'empty.log')
        with patch.dict(__import__('rig_contracts').DEFAULT_LIMITS,dependency_bytes=0):
            with capture_verified_files(root,[dict(path='empty.log',sha256=hashlib.sha256(b'').hexdigest(),bytes=0)]) as captured:
                self.assertEqual((captured/'empty.log').read_bytes(),b'')

    def test_caller_list_metadata_is_copied_before_validation_and_cannot_escape(self):
        import scene3d_capture
        record=dict(path='texture.png',**self.inventory['texture.png'])
        validate=scene3d_capture.portable_path
        def mutate(path):
            result=validate(path)
            record['path']='../escape.bin'
            return result
        with patch.object(scene3d_capture,'portable_path',mutate):
            with capture_verified_files(self.root,[record]) as captured:
                self.assertEqual((captured/'texture.png').read_bytes(),b'exact texture bytes')
                self.assertFalse((captured.parent/'escape.bin').exists())
        self.assertEqual(record['path'],'../escape.bin')

    def test_trailing_registered_empty_file_does_not_exceed_exact_aggregate_budget(self):
        from rig_contracts import DEFAULT_LIMITS
        (self.root/'zzz-empty.log').write_bytes(b'')
        inventory={**deepcopy(self.inventory),'zzz-empty.log':dict(sha256=hashlib.sha256(b'').hexdigest(),bytes=0)}
        exact=sum(record['bytes'] for record in inventory.values())
        with patch.dict(DEFAULT_LIMITS,dependency_bytes=exact):
            with capture_verified_files(self.root,inventory) as captured:
                self.assertEqual((captured/'zzz-empty.log').read_bytes(),b'')
                self.assertEqual(sum(p.stat().st_size for p in captured.rglob('*') if p.is_file()),exact)
