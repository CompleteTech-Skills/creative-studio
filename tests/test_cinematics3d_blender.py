"""Pure compiler/protocol tests; synthetic inventories do not prove a renderer."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cinematics3d_blender import compile_plan, validate_bound_plan, validate_configuration, validate_probe
from cinematics3d_blender_runtime import validate_plan
from cinematics3d_contracts import default_effect, Registry, canonical_bytes
from rig_errors import RigError
from test_render3d_contracts import request
from test_rig_cinematics3d_contracts import doc, ref
from test_rig_camera3d_contracts import fixtures


def inventory():
    return dict(schema_version=1,kind='cinematics3d-blender-probe',application_version='Blender 4.5.3',
        engines=['CYCLES'],view_transforms=['Standard','AgX'],nodes=[],devices=[dict(id='CPU',name='CPU',type='CPU',initialized=False)],
        passes=dict(depth=True,normal=True,vector=True,cryptomatte=True,albedo=True),transparency=True,
        effects={name:dict(available=name!='motion-blur',method='synthetic unit fixture',pixel_verified=False)
                 for name in ('exposure','tone-map','depth-of-field','volumetric-fog','motion-blur')},rendered=False)


def bundle(effects, external=None,scope='shot'):
    stack=doc('stack','stack',scope=scope,parent=None,effects=[ref(e) for e in effects],bypass=[])
    registry=Registry([(v['id']+'.json',canonical_bytes(v)) for v in [*effects,stack]],external=external)
    return registry,registry.reference('stack','1.0.0')


class Compiler(unittest.TestCase):
    def test_compile_retains_original_bytes_without_mutation(self):
        e=default_effect('exposure','exposure');e['operation']['parameters']['stops']=2
        registry,stack=bundle([e]);req=request();original=deepcopy(req)
        compiled=compile_plan(registry,stack,req,inventory())
        self.assertEqual(req,original);self.assertEqual(compiled['request']['settings']['exposure'],2)
        self.assertFalse(compiled['executed']);validate_bound_plan(compiled['plan'],compiled['request'])
        validate_configuration(compiled['plan'],compiled['configuration'])
        self.assertEqual(compiled['configuration']['documents/exposure.json'],canonical_bytes(e))

    def test_native_effects_are_bound_to_request_and_frame_cache_settings(self):
        from rig_errors import object_hash
        req=request();base,stack=bundle([]);a=compile_plan(base,stack,req,inventory())
        e=default_effect('volumetric-fog','fog');changed,stack=bundle([e]);b=compile_plan(changed,stack,req,inventory())
        self.assertNotEqual(object_hash(a['request']),object_hash(b['request']))
        self.assertNotEqual(object_hash(a['request']['settings']),object_hash(b['request']['settings']))
        with self.assertRaises(RigError):validate_bound_plan(a['plan'],b['request'])
        mutated=deepcopy(a['plan']);mutated['fog']=dict(density_per_meter=.1,anisotropy=0)
        mutated['request_sha256']=object_hash(a['request'])
        with self.assertRaises(RigError):validate_bound_plan(mutated,a['request'])
        from render3d_blender import BlenderBackend
        from render3d_contracts import Render3DError
        backend=object.__new__(BlenderBackend)
        with self.assertRaises(Render3DError) as caught:
            backend.render_frames(a['request'],'unused-snapshot','unused-output')
        self.assertEqual(caught.exception.report['category'],'unsupported-feature')

    def test_stale_request_and_bytes_rejected(self):
        registry,stack=bundle([]);compiled=compile_plan(registry,stack,request(),inventory())
        changed=deepcopy(compiled['request']);changed['settings']['seed']+=1
        with self.assertRaises(RigError):validate_bound_plan(compiled['plan'],changed)
        files=dict(compiled['configuration']);files['documents/stack.json']+=b' '
        with self.assertRaises(RigError):validate_configuration(compiled['plan'],files)

    def test_unsupported_enabled_effect_rejected_and_disabled_preserved(self):
        e=default_effect('motion-blur','blur');registry,stack=bundle([e])
        with self.assertRaisesRegex(RigError,'mapping'):compile_plan(registry,stack,request(),inventory())
        e['enabled']=False;registry,stack=bundle([e]);compiled=compile_plan(registry,stack,request(),inventory())
        self.assertEqual(compiled['plan']['sources'],[])
        self.assertIn('documents/blur.json',compiled['configuration'])

    def test_missing_engine_transform_capability_and_transparency_rejected(self):
        e=default_effect('tone-map','tone');e['operation']['parameters']['view']='agx';registry,stack=bundle([e])
        for change in (lambda p:p.update(engines=[]),lambda p:p.update(view_transforms=['Standard']),
                       lambda p:p['effects']['tone-map'].update(available=False),lambda p:p.update(transparency=False)):
            probe=inventory();change(probe)
            with self.assertRaises(RigError):compile_plan(registry,stack,request(),probe)

    def test_device_and_object_scope_rejected(self):
        registry,stack=bundle([]);req=request();req['settings'].update(device_policy='gpu-required',device='gpu')
        with self.assertRaises(RigError):compile_plan(registry,stack,req,inventory())
        registry,stack=bundle([],scope='object')
        with self.assertRaises(RigError):compile_plan(registry,stack,request(),inventory())

    def test_dof_consumes_verified_native_optics(self):
        optics=fixtures()['optics'];optics['depth_of_field']['enabled']=True;payload=canonical_bytes(optics)
        e=default_effect('depth-of-field','dof');e['inputs']=[ref(optics)]
        registry,stack=bundle([e],{'optics.json':payload})
        compiled=compile_plan(registry,stack,request(),inventory(),input_bytes={'optics.json':payload})
        self.assertEqual(compiled['plan']['dof']['focal_length_mm'],50)
        self.assertEqual(compiled['plan']['dof']['sensor_fit'],'horizontal')
        for inputs in ({},{'optics.json':payload+b' '}):
            with self.assertRaises(RigError):compile_plan(registry,stack,request(),inventory(),input_bytes=inputs)

    def test_dof_invalid_blade_and_projection_rejected(self):
        for change in (lambda p:p['depth_of_field'].update(aperture_blades=2),lambda p:p.update(projection=dict(type='orthographic',orthographic_height_m=3,shift_x=0,shift_y=0,near_clip_m=.1,far_clip_m=100))):
            optics=fixtures()['optics'];change(optics);payload=canonical_bytes(optics)
            e=default_effect('depth-of-field','dof');e['inputs']=[ref(optics)];registry,stack=bundle([e],{'optics.json':payload})
            with self.assertRaises(RigError):compile_plan(registry,stack,request(),inventory(),input_bytes={'optics.json':payload})

    def test_probe_does_not_admit_pixel_claims(self):
        for update in (dict(rendered=True),dict(application_version='Blender 5.0'),dict(devices=[dict(id='CPU',name='CPU',type='CPU',initialized=True)])):
            probe=inventory();probe.update(update)
            with self.assertRaises(RigError):validate_probe(probe)
        probe=inventory();probe['effects']['exposure']['pixel_verified']=True
        with self.assertRaises(RigError):validate_probe(probe)

    def test_runtime_protocol_strictness(self):
        registry,stack=bundle([]);plan=compile_plan(registry,stack,request(),inventory())['plan']
        for change in (lambda p:p.update(schema_version=True),lambda p:p.update(hidden=1),
                       lambda p:p['effective_settings'].update(exposure=float('nan')),
                       lambda p:p['effective_settings'].update(exposure=10**1000)):
            changed=deepcopy(plan);change(changed)
            with self.assertRaises(ValueError):validate_plan(changed)

    def test_duplicate_native_controls_rejected(self):
        effects=[default_effect('volumetric-fog','fog-a'),default_effect('volumetric-fog','fog-b')]
        effects[1]['order']=1;registry,stack=bundle(effects)
        with self.assertRaises(RigError):compile_plan(registry,stack,request(),inventory())

if __name__=='__main__':unittest.main()
