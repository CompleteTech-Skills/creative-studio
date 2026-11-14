"""Exact standalone clip closure admission with actual native rigs and readers."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).resolve().parent)]
from animation3d_assets import admit_clip, admit_clips
from animation3d_clips import source_time
from animation3d_contracts import KINDS
from rig_errors import RigError, object_hash
import test_animation3d_closure as fixtures

class StandaloneAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.NativeClosureRegressionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.rig,self.clip,self.rigref=self.f.bound_clip()
        self.reference=deepcopy(self.f.project['clips'][0])

    def sources(self):
        return {str(path.relative_to(self.f.root)):path.read_bytes() for path in self.f.root.rglob('*') if path.is_file()}

    def reader(self, files):
        def read(path, maximum):
            value=files[path]
            if len(value)>maximum:raise RigError('ANIMATION_LIMIT','Caller source read exceeds byte allowance.')
            return value
        return read

    def refresh(self):
        self.reference=self.f.put('clip.json',self.clip,'clip')

    def test_actual_filesystem_closure_and_source_hashes(self):
        before=self.sources()
        receipt=admit_clip(self.f.root,self.reference)
        self.assertTrue(receipt['source_ready'])
        self.assertEqual(receipt['clip'],self.clip)
        self.assertEqual(receipt['inventory']['clip.json']['sha256'],self.reference['sha256'])
        self.assertNotIn('animation-project.json',receipt['inventory'])
        self.assertEqual(receipt['native_context']['rig_bindings'][0]['vertices'],4)
        self.assertLessEqual(receipt['memory_admission']['estimated_bytes'],receipt['memory_admission']['limit_bytes'])
        self.assertEqual(self.sources(),before)

    def test_rootless_byte_map_reuses_the_same_actual_native_closure(self):
        files=self.sources();calls=[]
        original=self.reader(files)
        def reader(name,maximum):calls.append((name,maximum));return original(name,maximum)
        actual=dict(self.reference,kind=KINDS['clip'])
        receipt=admit_clip(None,actual,reader=reader)
        self.assertEqual(receipt['clip'],self.clip)
        self.assertEqual(set(receipt['inventory']),set(name for name,_ in calls))
        self.assertIn('fixture/rig.json',receipt['inventory'])
        self.assertEqual(receipt['native_context']['actor_ids'],['actor'])
        self.assertEqual(self.sources(),files)

    def test_raw_file_hash_is_distinct_from_canonical_document_hash(self):
        self.assertNotEqual(self.reference['sha256'],object_hash(self.clip))
        with self.assertRaises(RigError) as rejected:
            admit_clip(self.f.root,dict(self.reference,sha256=object_hash(self.clip)))
        self.assertEqual(rejected.exception.report['code'],'ANIMATION_HASH')
        for field,value in (('id','different'),('version','2.0.0')):
            with self.subTest(field=field),self.assertRaises(RigError) as identity:
                admit_clip(self.f.root,dict(self.reference,**{field:value}))
            self.assertEqual(identity.exception.report['code'],'ANIMATION_IDENTITY')

    def test_reader_tamper_missing_dependency_and_mutable_payload_fail(self):
        files=self.sources();files['clip.json']+=b'\n'
        with self.assertRaises(RigError) as rejected:admit_clip(None,self.reference,reader=self.reader(files))
        self.assertEqual(rejected.exception.report['code'],'ANIMATION_HASH')
        files=self.sources();del files['fixture/rig.json']
        with self.assertRaises(RigError):admit_clip(None,self.reference,reader=self.reader(files))
        files=self.sources()
        with self.assertRaises(RigError) as mutable:
            admit_clip(None,self.reference,reader=lambda name,maximum:bytearray(files[name]))
        self.assertEqual(mutable.exception.report['code'],'ANIMATION_SOURCE')

    def test_unregistered_targets_rig_signature_and_missing_binding_reject(self):
        self.clip['tracks'][0]['target']['bone_id']='fabricated';self.refresh()
        with self.assertRaises(RigError) as rejected:admit_clip(self.f.root,self.reference)
        self.assertEqual(rejected.exception.report['code'],'ANIMATION_TARGET')
        self.clip['tracks'][0]['target']['bone_id']='b0';self.clip['skeleton_signature']='0'*64;self.refresh()
        with self.assertRaises(RigError) as signature:admit_clip(self.f.root,self.reference)
        self.assertEqual(signature.exception.report['code'],'ANIMATION_TARGET')
        del self.clip['skeleton_signature'];del self.clip['rig_binding'];self.refresh()
        with self.assertRaises(RigError) as binding:admit_clip(self.f.root,self.reference)
        self.assertEqual(binding.exception.report['code'],'ANIMATION_TARGET')

    def test_limits_are_applied_before_callback_and_native_materialization(self):
        from unittest.mock import patch
        calls=[];files=self.sources()
        for policy in (dict(max_work_bytes=1),dict(max_memory_bytes=1000),dict(max_bones_per_rig=1),dict(max_vertices=1)):
            with self.subTest(policy=policy),self.assertRaises(RigError):
                admit_clip(None,self.reference,budgets=policy,reader=self.reader(files))
        def reader(name,maximum):calls.append(maximum);return files[name]
        with self.assertRaises(RigError) as over:
            admit_clip(None,self.reference,budgets=dict(max_work_bytes=1),reader=reader)
        self.assertEqual(calls,[1]);self.assertEqual(over.exception.report['code'],'ANIMATION_LIMIT')
        with patch('rig_models.load_geometry',side_effect=AssertionError('materialized despite low caller memory')):
            with self.assertRaises(RigError) as low:
                admit_clip(self.f.root,self.reference,budgets=dict(max_memory_bytes=1024))
        self.assertEqual(low.exception.report['code'],'ANIMATION_LIMIT')

    def test_policy_can_only_narrow_and_whole_float_caps_are_equivalent(self):
        for supplied in (dict(unknown=1),dict(max_work_bytes=True),dict(max_memory_bytes=10**400),dict(max_work_bytes=1.5)):
            with self.subTest(policy=supplied),self.assertRaises(RigError):admit_clip(self.f.root,self.reference,budgets=supplied)
        before=deepcopy(self.reference)
        a=admit_clip(self.f.root,self.reference,budgets=dict(max_memory_bytes=16*1024*1024))
        b=admit_clip(self.f.root,self.reference,budgets=dict(max_memory_bytes=float(16*1024*1024)))
        self.assertEqual(a['inventory'],b['inventory']);self.assertEqual(a['memory_admission'],b['memory_admission'])
        self.assertEqual(self.reference,before)
        clamped=admit_clip(self.f.root,self.reference,budgets=dict(max_memory_bytes=2**40,max_parallel_jobs=8))
        self.assertEqual(clamped['memory_admission']['limit_bytes'],256*1024*1024)

    def test_unsafe_reference_path_is_rejected_before_trusted_reader(self):
        from unittest.mock import Mock
        reader=Mock(side_effect=AssertionError('unsafe path reached source authority'))
        with self.assertRaises(RigError):admit_clip(None,dict(self.reference,path='../escape.json'),reader=reader)
        reader.assert_not_called()
        with self.assertRaises(RigError):admit_clip(None,dict(self.reference,kind='scene'),reader=reader)
        reader.assert_not_called()

    def test_batch_reuses_one_actual_native_decoder_and_aggregate_budget(self):
        from unittest.mock import patch
        from rig_models import load_rig
        second=deepcopy(self.clip);second.update(id='second',version='2.0.0',fps=dict(numerator=30,denominator=1))
        second_ref=self.f.put('second.json',second,'clip')
        refs=[self.reference,second_ref]
        files=self.sources();calls=[]
        original=self.reader(files)
        def reader(name,maximum):calls.append(name);return original(name,maximum)
        with patch('rig_models.load_rig',wraps=load_rig) as native:
            receipt=admit_clips(None,refs,reader=reader)
        self.assertEqual(native.call_count,1)
        self.assertEqual(set(receipt['clips']),{'clip.json','second.json'})
        self.assertEqual(len(calls),len(set(calls)))
        self.assertEqual(len(receipt['native_context']['rig_bindings']),1)
        self.assertEqual(receipt['roots'],refs)
        self.assertIn('fixture/identity.json',receipt['root_inventory']['clip.json'])
        self.assertNotIn('second.json',receipt['root_inventory']['clip.json'])
        self.assertNotIn('clip.json',receipt['root_inventory']['second.json'])
        self.assertIn('fixture/rig.json',receipt['root_inventory']['second.json'])
        self.assertEqual(receipt['bytes'],sum(len(files[name]) for name in receipt['inventory']))
        self.assertEqual(self.sources(),files)
        with self.assertRaises(RigError) as memory:
            admit_clips(None,refs,reader=original,budgets=dict(max_memory_bytes=receipt['memory_admission']['estimated_bytes']-1))
        self.assertEqual(memory.exception.report['code'],'ANIMATION_LIMIT')
        with self.assertRaises(RigError):admit_clips(None,refs,reader=original,budgets=dict(max_work_bytes=receipt['bytes']-1))
        with self.assertRaises(RigError):admit_clips(None,refs,reader=original,budgets=dict(max_tracks=1))

    def test_selective_roots_bind_only_their_real_transitive_rig_inventory(self):
        from unittest.mock import patch
        from rig_models import load_rig
        from rig_test_helpers import fixture
        second_rig=fixture(self.f.root,name='different-rig',animated=False)
        second_rig_ref=self.f.put('different-rig/rig.json',second_rig.manifest,'rig')
        second=deepcopy(self.clip);second['id']='second';second['rig_binding']=second_rig_ref
        second_ref=self.f.put('second.json',second,'clip')
        with patch('rig_models.load_rig',wraps=load_rig) as native:
            batch=admit_clips(self.f.root,[self.reference,second_ref])
        self.assertEqual(native.call_count,2)
        first_paths=set(batch['root_inventory']['clip.json']);second_paths=set(batch['root_inventory']['second.json'])
        self.assertIn('fixture/identity.json',first_paths)
        self.assertTrue(any(name.startswith('fixture/source/') for name in first_paths))
        self.assertTrue(any(name.startswith('fixture/geometry/') for name in first_paths))
        self.assertTrue(any(name.startswith('fixture/rig/weights/') for name in first_paths))
        self.assertFalse(any(name.startswith('different-rig/') for name in first_paths))
        self.assertFalse(any(name.startswith('fixture/') for name in second_paths))
        self.assertEqual(first_paths|second_paths,set(batch['inventory']))
        for root in batch['root_inventory'].values():
            for path,record in root.items():self.assertEqual(record,batch['inventory'][path])
        # Provenance is a real dependency of the derived root, never vice versa.
        second['provenance']['sources']=[self.reference]
        second_ref=self.f.put('second.json',second,'clip')
        derived=admit_clips(self.f.root,[self.reference,second_ref])
        self.assertLess(first_paths,set(derived['root_inventory']['second.json']))
        self.assertEqual(first_paths,set(derived['root_inventory']['clip.json']))

    def test_duplicate_and_same_identity_versions_share_the_policy_envelope(self):
        from unittest.mock import patch
        from rig_models import load_rig
        newer=deepcopy(self.clip);newer['version']='2.0.0'
        newer_ref=self.f.put('newer.json',newer,'clip')
        with patch('rig_models.load_rig',wraps=load_rig) as native:
            batch=admit_clips(self.f.root,[self.reference,self.reference,newer_ref])
        self.assertEqual(native.call_count,1)
        self.assertEqual(set(batch['clips']),{'clip.json','newer.json'})
        self.assertEqual(len(batch['roots']),3)
        self.assertNotIn('newer.json',batch['root_inventory']['clip.json'])

    def test_batch_root_count_is_bounded_before_iteration_or_source_read(self):
        from unittest.mock import Mock
        consumed=[]
        def roots():
            for i in range(10000):consumed.append(i);yield self.reference
        reader=Mock(side_effect=AssertionError('root count must be checked before source capture'))
        with self.assertRaises(RigError) as rejected:admit_clips(None,roots(),reader=reader)
        self.assertEqual(rejected.exception.report['code'],'ANIMATION_LIMIT')
        self.assertEqual(len(consumed),65)
        reader.assert_not_called()
        with self.assertRaises(RigError):admit_clips(None,[],reader=reader)
        reader.assert_not_called()

    def test_low_memory_callback_cap_precedes_bytesio_materialization(self):
        from unittest.mock import Mock,patch
        payload=self.sources()['clip.json'];observed=[]
        def reader(name,maximum):observed.append(maximum);return payload
        with patch('animation3d_assets.io.BytesIO',side_effect=AssertionError('oversized callback materialized')) as materialize:
            with self.assertRaises(RigError) as rejected:
                admit_clip(None,self.reference,budgets=dict(max_memory_bytes=1000,max_work_bytes=1_000_000),reader=reader)
        self.assertEqual(rejected.exception.report['code'],'ANIMATION_LIMIT')
        self.assertEqual(len(observed),1)
        self.assertLessEqual(observed[0],1000)
        self.assertLess(observed[0],1_000_000)
        materialize.assert_not_called()

    def test_reader_namespace_rejects_parent_case_aliases(self):
        from animation3d_assets import Snapshot
        import tempfile
        calls=[]
        def reader(name,maximum):calls.append(name);return b'data'
        with tempfile.TemporaryDirectory() as destination:
            snapshot=Snapshot(None,destination,reader=reader)
            snapshot.copy('Folder/one.bin')
            with self.assertRaises(RigError) as rejected:snapshot.copy('folder/two.bin')
            self.assertEqual(rejected.exception.report['code'],'ANIMATION_PATH')
            self.assertEqual(calls,['Folder/one.bin'])
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as destination:
            denied=Mock(side_effect=AssertionError('source opened before namespace admission'))
            snapshot=Snapshot(None,destination,reader=denied);snapshot.memory_limit=100
            with self.assertRaises(RigError):snapshot.copy('large-name.bin')
            self.assertEqual(snapshot.aliases,{})
            denied.assert_not_called()
            snapshot.memory_limit=256*1024*1024
            with self.assertRaises(RigError):snapshot.copy('a/'*64+'file.bin')
            denied.assert_not_called()

    def test_clip_source_rates_remain_independent_from_project_clock(self):
        old=deepcopy(self.clip);old.update(id='older',fps=dict(numerator=30,denominator=1))
        oldref=self.f.put('older.json',old,'clip')
        self.clip['provenance']['sources']=[oldref];self.refresh()
        receipt=admit_clip(self.f.root,self.reference)
        self.assertEqual(receipt['documents']['older.json']['fps'],old['fps'])
        self.assertEqual(source_time(self.clip,dict(frame=15),dict(numerator=30,denominator=1)),dict(frame=12,subframe_ticks=0))
        self.assertEqual(source_time(old,dict(frame=15),dict(numerator=30,denominator=1)),dict(frame=15,subframe_ticks=0))
        self.f.project['clips']=[self.reference]
        self.assertTrue(self.f.check()['source_ready'])
        sequence=deepcopy(self.f.values['sequence']);sequence['fps']=dict(numerator=30,denominator=1)
        self.f.project['sequences']=[self.f.put('sequence.json',sequence,'sequence')]
        with self.assertRaises(RigError) as mismatch:self.f.check()
        self.assertEqual(mismatch.exception.report['code'],'ANIMATION_RATE')

if __name__=='__main__':unittest.main()
