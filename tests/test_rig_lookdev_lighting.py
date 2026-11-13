"""Pure admission and exact unit tests for the optional Blender light adapter."""
import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from lookdev_contracts import Registry, canonical_bytes
from lookdev_lighting import (LUMINOUS_EFFICACY, _options, _validated, light_plan,
                              lighting_preset, luminance, preset_names, temperature_color)
from rig_errors import RigError


def source(kind='point', **updates):
    value = dict(schema_version=1,id='key',version='1.0.0',kind='light3d',dependencies=[],
                 provenance={'origin':'authored','tool':'test','created_at':'2026-11-13T00:00:00Z',
                             'source_hashes':[],'attribution':'fixture',
                             'license':{'identifier':'CC0-1.0','source':'self'}},
                 validation_status='unvalidated',capabilities=[], type=kind,
                 transform={'translation_meters':[1,2,3], 'rotation_xyzw':[0,0,0,1]},
                 color={'encoding':'linear-srgb','value':[1,1,1]},
                 intensity={'value':683,'unit':{'point':'candela','spot':'candela','directional':'lux','area':'watt'}[kind]},
                 casts_shadow=True,render_policy={'strict':True,'allow_approximations':False,
                                                 'required_features':[],'fallback':'reject'})
    if kind in ('point','spot'):value['radius_meters']=0
    if kind == 'spot':value.update(spot_inner_radians=.5,spot_outer_radians=1.)
    if kind == 'area':value['area_size_meters']=[2,3]
    value.update(updates)
    return value


def plan(value=None, **kwargs):
    value = value or source()
    registry = Registry([('light.json',canonical_bytes(value))])
    return light_plan(registry,registry.reference('key','1.0.0'),**kwargs)


class LightAdmission(unittest.TestCase):
    def test_point_and_sun_photometry(self):
        self.assertAlmostEqual(plan()['energy'],4*math.pi)
        self.assertAlmostEqual(plan(source('directional'))['energy'],1.)
        self.assertEqual(plan(source('area'))['energy'],683.)
        self.assertAlmostEqual(plan(source('spot'))['energy'],4*math.pi)

    def test_linear_color_preserves_photometric_luminance(self):
        actual=plan(source(color={'encoding':'srgb','value':[.3,.7,.1]}))
        self.assertAlmostEqual(luminance(actual['linear_color']),1.)
        self.assertGreater(actual['linear_color'][1],actual['linear_color'][0])

    def test_temperature_and_tint_are_explicit(self):
        warm=temperature_color(2000);cool=temperature_color(12000)
        self.assertGreater(warm[0]/max(warm[2],1e-10),cool[0]/cool[2])
        self.assertEqual(max(warm),1.)
        actual=plan(source(temperature_kelvin=2000),options={'temperature_mode':'ignore'})
        self.assertEqual(actual['linear_color'],[1.,1.,1.])
        with self.assertRaises(RigError):temperature_color(float('nan'))
        with self.assertRaises(RigError):temperature_color(40001)

    def test_disabled_and_black_lights_are_dark(self):
        self.assertEqual(plan(options={'enabled':False})['energy'],0)
        self.assertEqual(plan(source(color={'encoding':'linear-srgb','value':[0,0,0]}))['linear_color'],[0,0,0])

    def test_malformed_options(self):
        for value in ({'enabled':1},{'receivers':[['x']]},{'receivers':['x','x']},
                      {'receivers':['x'],'excluded_receivers':['x']},{'unknown':0},
                      {'area_shape':'DISK'},{'camera_visible':False},{'exposure_stops':1},
                      {'diffuse':'yes'}):
            with self.subTest(value=value),self.assertRaises(RigError):plan(options=value)

    def test_eevee_linking_is_explicitly_rejected(self):
        with self.assertRaises(RigError):plan(engine='BLENDER_EEVEE_NEXT',options={'receivers':['actor']})

    def test_eevee_cannot_claim_required_light_linking(self):
        value = source()
        value['render_policy']['required_features'] = ['light-linking']
        with self.assertRaises(RigError):
            plan(value, engine='BLENDER_EEVEE_NEXT')
        self.assertIsInstance(plan(value, engine='CYCLES'), dict)

    def test_rehashed_incomplete_reference_rejected_before_execution(self):
        from lookdev_contracts import content_hash
        for updates in ({'path': '../source.json'}, {'sha256': 'bad'},
                        {'sha256': 'A' * 64}):
            actual = plan()
            actual['reference'].update(updates)
            actual['sha256'] = content_hash({k: v for k, v in actual.items() if k != 'sha256'})
            with self.subTest(updates=updates), self.assertRaises(RigError):
                _validated(actual)
        actual = plan()
        del actual['reference']['sha256']
        actual['sha256'] = content_hash({k: v for k, v in actual.items() if k != 'sha256'})
        with self.assertRaises(RigError):
            _validated(actual)

    def test_source_is_not_mutated(self):
        value=source(); saved=copy.deepcopy(value)
        actual=plan(value);actual['light']['color']['value'][0]=0
        self.assertEqual(value,saved)

    def test_hash_binding(self):
        actual=plan();_validated(actual)
        actual['energy']*=2
        with self.assertRaises(RigError):_validated(actual)

    def test_rehashed_derived_values_cannot_bypass_source_validation(self):
        from lookdev_contracts import content_hash
        for field, value in (('energy', 2.), ('linear_color', [1., 0., 0.]),
                             ('units', 'unrelated'), ('reference', {'id': 'wrong'})):
            actual = plan()
            actual[field] = value
            actual['sha256'] = content_hash({k: v for k, v in actual.items() if k != 'sha256'})
            with self.subTest(field=field), self.assertRaises(RigError):
                _validated(actual)

    def test_required_features_are_not_invented(self):
        value=source();value['render_policy']['required_features']=['spectral-renderer']
        with self.assertRaises(RigError):plan(value)

    def test_sixteen_presets_are_real_light_documents(self):
        self.assertEqual(len(preset_names()),16)
        for name in preset_names():
            result=lighting_preset(name)
            self.assertTrue(result['lights']);self.assertFalse(result['material_mutations'])
            self.assertEqual(result,lighting_preset(name))
            for value in result['lights']:
                registry=Registry([('light.json',canonical_bytes(value))])
                light_plan(registry,registry.reference(value['id'],value['version']))


class NativeLightRendering(unittest.TestCase):
    @unittest.skipUnless(__import__('os').environ.get('HATCH_LOOKDEV_LIGHTING_TEST') == '1',
                         'Explicit actual Blender lighting gate is not enabled.')
    def test_real_lighting_pixels(self):
        import json
        import os
        import shutil
        import subprocess
        import tempfile
        executable=os.environ.get('HATCH_BLENDER') or shutil.which('blender')
        self.assertTrue(executable, 'The enabled native lighting gate requires Blender 4.5 LTS.')
        environment = dict(os.environ)
        if os.environ.get('HATCH_BLENDER_PYTHONPATH'):
            environment['PYTHONPATH'] = os.environ['HATCH_BLENDER_PYTHONPATH']
        with tempfile.TemporaryDirectory(prefix='hatch-lighting-test-') as temporary:
            destination=Path(temporary)/'evidence'
            process=subprocess.run([executable,'--background','--factory-startup','--disable-autoexec','--python-use-system-env',
                                    '--threads','2','--python-exit-code','1','--python',
                                    str(Path(__file__).resolve().parents[1]/'scripts/verify_lookdev_lighting.py'),
                                    '--',str(destination)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                   timeout=300,text=True,check=False,env=environment)
            self.assertEqual(process.returncode,0,process.stdout[-8000:])
            result=json.loads((destination/'report.json').read_text())
            self.assertGreater(len(result['frames']),10)
            self.assertFalse(result['visual_approval'])


if __name__ == '__main__':
    unittest.main()
