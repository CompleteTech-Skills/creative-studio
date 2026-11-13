"""Actual immutable native timeline transactions and exact evaluation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from editor3d_repository import EditorRepository
from editor3d_timeline import apply_timeline, evaluate_timeline, prepare_timeline_commands, sample_curve, select_scene
from rig_errors import RigError
from scene3d_example import build_example


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);build_example(self.root/'source')
        self.repo=EditorRepository.create(self.root/'source',self.root/'editor')

    def apply(self,commands):
        snap=self.repo.snapshot()
        return apply_timeline(self.repo,dict(expected_revision=snap['revision'],expected_sha256=snap['sha256'],
            transaction_id='edit-'+str(snap['revision']),commands=commands))

    def shot(self,snap):return snap['project']['shots'][0]['path']
    def key(self,frame,value,**kw):
        return dict(operation='key-upsert',document='example-shot.json',target='mesh',property='translation',frame=frame,value=value,**kw)

    def test_save_reopen_evaluate_undo_redo(self):
        original=(self.root/'source/example-shot.json').read_bytes()
        result=self.apply([self.key(0,[0,0,0]),self.key(24,[24,0,0])])
        reopened=EditorRepository(self.root/'editor')
        evaluated=evaluate_timeline(reopened,dict(revision=result['revision'],sha256=result['sha256'],shot=self.shot(result),frame=12))
        self.assertEqual(evaluated['nodes']['mesh']['world_matrix'][0][3],12)
        self.assertEqual((self.root/'source/example-shot.json').read_bytes(),original)
        undo=reopened.undo(1,'undo');self.assertEqual(undo['documents'][self.shot(undo)]['tracks'],[])
        redo=reopened.redo(2,'redo');self.assertEqual(len(redo['documents'][self.shot(redo)]['tracks'][0]['keys']),2)

    def test_move_delete_interpolation_and_collision(self):
        first=self.apply([self.key(0,[1,0,0]),self.key(12,[2,0,0])]);path=self.shot(first)
        result=self.apply([dict(operation='key-move',document=path,target='mesh',property='translation',frame=12,to_frame=24),
          dict(operation='track-interpolation',document=path,target='mesh',property='translation',interpolation='step')])
        evaluated=evaluate_timeline(self.repo,dict(revision=result['revision'],sha256=result['sha256'],shot=self.shot(result),frame=20))
        self.assertEqual(evaluated['nodes']['mesh']['world_matrix'][0][3],1)
        before=self.repo.snapshot();path=self.shot(before)
        with self.assertRaises(RigError):self.apply([dict(operation='key-move',document=path,target='mesh',property='translation',frame=24,to_frame=0)])
        self.assertEqual(self.repo.snapshot(),before)
        result=self.apply([dict(operation='key-delete',document=path,target='mesh',property='translation',frame=24)])
        self.assertEqual(len(result['documents'][self.shot(result)]['tracks'][0]['keys']),1)

    def test_reusable_clip_preserves_source_and_evaluates(self):
        source=(self.root/'source/example-animation.json').read_bytes()
        first=self.apply([dict(operation='clip-bind',document='example-shot.json',clip='example-animation.json',target='mesh',start_frame=0,end_frame=24,speed=1,loop=False)])
        out=evaluate_timeline(self.repo,dict(revision=first['revision'],sha256=first['sha256'],shot=self.shot(first),frame=12))
        self.assertEqual(out['nodes']['mesh']['world_matrix'][0][3],2.5)
        self.assertEqual((self.root/'source/example-animation.json').read_bytes(),source)

    def test_shot_duplicate_reorder_and_settings_persist(self):
        first=self.apply([dict(operation='shot-duplicate',source='example-shot.json',id='second',name='Second')])
        refs=first['project']['shots'];self.assertEqual(len(refs),2)
        result=self.apply([dict(operation='project-order',field='shots',paths=[r['path'] for r in reversed(refs)]),
          dict(operation='shot-settings',document=refs[1]['path'],duration_frames=48,fps=dict(numerator=30000,denominator=1001))])
        self.assertEqual(result['project']['shots'][0]['id'],'second')
        self.assertEqual(result['documents'][self.shot(result)]['duration_frames'],48)

    def test_invalid_batch_is_atomic_and_bounded(self):
        before=self.repo.snapshot()
        for commands in ([self.key(25,[1,0,0])],[self.key(True,[1,0,0])],[self.key(0,[1,0])],
          [self.key(0,[1,0,0]),dict(operation='not-supported')],[],[dict(operation='project-order',field='shots',paths=['missing'])]):
            with self.subTest(commands=commands),self.assertRaises(RigError):self.apply(commands)
            self.assertEqual(self.repo.snapshot(),before)

    def test_rational_scrub_uses_saved_fps_and_half_open_interval(self):
        result=self.apply([self.key(0,[0,0,0]),self.key(24,[24,0,0])])
        body=dict(revision=result['revision'],sha256=result['sha256'],shot=self.shot(result),frame=dict(numerator=3,denominator=2))
        out=evaluate_timeline(self.repo,body)
        self.assertEqual(out['nodes']['mesh']['world_matrix'][0][3],1.5)
        self.assertEqual(out['time'],dict(numerator=1,denominator=16))
        with self.assertRaises(RigError):evaluate_timeline(self.repo,{**body,'sample_geometry':'yes'})
        for frame in (dict(numerator=1,denominator=0),24,True):
            with self.subTest(frame=frame),self.assertRaises(RigError):evaluate_timeline(self.repo,{**body,'frame':frame})

    def test_scene_lifecycle_and_shot_deletion_preserve_selected_closure(self):
        first=self.apply([dict(operation='scene-create',source='example-scene.json',id='new-scene',name='New')])
        ref=next(r for r in first['project']['scenes'] if r['id']=='new-scene')
        second=self.apply([dict(operation='scene-rename',document=ref['path'],name='Renamed')])
        ref=next(r for r in second['project']['scenes'] if r['id']=='new-scene')
        third=self.apply([dict(operation='scene-delete',document=ref['path'])])
        self.assertEqual(len(third['project']['scenes']),1)
        before=self.repo.snapshot()
        with self.assertRaises(RigError):self.apply([dict(operation='scene-delete',document=before['project']['scenes'][0]['path'])])
        self.assertEqual(self.repo.snapshot(),before)

    def test_duplicate_delivery_returns_original_receipt_after_later_edit(self):
        before=self.repo.snapshot()
        body=dict(expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='delivered',commands=[self.key(0,[7,0,0])])
        first=apply_timeline(self.repo,body)
        self.assertEqual(apply_timeline(self.repo,body),first)
        path=self.shot(first)
        self.apply([dict(operation='key-upsert',document=path,target='mesh',property='translation',frame=12,value=[9,0,0])])
        self.assertEqual(apply_timeline(self.repo,body),first)
        self.assertEqual(self.repo.snapshot()['revision'],2)

    def test_curve_samples_delegate_exact_native_channels_and_bound_work(self):
        result=self.apply([self.key(0,[0,0,0]),self.key(24,[24,0,0])])
        body=dict(revision=result['revision'],sha256=result['sha256'],shot=self.shot(result),target='mesh',property='translation',sample_count=3)
        curve=sample_curve(self.repo,body)
        self.assertEqual([sample['value'][0] for sample in curve['samples']],[0,11.5,23])
        self.assertEqual(curve['samples'][1]['frame'],dict(numerator=23,denominator=2))
        self.assertEqual(self.repo.snapshot(),result)
        for updates in ({'sample_count':34},{'sample_count':True},{'target':'missing'},{'property':'unsupported'}):
            with self.subTest(updates=updates),self.assertRaises(RigError):sample_curve(self.repo,{**body,**updates})

    def test_active_scene_switch_preserves_bytes_and_undo(self):
        first=self.apply([dict(operation='scene-duplicate',source='example-scene.json',id='second-scene',name='Second')])
        scene=next(ref for ref in first['project']['scenes'] if ref['id']=='second-scene')
        body=dict(expected_revision=first['revision'],expected_sha256=first['sha256'],transaction_id='open-scene',scene=scene['path'])
        switched=select_scene(self.repo,body)
        self.assertEqual(switched['scene_path'],scene['path']);self.assertEqual(switched['sha256'],first['sha256'])
        self.assertEqual(select_scene(self.repo,body),switched)
        reopened=EditorRepository(self.repo.root).snapshot();self.assertEqual(reopened['scene']['id'],'second-scene')
        undo=self.repo.undo(switched['revision'],'undo-scene');self.assertEqual(undo['scene_path'],first['scene_path'])
        with self.assertRaises(RigError):select_scene(self.repo,{**body,'expected_revision':undo['revision'],'transaction_id':'missing','scene':'absent'})
        self.assertEqual(self.repo.snapshot(),undo)

    def test_prepare_never_mutates_input(self):
        snap=self.repo.snapshot();before=deepcopy(snap)
        prepare_timeline_commands(snap,[self.key(0,[5,0,0])]);self.assertEqual(snap,before)

if __name__=='__main__':unittest.main()
