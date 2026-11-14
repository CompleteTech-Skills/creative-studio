"""Real native evaluator admission, lifetime and bounded source regressions."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from animation3d_evaluator import RigClipEvaluator
from test_animation3d_clips import clip
from rig_test_helpers import fixture
from rig_errors import RigError, object_hash

class EvaluatorAdmissionTests(unittest.TestCase):
    def test_lazy_source_limit_precedes_unbounded_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False)
            evaluator=RigClipEvaluator(rig.root,'actor')
            consumed=[]
            def sources():
                for index in range(1000):
                    consumed.append(index)
                    yield clip(),dict(frame=0)
            with self.assertRaises(RigError) as caught:
                evaluator.evaluate_sources(sources())
            self.assertEqual(caught.exception.report['code'],'ANIMATION_LIMIT')
            self.assertEqual(len(consumed),65)

    def test_each_call_revalidates_saved_dependency_and_returns_detached_state(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False)
            evaluator=RigClipEvaluator(rig.root,'actor')
            value=clip();value['tracks'][0]['target']['bone_id']='b0'
            before=object_hash(value)
            first=evaluator.evaluate([value],dict(frame=6))
            signature=first['dependency_signature']
            first['geometry'][rig.geometry.meshes[0].id]['positions'][0][0]=999
            first['pose']['bones']['b0']['translation'][0]=999
            second=evaluator.evaluate([value],dict(frame=6))
            self.assertEqual(second['dependency_signature'],signature)
            self.assertNotEqual(second['geometry'][rig.geometry.meshes[0].id]['positions'][0][0],999)
            self.assertEqual(object_hash(value),before)
            manifest=rig.root/'rig.json';manifest.write_bytes(manifest.read_bytes()+b'\n')
            with self.assertRaises(RigError) as caught:
                evaluator.evaluate([value],dict(frame=6))
            self.assertEqual(caught.exception.report['code'],'ANIMATION_HASH')

    def test_native_owner_data_is_loaded_only_through_exact_snapshot_admission(self):
        # A legacy live loader call in this module would bypass the snapshot.
        import animation3d_evaluator
        self.assertFalse(hasattr(animation3d_evaluator,'load_rig'))
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False)
            from animation3d_assets import verify_native
            calls=[]
            def admission(root,reference):
                calls.append(deepcopy(reference))
                return verify_native(root,reference)
            with patch('animation3d_assets.verify_native',side_effect=admission):
                evaluator=RigClipEvaluator(rig.root,'actor')
            self.assertGreaterEqual(len(calls),2)
            self.assertTrue(all(item['sha256']==evaluator._dependency for item in calls))


class PoseLibraryCapacityTests(unittest.TestCase):
    def test_bounded_immutable_named_pose_store_and_native_constraint_validation(self):
        from animation3d_evaluator import PoseLibrary
        from rig_skeleton import neutral_pose
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False)
            value=neutral_pose(rig.skeleton)
            library=PoseLibrary(rig,max_entries=1)
            digest=library.add(value)
            self.assertEqual(digest,object_hash(value))
            self.assertEqual(library.add(deepcopy(value)),digest)
            with self.assertRaises(RigError) as rejected:library.duplicate(value['id'],digest,'new')
            self.assertEqual(rejected.exception.report['code'],'ANIMATION_LIMIT')
            self.assertEqual(library.get(value['id'],digest),value)
            low=PoseLibrary(rig,max_pose_bytes=1)
            with self.assertRaises(RigError):low.add(value)
            self.assertEqual(low._poses,{})
            with self.assertRaises(RigError):PoseLibrary(rig,max_entries=True)
            edited=deepcopy(value);edited['bones']['b0']={'translation':[1,0,0]}
            with self.assertRaises(RigError) as conflict:library.add(edited)
            self.assertEqual(conflict.exception.report['code'],'ANIMATION_IDENTITY')

if __name__=='__main__': unittest.main()
