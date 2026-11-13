"""Behavioral tests for frozen style mappings and semantic identity constraints."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_contracts import Registry,canonical_bytes,content_hash
from lookdev_styles import (mapping_ids, resolve_mapping,validate_mapping,material_resource,
    bind_identity,validate_identity_material,validate_binding,look_state,shot_state)
from rig_errors import RigError


class StyleTests(unittest.TestCase):
    def test_required_twelve_styles_resolve_to_physical_materials(self):
        self.assertEqual(len(mapping_ids()),12)
        for name in mapping_ids():
            mapping=resolve_mapping(name)
            resource=material_resource(mapping)
            self.assertEqual(resource['id'],name)
            self.assertEqual(mapping['source_style']['resolved_id'],name)
            self.assertEqual(validate_mapping(mapping),mapping)
            self.assertEqual(resource['surface']['base_color'],[.45,.45,.45,1.])
        self.assertNotEqual(material_resource(resolve_mapping('clay'))['surface']['roughness'],
                            material_resource(resolve_mapping('porcelain'))['surface']['roughness'])
        self.assertEqual(resolve_mapping('low-poly-3d')['recipe']['geometry'],'flat')
        self.assertEqual(material_resource(resolve_mapping('frosted-glass'))['surface']['transmission'],1)

    def test_frozen_mapping_independent_of_current_recipe_updates(self):
        import lookdev_styles
        frozen=resolve_mapping('clay');original=deepcopy(frozen)
        old=lookdev_styles._RECIPES['clay']['roughness']
        try:
            lookdev_styles._RECIPES['clay']['roughness']=.1
            self.assertEqual(validate_mapping(frozen),original)
            self.assertNotEqual(resolve_mapping('clay')['sha256'],frozen['sha256'])
        finally:lookdev_styles._RECIPES['clay']['roughness']=old

    def test_mapping_rejects_tampering_unsupported_styles_and_fields(self):
        with self.assertRaises(RigError):resolve_mapping('pixel')
        for field,value in [('rendering_mode','unlit'),('mapping_version','2.0.0'),('schema_version',True)]:
            mapping=resolve_mapping('clay');mapping[field]=value
            mapping['sha256']=content_hash({k:v for k,v in mapping.items() if k!='sha256'})
            with self.assertRaises(RigError):validate_mapping(mapping)
        m=resolve_mapping('clay');m['recipe']['roughness']=-1
        with self.assertRaises(RigError):material_resource(m)

    def test_identity_protects_palette_but_accepts_allowed_surface_changes(self):
        base=material_resource(resolve_mapping('clay'),color=[.1,.2,.3,1.]);base['id']='base'
        changed=deepcopy(base);changed['id']='changed';changed['surface']['roughness']=.5
        bad=deepcopy(changed);bad['id']='bad';bad['surface']['base_color']=[1.,0.,0.,1.]
        registry=Registry([(r['id']+'.json',canonical_bytes(r)) for r in (base,changed,bad)])
        binding=bind_identity(registry,character={'id':'character','version':'1.0.0','sha256':'a'*64},
            identity_sha256='b'*64,slots={'body':{'material':registry.reference('base','1.0.0'),
            'targets':[{'object_id':'body-mesh','slot_id':'body'}],'protected_fields':['base_color']}})
        copy=deepcopy(binding)
        self.assertEqual(validate_identity_material(binding,'body',registry,registry.reference('changed','1.0.0'))['surface']['roughness'],.5)
        with self.assertRaises(RigError):validate_identity_material(binding,'body',registry,registry.reference('bad','1.0.0'))
        self.assertEqual(binding,copy)
        binding['slots']['body']['protected']['base_color']=[1,0,0,1]
        binding['sha256']=content_hash({k:v for k,v in binding.items() if k!='sha256'})
        with self.assertRaises(RigError):validate_identity_material(binding,'body',registry,registry.reference('bad','1.0.0'))

    def test_optional_surface_protection_roundtrips_through_authoritative_schema(self):
        resource=material_resource(resolve_mapping('clay'));resource['surface']['subsurface']={
            'weight':.5,'radius_meters':[.01,.01,.01],'color':[.2,.3,.4],'method':'random-walk'}
        registry=Registry([('clay.json',canonical_bytes(resource))])
        reference=registry.reference('clay','1.0.0')
        binding=bind_identity(registry,character={'id':'x','version':'1.0.0','sha256':'a'*64},
            identity_sha256='b'*64,slots={'skin':{'material':reference,
                'targets':[{'object_id':'mesh','slot_id':'skin'}],'protected_fields':['subsurface']}})
        self.assertEqual(validate_binding(binding),binding)
        for path,value in [(('character','version'),'invalid-version'),
                           (('slots','skin','material','version'),['1.0.0']),
                           (('slots','skin','material','path'),'../outside.json'),
                           (('slots','skin','material','kind'),'light3d')]:
            altered=deepcopy(binding);target=altered
            for part in path[:-1]:target=target[part]
            target[path[-1]]=value
            altered['sha256']=content_hash({k:v for k,v in altered.items() if k!='sha256'})
            with self.subTest(path=path),self.assertRaises(RigError):validate_binding(altered)
        self.assertEqual(validate_identity_material(binding,'skin',registry,reference)['surface']['subsurface']['weight'],.5)

    def test_slot_ownership_is_unique_and_bounded(self):
        resource=material_resource(resolve_mapping('clay'));registry=Registry([('clay.json',canonical_bytes(resource))])
        slot={'material':registry.reference('clay','1.0.0'),'targets':[{'object_id':'mesh','slot_id':'body'}],'protected_fields':['base_color']}
        with self.assertRaises(RigError):bind_identity(registry,character={'id':'x','version':'1.0.0','sha256':'a'*64},identity_sha256='b'*64,slots={'a':slot,'b':deepcopy(slot)})

    def test_shots_are_isolated_and_locks_reject_changes(self):
        base=look_state(materials={'body':{'version':'1.0.0'}},lighting={'key':3.},environment={'rotation':0.},color={'exposure':0.})
        copy=deepcopy(base)
        first=shot_state(base,{'color':{'exposure':1.}})
        second=shot_state(base,{})
        self.assertEqual(base,copy);self.assertEqual(first['color']['exposure'],1.)
        self.assertEqual(second['color']['exposure'],0.)
        with self.assertRaises(RigError):shot_state(base,{'materials':{'body':{'version':'2.0.0'}}},locks=['materials.body'])
        with self.assertRaises(RigError):shot_state(base,{'color':{'new':123}})
