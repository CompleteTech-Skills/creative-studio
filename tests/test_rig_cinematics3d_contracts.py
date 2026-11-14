"""Executable authoring/admission checks; no synthetic render success claims."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from jsonschema import Draft202012Validator
from cinematics3d_contracts import (Registry, admission, default_effect, effect_registry,
                                   schemas, validate_document, migrate, canonical_bytes)
from rig_errors import RigError, decode_json

ROOT = Path(__file__).resolve().parents[1]


def doc(kind, identifier, **fields):
    return dict(schema_version=1, kind='cinematics3d-' + kind, id=identifier,
                version='1.0.0', registry_version='1.0.0',
                metadata=dict(fixture=True, description='Contract test fixture; no rendering.'), **fields)


def ref(value, path=None):
    return dict(id=value['id'], version=value['version'], kind=value['kind'],
                path=path or value['id']+'.json', sha256=hashlib.sha256(canonical_bytes(value)).hexdigest())


def external(kind, identifier='native'):
    return dict(id=identifier, version='1.0.0', kind=kind, path=identifier+'.json', sha256=hashlib.sha256(b'{}').hexdigest())


def bundle(*values, external_files=None):
    return Registry([(v['id']+'.json', canonical_bytes(v)) for v in values], external=external_files)


COLOR = dict(working_space='scene-linear-srgb', alpha='premultiplied', display_space='srgb')
START = dict(frame=0, ticks=0, ticks_per_frame=1000000)
END = dict(frame=24, ticks=0, ticks_per_frame=1000000)
RATE = dict(numerator=24, denominator=1)


class CinematicsTests(unittest.TestCase):
    def setUp(self):
        self.effect = default_effect('exposure', 'exposure', scope='scene')
        self.stack = doc('stack', 'stack', scope='scene', effects=[ref(self.effect)], parent=None, bypass=[])

    def assertRejected(self, value):
        with self.assertRaises(RigError): validate_document(value)

    def test_all_families_schema_runtime_roundtrip(self):
        shot = doc('shot', 'shot', source=external('scene3d-shot'), stack=ref(self.stack), fps=RATE, start=START, end=END, color=COLOR)
        scene = doc('scene', 'scene', source=external('scene3d-scene'), stack=ref(self.stack), shots=[ref(shot)], objects=[])
        preset = doc('preset', 'preset', stack=ref(self.stack), previous=None)
        render = doc('render', 'render', scene=ref(scene), source_snapshot=external('render3d-snapshot'), graph=None,
                     color=COLOR, width=1280, height=720, fps=RATE, start=START, end=END, max_pixels=1280*720,
                     output='png', audio_streams=0, policy=dict(allow_approximation=False, on_unavailable='reject'))
        graph = self.graph()
        for value in [self.effect, self.stack, shot, scene, preset, render, graph]:
            with self.subTest(kind=value['kind']):
                schema=schemas()[value['kind']+'.schema.json']; Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(value)
                self.assertEqual(validate_document(value), json.loads(canonical_bytes(value)))
                self.assertEqual(migrate(value), value)

    def test_all_effects_have_explicit_defaults_units_and_unavailable_status(self):
        for key, record in effect_registry().items():
            effect=default_effect(key, 'effect')
            if key in ('lut','cryptomatte-select','depth-of-field'):
                effect['inputs']=[external({'lut':'lut-file','cryptomatte-select':'cryptomatte-manifest','depth-of-field':'camera3d-optics'}[key], 'input')]
            validate_document(effect)
            self.assertEqual(record['default_status'], 'unavailable')
            self.assertFalse(record['executed'])
            self.assertTrue(all(p['unit'] for p in record['parameters'].values()))
            self.assertFalse(admission([effect])['admitted'])

    def test_unknown_effect_and_parameters(self):
        for field in ['new_field', 'stopz']:
            bad=deepcopy(self.effect); bad['operation']['parameters'][field]=3
            self.assertRejected(bad)
        bad=deepcopy(self.effect);bad['operation']['type']='imaginary';self.assertRejected(bad)

    def test_wrong_bounds_types_and_nonfinite(self):
        for value in [-25,25,True,'3',float('nan'),float('inf')]:
            bad=deepcopy(self.effect);bad['operation']['parameters']['stops']=value;self.assertRejected(bad)
        for value in [-24,24,0]:
            bad=deepcopy(self.effect);bad['operation']['parameters']['stops']=value;validate_document(bad)

    def test_schema_rejects_parameter_bounds_and_unknown_fields(self):
        bad=deepcopy(self.effect);bad['operation']['parameters']['stops']=25
        self.assertFalse(Draft202012Validator(schemas()['cinematics3d-effect.schema.json']).is_valid(bad))
        bad=deepcopy(self.effect);bad['surprise']=True
        self.assertFalse(Draft202012Validator(schemas()['cinematics3d-effect.schema.json']).is_valid(bad))

    def test_duplicate_keys_and_invalid_json(self):
        for value in ['{"id":"x","id":"x"}', '{"x":NaN}', '{"x":1e999}']:
            with self.assertRaises(RigError): decode_json(value)

    def test_path_security(self):
        for path in ['../x','/tmp/x','C:/x','a\\b','CON.json','a//b','a/../b','a.','a\n','e\u0301.json']:
            bad=deepcopy(self.stack);bad['effects'][0]['path']=path;self.assertRejected(bad)

    def test_id_suffix_and_version_rejection(self):
        for key,value in [('id','effect\n'),('version','01.0.0'),('schema_version',2),('registry_version','2.0.0')]:
            bad=deepcopy(self.effect);bad[key]=value;self.assertRejected(bad)

    def test_immutable_registry_copies_and_exact_hash(self):
        registry=bundle(self.effect,self.stack)
        source=registry.get(ref(self.effect));source['enabled']=False
        self.assertTrue(registry.get(ref(self.effect))['enabled'])
        b=registry.bytes(); b.clear();self.assertEqual(len(registry.bytes()),2)
        bad=deepcopy(self.stack);bad['effects'][0]['sha256']='0'*64
        with self.assertRaises(RigError):bundle(self.effect,bad)

    def test_missing_conflicting_and_alias_inputs(self):
        with self.assertRaises(RigError):bundle(self.stack)
        with self.assertRaises(RigError):Registry([('a.json',canonical_bytes(self.effect)),('A.json',canonical_bytes(self.stack))])
        with self.assertRaises(RigError):bundle(self.effect,self.effect)
        bad=deepcopy(self.stack);bad['effects'].append(ref(self.effect));self.assertRejected(bad)

    def test_reference_identity_not_just_hash(self):
        bad=deepcopy(self.stack);bad['effects'][0]['id']='other'
        with self.assertRaises(RigError):bundle(self.effect,bad)

    def test_order_inheritance_override_bypass(self):
        other=default_effect('film-grain','grain',scope='shot')
        child=doc('stack','child',scope='shot',effects=[ref(other)],parent=ref(self.stack),bypass=['exposure'])
        registry=bundle(self.effect,self.stack,other,child)
        self.assertEqual([e['id'] for e in registry.effective_stack(ref(child))],['grain'])
        child['bypass']=[];registry=bundle(self.effect,self.stack,other,child)
        self.assertEqual([e['id'] for e in registry.effective_stack(ref(child))],['exposure','grain'])

    def test_scope_reversal_and_invalid_bypass(self):
        child=doc('stack','child',scope='shot',effects=[],parent=ref(self.stack),bypass=['unknown'])
        with self.assertRaises(RigError):bundle(self.effect,self.stack,child)
        self.effect['scope']='object';self.stack['scope']='object';self.stack['effects']=[ref(self.effect)]
        child['bypass']=[];child['parent']=ref(self.stack)
        with self.assertRaises(RigError):bundle(self.effect,self.stack,child)

    def test_duplicate_effect_order_and_disabled_bypass(self):
        other=default_effect('color-grade','grade',scope='scene')
        self.stack['effects'].append(ref(other))
        with self.assertRaises(RigError):bundle(self.effect,other,self.stack)
        other['enabled']=False;self.stack['effects'][1]=ref(other)
        bundle(self.effect,other,self.stack)

    def test_graph_connections_types_cycles_and_unknown_nodes(self):
        good=self.graph();validate_document(good)
        bad=deepcopy(good);bad['edges'][0]['source']='missing';self.assertRejected(bad)
        bad=deepcopy(good);bad['nodes'][1]['inputs'][0]['type']='scalar';self.assertRejected(bad)
        bad=deepcopy(good);bad['edges'].append(bad['edges'][0]);self.assertRejected(bad)
        bad=deepcopy(good);bad['edges']=[];self.assertRejected(bad)
        a=dict(id='a',operation='effect',effect=ref(self.effect),inputs=[dict(name='in',type='rgba')],outputs=[dict(name='out',type='rgba')])
        b=deepcopy(a);b['id']='b'
        bad=doc('graph','cycle',nodes=[a,b],edges=[dict(source='a',source_port='out',target='b',target_port='in'),dict(source='b',source_port='out',target='a',target_port='in')],color=COLOR)
        self.assertRejected(bad)

    def test_graph_operation_rules(self):
        bad=self.graph();bad['nodes'][0]['effect']=ref(self.effect);self.assertRejected(bad)
        bad=self.graph();bad['nodes'][0]['inputs']=[dict(name='in',type='rgba')];self.assertRejected(bad)

    def test_explicit_admission_and_approximation(self):
        self.assertFalse(admission([self.effect])['admitted'])
        self.assertTrue(admission([self.effect],{'exposure':'supported'})['admitted'])
        self.assertFalse(admission([self.effect],{'exposure':'approximate'})['admitted'])
        self.effect['policy']['allow_approximation']=True
        report=admission([self.effect],{'exposure':'approximate'})
        self.assertTrue(report['admitted']);self.assertFalse(report['executed'])
        with self.assertRaises(RigError):admission([self.effect],{'exposure':'editor-only'})

    def test_disabled_effects_do_not_require_execution(self):
        self.effect['enabled']=False;self.assertTrue(admission([self.effect])['admitted'])

    def test_external_input_hash_and_no_mutation(self):
        effect=default_effect('lut','lut');effect['inputs']=[external('lut-file')]
        raw=canonical_bytes(effect)
        Registry([('lut.json',raw)],external={'native.json':b'{}'})
        with self.assertRaises(RigError):Registry([('lut.json',raw)],external={'native.json':b'bad'})
        self.assertEqual(raw,canonical_bytes(effect))

    def test_version_migration_fails_closed(self):
        with self.assertRaises(RigError):migrate(self.effect,2)
        bad=deepcopy(self.effect);bad['schema_version']=0
        with self.assertRaises(RigError):migrate(bad)

    def test_history_roundtrip(self):
        previous=doc('preset','look',stack=ref(self.stack),previous=None)
        current=deepcopy(previous);current['version']='1.0.1';current['previous']=ref(previous,'old.json')
        documents=[('exposure.json',canonical_bytes(self.effect)),('stack.json',canonical_bytes(self.stack)),('old.json',canonical_bytes(previous)),('new.json',canonical_bytes(current))]
        registry=Registry(documents)
        self.assertEqual(Registry(list(registry.bytes().items())).get(registry.reference('look','1.0.1')),current)
        current['version']='0.9.0';documents[-1]=('new.json',canonical_bytes(current))
        with self.assertRaises(RigError):Registry(documents)

    def test_history_supports_byte_bounded_large_version_components(self):
        previous=doc('preset','look',stack=ref(self.stack),previous=None)
        previous['version']='9'*4301+'.0.0'
        current=deepcopy(previous);current['version']='1'+'0'*4301+'.0.0';current['previous']=ref(previous,'old.json')
        documents=[('exposure.json',canonical_bytes(self.effect)),('stack.json',canonical_bytes(self.stack)),
                   ('old.json',canonical_bytes(previous)),('new.json',canonical_bytes(current))]
        registry=Registry(documents)
        self.assertEqual(registry.get(registry.reference('look',current['version'])),current)
        current['version']='8'*4301+'.0.0';documents[-1]=('new.json',canonical_bytes(current))
        with self.assertRaises(RigError) as caught:Registry(documents)
        self.assertEqual(caught.exception.report['code'],'CINEMATICS_HISTORY')

    def test_read_cli_and_schema_drift(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/cinematics3d_contracts.py'),'--check'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        output=subprocess.check_output([sys.executable,str(ROOT/'scripts/cinematics3d_contracts.py'),'--registry'],text=True)
        self.assertFalse(json.loads(output)['executed'])

    def test_real_example_and_relocation(self):
        from cinematics3d_example import verify
        result=verify()
        self.assertEqual(result['native_scene_status'],'hash-and-contract-verified')
        self.assertEqual(result['relocation'],'exact-byte-verified')
        self.assertFalse(result['admission']['admitted'])
        self.assertFalse(result['rendered'])

    def test_registry_and_definitions_are_defensive_copies(self):
        from cinematics3d_contracts import definitions
        copied=effect_registry();copied['exposure']['parameters']['stops']['default']=999
        self.assertEqual(effect_registry()['exposure']['parameters']['stops']['default'],0)
        copied=definitions();copied['effect']['properties']['id']={}
        self.assertTrue(definitions()['effect']['properties']['id'])

    def test_input_kind_and_unknown_capability_rejected(self):
        effect=default_effect('depth-of-field','dof')
        self.assertRejected(effect)
        effect['inputs']=[external('camera3d-optics')];validate_document(effect)
        effect['inputs'][0]['kind']='image';self.assertRejected(effect)
        with self.assertRaises(RigError):admission([self.effect],{'made-up':'supported'})

    def test_render_budget_time_and_audio(self):
        value=doc('render','render',scene=external('cinematics3d-scene'),
                  source_snapshot=external('render3d-snapshot','snapshot'),graph=None,
                  color=COLOR,width=1280,height=720,fps=RATE,start=START,end=END,
                  max_pixels=1280*720,output='mp4',audio_streams=0,
                  policy=dict(allow_approximation=False,on_unavailable='reject'))
        validate_document(value)
        for field,bad_value in [('audio_streams',1),('end',START),('max_pixels',1)]:
            bad=deepcopy(value);bad[field]=bad_value;self.assertRejected(bad)
        value['output']='exr';value['color']={**COLOR,'display_space':'rec2020-pq'};self.assertRejected(value)

    def test_scope_binding_mismatch_rejected(self):
        shot=doc('shot','shot',source=external('scene3d-shot'),stack=ref(self.stack),
                 fps=RATE,start=START,end=END,color=COLOR)
        with self.assertRaises(RigError):bundle(self.effect,self.stack,shot,external_files={'native.json':b'{}'})

    def test_public_lookup_and_migration_reject_malformed_inputs(self):
        registry=bundle(self.effect,self.stack)
        for value in [None,[],{'id':[]}]:
            with self.assertRaises(RigError):registry.get(value)
            with self.assertRaises(RigError):migrate(value)
        with self.assertRaises(RigError):registry.reference([], '1.0.0')
        with self.assertRaises(RigError):admission([self.effect,self.effect])
        with self.assertRaises(RigError):admission([self.effect],[])

    def test_optional_runtime_dependencies_are_not_imported(self):
        code="""import sys
class Block:
 def find_spec(self,name,*args):
  if name.split('.')[0] in {'numpy','PIL','bpy','playwright'}: raise AssertionError(name)
sys.meta_path.insert(0,Block())
import cinematics3d_contracts
cinematics3d_contracts.schemas()
"""
        result=subprocess.run([sys.executable,'-c',code],env={**__import__('os').environ,'PYTHONPATH':str(ROOT/'scripts')},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    @staticmethod
    def graph():
        return doc('graph','graph',nodes=[dict(id='source',operation='input',effect=None,inputs=[],outputs=[dict(name='image',type='rgba')]),
            dict(id='output',operation='output',effect=None,inputs=[dict(name='image',type='rgba')],outputs=[])],
            edges=[dict(source='source',source_port='image',target='output',target_port='image')],color=COLOR)


if __name__ == '__main__': unittest.main()
