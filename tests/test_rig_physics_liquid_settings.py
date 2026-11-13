from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as core
from physics_liquid_example import build_liquid_inputs
from physics_liquid_settings import admit,validate_settings,schemas


class LiquidSettingsTests(unittest.TestCase):
    def test_static_and_authored_obstacle_reuse_real_native_source_owner(self):
        for moving in [False,True]:
            world,payloads,settings=build_liquid_inputs(moving=moving)
            before=deepcopy(world);checked,options=admit(world,settings)
            self.assertEqual(world,before)
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder)
                for path,payload in payloads.items():core._write(root/path,payload)
                bundle,meshes,v,t=core._source_admission(root,checked,'project.json')
                self.assertEqual(len(meshes),7 if moving else 6)
                self.assertEqual(v,len(meshes)*8);self.assertEqual(t,len(meshes)*12)
                if moving:self.assertEqual(meshes['paddle']['motion']['keys'][0]['value'],[-.5,.45,0])
                self.assertEqual(set(bundle['inventory']),set(payloads))

    def test_render_timing_and_allocation_settings_fail_closed(self):
        _,_,base=build_liquid_inputs()
        for key,value in [('render_frames',[0,48]),('render_frames',[0,0]),('render_size',True),('mesh_scale',3),
            ('initial_velocity',[float('nan'),0,0]),('initial_velocity',[21,0,0]),('boundaries',{'top':True}),
            ('maximum_substeps',False),('flow_behavior','SCRIPT')]:
            with self.subTest(key=key,value=value),self.assertRaises((core.BackendError,ValueError)):
                validate_settings({**base,key:value},48)
        with self.assertRaises(core.BackendError):validate_settings({**base,'unrecognized':1},48)

    def test_unimplemented_physical_requests_are_not_ignored(self):
        world,_,settings=build_liquid_inputs()
        changes=[lambda w:w['components'][0].update(max_secondary_particles=1),
            lambda w:w['components'][0].update(density=500),lambda w:w['solver'].update(iterations=20),
            lambda w:w['components'][0].update(resolution=[24,12,24]),
            lambda w:w['components'][0].update(viscosity=2)]
        for change in changes:
            w=deepcopy(world);change(w)
            with self.assertRaises(core.BackendError):admit(w,settings)

    def test_generated_schema_has_exact_fields_and_matches_validated_settings(self):
        import jsonschema
        world,_,settings=build_liquid_inputs()
        schema=schemas()['physics-liquid-settings.schema.json']
        jsonschema.Draft202012Validator(schema).validate(settings)
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['required']),set(settings))
        self.assertEqual(admit(world,settings)[1],settings)
