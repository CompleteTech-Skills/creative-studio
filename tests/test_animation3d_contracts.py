from copy import deepcopy
import json
import io
import subprocess
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import unittest
from jsonschema import Draft202012Validator
from animation3d_contracts import schemas,validate_contract,KINDS
from animation3d_test_helpers import samples
from rig_errors import RigError,decode_json


class AnimationContractTests(unittest.TestCase):
    def test_all_generated_schemas_and_structural_runtime_agree(self):
        for name,value in samples().items():
            schema=schemas()['animation3d-'+name+'.schema.json']
            with self.subTest(name=name):
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(value)
                self.assertEqual(value,validate_contract(value))
                self.assertEqual(schema,json.loads((Path(__file__).resolve().parents[1]/'profiles'/('animation3d-'+name+'.schema.json')).read_text()))
                bad=deepcopy(value);bad['unversioned']=True
                self.assertFalse(Draft202012Validator(schema).is_valid(bad))
                with self.assertRaises(RigError):validate_contract(bad)

    def test_duplicate_json_and_nonfinite_fail_before_document_admission(self):
        for value in ('{"kind":"a","kind":"b"}', '{"n":NaN}', '{"n":Infinity}'):
            with self.assertRaises(RigError):decode_json(value)
        value=samples()['clip']; value['default_speed']=float('nan')
        with self.assertRaises(RigError):validate_contract(value)

    def test_clip_rejects_duplicate_targets_terminal_overrun_and_bad_quaternions(self):
        for mutate in (lambda v:v['tracks'].append(deepcopy(v['tracks'][0])),
                       lambda v:v['tracks'][0]['keys'][-1]['time'].update(frame=25),
                       lambda v:v['root_motion'].update(enabled=True)):
            value=samples()['clip'];mutate(value)
            with self.assertRaises(RigError):validate_contract(value)
        value=samples()['track'];value['value'].update(target=dict(kind='bone',actor_id='actor',bone_id='root',property='rotation'),value_type='quaternion',default_value=[0,0,0,0],keys=[])
        with self.assertRaises(RigError):validate_contract(value)

    def test_state_machine_rejects_broken_graph_condition_types_and_zero_time_cycle(self):
        value=samples()['state-machine'];value['initial_state']='missing'
        with self.assertRaises(RigError):validate_contract(value)
        for duration in (0,1):
            value=samples()['state-machine'];value['parameters']=[dict(name='go',type='bool',default=False)]
            value['transitions']=[dict(id='loop',from_state='idle',to_state='idle',conditions=[dict(parameter='go',operator='eq',value=True)],condition_mode='all',priority=0,duration_frames=duration,blend_curve='linear',interruption_policy='none')]
            if duration==0:
                with self.assertRaises(RigError):validate_contract(value)
            else:validate_contract(value)
        value['transitions'][0]['conditions'][0]['operator']='lt'
        with self.assertRaises(RigError):validate_contract(value)

    def test_numeric_condition_intersection_and_wildcard_priority(self):
        value=samples()['state-machine'];value['parameters']=[dict(name='speed',type='float',default=0)]
        transition=dict(id='loop',from_state='idle',to_state='idle',conditions=[dict(parameter='speed',operator='gt',value=10),dict(parameter='speed',operator='lt',value=5)],
            condition_mode='all',priority=0,duration_frames=1,blend_curve='linear',interruption_policy='none')
        value['transitions']=[transition]
        with self.assertRaises(RigError):validate_contract(value)
        transition['conditions']=[dict(parameter='speed',operator='gt',value=0)]
        value['transitions'].append(dict(transition,id='global',from_state='*'))
        with self.assertRaises(RigError):validate_contract(value)

    def test_interaction_chain_and_sequence_are_consistent(self):
        value=samples()['interaction'];value['ownership_changes']=[dict(frame=2,previous_owner='a',next_owner='b',world_pose_policy='preserve'),dict(frame=4,previous_owner='a',world_pose_policy='preserve')]
        with self.assertRaises(RigError):validate_contract(value)
        value=samples()['sequence'];value['shots'][0]['start_frame']=1
        with self.assertRaises(RigError):validate_contract(value)
        value=samples()['sequence'];value['shots'][0]['transition_to_next']=dict(type='dissolve',duration_frames=2)
        with self.assertRaises(RigError):validate_contract(value)

    def test_sample_cli_rejects_wrong_document_kind_with_typed_json(self):
        from animation3d import main
        root=Path(__file__).resolve().parents[1]
        output=io.StringIO()
        with redirect_stdout(output):
            code=main(['sample','--file',str(root/'examples/animation3d-contracts/clip.json'),
                '--registry','missing.json','--root',str(root),'--frame','1'])
        self.assertEqual(code,2)
        self.assertEqual(json.loads(output.getvalue())['error']['code'],'ANIMATION_KIND')

    def test_extended_math_rejects_overflow_and_composed_shear(self):
        import math
        import animation3d_math as am
        for operation in (lambda:am.arc_length([[-1e308,0,0],[1e308,0,0]]),
                          lambda:am.vector_add([1e308,0,0],[1e308,0,0]),
                          lambda:am.quaternion_log([1e308,0,0,1])):
            with self.assertRaises(RigError):operation()
        parent=am.compose(scale=[2,1,1])
        local=am.compose(rotation=am.euler_xyz([0,0,math.pi/4]))
        with self.assertRaises(RigError):am.local_to_world(local,parent)

    def test_metadata_is_not_treated_as_embedded_animation_records(self):
        value=samples()['clip']
        for payload in (dict(source='s',target='t',weight=1,type='custom',enabled=True),dict(numerator=0,denominator=0)):
            value['provenance']['parameters']=payload
            value['tracks'][0]['keys'][0]['metadata']=payload
            validate_contract(value)

    def test_missing_optional_numeric_dependency_is_actionable(self):
        script="""
import sys, importlib.abc
class BlockNumpy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'numpy' or fullname.startswith('numpy.'):
            raise ImportError('deliberately unavailable')
sys.meta_path.insert(0,BlockNumpy())
from animation3d_registry import TargetRegistry, sample_track
from rig_errors import RigError
for call in (lambda:TargetRegistry.from_rig('.', 'actor'),lambda:sample_track({}, {}, None)):
    try:call()
    except RigError as error:
        assert error.report['code']=='ANIMATION_DEPENDENCY'
        assert 'requirements-rig.txt' in str(error)
    else:raise AssertionError('missing dependency accepted')
"""
        result=subprocess.run([sys.executable,'-c',script],env={**__import__('os').environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'scripts')},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_missing_schema_dependency_is_actionable(self):
        script="""
import sys, importlib.abc
class BlockSchema(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'jsonschema' or fullname.startswith('jsonschema.'):
            raise ImportError('deliberately unavailable')
sys.meta_path.insert(0,BlockSchema())
from animation3d_contracts import validate_contract
from rig_errors import RigError
try:validate_contract(dict(schema_version=1,kind='animation_project_3d'))
except RigError as error:assert error.report['code']=='ANIMATION_DEPENDENCY'
else:raise AssertionError('missing dependency accepted')
"""
        result=subprocess.run([sys.executable,'-c',script],env={**__import__('os').environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'scripts')},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_registry_rejects_hash_valid_but_semantically_cyclic_native_rig(self):
        from rig_test_helpers import fixture
        from rig_errors import atomic_json,sha256
        from animation3d_registry import TargetRegistry
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False)
            registry=TargetRegistry.from_rig(rig.root,'actor')
            manifest=rig.root/'rig.json'
            value=json.loads(manifest.read_text())
            value['skeleton']['bones'][0]['parent']=value['skeleton']['bones'][0]['id']
            atomic_json(manifest,value)
            document=deepcopy(registry.document)
            for entry in document['entries']:entry['source']['sha256']=sha256(manifest)
            with self.assertRaises(RigError):TargetRegistry.from_rig(rig.root,'actor')
            rebuilt=TargetRegistry(rig.root,document)
            with self.assertRaises(RigError):rebuilt.resolve(document['entries'][0]['target'])

    def test_renderer_audio_and_path_are_explicit(self):
        for key,content in [('include_audio',True),('output_directory','../outside'),('end_frame_exclusive',0)]:
            value=samples()['render'];value[key]=content
            with self.assertRaises(RigError):validate_contract(value)


class AnimationReviewRegressions(unittest.TestCase):
    def test_named_record_keys_cannot_bypass_track_or_asset_validation(self):
        from animation3d_test_helpers import TRACK,ASSET
        for name in ('actor','metadata','payload','tool_versions','per_bone_weights','initial_state_parameters','parameters'):
            value=samples()['sequence']
            track=deepcopy(TRACK)
            value['shots'][0]['actor_overrides']={name:[track]}
            validate_contract(value)
            track['keys'][0]['interpolation']='bezier'
            with self.subTest(name=name,record='track'):
                with self.assertRaises(RigError) as raised:validate_contract(value)
                self.assertEqual(raised.exception.report['code'],'ANIMATION_INTERPOLATION')
            value=samples()['project'];asset={**ASSET,'kind':'state-machine'}
            value['character_state_bindings']={name:asset}
            validate_contract(value)
            asset['path']='../outside.json'
            with self.subTest(name=name,record='asset'),self.assertRaises(RigError):
                validate_contract(value)

    def test_small_valid_scales_remain_invertible(self):
        import numpy as np
        import animation3d_math as am
        for scale in (1e-5,1e-6,1e-7):
            parent=am.compose(scale=[scale]*3)
            np.testing.assert_allclose(am.inverse(parent) @ parent,np.eye(4),atol=1e-12)
            np.testing.assert_allclose(am.world_to_local(parent,parent),np.eye(4),atol=1e-12)
        with self.assertRaises(RigError) as raised:am.inverse(np.diag([1,0,1,1]))
        self.assertEqual(raised.exception.report['code'],'ANIMATION_MATRIX')

    def test_embedded_interactions_apply_standalone_ownership_rules(self):
        base=samples()['interaction']
        base['ownership_changes']=[dict(frame=2,previous_owner='a',next_owner='b',world_pose_policy='preserve'),
                                   dict(frame=4,previous_owner='b',next_owner='a',world_pose_policy='preserve')]
        scene=samples()['scene'];scene['interactions']=[base]
        validate_contract(base);validate_contract(scene)
        for case in ('broken-chain','duplicate-participant','nonparticipant','outside-interval'):
            interaction=deepcopy(base)
            if case=='broken-chain':interaction['ownership_changes'][1]['previous_owner']='a'
            elif case=='duplicate-participant':interaction['participants'].append(deepcopy(interaction['participants'][0]))
            elif case=='nonparticipant':interaction['ownership_changes'][0]['next_owner']='other'
            else:interaction['ownership_changes'][1]['frame']=interaction['end_frame']
            scene['interactions']=[interaction]
            for document in (interaction,scene):
                with self.subTest(case=case,kind=document['kind']),self.assertRaises(RigError):validate_contract(document)

    def test_shot_placements_apply_the_same_interval_rules_as_layers(self):
        from animation3d_test_helpers import ASSET
        placement=dict(id='placement',clip={**ASSET,'kind':'clip'},start_frame=0,duration_frames=12,
            source_in_frame=0,source_out_frame=12,playback_rate=dict(numerator=1,denominator=1),
            blend_in_frames=2,blend_out_frames=2,weight=1,layer_id='body',enabled=True)
        sequence=samples()['sequence'];sequence['shots'][0]['clip_placements']=[placement]
        layer=samples()['layer'];layer['value']['placements']=[placement]
        validate_contract(sequence);validate_contract(layer)
        for changes in (dict(source_in_frame=12,source_out_frame=6),
                        dict(source_in_frame=12,source_out_frame=12),
                        dict(blend_in_frames=7,blend_out_frames=6)):
            candidate={**placement,**changes}
            sequence['shots'][0]['clip_placements']=[candidate];layer['value']['placements']=[candidate]
            for document in (sequence,layer):
                with self.subTest(changes=changes,kind=document['kind']),self.assertRaises(RigError):validate_contract(document)

    def test_scene_track_collections_reject_duplicate_channels(self):
        from animation3d_test_helpers import TRACK
        for field,target in [('object_tracks',dict(kind='node',node_id='node',property='translation')),
                             ('camera_tracks',dict(kind='camera',camera_id='camera',property='translation')),
                             ('lighting_tracks',dict(kind='light',light_id='light',property='translation')),
                             ('environment_tracks',dict(kind='environment',property='sky_rotation'))]:
            track=deepcopy(TRACK);track['target']=target
            if field=='environment_tracks':
                track['value_type']='float';track['default_value']=0
                for key in track['keys']:key['value']=0
            scene=samples()['scene'];scene[field]=[track]
            validate_contract(scene)
            other=deepcopy(track);other['id']='other';scene[field].append(other)
            with self.subTest(field=field):
                with self.assertRaises(RigError) as raised:validate_contract(scene)
                self.assertEqual(raised.exception.report['code'],'ANIMATION_DUPLICATE')
            # A different channel remains allowed within the same owner domain.
            if field=='environment_tracks':other['target']['property']='ambient_intensity'
            else:other['target'][next(k for k in target if k.endswith('_id'))]='another'
            validate_contract(scene)

    def test_wildcard_expansion_is_bounded_before_graph_allocation(self):
        from unittest.mock import patch
        import animation3d_contracts as contracts
        value=samples()['state-machine']
        value['states']=[dict(value['states'][0],id=f's{i}') for i in range(3)]
        value['initial_state']='s0';value['parameters']=[dict(name='go',type='bool',default=False)]
        value['transitions']=[dict(id=f't{i}',from_state='*',to_state=f's{i}',conditions=[dict(parameter='go',operator='eq',value=True)],condition_mode='all',priority=i,duration_frames=1,blend_curve='linear',interruption_policy='none') for i in range(3)]
        with patch.object(contracts,'MAX_STATE_TRANSITION_EXPANSIONS',8):
            with self.assertRaises(RigError) as caught:validate_contract(value)
            self.assertEqual(caught.exception.report['code'],'ANIMATION_LIMIT')
        with patch.object(contracts,'MAX_STATE_TRANSITION_EXPANSIONS',9):validate_contract(value)

    def test_zero_exit_time_does_not_hide_zero_time_cycle(self):
        value=samples()['state-machine'];value['parameters']=[dict(name='go',type='bool',default=False)]
        value['transitions']=[dict(id='loop',from_state='idle',to_state='idle',conditions=[dict(parameter='go',operator='eq',value=True)],condition_mode='all',priority=0,duration_frames=0,exit_time_normalized=0,blend_curve='linear',interruption_policy='none')]
        with self.assertRaises(RigError):validate_contract(value)
        value['transitions'][0]['exit_time_normalized']=.1
        validate_contract(value)

    def test_typed_reference_sites_reject_wrong_asset_kinds(self):
        for name,field in [('render','project'),('scene','scene_asset'),('retarget','source_rig')]:
            value=samples()[name];validate_contract(value)
            value[field]['kind']='wrong-owner'
            with self.subTest(name=name),self.assertRaises(RigError):validate_contract(value)
        value=samples()['state-machine'];value['states'][0]['clip']['kind']='rig'
        with self.assertRaises(RigError):validate_contract(value)
        value=samples()['sequence'];from animation3d_test_helpers import ASSET
        value['shots'][0]['clip_placements']=[dict(id='p',clip={**ASSET,'kind':'clip'},start_frame=0,duration_frames=1,source_in_frame=0,playback_rate=dict(numerator=1,denominator=1),blend_in_frames=0,blend_out_frames=0,weight=1,layer_id='body',enabled=True)]*2
        with self.assertRaises(RigError):validate_contract(value)

    def test_native_morph_range_and_one_resolution_per_sample(self):
        from unittest.mock import patch
        from animation3d_registry import TargetRegistry,sample_track
        from rig_test_helpers import fixture
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False);registry=TargetRegistry.from_rig(rig.root,'actor')
            entry=next(e for e in registry.document['entries'] if e['target']['kind']=='morph')
            self.assertEqual((entry['minimum'],entry['maximum']),(-2,2))
            track=dict(id='morph',target=entry['target'],value_type='float',default_value=-1,keys=[dict(time=dict(frame=0),value=-2,interpolation='linear'),dict(time=dict(frame=10),value=2,interpolation='linear')],extrapolation_before='hold',extrapolation_after='hold',enabled=True)
            for enabled,keys in [(True,track['keys']),(False,track['keys']),(True,[])]:
                candidate={**track,'enabled':enabled,'keys':keys}
                with patch.object(registry,'resolve',wraps=registry.resolve) as resolve:
                    sample_track(candidate,dict(frame=5),registry)
                    self.assertEqual(resolve.call_count,1)
            track['keys'][0]['value']=-2.1
            with self.assertRaises(RigError):sample_track(track,dict(frame=5),registry)

    def test_hermite_overshoot_cannot_exceed_numeric_contract(self):
        from animation3d_registry import TargetRegistry,sample_track
        from rig_test_helpers import fixture
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False);registry=TargetRegistry.from_rig(rig.root,'actor')
            target=next(e['target'] for e in registry.document['entries'] if e['target']['kind']=='bone' and e['target']['property']=='translation')
            track=dict(id='move',target=target,value_type='vec3',default_value=[0,0,0],keys=[dict(time=dict(frame=0),value=[0,0,0],interpolation='hermite',out_tangent=[1e9,0,0]),dict(time=dict(frame=100),value=[0,0,0],interpolation='linear',in_tangent=[-1e9,0,0])],extrapolation_before='hold',extrapolation_after='hold',enabled=True)
            registry.validate_track(track)
            with self.assertRaises(RigError):sample_track(track,dict(frame=50),registry)

class AnimationClosureTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, value):
        import hashlib
        payload = (json.dumps(value) + '\n').encode()
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return dict(id=value['id'], version=value['version'], path=name,
                    kind=value['kind'], sha256=hashlib.sha256(payload).hexdigest())

    def test_complete_clip_closure_and_missing_payload(self):
        from animation3d_assets import load_project
        from animation3d_test_helpers import samples
        values=samples(); clip=values['clip']; clip['tracks']=[]
        reference=self.write('clip.json',clip); reference['kind']='clip'
        project=values['project']; project['clips']=[reference]
        project['budgets']['max_work_bytes']=1000000
        self.write('animation-project.json',project)
        result=load_project(self.root)
        self.assertTrue(result['source_ready'])
        self.assertEqual(set(result['inventory']), {'animation-project.json','clip.json'})
        (self.root/'clip.json').unlink()
        with self.assertRaises(RigError): load_project(self.root)

    def test_closure_rejects_hash_identity_cycles_and_budget(self):
        from animation3d_assets import load_project
        from animation3d_test_helpers import samples
        values=samples(); clip=values['clip']; clip['tracks']=[]
        reference=self.write('clip.json',clip); reference['kind']='clip'
        project=values['project']; project['clips']=[reference]
        project['budgets']['max_work_bytes']=1000000
        self.write('animation-project.json',project)
        reference['id']='wrong'; self.write('animation-project.json',project)
        with self.assertRaisesRegex(RigError,'identity'): load_project(self.root)
        reference['id']=clip['id']; reference['sha256']='0'*64
        self.write('animation-project.json',project)
        with self.assertRaisesRegex(RigError,'hash'): load_project(self.root)
        reference.update(self.write('clip.json',clip)); reference['kind']='clip'
        project['budgets']['max_work_bytes']=100
        self.write('animation-project.json',project)
        with self.assertRaises(RigError): load_project(self.root)

    def test_native_camera_and_lookdev_targets(self):
        import shutil
        from animation3d_registry import TargetRegistry
        from animation3d_contracts import KINDS
        base=Path(__file__).resolve().parents[1]
        shutil.copytree(base/'examples/camera3d',self.root/'camera')
        camera=json.loads((self.root/'camera/registry.json').read_text())
        camera_ref=self.write('camera/registry.json',camera)
        material=json.loads((base/'examples/lookdev-contracts/ceramic.json').read_text())
        material_ref=self.write('material.json',material)
        entries=[dict(owner='camera3d',source=camera_ref,target=dict(kind='camera',camera_id='camera',property='focal_length')),
                 dict(owner='lookdev',source=material_ref,target=dict(kind='material',material_id='ceramic',property='roughness'))]
        registry=TargetRegistry(self.root,dict(schema_version=1,kind=KINDS['registry'],id='targets',version='1.0.0',entries=entries))
        for entry in entries: self.assertEqual(registry.resolve(entry['target']),entry)
        material['surface']['roughness']=0.7;self.write('material.json',material)
        with self.assertRaises(RigError): registry.resolve(entries[1]['target'])
        (self.root/'camera/optics.json').write_text('{}')
        with self.assertRaises(RigError): registry.resolve(entries[0]['target'])

    def test_native_scene_target_and_unknown_node(self):
        import shutil
        from animation3d_registry import TargetRegistry
        from animation3d_contracts import KINDS
        base=Path(__file__).resolve().parents[1]
        shutil.copytree(base/'examples/scene3d-contracts',self.root/'scene')
        value=json.loads((self.root/'scene/project.json').read_text())
        reference=self.write('scene/project.json',value)
        scene=json.loads((self.root/'scene/example-scene.json').read_text())
        target=dict(kind='node',node_id=scene['nodes'][0]['id'],property='translation')
        entry=dict(owner='scene3d',source=reference,target=target)
        registry=TargetRegistry(self.root,dict(schema_version=1,kind=KINDS['registry'],id='targets',version='1.0.0',entries=[entry]))
        self.assertEqual(registry.resolve(target),entry)
