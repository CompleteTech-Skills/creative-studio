"""Real curve, document, asset-authority and hostile-input clip edit checks."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from animation3d_clips import (split_clip,join_clips,splice_frames,ClipLibrary,ClipHistory,
                               edit_clip,load_clip,save_clip)
from animation3d_curves import evaluate_segment
from animation3d_time import frame_position
from animation3d_registry import TargetRegistry
from animation3d_contracts import validate_contract
from rig_errors import RigError,object_hash
from rig_test_helpers import fixture
from test_animation3d_clips import clip


def sampled(track,position):
    position=Fraction(position); keys=track['keys']
    if position<=frame_position(keys[0]['time']):return keys[0]['value']
    if position>=frame_position(keys[-1]['time']):return keys[-1]['value']
    right=next(i for i,k in enumerate(keys) if frame_position(k['time'])>position)
    return evaluate_segment(track,right-1,position)


def configured(mode):
    value=clip(mode);a,b=value['tracks'][0]['keys']
    if mode=='hermite':a['out_tangent']=[2,1,0];b['in_tangent']=[0,-1,0]
    if mode=='bezier':
        a['out_handle']=dict(time_delta_frames=4,value_delta=[6,3,0],weighted=True,weight=.5)
        b['in_handle']=dict(time_delta_frames=-8,value_delta=[-2,4,0])
    if mode=='ease':a['ease_out']=.3;b['ease_in']=.8
    if mode=='tcb':a['tension']=.3;a['bias']=-.4;b['continuity']=.5
    validate_contract(value);return value


class ClipEditingTests(unittest.TestCase):
    def test_timeline_creation_metadata_events_tracks_and_history_use_one_contract(self):
        from animation3d_clips import create_clip
        source=clip()
        created=create_clip('timeline','Timeline',source['fps'],24,source['provenance'])
        self.assertEqual(created['tracks'],[])
        history=ClipHistory(created)
        history.apply(edit_clip,add_track=source['tracks'][0],tags=['walk'],categories=['motion'],loop_mode='loop')
        marker=dict(id='marker',name='in',frame=0,type='marker')
        history.apply(edit_clip,add_event=marker,name='Revised',duration=30)
        self.assertEqual(history.document['events'],[marker]);self.assertEqual(history.document['duration_frames'],30)
        history.apply(edit_clip,remove_event='marker',remove_track='move')
        self.assertEqual(history.document['tracks'],[]);self.assertEqual(history.document['events'],[])
        history.undo();self.assertEqual(history.document['events'],[marker])
        before=history.document
        with self.assertRaises(RigError):history.apply(edit_clip,add_event=marker)
        self.assertEqual(history.document,before)
        with self.assertRaises(RigError):create_clip('bad','Bad',source['fps'],0,source['provenance'])

    def test_named_regions_use_strict_marker_pairs_and_exact_source_ranges(self):
        from animation3d_clips import set_region,remove_region,region_range,validate_named_regions,source_time
        source=set_region(clip(),'take','Take',6,18)
        self.assertEqual(validate_named_regions(source),[dict(id='take',name='Take',start_frame=6,end_frame=18)])
        self.assertEqual(region_range(source,'take'),dict(source_in=6,source_out=18))
        source['interruption_markers']=['Take:start']
        renamed=set_region(source,'take','Renamed',7,19)
        self.assertEqual(renamed['interruption_markers'],['Renamed:start'])
        for mutate in ('missing','peer','version','name','interval'):
            bad=deepcopy(source)
            if mutate=='missing':bad['events'].pop()
            elif mutate=='peer':bad['events'][0]['payload']['animation3d_region_peer']='wrong'
            elif mutate=='version':bad['events'][0]['payload']['animation3d_region_kind']='unknown'
            elif mutate=='name':bad['events'][0]['name']='wrong'
            else:bad['events'][1]['frame']=bad['events'][0]['frame']
            with self.subTest(mutate=mutate),self.assertRaises(RigError):validate_named_regions(bad)
        self.assertEqual(remove_region(source,'take')['events'],[])
        self.assertEqual(frame_position(source_time(source,dict(frame=6),source['fps'],**region_range(source,'take'))),12)
        self.assertEqual(frame_position(source_time(source,dict(frame=12),source['fps'],loop=True,**region_range(source,'take'))),6)
        with tempfile.TemporaryDirectory() as directory:
            from animation3d_evaluator import RigClipEvaluator
            rig=fixture(directory,animated=False);source['tracks'][0]['target']['bone_id']='b0'
            evaluator=RigClipEvaluator(rig.root,'actor')
            selected=source_time(source,dict(frame=6),source['fps'],**region_range(source,'take'))
            result=evaluator.evaluate([source],selected)
            np.testing.assert_allclose(result['geometry'][rig.geometry.meshes[0].id]['positions'],rig.geometry.meshes[0].positions+[12,0,0],atol=1e-10)

    def test_named_region_boundaries_survive_reverse_split_join_and_splice(self):
        from animation3d_clips import set_region,validate_named_regions,reverse_clip,retime_clip
        source=set_region(clip(),'take','Take',6,12)
        self.assertEqual(validate_named_regions(reverse_clip(source))[0]['start_frame'],12)
        self.assertEqual(validate_named_regions(retime_clip(source,2))[0]['end_frame'],24)
        left,right=split_clip(source,12)
        self.assertEqual(len(validate_named_regions(left)),1);self.assertEqual(validate_named_regions(right),[])
        self.assertEqual(validate_named_regions(join_clips(left,right)),validate_named_regions(source))
        self.assertEqual(validate_named_regions(splice_frames(source,12,3)),validate_named_regions(source))
        self.assertEqual(validate_named_regions(splice_frames(source,8,3))[0]['end_frame'],15)
        self.assertEqual(validate_named_regions(splice_frames(source,12,-3)),validate_named_regions(source))
        self.assertEqual(validate_named_regions(splice_frames(source,0,-3))[0]['start_frame'],3)
        with self.assertRaises(RigError):split_clip(source,8)
        with self.assertRaises(RigError):splice_frames(source,8,-3)
        with self.assertRaises(RigError):edit_clip(source,remove_event='take:start')
        before=object_hash(source);history=ClipHistory(source)
        with self.assertRaises(RigError):history.apply(splice_frames,8,-3)
        self.assertEqual(object_hash(history.document),before)

    def test_split_join_preserves_supported_curve_shapes_and_sources(self):
        for mode in ('linear','hermite','bezier','ease','catmull-rom','tcb','step'):
            with self.subTest(mode=mode):
                source=configured(mode);before=object_hash(source)
                left,right=split_clip(source,9)
                joined=join_clips(left,right,identifier='rejoined')
                for position in (Fraction(0),Fraction(1,2),Fraction(8),Fraction(9),Fraction(13),Fraction(47,2),Fraction(24)):
                    side,local=(left,position) if position<9 else (right,position-9)
                    np.testing.assert_allclose(sampled(side['tracks'][0],local),sampled(source['tracks'][0],position),atol=1e-9)
                    np.testing.assert_allclose(sampled(joined['tracks'][0],position),sampled(source['tracks'][0],position),atol=1e-9)
                self.assertEqual(object_hash(source),before)
                validate_contract(joined)

    def test_split_events_contacts_named_boundary_poses_and_markers(self):
        source=clip();source.update(entry_pose='entry',exit_pose='exit',interruption_markers=['before','seam','after'])
        source['events']=[dict(id=str(i),name=name,frame=i,type='marker') for i,name in ((3,'before'),(9,'seam'),(20,'after'))]
        source['contacts']=[dict(id='hand',kind='hand',target_bone='b0',start_frame=5,end_frame=15,
                                 coordinate_space='actor',position_tolerance_m=.01,solver_weight=1)]
        left,right=split_clip(source,9)
        self.assertEqual([e['frame'] for e in left['events']],[3])
        self.assertEqual([e['frame'] for e in right['events']],[0,11])
        self.assertEqual(left['interruption_markers'],['before'])
        self.assertEqual(right['interruption_markers'],['seam','after'])
        self.assertNotIn('exit_pose',left);self.assertNotIn('entry_pose',right)
        self.assertEqual(left['contacts'][0]['end_frame'],9)
        self.assertEqual(right['contacts'][0]['start_frame'],0)
        joined=join_clips(left,right)
        self.assertEqual(joined['events'],source['events']);self.assertEqual(joined['contacts'],source['contacts'])
        self.assertEqual(joined['entry_pose'],'entry');self.assertEqual(joined['exit_pose'],'exit')
        self.assertEqual(joined['interruption_markers'],source['interruption_markers'])

    def test_insert_frames_holds_only_inserted_range_for_all_polynomials(self):
        for mode in ('linear','hermite','bezier','ease','catmull-rom','tcb','step'):
            with self.subTest(mode=mode):
                source=configured(mode);edited=splice_frames(source,9,5)
                self.assertEqual(edited['duration_frames'],29)
                for position in (Fraction(0),Fraction(8),Fraction(9),Fraction(11),Fraction(14),Fraction(18),Fraction(29)):
                    original=position if position<9 else Fraction(9) if position<14 else position-5
                    np.testing.assert_allclose(sampled(edited['tracks'][0],position),sampled(source['tracks'][0],original),atol=1e-9)

    def test_remove_frames_preserves_unaffected_tick_samples_and_marks_cut(self):
        for mode in ('linear','hermite','bezier','ease','catmull-rom','tcb'):
            with self.subTest(mode=mode):
                source=configured(mode);edited=splice_frames(source,8,-5)
                self.assertEqual(edited['duration_frames'],19)
                for position in (Fraction(0),Fraction(3),Fraction(7999999,1000000),Fraction(8),Fraction(12),Fraction(19)):
                    original=position if position<8 else position+5
                    np.testing.assert_allclose(sampled(edited['tracks'][0],position),sampled(source['tracks'][0],original),atol=1e-8)
                self.assertEqual(edited['provenance']['parameters']['clip_edit_0_operation'],'splice_frames')
                self.assertEqual(edited['provenance']['parameters']['clip_edit_0_count_frames'],-5)

    def test_splice_contacts_use_half_open_boundaries_and_events_are_explicit(self):
        source=clip();source['events']=[dict(id=str(i),name='marker',frame=i,type='marker') for i in (7,8,12,13,24)]
        source['contacts']=[dict(id='hand',kind='hand',target_bone='b0',start_frame=2,end_frame=8,
                                 coordinate_space='actor',position_tolerance_m=.01,solver_weight=1)]
        inserted=splice_frames(source,8,5)
        self.assertEqual(inserted['contacts'][0]['end_frame'],8)
        self.assertEqual([e['frame'] for e in inserted['events']],[7,13,17,18,29])
        removed=splice_frames(source,8,-5)
        self.assertEqual([e['frame'] for e in removed['events']],[7,8,19])
        with self.assertRaises(RigError):splice_frames(source,0,-24)

    def test_join_rejects_discontinuity_wrong_fps_and_duplicate_events(self):
        left,right=split_clip(clip(),12)
        changed=deepcopy(right);changed['tracks'][0]['keys'][0]['value']=[999,0,0]
        with self.assertRaises(RigError):join_clips(left,changed)
        changed=deepcopy(right);changed['fps']['numerator']=30
        with self.assertRaises(RigError):join_clips(left,changed)
        left['events']=[dict(id='duplicate',name='event',frame=1,type='animation_event')]
        right['events']=[dict(id='duplicate',name='event',frame=1,type='animation_event')]
        with self.assertRaises(RigError):join_clips(left,right)

    def test_boolean_and_quaternion_slerp_edits_preserve_typed_values(self):
        boolean=clip('step');track=boolean['tracks'][0]
        track.update(target=dict(kind='node',node_id='mesh',property='visibility'),value_type='boolean',default_value=True,
                     keys=[dict(time=dict(frame=0),value=True,interpolation='step'),dict(time=dict(frame=24),value=False,interpolation='step')])
        left,right=split_clip(boolean,9)
        self.assertIs(sampled(left['tracks'][0],6),True)
        self.assertIs(sampled(right['tracks'][0],15),False)
        quaternion=clip('quaternion-slerp');track=quaternion['tracks'][0]
        track.update(target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'),value_type='quaternion',default_value=[0,0,0,1],
                     keys=[dict(time=dict(frame=0),value=[0,0,0,1],interpolation='quaternion-slerp'),dict(time=dict(frame=24),value=[0,0,math.sin(.5),math.cos(.5)])])
        left,right=split_clip(quaternion,12)
        right['tracks'][0]['keys'][0]['value']=[-v for v in right['tracks'][0]['keys'][0]['value']]
        joined=join_clips(left,right)
        actual=sampled(joined['tracks'][0],18);expected=sampled(quaternion['tracks'][0],18)
        self.assertAlmostEqual(abs(float(np.dot(actual,expected))),1)

    def test_squad_bake_is_explicit_bounded_and_measured_against_native_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False);registry=TargetRegistry.from_rig(rig.root,'actor')
            source=clip('quaternion-squad');track=source['tracks'][0]
            track.update(target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'),value_type='quaternion',default_value=[0,0,0,1])
            angles=(0,1.1,-.2,1.5)
            track['keys']=[dict(time=dict(frame=i*8),value=[0,0,math.sin(a/2),math.cos(a/2)],interpolation='quaternion-squad') for i,a in enumerate(angles)]
            # Antipodal quaternion encodings must describe the same rotation.
            track['keys'][1]['value']=[-v for v in track['keys'][1]['value']]
            before=object_hash(source)
            with self.assertRaises(RigError):split_clip(source,9)
            with self.assertRaises(RigError):split_clip(source,9,registry=registry,quaternion_tolerance_rad=1e-8,max_bake_keys=2)
            left,right=split_clip(source,9,registry=registry,quaternion_tolerance_rad=1e-4,max_bake_keys=4096)
            for position in (Fraction(37,10),Fraction(113,10),Fraction(99,10),Fraction(24),Fraction(0),Fraction(113,10)):
                side,local=(left,position) if position<9 else (right,position-9)
                actual=sampled(side['tracks'][0],local);expected=sampled(source['tracks'][0],position)
                error=2*math.acos(min(1,abs(float(np.dot(actual,expected)))))
                self.assertLessEqual(error,1.1e-4)
            record=left['provenance']['parameters']
            self.assertEqual(record['squad_bake_0_source_sha256'],before)
            self.assertLessEqual(record['squad_bake_0_maximum_probe_error_rad'],1e-4)
            self.assertEqual(object_hash(source),before)

    def test_saved_edit_operations_drive_actual_native_skinned_geometry(self):
        from animation3d_evaluator import RigClipEvaluator
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False)
            value=clip();value['tracks'][0]['target']['bone_id']='b0'
            left,right=split_clip(value,9)
            evaluator=RigClipEvaluator(rig.root,'actor')
            saved=Path(directory)/'edited.json';save_clip(saved,splice_frames(value,8,4))
            edited=load_clip(saved)
            for document,time,translation in ((left,6,6),(right,7,16),(edited,10,8),(edited,14,10)):
                result=evaluator.evaluate([document],dict(frame=time))
                actual=result['geometry'][rig.geometry.meshes[0].id]['positions']
                np.testing.assert_allclose(actual,rig.geometry.meshes[0].positions+[translation,0,0],atol=1e-10)

    def test_squad_bake_near_antipodal_encodings_remains_shortest_arc(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False);registry=TargetRegistry.from_rig(rig.root,'actor')
            source=clip('quaternion-squad');track=source['tracks'][0]
            track.update(target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'),value_type='quaternion',default_value=[0,0,0,1])
            angles=(0,1e-7,.8,-.2)
            track['keys']=[dict(time=dict(frame=i*8),value=[0,0,math.sin(a/2),math.cos(a/2)],interpolation='quaternion-squad') for i,a in enumerate(angles)]
            track['keys'][1]['value']=[-v for v in track['keys'][1]['value']]
            self.assertLess(np.dot(track['keys'][0]['value'],track['keys'][1]['value']),-.999999)
            left,right=split_clip(source,11,registry=registry,quaternion_tolerance_rad=1e-4,max_bake_keys=4096)
            for position in (Fraction(i,10) for i in (0,13,79,81,109,111,129,173,239,240)):
                side,local=(left,position) if position<11 else (right,position-11)
                actual=sampled(side['tracks'][0],local);expected=sampled(source['tracks'][0],position)
                error=2*math.acos(min(1,abs(float(np.dot(actual,expected)/(np.linalg.norm(actual)*np.linalg.norm(expected))))))
                self.assertLessEqual(error,1.1e-4)

    def test_join_can_explicitly_bake_two_squad_sources_with_one_work_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False);registry=TargetRegistry.from_rig(rig.root,'actor')
            source=clip('quaternion-squad');track=source['tracks'][0]
            track.update(target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'),value_type='quaternion',default_value=[0,0,0,1])
            track['keys']=[dict(time=dict(frame=i*8),value=[0,0,math.sin(a/2),math.cos(a/2)],interpolation='quaternion-squad') for i,a in enumerate((0,.3,-.3,0))]
            other=deepcopy(source);other['id']='other'
            with self.assertRaises(RigError):join_clips(source,other)
            joined=join_clips(source,other,registry=registry,quaternion_tolerance_rad=1e-4,max_bake_keys=4096)
            self.assertEqual(joined['duration_frames'],48)
            parameters=joined['provenance']['parameters']
            self.assertEqual(parameters['squad_bake_0_source_sha256'],object_hash(source))
            self.assertEqual(parameters['squad_bake_1_source_sha256'],object_hash(other))
            for position in (3,20,24,33,46):
                actual=sampled(joined['tracks'][0],position);expected=sampled(source['tracks'][0],position if position<24 else position-24)
                error=2*math.acos(min(1,abs(float(np.dot(actual,expected)))))
                self.assertLessEqual(error,1.1e-4)

    def test_squad_bake_budget_is_aggregate_across_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False);registry=TargetRegistry.from_rig(rig.root,'actor')
            source=clip('quaternion-squad');track=source['tracks'][0]
            track.update(target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'),value_type='quaternion',default_value=[0,0,0,1])
            track['keys']=[dict(time=dict(frame=i*8),value=[0,0,math.sin(a/2),math.cos(a/2)],interpolation='quaternion-squad') for i,a in enumerate((0,1,-.2,1.5))]
            one,_=split_clip(source,9,registry=registry,quaternion_tolerance_rad=1e-4)
            count=one['provenance']['parameters']['squad_bake_0_keys']
            second=deepcopy(track);second['id']='second';second['target']['bone_id']='b1';source['tracks'].append(second)
            with self.assertRaises(RigError):split_clip(source,9,registry=registry,quaternion_tolerance_rad=1e-4,max_bake_keys=count+1)
            both,_=split_clip(source,9,registry=registry,quaternion_tolerance_rad=1e-4,max_bake_keys=count*2+2)
            self.assertLessEqual(both['provenance']['parameters']['squad_bake_0_keys'],count*2+2)

    def test_whole_float_duration_clock_edits_and_invalid_numeric_values(self):
        from animation3d_clips import retime_clip,reverse_clip,source_time,paste_keys
        value=clip();value['duration_frames']=24.0
        self.assertEqual(retime_clip(value,Fraction(1,2))['duration_frames'],12)
        self.assertEqual(reverse_clip(value)['duration_frames'],24.0)
        left,right=split_clip(value,9)
        self.assertEqual(join_clips(left,right)['duration_frames'],24)
        self.assertEqual(frame_position(source_time(value,dict(frame=10),value['fps'],speed=.1)),1)
        for scale in (True,None,'bad',float('nan'),float('inf'),10**400):
            with self.subTest(scale=type(scale).__name__),self.assertRaises(RigError):retime_clip(value,scale)
        for keys in (None,{},[{}]):
            with self.assertRaises(RigError):paste_keys(value,'move',keys)

    def test_history_capacity_failure_does_not_mutate_validated_state(self):
        history=ClipHistory(clip(),max_edits=1)
        history.apply(edit_clip,name='changed');before=history.document
        with self.assertRaises(RigError):history.apply(edit_clip,name='blocked')
        self.assertEqual(history.document,before);history.undo();history.redo()
        self.assertEqual(history.document,before)


class ClipLibraryTests(unittest.TestCase):
    def asset_fixture(self):
        import test_asset_library as asset_helpers
        from asset_library import file_record
        from asset_library_contracts import seal
        f=asset_helpers.AssetLibraryTests();f.setUp();self.addCleanup(f.doCleanups)
        source=clip();path=f.root/'clip.json';save_clip(path,source)
        manifest=deepcopy(f.manifest);manifest['identity']['kind']='animation'
        record=file_record(path,'motion/clip.json','animation')
        manifest['files']=[record,f.files[1]];manifest['provenance']['sources']=[record]
        manifest['compatibility']['formats']=['json'];manifest['publication']['inventory']=['motion/clip.json',f.files[1]['path']]
        sources={record['path']:path,f.files[1]['path']:f.license}
        return f,source,path,seal(manifest),sources,record

    def test_immutable_versions_duplicate_compare_restore_and_capacity(self):
        index=ClipLibrary(max_clips=2);source=clip();first=index.add(source)
        duplicate=index.duplicate('clip','1.0.0',first,new_id='copy',new_name='Copy')
        self.assertEqual(duplicate['id'],'copy');self.assertEqual(source['id'],'clip')
        second=deepcopy(source);second.update(version='2.0.0',name='Changed');second_hash=index.add(second)
        self.assertEqual(len(index.versions('clip')),2)
        self.assertEqual(index.restore('clip','1.0.0',first),source)
        self.assertEqual(index.compare(('clip','1.0.0',first),('clip','2.0.0',second_hash))['changed_fields'],['name','version'])
        with self.assertRaises(RigError):index.add(duplicate)
        with self.assertRaises(RigError):ClipLibrary(max_bytes=1).add(source)
        with self.assertRaises(RigError):index.search(tags=(str(i) for i in range(1000)))

    def test_exact_asset_publication_restart_reuse_and_rejection_preserve_store(self):
        from asset_library import AssetLibrary,file_record
        from asset_library_contracts import seal
        f,source,path,manifest,sources,record=self.asset_fixture()
        index=ClipLibrary(max_clips=1);registered=index.register_asset(f.library,manifest,sources,record['path'])
        reopened=AssetLibrary(f.library.root);other=ClipLibrary()
        loaded=other.load_asset(reopened,registered['asset'],record['path'])
        self.assertEqual(other.get(loaded['id'],loaded['version'],loaded['sha256']),source)
        self.assertEqual(reopened.read_file(registered['asset'],record['path']),path.read_bytes())
        bad=deepcopy(manifest);bad['version']='2.0.0';bad=seal(bad)
        path.write_text(json.dumps({'invalid':'not a clip'}))
        with self.assertRaises(RigError):index.register_asset(f.library,bad,sources,record['path'])
        self.assertEqual(reopened.read_file(registered['asset'],record['path']),json.dumps(source,indent=2,ensure_ascii=False).encode()+b'\n')
        changed=deepcopy(source);changed['version']='2.0.0';save_clip(path,changed)
        later=deepcopy(manifest);later['version']='2.0.0'
        later['files'][0]=file_record(path,record['path'],'animation')
        later['provenance']['sources']=[later['files'][0]];later=seal(later)
        with self.assertRaises(RigError):index.register_asset(f.library,later,sources,record['path'])
        missing=dict(id=later['identity']['id'],version=later['version'],sha256=later['manifest_sha256'])
        with self.assertRaises(RigError):reopened.resolve(missing)
        self.assertEqual(len(index.versions('clip')),1)

    def test_concurrent_index_write_cannot_invalidate_canonical_publication_admission(self):
        import threading
        from unittest.mock import patch
        f,source,path,manifest,sources,record=self.asset_fixture()
        index=ClipLibrary(max_clips=1);candidate=deepcopy(source);candidate['name']='Competing content'
        entered=threading.Event();release=threading.Event();attempting=threading.Event();finished=threading.Event()
        original=f.library.register;published=[];errors=[]
        def paused(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('owned publication gate timed out')
            return original(*args,**kwargs)
        def publisher():
            try:published.append(index.register_asset(f.library,manifest,sources,record['path']))
            except BaseException as exc:errors.append(('publication',exc))
        def competitor():
            attempting.set()
            try:index.add(candidate)
            except BaseException as exc:errors.append(('competitor',exc))
            finally:finished.set()
        with patch.object(f.library,'register',side_effect=paused):
            first=threading.Thread(target=publisher);second=threading.Thread(target=competitor)
            first.start()
            try:
                self.assertTrue(entered.wait(5));second.start();self.assertTrue(attempting.wait(5))
                self.assertFalse(finished.wait(.1),'Concurrent view write must wait for the admitted publication.')
            finally:
                release.set();first.join(10)
                if second.ident is not None:second.join(10)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
        self.assertEqual(len(published),1)
        self.assertEqual(len(errors),1);self.assertEqual(errors[0][0],'competitor')
        self.assertIsInstance(errors[0][1],RigError)
        self.assertEqual(index.get('clip','1.0.0',published[0]['sha256']),source)
        self.assertEqual(f.library.read_file(published[0]['asset'],record['path']),path.read_bytes())

    def test_bounded_clip_file_read_rejects_types_aliases_and_wrong_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'clip.json';save_clip(source,clip())
            self.assertEqual(load_clip(source),clip())
            with self.assertRaises(RigError):load_clip(source,max_bytes=1)
            with self.assertRaises(RigError):load_clip(source,expected_sha256='0'*64)
            with self.assertRaises(RigError):load_clip(root)
            link=root/'alias.json'
            try:link.symlink_to(source)
            except (OSError,NotImplementedError):return
            with self.assertRaises(RigError):load_clip(link)

    def test_clip_file_fifo_replacement_rejects_without_blocking(self):
        import multiprocessing as mp
        import os
        from unittest.mock import patch
        if not hasattr(os,'mkfifo') or 'fork' not in mp.get_all_start_methods():
            self.skipTest('POSIX FIFO/fork replacement race')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'clip.json';save_clip(source,clip())
            context=mp.get_context('fork');replaced=context.Event();receive,send=context.Pipe(duplex=False)
            def child():
                original_open=os.open;original_path_open=Path.open
                def replace():
                    if not replaced.is_set():source.unlink();os.mkfifo(source);replaced.set()
                def descriptor_open(path,*args,**kwargs):
                    if str(path) in (str(source),source.name):replace()
                    return original_open(path,*args,**kwargs)
                def path_open(path,*args,**kwargs):
                    if path==source:replace()
                    return original_path_open(path,*args,**kwargs)
                supported={descriptor_open if operation is original_open else operation for operation in os.supports_dir_fd}
                try:
                    with patch('rig_errors.os.open',descriptor_open),patch.object(os,'supports_dir_fd',supported),patch.object(Path,'open',path_open):load_clip(source)
                except RigError as error:send.send(('rejected',error.report['code']))
                except BaseException as error:send.send(('unexpected',type(error).__name__))
                else:send.send(('admitted',None))
                finally:send.close()
            process=context.Process(target=child);process.start();send.close()
            try:
                self.assertTrue(replaced.wait(5))
                process.join(5);self.assertFalse(process.is_alive())
                self.assertTrue(receive.poll(1));self.assertEqual(receive.recv(),('rejected','RIG_PAYLOAD_TYPE'))
                self.assertEqual(process.exitcode,0)
            finally:
                if process.is_alive():process.terminate();process.join(5)
                receive.close()
