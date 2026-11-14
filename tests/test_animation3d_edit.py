"""Actual native command edits, immutable inputs and atomic failure coverage."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).resolve().parent)]
from animation3d_edit import apply_clip_commands, request_schema, read_edit_document, MAX_COMMANDS
from animation3d_evaluator import RigClipEvaluator
from test_animation3d_clips import clip
from rig_test_helpers import fixture
from rig_errors import RigError, object_hash

REPO=Path(__file__).resolve().parents[1]

def request(value,*commands):
    return dict(schema_version=1,kind='animation3d-edit-request',expected_document_hash=object_hash(value),commands=list(commands))

class EditCommandsTests(unittest.TestCase):
    def test_replace_key_changes_actual_geometry_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False)
            value=clip('bezier');value['tracks'][0]['target']['bone_id']='b0'
            value['tracks'][0]['keys'][0]['out_handle']=dict(time_delta_frames=8,value_delta=[8,0,0])
            value['tracks'][0]['keys'][-1]['in_handle']=dict(time_delta_frames=-8,value_delta=[-8,0,0])
            key=deepcopy(value['tracks'][0]['keys'][-1]);key['value']=[26,0,0]
            command=request(value,dict(op='replace-key',track_id=value['tracks'][0]['id'],time=key['time'],key=key))
            before=deepcopy((value,command))
            files={str(p.relative_to(rig.root)):p.read_bytes() for p in rig.root.rglob('*') if p.is_file()}
            result=apply_clip_commands(value,command,rig_root=rig.root,actor_id='actor')
            evaluator=RigClipEvaluator(rig.root,'actor')
            original=evaluator.evaluate([value],dict(frame=12))
            edited=evaluator.evaluate([result['clip']],dict(frame=12))
            mesh=rig.geometry.meshes[0].id
            for old,new in zip(original['geometry'][mesh]['positions'],edited['geometry'][mesh]['positions']):
                self.assertAlmostEqual(new[0]-old[0],1)
                self.assertEqual(new[1:],old[1:])
            self.assertTrue(result['native_validated']);self.assertTrue(result['changed'])
            self.assertEqual(result['before_document_hash'],object_hash(value))
            self.assertEqual(result['after_document_hash'],object_hash(result['clip']))
            self.assertEqual((value,command),before)
            self.assertEqual(files,{str(p.relative_to(rig.root)):p.read_bytes() for p in rig.root.rglob('*') if p.is_file()})

    def test_late_invalid_edit_and_stale_request_leave_source_unchanged(self):
        value=clip();before=deepcopy(value)
        commands=request(value,dict(op='edit-clip',name='changed'),dict(op='remove-key',track_id='absent',time=dict(frame=0)))
        with self.assertRaises(RigError):apply_clip_commands(value,commands)
        self.assertEqual(value,before)
        commands=request(value,dict(op='reverse-clip'));commands['expected_document_hash']='0'*64
        with self.assertRaises(RigError) as rejected:apply_clip_commands(value,commands)
        self.assertEqual(rejected.exception.report['code'],'ANIMATION_EDIT_STALE')
        self.assertEqual(value,before)

    def test_unknown_or_boolean_controls_and_excess_commands_fail_closed(self):
        value=clip()
        for command in (dict(op='unknown'),dict(op='splice-frames',start=0,count=True),
                        dict(op='reverse-clip',untrusted=True),dict(op='retime-clip',scale=dict(numerator=1,denominator=0))):
            with self.subTest(command=command),self.assertRaises(RigError) as rejected:
                apply_clip_commands(value,request(value,command))
            self.assertEqual(rejected.exception.report['code'],'ANIMATION_EDIT_SCHEMA')
        commands=request(value,*[dict(op='reverse-clip')]*(MAX_COMMANDS+1))
        with self.assertRaises(RigError):apply_clip_commands(value,commands)

    def test_copy_move_paste_and_track_replacement_use_one_document_result(self):
        value=clip();identifier=value['tracks'][0]['id']
        result=apply_clip_commands(value,request(value,dict(op='copy-keys',track_id=identifier,start=dict(frame=0),end=dict(frame=0)),
            dict(op='move-keys',track_id=identifier,start=dict(frame=0),end=dict(frame=0),offset=dict(numerator=1,denominator=1))))
        self.assertEqual(result['copied_keys'][0]['keys'][0]['time']['frame'],0)
        self.assertEqual(result['clip']['tracks'][0]['keys'][0]['time']['frame'],1)
        second=apply_clip_commands(result['clip'],request(result['clip'],dict(op='paste-keys',track_id=identifier,keys=result['copied_keys'][0]['keys'])))
        self.assertEqual([k['time']['frame'] for k in second['clip']['tracks'][0]['keys']],[0,1,24])
        replacement=deepcopy(value['tracks'][0]);replacement['default_value']=[1,0,0]
        third=apply_clip_commands(value,request(value,dict(op='replace-track',track=replacement)))
        self.assertEqual(third['clip']['tracks'][0]['default_value'],[1,0,0])
        self.assertFalse(third['native_validated'])

    def test_native_context_rejects_disabled_fabricated_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory,animated=False)
            value=clip();value['tracks'][0]['enabled']=False;value['tracks'][0]['target']['bone_id']='fabricated'
            with self.assertRaises(RigError) as rejected:
                apply_clip_commands(value,request(value,dict(op='edit-clip',name='edited')),rig_root=rig.root,actor_id='actor')
            self.assertEqual(rejected.exception.report['code'],'ANIMATION_TARGET')

    def test_schema_is_detached_and_cli_reports_real_validated_result(self):
        from animation3d_edit import request_schema
        schema=request_schema();schema['additionalProperties']=True
        self.assertFalse(request_schema()['additionalProperties'])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);value=clip();source=root/'clip.json';source.write_text(json.dumps(value))
            commands=root/'commands.json';commands.write_text(json.dumps(request(value,dict(op='edit-clip',name='saved edit'))))
            before=source.read_bytes()
            run=subprocess.run([sys.executable,str(REPO/'scripts/animation3d_edit.py'),'--clip',str(source),'--request',str(commands)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            result=json.loads(run.stdout);self.assertEqual(result['clip']['name'],'saved edit')
            self.assertEqual(source.read_bytes(),before)
            self.assertFalse(result['native_validated'])
            if hasattr(Path,'symlink_to'):
                link=root/'link.json'
                try:link.symlink_to(source)
                except OSError:pass
                else:
                    with self.assertRaises(RigError):read_edit_document(link)

if __name__=='__main__':unittest.main()
