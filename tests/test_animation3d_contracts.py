from copy import deepcopy
import json
import io
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

    def test_renderer_audio_and_path_are_explicit(self):
        for key,content in [('include_audio',True),('output_directory','../outside'),('end_frame_exclusive',0)]:
            value=samples()['render'];value[key]=content
            with self.assertRaises(RigError):validate_contract(value)
