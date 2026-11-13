"""Real gas acceptance remains opt-in because Blender/OpenVDB are optional."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as core
from physics_gas import GasBackend, DEFAULT_SETTINGS, validate_settings, verify_cache
from physics_gas_example import build_gas_inputs


class GasAdmissionTests(unittest.TestCase):
    def test_configuration_does_not_claim_native_execution(self):
        world,payloads,settings=build_gas_inputs()
        report=GasBackend('/missing','/missing').validate_scene(world,settings)
        self.assertFalse(report['solver_executed'])
        self.assertEqual(report['category'],'gas')

    def test_unsupported_native_mapping_fails_before_any_launch(self):
        mutations=[lambda w:w['solver'].update(substeps=2),
            lambda w:w['components'][0].update(resolution=[4096]*3),
            lambda w:w['components'][0].update(viscosity=.1),
            lambda w:w['components'][0].update(size=[4,3,4]),
            lambda w:w['bodies'][0]['colliders'][0].update(sensor=True),
            lambda w:w['bodies'][0]['transform']['translation'].__setitem__(0,float('nan'))]
        for mutate in mutations:
            world,_,settings=build_gas_inputs();mutate(world)
            with self.subTest(mutate=mutate),self.assertRaises(core.BackendError):
                GasBackend('/missing','/missing').validate_scene(world,settings)

    def test_strict_settings_reject_malformed_unbounded_and_unknown_values(self):
        for key,value in [('render_frames',[[1]]),('render_frames',[True]),('render_size',True),
            ('fuel',float('nan')),('vorticity',5),('new_field',1),('maximum_substeps',0)]:
            settings=deepcopy(DEFAULT_SETTINGS);settings[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(core.BackendError):validate_settings(settings,24)

    def test_gas_budgets_admit_estimates_before_native_discovery(self):
        world,_,settings=build_gas_inputs()
        world['budgets']['memory_bytes']=1024
        with self.assertRaises(core.BackendError) as error:GasBackend('/missing','/missing').estimate_resources(world,settings)
        self.assertEqual(error.exception.report['code'],'PHYSICS_RESOURCE_LIMIT')


@unittest.skipUnless(os.environ.get('HATCH_GAS_TEST')=='1','Explicit real gas/OpenVDB acceptance gate')
class GasNativeTests(unittest.TestCase):
    def test_real_smoke_and_fire_cache_fields_and_relocation(self):
        target=os.environ.get('HATCH_GAS_EVIDENCE')
        with tempfile.TemporaryDirectory(prefix='hatch-gas-tests-') as temporary:
            root=Path(target) if target else Path(temporary)
            root.mkdir(parents=True,exist_ok=True)
            for fire in (False,True):
                backend=GasBackend()
                world,payloads,settings=build_gas_inputs(fire=fire)
                prepared=backend.prepare(world,payloads,settings=settings)
                try:
                    result=backend.simulate(prepared)
                    destination=root/('fire' if fire else 'smoke')
                    report=backend.bake(result,destination)
                    self.assertEqual(report['sample_count'],24)
                    fields=core.parse((destination/'volume-fields.json').read_bytes())['samples']
                    self.assertGreater(max(s['fields']['density']['maximum'] for s in fields),0)
                    if fire:self.assertGreater(max(s['fields']['flame']['maximum'] for s in fields),0)
                    density=[s['fields']['density']['total'] for s in fields]
                    self.assertGreater(max(density)-min(density),.01)
                    self.assertLess(density[-1],max(density))
                    import shutil
                    restored=root/('fire-restored' if fire else 'smoke-restored')
                    shutil.copytree(destination,restored)
                    self.assertEqual(verify_cache(restored),report)
                    missing=next((restored/'cache/data').glob('*.vdb'))
                    missing.unlink()
                    with self.assertRaises(core.BackendError):verify_cache(restored)
                finally:backend.cleanup(prepared)
