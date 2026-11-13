"""Regressions for independent release review of frozen candidate 8bc8071."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
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
