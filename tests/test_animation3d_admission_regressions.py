"""Regressions for independent release review of frozen candidate 8bc8071."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))

import test_animation3d_closure as helpers
from animation3d_contracts import KINDS, validate_contract
from animation3d_registry import TargetRegistry, sample_track
from animation3d_test_helpers import LAYER
from rig_errors import RigError


class ReleaseAdmissionRegressions(unittest.TestCase):
    def setUp(self):
        self.f = helpers.NativeClosureRegressionTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.rig, self.clip, self.rigref = self.f.bound_clip()
        self.scene, self.nativeref = self.f.native_scene()
        self.layer = deepcopy(LAYER)
        self.actor = dict(actor_id='actor', model={**self.nativeref,'kind':'model'}, rig=self.rigref,
                          initial_transform=dict(position=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1]),
                          animation_layers=[self.layer],constraints=[])
        self.scene['actors']=[self.actor]

    def check(self):
        self.f.project['scene_assets']=[self.f.put('scene.json',self.scene,'scene')]
        return self.f.check()

    def placement(self):
        return dict(id='placement',clip=self.f.project['clips'][0],layer_id='body',
                    start_frame=0,duration_frames=24,source_in_frame=0,
                    playback_rate=dict(numerator=1,denominator=1),blend_in_frames=0,
                    blend_out_frames=0,weight=1,enabled=True)

    def constraint(self, identifier, source='b1', destination=None):
        item=dict(id=identifier,type='position',source=dict(kind='bone',actor_id='actor',bone_id=source,property='translation'),weight=1,enabled=True)
        if destination: item['target']=dict(kind='bone',actor_id='actor',bone_id=destination,property='translation')
        else: item['target_position']=[0,0,0]
        return item

    def assert_code(self, code):
        with self.assertRaises(RigError) as caught: self.check()
        self.assertEqual(caught.exception.report['code'],code)

    def test_nested_weight_controls_fail_explicitly_without_canonical_owner(self):
        for placement in (False,True):
            for scalar in (False,True):
                track=deepcopy(self.clip['tracks'][0])
                if scalar:
                    mesh=self.rig.geometry.meshes[0]
                    track.update(target=dict(kind='morph',actor_id='actor',mesh_id=mesh.id,morph_id='blink',property='weight'),
                                 value_type='float',default_value=0,keys=[])
                else:track['target']['bone_id']='fabricated'
                container=self.placement() if placement else self.layer
                container['weight_track']=track
                self.layer['placements']=[container] if placement else []
                with self.subTest(placement=placement,scalar=scalar):self.assert_code('ANIMATION_OWNER')
                self.layer.pop('weight_track',None);self.layer['placements']=[]
        self.assertTrue(self.check()['source_ready'])

    def test_all_nested_timeline_coordinates_and_interval_sums_use_project_bound(self):
        for field,value in [('start_frame',10000000),('duration_frames',10000000),('start_frame',90)]:
            item=self.placement();item[field]=value;self.layer['placements']=[item]
            with self.subTest(field=field,value=value):self.assert_code('ANIMATION_LIMIT')
        self.layer['placements']=[]
        self.scene['markers']=[dict(id='later',name='later',frame=101,type='marker')]
        self.assert_code('ANIMATION_LIMIT');self.scene['markers']=[]
        constraint=self.constraint('bounded');constraint.update(start_frame=1,end_frame=101)
        self.actor['constraints']=[constraint];self.assert_code('ANIMATION_LIMIT');self.actor['constraints']=[]
        interaction=dict(schema_version=1,kind=KINDS['interaction'],id='interaction',participants=[dict(actor_id='actor',role='participant')],start_frame=0,end_frame=102,
                         synchronization_markers=[],ownership_changes=[dict(frame=101,next_owner='actor',world_pose_policy='preserve')],
                         constraints=[],position_tolerance_m=.01,orientation_tolerance_rad=.01,failure_policy='error')
        self.scene['interactions']=[interaction];self.assert_code('ANIMATION_LIMIT');self.scene['interactions']=[]
        self.layer['weight_track']=deepcopy(self.clip['tracks'][0]);self.layer['weight_track']['keys'][-1]['time']['frame']=101
        self.assert_code('ANIMATION_LIMIT');self.layer.pop('weight_track')
        machine=deepcopy(self.f.values['state-machine']);machine['states'][0]['clip']=self.f.project['clips'][0]
        machine['parameters']=[dict(name='go',type='bool',default=False)]
        machine['transitions']=[dict(id='loop',from_state='idle',to_state='idle',conditions=[dict(parameter='go',operator='eq',value=True)],
            condition_mode='all',priority=0,duration_frames=101,blend_curve='linear',interruption_policy='none')]
        self.f.project['state_machines']=[self.f.put('machine.json',machine,'state-machine')]
        self.assert_code('ANIMATION_LIMIT');self.f.project['state_machines']=[]
        sequence=deepcopy(self.f.values['sequence']);sequence.update(total_frames=101)
        sequence['shots'][0]['duration_frames']=101
        self.f.project['sequences']=[self.f.put('sequence.json',sequence,'sequence')]
        self.assert_code('ANIMATION_LIMIT')

    def test_constraint_cycles_and_native_hierarchy_dependencies_reject(self):
        self.actor['constraints']=[self.constraint('a','b0','b1'),self.constraint('b','b1','b0')]
        self.assert_code('ANIMATION_CYCLE')
        self.actor['constraints']=[self.constraint('descendant','b0','b2')]
        self.assert_code('ANIMATION_CYCLE')
        self.actor['constraints']=[self.constraint('parent','b2','b0')]
        self.assertTrue(self.check()['source_ready'])

    def test_constraint_chain_must_follow_actual_parent_adjacency(self):
        constraint=self.constraint('ik','b0');constraint.update(type='two_bone_ik',chain_bones=['b0','b2','b1'])
        self.actor['constraints']=[constraint];self.assert_code('ANIMATION_CONSTRAINT')
        constraint['chain_bones']=['b0','b1','b2'];self.assertTrue(self.check()['source_ready'])

    def test_constraint_targets_resolve_real_actor_and_object(self):
        constraint=self.constraint('foreign','b2','b0');constraint['target']['actor_id']='fabricated'
        self.actor['constraints']=[constraint];self.assert_code('ANIMATION_TARGET')
        constraint.pop('target');constraint['target_object_id']='fabricated';self.assert_code('ANIMATION_TARGET')
        constraint['target_object_id']='root';self.assertTrue(self.check()['source_ready'])

    def test_constraint_target_uses_other_actors_actual_rig(self):
        from rig_test_helpers import fixture
        from rig_jobs import save_rig
        other=fixture(self.f.root,name='other',animated=False)
        for item in other.skeleton['bones']:
            if item['id']=='b1':item['id']='other-b1'
            if item['parent']=='b1':item['parent']='other-b1'
        for weights in other.weights.values():weights.joints=['other-b1' if name=='b1' else name for name in weights.joints]
        other.manifest['controllers']['ik'][0]['bones']=['b0','other-b1','b2']
        save_rig(other,operation='test-other-actor')
        ref=self.f.put('other/rig.json',json.loads((other.root/'rig.json').read_text()),'rig')
        self.f.project['rig_assets'].append(ref)
        actor=deepcopy(self.actor);actor.update(actor_id='other',rig=ref,animation_layers=[])
        self.scene['actors'].append(actor)
        constraint=self.constraint('foreign','b2','b1');constraint['target']['actor_id']='other'
        self.actor['constraints']=[constraint];self.assert_code('ANIMATION_TARGET')
        constraint['target']['bone_id']='other-b1';self.assertTrue(self.check()['source_ready'])

    def test_native_property_domains_reject_defaults_keys_and_interpolation_overshoot(self):
        base=Path(__file__).resolve().parents[1]
        shutil.copytree(base/'examples/camera3d',self.f.root/'camera')
        source=self.f.put('camera/registry.json',json.loads((self.f.root/'camera/registry.json').read_text()))
        target=dict(kind='camera',camera_id='camera',property='focal_length')
        entry=dict(owner='camera3d',source=source,target=target,minimum=-1000,maximum=1000)
        registry=TargetRegistry(self.f.root,dict(schema_version=1,kind=KINDS['registry'],id='targets',version='1.0.0',entries=[entry]))
        track=deepcopy(self.clip['tracks'][0]);track.update(target=target,value_type='float',default_value=-50,keys=[])
        with self.assertRaises(RigError):sample_track(track,dict(frame=0),registry)
        track.update(default_value=50,keys=[dict(time=dict(frame=0),value=-50,interpolation='linear')])
        with self.assertRaises(RigError):sample_track(track,dict(frame=0),registry)
        track['keys']=[dict(time=dict(frame=0),value=1,interpolation='hermite',out_tangent=-1),
                       dict(time=dict(frame=10),value=1,interpolation='linear',in_tangent=1)]
        with self.assertRaises(RigError):sample_track(track,dict(frame=5),registry)
        for domain,prop,value in [('light','intensity',-1),('light','color',[1,-1,0]),('light','cone_angle',4),
                                  ('material','base_color',[2,0,0,1]),('material','emissive_intensity',-1)]:
            candidate=deepcopy(track);candidate.update(default_value=value,keys=[])
            candidate['target']=dict(kind=domain,property=prop,**{domain+'_id':'native'})
            candidate['value_type']='vec4' if prop=='base_color' else 'vec3' if prop=='color' else 'float'
            with self.subTest(domain=domain,prop=prop),self.assertRaises(RigError):
                validate_contract(dict(schema_version=1,kind=KINDS['track'],value=candidate))

    def test_contextual_native_ranges_cannot_be_widened_by_registry_bounds(self):
        base=Path(__file__).resolve().parents[1]
        material=json.loads((base/'examples/lookdev-contracts/ceramic.json').read_text())
        material_ref=self.f.put('material.json',material)
        light=json.loads((base/'examples/lookdev-contracts/key.json').read_text())
        light.update(type='spot',spot_inner_radians=.5,spot_outer_radians=1)
        light_ref=self.f.put('spot.json',light)
        for domain,prop,source,identifier,value in [
                ('material','opacity',material_ref,material['id'],.5),
                ('material','base_color',material_ref,material['id'],[.5,.5,.5,.5]),
                ('light','cone_angle',light_ref,light['id'],.25)]:
            target=dict(kind=domain,property=prop,**{domain+'_id':identifier})
            entry=dict(owner='lookdev',source=source,target=target)
            if prop!='base_color':entry.update(minimum=-1000,maximum=1000)
            registry=TargetRegistry(self.f.root,dict(schema_version=1,kind=KINDS['registry'],id='targets',version='1.0.0',entries=[entry]))
            track=deepcopy(self.clip['tracks'][0]);track.update(target=target,value_type='vec4' if prop=='base_color' else 'float',default_value=value,keys=[])
            with self.subTest(domain=domain,prop=prop),self.assertRaises(RigError):sample_track(track,dict(frame=0),registry)
        target=dict(kind='light',light_id=light['id'],property='cone_angle')
        registry=TargetRegistry(self.f.root,dict(schema_version=1,kind=KINDS['registry'],id='spot-targets',version='1.0.0',entries=[dict(owner='lookdev',source=light_ref,target=target)]))
        track=deepcopy(self.clip['tracks'][0]);track.update(target=target,value_type='float',default_value=1,
            keys=[dict(time=dict(frame=0),value=1,interpolation='hermite',out_tangent=-.3),dict(time=dict(frame=10),value=1,interpolation='linear',in_tangent=.3)])
        with self.assertRaises(RigError):sample_track(track,dict(frame=5),registry)
        # Project admission uses the same persisted material context as sampling.
        self.clip['tracks']=[dict(track,id='opacity',target=dict(kind='material',material_id=material['id'],property='opacity'),default_value=.5,keys=[])]
        self.clip['dependencies']=[material_ref]
        self.f.project['clips']=[self.f.put('clip.json',self.clip,'clip')]
        self.assert_code('ANIMATION_RANGE')


    def test_project_terminal_budget_uses_exact_subframe_position(self):
        track=deepcopy(self.clip['tracks'][0])
        track['target']=dict(kind='node',node_id='root',property='translation')
        self.scene['object_tracks']=[track]
        for frame,ticks,accepted in [(100,0,True),(99,999999,True),(100,1,False),(100,500000,False)]:
            track['keys'][-1]['time']=dict(frame=frame,subframe_ticks=ticks)
            with self.subTest(frame=frame,ticks=ticks):
                if accepted:self.assertTrue(self.check()['source_ready'])
                else:self.assert_code('ANIMATION_LIMIT')

    def test_contact_object_requires_real_scene_context_and_target(self):
        contact=dict(id='plant',target_bone='b0',kind='foot',start_frame=0,end_frame=24,
                     coordinate_space='object',target_object='root',position_tolerance_m=.01,solver_weight=1)
        self.clip['contacts']=[contact]
        def refresh():self.f.project['clips']=[self.f.put('clip.json',self.clip,'clip')]
        refresh();self.assert_code('ANIMATION_TARGET')  # Unplaced clip has no object-space scene binding.
        self.layer['placements']=[self.placement()]
        self.assertTrue(self.check()['source_ready'])
        contact['target_object']='fabricated';refresh();self.layer['placements'][0]['clip']=self.f.project['clips'][0]
        self.assert_code('ANIMATION_TARGET')
        del contact['target_object'];refresh();self.layer['placements'][0]['clip']=self.f.project['clips'][0]
        self.assert_code('ANIMATION_CONTACT')

    def test_interaction_marker_names_resolve_existing_scene_markers(self):
        interaction=deepcopy(self.f.values['interaction'])
        interaction.update(participants=[dict(actor_id='actor',role='participant')],synchronization_markers=['missing'])
        self.scene['interactions']=[interaction];self.assert_code('ANIMATION_REFERENCE')
        self.scene['markers']=[dict(id='marker-id',name='beat',frame=0,type='marker')]
        interaction['synchronization_markers']=['beat'];self.assertTrue(self.check()['source_ready'])
        interaction['participants'][0]['interaction_marker']='missing';self.assert_code('ANIMATION_REFERENCE')
        interaction['participants'][0]['interaction_marker']='beat';self.assertTrue(self.check()['source_ready'])

    def test_dependency_registries_validate_native_targets_and_owner_dispatch(self):
        document=dict(schema_version=1,kind=KINDS['registry'],id='dependency-registry',version='1.0.0',
                      entries=[dict(owner='rig',source=self.rigref,target=dict(kind='bone',actor_id='actor',bone_id='fabricated',property='translation'))])
        def refresh():
            self.clip['dependencies']=[self.f.put('registry.json',document)]
            self.f.project['clips']=[self.f.put('clip.json',self.clip,'clip')]
        refresh();self.assert_code('ANIMATION_TARGET')
        document['entries'][0]['target']['bone_id']='b0';refresh();self.assertTrue(self.check()['source_ready'])
        shutil.copytree(Path(__file__).resolve().parents[1]/'examples/camera3d',self.f.root/'camera')
        source=self.f.put('camera/registry.json',json.loads((self.f.root/'camera/registry.json').read_text()))
        for source,target in [(source,dict(kind='camera',camera_id='camera',property='translation')),
                              (self.rigref,dict(kind='bone',actor_id='actor',bone_id='b0',property='translation'))]:
            document['entries']=[dict(owner='lookdev',source=source,target=target)];refresh()
            with self.subTest(kind=target['kind']):
                self.assert_code('ANIMATION_OWNER')
                registry=TargetRegistry(self.f.root,document)
                with self.assertRaises(RigError) as caught:registry.resolve(target)
                self.assertEqual(caught.exception.report['code'],'ANIMATION_OWNER')

    def test_enabled_spatial_constraints_require_explicit_destinations(self):
        for kind in ('position','rotation','scale','parent','aim','look_at','distance','grasp','path_follow'):
            value=self.constraint('targetless','b2');value['type']=kind;value.pop('target_position')
            self.actor['constraints']=[value]
            with self.subTest(kind=kind):self.assert_code('ANIMATION_CONSTRAINT')
        self.actor['constraints']=[self.constraint('valid-point','b2')]
        self.actor['constraints'][0]['target']=dict(kind='bone',actor_id='actor',bone_id='b0',property='translation')
        self.assert_code('ANIMATION_CONSTRAINT');self.actor['constraints'][0].pop('target')
        self.assertTrue(self.check()['source_ready'])
        self.actor['constraints'][0].update(type='limit');self.actor['constraints'][0].pop('target_position')
        self.assert_code('ANIMATION_CONSTRAINT')
        self.actor['constraints'][0]['position_limits']=dict(minimum=[-1,-1,-1],maximum=[1,1,1])
        self.assertTrue(self.check()['source_ready'])

    def test_initial_state_parameter_overrides_bind_machine_names_and_types(self):
        self.actor['initial_state_parameters']={'orphan':True};self.assert_code('ANIMATION_REFERENCE')
        machine=deepcopy(self.f.values['state-machine']);machine['states'][0]['clip']=self.f.project['clips'][0]
        machine['parameters']=[dict(name='go',type='bool',default=False),dict(name='count',type='int',default=0),
                               dict(name='speed',type='float',default=0),dict(name='fire',type='trigger',default=False)]
        self.actor['state_machine']=self.f.put('machine.json',machine,'state-machine')
        for overrides in [{'missing':True},{'go':123},{'count':True},{'count':1.0},{'count':10000000000},{'speed':True},{'speed':1e10},{'fire':12},{'fire':True}]:
            self.actor['initial_state_parameters']=overrides
            with self.subTest(overrides=overrides),self.assertRaises(RigError):self.check()
        self.actor['initial_state_parameters']={'go':True,'count':2,'speed':.5,'fire':False}
        self.assertTrue(self.check()['source_ready'])

    def test_named_state_events_require_explicit_event_owner(self):
        machine=deepcopy(self.f.values['state-machine']);machine['states'][0]['clip']=self.f.project['clips'][0]
        def refresh():self.actor['state_machine']=self.f.put('machine.json',machine,'state-machine')
        refresh();self.assertTrue(self.check()['source_ready'])
        for field in ('enter_events','exit_events'):
            machine['states'][0][field]=['missing-event'];refresh()
            with self.subTest(field=field):self.assert_code('ANIMATION_OWNER')
            machine['states'][0].pop(field)
        machine['parameters']=[dict(name='go',type='bool',default=False)]
        machine['transitions']=[dict(id='loop',from_state='idle',to_state='idle',conditions=[dict(parameter='go',operator='eq',value=True)],
            condition_mode='all',priority=0,duration_frames=1,blend_curve='linear',interruption_policy='none',exit_marker='missing-marker')]
        refresh();self.assert_code('ANIMATION_OWNER')

    def test_transfer_duration_must_fit_its_interaction_interval(self):
        interaction=deepcopy(self.f.values['interaction']);interaction['participants']=[dict(actor_id='actor',role='participant')]
        transfer=dict(frame=23,next_owner='actor',world_pose_policy='blend_to_receiver',transfer_duration_frames=100)
        interaction['ownership_changes']=[transfer];self.scene['interactions']=[interaction]
        self.assert_code('ANIMATION_INTERVAL')
        transfer['transfer_duration_frames']=1;self.assertTrue(self.check()['source_ready'])
        transfer['transfer_duration_frames']=2;self.assert_code('ANIMATION_INTERVAL')

    def test_spatial_constraints_reject_real_scalar_morph_channels(self):
        constraint=self.constraint('spatial','b2')
        mesh=self.rig.geometry.meshes[0]
        scalar=dict(kind='morph',actor_id='actor',mesh_id=mesh.id,morph_id='blink',property='weight')
        constraint['source']=scalar;self.actor['constraints']=[constraint]
        self.assert_code('ANIMATION_CONSTRAINT')
        constraint['source']=dict(kind='node',node_id='root',property='visibility')
        self.assert_code('ANIMATION_CONSTRAINT')
        constraint['source']=dict(kind='bone',actor_id='actor',bone_id='b2',property='translation')
        self.assertTrue(self.check()['source_ready'])
        constraint['source']=dict(kind='node',node_id='root',property='translation')
        self.assertTrue(self.check()['source_ready'])
        constraint.pop('target_position');constraint['target']=scalar
        self.assert_code('ANIMATION_CONSTRAINT')
        constraint.update(type='rotation',source=dict(kind='bone',actor_id='actor',bone_id='b2',property='rotation'),
                          target=dict(kind='bone',actor_id='actor',bone_id='b0',property='rotation'))
        self.assertTrue(self.check()['source_ready'])
        constraint.pop('target');constraint['target_position']=[0,0,0]
        self.assert_code('ANIMATION_CONSTRAINT')

    def test_provenance_scene_versions_cannot_select_active_shot_scene(self):
        historical=deepcopy(self.scene);historical.update(version='2.0.0',actors=[])
        self.f.project['provenance']['sources']=[self.f.put('historical-scene.json',historical,'scene')]
        sequence=deepcopy(self.f.values['sequence']);placement=self.placement();placement['actor_id']='actor'
        sequence['shots'][0]['clip_placements']=[placement]
        self.f.project['sequences']=[self.f.put('sequence.json',sequence,'sequence')]
        self.assertTrue(self.check()['source_ready'])  # Root version1 retains its actor.
        historical['actors']=[deepcopy(self.actor)]
        self.f.project['provenance']['sources']=[self.f.put('historical-scene.json',historical,'scene')]
        self.scene['actors']=[]
        self.assert_code('ANIMATION_REFERENCE')  # History cannot supply a missing active actor.
        self.scene.update(version='2.0.0',actors=[self.actor]);self.f.project['provenance']['sources']=[]
        self.assertTrue(self.check()['source_ready'])

    def test_zip_directory_is_admitted_before_constructor_allocation(self):
        import zipfile
        from unittest.mock import patch
        from animation3d_assets import Snapshot
        from rig_contracts import DEFAULT_LIMITS
        original=zipfile.ZipFile
        archive_path=self.f.root/'directory.npz'
        with original(archive_path,'w') as archive:
            for index in range(1000):archive.writestr(str(index),b'')
        with tempfile.TemporaryDirectory() as destination:
            snapshot=Snapshot(self.f.root,destination);snapshot.memory_limit=1000
            with patch('animation3d_assets.zipfile.ZipFile',wraps=original) as constructor:
                with self.assertRaises(RigError):snapshot.copy(archive_path.name)
                self.assertEqual(constructor.call_count,0)
        with original(archive_path,'w') as archive:
            for index in range(3):archive.writestr(str(index),b'')
        # Metadata alone exceeds this bound although encoded bytes fit it.
        with tempfile.TemporaryDirectory() as destination:
            snapshot=Snapshot(self.f.root,destination);snapshot.memory_limit=1000
            with patch('animation3d_assets.zipfile.ZipFile',wraps=original) as constructor:
                with self.assertRaises(RigError):snapshot.copy(archive_path.name)
                self.assertEqual(constructor.call_count,0)
        # Exercise the canonical member-count authority with a small limit.
        with tempfile.TemporaryDirectory() as destination:
            snapshot=Snapshot(self.f.root,destination);snapshot.memory_limit=100000
            with patch.dict(DEFAULT_LIMITS,archive_files=2),patch('animation3d_assets.zipfile.ZipFile',wraps=original) as constructor:
                with self.assertRaises(RigError):snapshot.copy(archive_path.name)
                self.assertEqual(constructor.call_count,0)
        with tempfile.TemporaryDirectory() as destination:
            snapshot=Snapshot(self.f.root,destination);snapshot.memory_limit=100000
            snapshot.copy(archive_path.name)
            self.assertGreater(snapshot.expanded_bytes,archive_path.stat().st_size)

    def test_snapshot_fifo_replacement_is_rejected_at_descriptor_open(self):
        import multiprocessing as mp
        import os
        from unittest.mock import patch
        from animation3d_assets import Snapshot
        if not hasattr(os,'mkfifo') or 'fork' not in mp.get_all_start_methods():
            self.skipTest('POSIX FIFO/fork replacement race')
        context=mp.get_context('fork');replaced=context.Event();receive,send=context.Pipe(duplex=False)
        root=self.f.root/'race';root.mkdir();destination=self.f.root/'race-out';destination.mkdir()
        source=root/'race.json';source.write_text('{}')
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
                with patch('rig_errors.os.open',descriptor_open),patch.object(os,'supports_dir_fd',supported),patch.object(Path,'open',path_open):
                    Snapshot(root,destination).copy(source.name)
            except RigError as error:send.send(('rejected',error.report['code']))
            except BaseException as error:send.send(('unexpected',type(error).__name__))
            else:send.send(('admitted',None))
            finally:send.close()
        process=context.Process(target=child);process.start();send.close()
        try:
            self.assertTrue(replaced.wait(5),'The replacement hook must actually execute.')
            process.join(2)
            self.assertFalse(process.is_alive(),'Only this own bounded child may be stopped on a blocking regression.')
            self.assertTrue(receive.poll(1))
            self.assertEqual(receive.recv(),('rejected','RIG_PAYLOAD_TYPE'))
            self.assertEqual(process.exitcode,0)
        finally:
            if process.is_alive():process.terminate();process.join(5)
            receive.close()

    def test_target_registry_manifest_fifo_replacement_is_rejected(self):
        import multiprocessing as mp
        import os
        from unittest.mock import patch
        if not hasattr(os, 'mkfifo') or 'fork' not in mp.get_all_start_methods():
            self.skipTest('POSIX FIFO/fork replacement race')
        context = mp.get_context('fork')
        replaced = context.Event()
        receive, send = context.Pipe(duplex=False)
        root = self.rig.root
        source = root/'rig.json'
        def child():
            original_open = os.open
            original_path_open = Path.open
            def replace():
                if not replaced.is_set():
                    source.unlink()
                    os.mkfifo(source)
                    replaced.set()
            def descriptor_open(path, *args, **kwargs):
                if str(path) in (str(source), source.name):
                    replace()
                return original_open(path, *args, **kwargs)
            def path_open(path, *args, **kwargs):
                if path == source:
                    replace()
                return original_path_open(path, *args, **kwargs)
            supported = {descriptor_open if operation is original_open else operation for operation in os.supports_dir_fd}
            try:
                with patch('rig_errors.os.open', descriptor_open), patch.object(os, 'supports_dir_fd', supported), patch.object(Path, 'open', path_open):
                    TargetRegistry.from_rig(root, 'actor')
            except RigError as error:
                send.send(('rejected', error.report['code']))
            except BaseException as error:
                send.send(('unexpected', type(error).__name__))
            else:
                send.send(('admitted', None))
            finally:
                send.close()
        process = context.Process(target=child)
        process.start()
        send.close()
        try:
            self.assertTrue(replaced.wait(5), 'Replacement hook must execute.')
            process.join(2)
            self.assertFalse(process.is_alive(), 'Only this own bounded child may be stopped on blocking regression.')
            self.assertTrue(receive.poll(1))
            self.assertEqual(receive.recv(), ('rejected', 'RIG_PAYLOAD_TYPE'))
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            receive.close()

    def _set_numeric_budget_case(self, family, value):
        self.scene['markers']=[];self.scene['interactions']=[];self.actor['constraints']=[];self.layer['placements']=[]
        self.f.project['state_machines']=[];self.f.project['sequences']=[]
        if family=='marker':
            self.scene['markers']=[dict(id='boundary',name='boundary',frame=value,type='marker')]
        elif family=='constraint-end':
            constraint=self.constraint('bounded');constraint.update(start_frame=0,end_frame=value)
            self.actor['constraints']=[constraint]
        elif family in ('interaction-end','ownership-duration'):
            interaction=deepcopy(self.f.values['interaction'])
            interaction.update(participants=[dict(actor_id='actor',role='participant')],end_frame=value)
            if family=='ownership-duration':
                interaction['ownership_changes']=[dict(frame=0,next_owner='actor',world_pose_policy='blend_to_receiver',transfer_duration_frames=value)]
            self.scene['interactions']=[interaction]
        elif family=='transition-duration':
            machine=deepcopy(self.f.values['state-machine']);machine['states'][0]['clip']=self.f.project['clips'][0]
            machine['parameters']=[dict(name='go',type='bool',default=False)]
            machine['transitions']=[dict(id='loop',from_state='idle',to_state='idle',conditions=[dict(parameter='go',operator='eq',value=True)],
                condition_mode='all',priority=0,duration_frames=value,blend_curve='linear',interruption_policy='none')]
            self.f.project['state_machines']=[self.f.put('bounded-machine.json',machine,'state-machine')]
        elif family=='sequence-duration':
            sequence=deepcopy(self.f.values['sequence']);sequence['total_frames']=value;sequence['shots'][0]['duration_frames']=value
            self.f.project['sequences']=[self.f.put('bounded-sequence.json',sequence,'sequence')]
        elif family=='placement-end':
            placement=self.placement();placement.update(start_frame=value-24,duration_frames=24.0)
            self.layer['placements']=[placement]
        elif family=='placement-offset':
            placement=self.placement();placement['time_offset_frames']=value;self.layer['placements']=[placement]
        else:raise AssertionError(family)

    def test_integer_and_whole_float_frame_budget_boundaries_match(self):
        for family in ('marker','constraint-end','interaction-end','ownership-duration','transition-duration','sequence-duration','placement-end','placement-offset'):
            for encoding in (int,float):
                self.f.project['budgets']['max_frames']=encoding(100)
                for boundary,accepted in ((100,True),(101,False)):
                    self._set_numeric_budget_case(family,encoding(boundary))
                    with self.subTest(family=family,encoding=encoding.__name__,boundary=boundary):
                        if accepted:self.assertTrue(self.check()['source_ready'])
                        else:self.assert_code('ANIMATION_LIMIT')

    def test_boolean_and_fractional_interval_encodings_remain_rejected(self):
        for family in ('marker','constraint-end','interaction-end','ownership-duration','transition-duration','sequence-duration','placement-offset'):
            for value in (True,100.5):
                self._set_numeric_budget_case(family,value)
                with self.subTest(family=family,value=value):self.assert_code('ANIMATION_SCHEMA')
