"""Independent contract, exact-byte, inheritance and legacy-boundary checks."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from PIL import Image
from jsonschema import Draft202012Validator
from lookdev_contracts import (KINDS, Registry, canonical_bytes, check_compatibility,
                               definitions, schemas, validate_resource)
from rig_errors import RigError, decode_json

ROOT = Path(__file__).resolve().parents[1]


def common(kind, identifier):
    return dict(schema_version=1, kind=kind, id=identifier, version='1.0.0',
                dependencies=[], provenance=dict(origin='procedural-fixture',
                tool='lookdev-contract-tests', created_at='2026-10-23T00:00:00Z',
                source_hashes=[], attribution='Self-authored engineering fixture',
                license=dict(identifier='CC0-1.0', source='self-authored')),
                validation_status='unvalidated', capabilities=[])


def policy():
    return dict(strict=True, allow_approximations=False, required_features=['metallic-roughness'], fallback='reject')


def material():
    return dict(**common('material3d', 'ceramic'), workflow='metallic-roughness',
                surface=dict(base_color=[0.2, 0.4, 0.6, 1], base_color_space='linear-srgb',
                             metallic=0, roughness=0.24, ior=1.5, opacity=1,
                             alpha_mode='OPAQUE', alpha_cutoff=0.5, double_sided=False,
                             normal_orientation='opengl-positive-y'),
                texture_slots={}, variants={}, render_policy=policy())


def reference(value, path=None, payload=None):
    return dict(id=value['id'], version=value['version'], kind=value['kind'],
                path=path or value['id']+'.json',
                sha256=hashlib.sha256(payload or canonical_bytes(value)).hexdigest())


def image_bytes():
    stream = io.BytesIO()
    Image.new('RGBA', (2, 2), (128, 200, 40, 255)).save(stream, format='PNG')
    return stream.getvalue()


def texture():
    return dict(**common('texture3d', 'albedo'), file=dict(path='textures/albedo.png',
                sha256=hashlib.sha256(image_bytes()).hexdigest(), bytes=len(image_bytes())), role='base-color', encoding='srgb', width=2, height=2,
                channels=4, bit_depth=8, wrap='repeat', filter='linear', normal_orientation='not-applicable')


def light():
    return dict(**common('light3d', 'key'), type='point',
                transform=dict(translation_meters=[0, 2, 1], rotation_xyzw=[0, 0, 0, 1]),
                color=dict(value=[1, 0.9, 0.8], encoding='linear-srgb'),
                intensity=dict(value=100, unit='candela'), radius_meters=0.1,
                casts_shadow=True, render_policy=policy())


def graph():
    return dict(**common('material-graph3d', 'graph'), nodes=[
        dict(id='color', type='constant-color', inputs={}, value=dict(value=[0.2, 0.3, 0.4], encoding='linear-srgb')),
        dict(id='roughness', type='constant-float', inputs={}, value=0.4),
        dict(id='metallic', type='constant-float', inputs={}, value=0),
        dict(id='surface', type='principled-output', inputs=dict(
            base_color=dict(node='color', socket='color'),
            roughness=dict(node='roughness', socket='value'),
            metallic=dict(node='metallic', socket='value')))],
        output=dict(node='surface', socket='surface'), render_policy=policy())


def fixtures():
    base, tex, lamp, node_graph = material(), texture(), light(), graph()
    instance = dict(**common('material-instance3d', 'blue-coat'), base_material=reference(base),
                    overrides=dict(roughness=0.8), render_policy=policy())
    instance['dependencies'] = [instance['base_material']]
    variant = dict(**common('material-variant3d', 'wet-coat'), base_material=reference(instance),
                   overrides=dict(roughness=0.05), graph=reference(node_graph), render_policy=policy())
    variant['dependencies'] = [variant['base_material'], variant['graph']]
    rig = dict(**common('lighting-rig3d', 'studio'), lights=[reference(lamp)], render_policy=policy())
    rig['dependencies'] = rig['lights'][:]
    quality = dict(**common('render-quality3d', 'draft'), samples=16, max_bounces=4,
                   resolution=[640, 480], seed=7, denoise=False,
                   budgets=dict(memory_bytes=1024**3, texture_bytes=1024**2, timeout_seconds=30), render_policy=policy())
    look = dict(**common('look3d', 'ceramic-look'), materials=dict(body=reference(base)),
                lighting=reference(rig), quality=reference(quality),
                identity=dict(id='pip', version='1.0.0', sha256='b'*64),
                style=dict(id='clay', version='1.0.0', sha256='c'*64),
                color_management=dict(working_space='linear-srgb', output_encoding='srgb', exposure_stops=0,
                                      white_balance_kelvin=6500, tone_mapping='none'))
    look['dependencies'] = [reference(base), reference(rig), reference(quality)]
    return [base, instance, variant, tex, node_graph, lamp, rig, look, quality]


class LookdevContractTests(unittest.TestCase):
    def test_all_kinds_roundtrip_and_schema_runtime_source_parity(self):
        examples = fixtures()
        self.assertEqual(set(KINDS), {x['kind'] for x in examples})
        for value in examples:
            with self.subTest(kind=value['kind']):
                schema = schemas()['lookdev-'+value['kind']+'.schema.json']
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(value)
                self.assertEqual(validate_resource(decode_json(canonical_bytes(value))), value)
                self.assertEqual(json.loads((ROOT/'profiles'/('lookdev-'+value['kind']+'.schema.json')).read_text()), schema)
        registry = Registry([(x['id']+'.json', canonical_bytes(x)) for x in examples], payloads={'textures/albedo.png': image_bytes()})
        for value in examples:
            self.assertEqual(registry.get(reference(value)), value)

    def test_unknown_fields_versions_enums_bounds_and_nonfinite_rejected(self):
        mutations = [lambda x: x.update(schema_version=2), lambda x: x.update(extra=True),
                     lambda x: x.update(version='01.0.0'), lambda x: x.update(version='1.0.0\n'),
                     lambda x: x.update(kind=[]), lambda x: x.update(id='a\n'),
                     lambda x: x['surface'].update(metallic=1.1),
                     lambda x: x['surface'].update(roughness=-0.1),
                     lambda x: x['surface'].update(ior=0.1),
                     lambda x: x['surface'].update(base_color_space='srgb'),
                     lambda x: x['surface'].update(alpha_mode='HASHED'),
                     lambda x: x['surface'].update(roughness=float('nan')),
                     lambda x: x['surface'].update(roughness=float('inf')),
                     lambda x: x['surface'].update(roughness=True),
                     lambda x: x.update(tags=['x']*65)]
        for mutate in mutations:
            value = material(); mutate(value)
            with self.subTest(value=value):
                with self.assertRaises(RigError):
                    validate_resource(value)

    def test_duplicate_json_and_nonfinite_literals(self):
        for payload in (b'{"kind":"material3d","kind":"light3d"}', b'{"x":NaN}', b'{"x":1e999}'):
            with self.assertRaises(RigError):
                decode_json(payload)

    def test_paths_are_portable_and_registry_rejects_aliases(self):
        for path in ('../x', '/x', 'C:/x', 'a\\x', 'a//b', './a', 'a/../b', 'nul.json',
                     'a/COM1.png', 'a/b.', 'a/b ', 'a/\x00b', 'e\u0301.png', 'CONIN$.json', 'a/\x7fb', 'a/\ud800b'):
            value = texture(); value['file']['path'] = path
            with self.subTest(path=path), self.assertRaises(RigError):
                validate_resource(value)
        value = material()
        other = deepcopy(value); other['id'] = 'other'
        with self.assertRaises(RigError):
            Registry([('Material.json', canonical_bytes(value)), ('material.json', canonical_bytes(other))])

    def test_registry_checks_exact_bytes_not_reformatted_semantic_equivalence(self):
        base, instance = fixtures()[:2]
        source = json.dumps(base, indent=2).encode()
        with self.assertRaises(RigError):
            Registry([('ceramic.json', source), ('blue-coat.json', canonical_bytes(instance))])
        instance['base_material'] = reference(base, payload=source)
        instance['dependencies'] = [instance['base_material']]
        registry = Registry([('ceramic.json', source), ('blue-coat.json', canonical_bytes(instance))])
        self.assertEqual(registry.get(reference(base, payload=source)), base)

    def test_missing_dependency_wrong_hash_kind_version_path_and_duplicate_id(self):
        base, instance = fixtures()[:2]
        for field, invalid in [('sha256', '0'*64), ('version', '2.0.0'), ('path', 'wrong.json'), ('kind', 'texture3d')]:
            value = deepcopy(instance)
            value['base_material'][field] = invalid
            value['dependencies'] = [value['base_material']]
            with self.subTest(field=field), self.assertRaises(RigError):
                Registry([('ceramic.json', canonical_bytes(base)), ('blue-coat.json', canonical_bytes(value))])
        with self.assertRaises(RigError):
            Registry([('blue-coat.json', canonical_bytes(instance))])
        with self.assertRaises(RigError):
            Registry([('a.json', canonical_bytes(base)), ('b.json', canonical_bytes(base))])
        bad = deepcopy(instance); bad['dependencies'] = []
        with self.assertRaises(RigError):
            validate_resource(bad)

    def test_instances_and_variants_are_immutable_and_hash_effective_surface(self):
        examples = fixtures()
        registry = Registry([(x['id']+'.json', canonical_bytes(x)) for x in examples], payloads={'textures/albedo.png': image_bytes()})
        base, instance, variant = examples[:3]
        before = canonical_bytes(base)
        self.assertEqual(registry.effective_material(reference(instance))['surface']['roughness'], 0.8)
        effective = registry.effective_material(reference(variant))
        self.assertEqual(effective['surface']['roughness'], 0.05)
        self.assertEqual(effective['graph'], variant['graph'])
        effective['surface']['roughness'] = 0.99
        self.assertEqual(registry.effective_material(reference(variant))['surface']['roughness'], 0.05)
        self.assertEqual(canonical_bytes(registry.get(reference(base))), before)
        self.assertNotEqual(registry.effective_material(reference(instance))['sha256'], registry.effective_material(reference(variant))['sha256'])
        clone = registry.get(reference(base)); clone['surface']['roughness'] = 1
        self.assertEqual(registry.get(reference(base)), base)

    def test_override_rejects_unknown_and_wrong_types_and_effective_conflicts(self):
        for override in (dict(shininess=1), dict(roughness='wet'), dict(roughness=True), dict(base_color=[1, 0])):
            value = fixtures()[1]; value['overrides'] = override
            with self.subTest(override=override), self.assertRaises(RigError):
                validate_resource(value)
        base, instance = fixtures()[:2]
        instance['overrides'] = dict(opacity=0.2)
        with self.assertRaises(RigError):
            Registry([('ceramic.json', canonical_bytes(base)), ('blue-coat.json', canonical_bytes(instance))])

    def test_surface_physical_ranges_and_interactions(self):
        value = material(); value['surface'].update(alpha_mode='BLEND', opacity=0.3)
        validate_resource(value)
        value['surface']['alpha_mode'] = 'OPAQUE'
        with self.assertRaises(RigError): validate_resource(value)
        value = material(); value['surface']['iridescence'] = dict(weight=1, ior=1.3,
                         thickness_min_nanometers=400, thickness_max_nanometers=300)
        with self.assertRaises(RigError): validate_resource(value)

    def test_all_light_units_and_conditional_fields(self):
        conditional = {'point': dict(radius_meters=0.1), 'directional': {},
                       'spot': dict(radius_meters=0.1, spot_inner_radians=0.2, spot_outer_radians=0.3),
                       'area': dict(area_size_meters=[1, 2]), 'environment': dict(environment=reference(texture()))}
        units = dict(point='candela', directional='lux', spot='candela', area='watt', environment='watt-per-square-meter')
        for kind in conditional:
            value = light(); value.pop('radius_meters'); value.update(type=kind, **conditional[kind])
            value['intensity']['unit'] = units[kind]
            if kind == 'environment': value['dependencies'] = [value['environment']]
            with self.subTest(kind=kind):
                validate_resource(value)
                bad = deepcopy(value); bad['intensity']['unit'] = 'lux' if kind != 'directional' else 'watt'
                with self.assertRaises(RigError): validate_resource(bad)
                bad = deepcopy(value); bad['transform']['rotation_xyzw'] = [0, 0, 0, 0]
                with self.assertRaises(RigError): validate_resource(bad)
        value = light(); value.update(type='spot', spot_inner_radians=1, spot_outer_radians=0.2)
        with self.assertRaises(RigError): validate_resource(value)
        value = light(); value['area_size_meters'] = [1, 1]
        with self.assertRaises(RigError): validate_resource(value)

    def test_texture_color_and_data_encoding_normal_convention_and_budget(self):
        value = texture(); validate_resource(value)
        value['encoding'] = 'data'
        with self.assertRaises(RigError): validate_resource(value)
        value.update(role='normal', normal_orientation='opengl-positive-y'); validate_resource(value)
        value['encoding'] = 'srgb'
        with self.assertRaises(RigError): validate_resource(value)
        value.update(encoding='data', channels=1)
        with self.assertRaises(RigError): validate_resource(value)
        value = texture(); value.update(width=16384, height=16384, channels=4, bit_depth=32)
        with self.assertRaises(RigError): validate_resource(value)

    def test_typed_graph_checks_dangling_sockets_cycles_and_output(self):
        validate_resource(graph())
        mutations = [lambda x: x['nodes'].append(deepcopy(x['nodes'][0])),
                     lambda x: x['nodes'][3]['inputs']['base_color'].update(node='missing'),
                     lambda x: x['nodes'][3]['inputs']['base_color'].update(socket='value'),
                     lambda x: x['nodes'][3]['inputs']['roughness'].update(node='color', socket='color'),
                     lambda x: x['nodes'][3]['inputs'].pop('metallic'),
                     lambda x: x['output'].update(node='color', socket='color'),
                     lambda x: x['nodes'][1].update(value=dict(value=[1, 0, 0], encoding='srgb'))]
        for mutate in mutations:
            value = graph(); mutate(value)
            with self.assertRaises(RigError): validate_resource(value)
        value = graph()
        value['nodes'].append(dict(id='cycle', type='multiply-float', inputs=dict(
            a=dict(node='cycle', socket='value'), b=dict(node='roughness', socket='value'))))
        with self.assertRaisesRegex(RigError, 'Cyclic'): validate_resource(value)

    def test_capabilities_are_explicit_and_never_claim_execution(self):
        value = material()
        with self.assertRaises(RigError): check_compatibility(value, set())
        self.assertEqual(check_compatibility(value, {'metallic-roughness'}), dict(status='compatible', missing=[], executed=False))
        value['render_policy'].update(strict=False, allow_approximations=True, fallback='bake')
        self.assertEqual(check_compatibility(value, set())['status'], 'requires-explicit-fallback')
        self.assertFalse(check_compatibility(value, set())['executed'])
        value['render_policy']['strict'] = True
        with self.assertRaises(RigError): validate_resource(value)

    def test_texture_slot_roles_and_unsupported_slot_names_are_rejected(self):
        base, tex = material(), texture()
        tex_ref = reference(tex)
        base['dependencies'] = [tex_ref]
        base['surface']['normal'] = dict(texture=tex_ref, strength=1, orientation='opengl-positive-y')
        with self.assertRaisesRegex(RigError, 'Texture role'):
            Registry([('ceramic.json', canonical_bytes(base)), ('albedo.json', canonical_bytes(tex))],
                     payloads={'textures/albedo.png': image_bytes()})
        base = material()
        base['texture_slots']['unsupported'] = dict(texture=tex_ref, texcoord=0, channels='rgb',
                           scale=[1, 1, 1], offset=[0, 0, 0], rotation_radians=0, strength=1)
        base['dependencies'] = [tex_ref]
        with self.assertRaises(RigError): validate_resource(base)

    def test_real_texture_bytes_are_required_and_hash_bound(self):
        value = texture()
        entries = [('albedo.json', canonical_bytes(value))]
        with self.assertRaises(RigError): Registry(entries)
        with self.assertRaises(RigError): Registry(entries, payloads={'textures/albedo.png': b'wrong'})
        registry = Registry(entries, payloads={'textures/albedo.png': image_bytes()})
        self.assertEqual(registry.get(reference(value)), value)
        with Image.open(io.BytesIO(image_bytes())) as image:
            self.assertEqual(image.size, (2, 2))
            self.assertEqual(image.mode, 'RGBA')

    def test_explicit_defaults_and_valid_provenance_time(self):
        from lookdev_contracts import default_surface
        value = material(); value['surface'] = default_surface()
        validate_resource(value)
        first = default_surface(); first['base_color'][0] = 1
        self.assertEqual(default_surface()['base_color'][0], 0.8)
        value['provenance']['created_at'] = '2026-99-99T00:00:00Z'
        with self.assertRaises(RigError): validate_resource(value)

    def test_advanced_requirements_are_inferred_even_if_not_declared(self):
        value = material(); value['surface']['transmission'] = 0.5
        with self.assertRaises(RigError): check_compatibility(value, {'metallic-roughness'})
        self.assertEqual(check_compatibility(value, {'metallic-roughness', 'transmission'})['status'], 'compatible')
        base, instance = fixtures()[:2]
        instance['overrides'] = dict(transmission=0.2)
        registry = Registry([('ceramic.json', canonical_bytes(base)), ('blue-coat.json', canonical_bytes(instance))])
        with self.assertRaises(RigError): registry.check_material_compatibility(reference(instance), {'metallic-roughness'})
        self.assertEqual(registry.check_material_compatibility(reference(instance), {'metallic-roughness', 'transmission'})['status'], 'compatible')
        with self.assertRaises(RigError): check_compatibility(instance, {'metallic-roughness'})

    def test_review_graph_policy_and_node_capabilities_are_transitive(self):
        base, shader = material(), graph()
        shader['render_policy']['required_features'] = ['special-graph-backend']
        base['graph'] = reference(shader); base['dependencies'] = [base['graph']]
        registry = Registry([('ceramic.json', canonical_bytes(base)), ('graph.json', canonical_bytes(shader))])
        supported = {'metallic-roughness', 'material-graph'}
        with self.assertRaises(RigError): registry.check_material_compatibility(reference(base), supported)
        supported |= {'special-graph-backend', 'graph-constant-color', 'graph-constant-float', 'graph-principled-output'}
        self.assertEqual(registry.check_material_compatibility(reference(base), supported)['status'], 'compatible')
        with self.assertRaises(RigError): check_compatibility(base, supported)
        # A derived policy cannot drop a pinned base's explicit requirement.
        instance = fixtures()[1]; instance['base_material'] = reference(base); instance['dependencies'] = [reference(base)]
        base['render_policy']['required_features'].append('base-special-feature')
        instance['base_material'] = reference(base); instance['dependencies'] = [reference(base)]
        registry = Registry([('ceramic.json', canonical_bytes(base)), ('graph.json', canonical_bytes(shader)),
                             ('blue-coat.json', canonical_bytes(instance))])
        with self.assertRaises(RigError): registry.check_material_compatibility(reference(instance), supported)

    def test_review_slot_spelling_cannot_bypass_active_surface_capabilities(self):
        slots = dict(normal='normal', height='height', displacement='true-displacement',
                     occlusion='occlusion', emission='emission', opacity='opacity', transmission='transmission',
                     thickness='volume', clearcoat='clearcoat', clearcoat_roughness='clearcoat',
                     sheen_color='sheen', sheen_roughness='sheen', anisotropy='anisotropy', iridescence='iridescence')
        for slot, feature in slots.items():
            tex = texture(); tex['role'] = slot.replace('_', '-')
            tex['encoding'] = 'srgb' if slot in ('emission', 'sheen_color') else 'data'
            tex['normal_orientation'] = 'opengl-positive-y' if slot == 'normal' else 'not-applicable'
            base = material(); base['texture_slots'][slot] = dict(texture=reference(tex), texcoord=0,
                channels='rgb' if slot in ('normal', 'emission', 'sheen_color') else 'r',
                scale=[1, 1, 1], offset=[0, 0, 0], rotation_radians=0, strength=1)
            base['dependencies'] = [reference(tex)]
            registry = Registry([('ceramic.json', canonical_bytes(base)), ('albedo.json', canonical_bytes(tex))],
                                payloads={'textures/albedo.png': image_bytes()})
            with self.subTest(slot=slot):
                with self.assertRaises(RigError):
                    registry.check_material_compatibility(reference(base), {'metallic-roughness', 'texture-slots'})
                self.assertEqual(registry.check_material_compatibility(reference(base),
                    {'metallic-roughness', 'texture-slots', feature})['status'], 'compatible')

    def test_review_graph_fallback_cannot_weaken_strict_material(self):
        base, shader = material(), graph()
        shader['render_policy'].update(strict=False, allow_approximations=True, fallback='bake')
        base['graph'] = reference(shader); base['dependencies'] = [base['graph']]
        registry = Registry([('ceramic.json', canonical_bytes(base)), ('graph.json', canonical_bytes(shader))])
        with self.assertRaises(RigError):
            registry.check_material_compatibility(reference(base), {'metallic-roughness', 'material-graph'})
        base['render_policy'].update(strict=False, allow_approximations=True, fallback='bake')
        registry = Registry([('ceramic.json', canonical_bytes(base)), ('graph.json', canonical_bytes(shader))])
        result = registry.check_material_compatibility(reference(base), {'metallic-roughness', 'material-graph'})
        self.assertEqual(result['status'], 'requires-explicit-fallback')
        self.assertIn('graph-principled-output', result['missing'])
        self.assertFalse(result['executed'])

    def test_review_nondefault_specular_ior_volume_and_quality_require_capabilities(self):
        changes = [('specular', dict(strength=0.3, color=[1, 1, 1]), 'specular'),
                   ('ior', 1.8, 'ior'), ('volume', dict(thickness_meters=0.2,
                    attenuation_distance_meters=1, attenuation_color=[1, 1, 1]), 'volume')]
        for key, setting, feature in changes:
            base = material(); base['surface'][key] = setting
            with self.subTest(feature=feature), self.assertRaises(RigError):
                check_compatibility(base, {'metallic-roughness'})
            self.assertEqual(check_compatibility(base, {'metallic-roughness', feature})['status'], 'compatible')
        quality = fixtures()[-1]; quality['denoise'] = True
        with self.assertRaises(RigError): check_compatibility(quality, {'metallic-roughness'})
        self.assertEqual(check_compatibility(quality, {'metallic-roughness', 'render-quality', 'denoise', 'indirect-lighting'})['status'], 'compatible')
        with self.assertRaises(RigError): check_compatibility(fixtures()[6], {'metallic-roughness'})

    def test_review_graph_domains_require_explicit_clamp_and_texture_channels(self):
        shader = graph(); shader['nodes'][1]['value'] = -0.5
        with self.assertRaises(RigError): validate_resource(shader)
        shader['nodes'].append(dict(id='clamped', type='clamp-float', inputs=dict(value=dict(node='roughness', socket='value'))))
        shader['nodes'][3]['inputs']['roughness'] = dict(node='clamped', socket='value')
        validate_resource(shader)
        shader = graph(); shader['nodes'][1]['value'] = 2
        with self.assertRaises(RigError): validate_resource(shader)
        # Defined scalar selection has [0,1] normalized/clamped sampling semantics.
        tex = texture(); shader = graph()
        shader['nodes'][1] = dict(id='roughness', type='texture', inputs={}, texture=reference(tex), channels='r')
        shader['dependencies'] = [reference(tex)]
        Registry([('graph.json', canonical_bytes(shader)), ('albedo.json', canonical_bytes(tex))],
                 payloads={'textures/albedo.png': image_bytes()})
        shader['nodes'][1].pop('channels')
        with self.assertRaises(RigError): validate_resource(shader)

    def test_review_data_texture_color_coercion_and_scalar_rgba_slot_fail(self):
        tex = texture(); tex.update(role='normal', encoding='data', normal_orientation='opengl-positive-y')
        shader = graph(); shader['nodes'][0] = dict(id='color', type='texture', inputs={}, texture=reference(tex), channels='rgb')
        shader['dependencies'] = [reference(tex)]
        with self.assertRaises(RigError):
            Registry([('graph.json', canonical_bytes(shader)), ('albedo.json', canonical_bytes(tex))],
                     payloads={'textures/albedo.png': image_bytes()})
        tex = texture(); tex.update(role='roughness', encoding='data')
        base = material(); base['texture_slots']['roughness'] = dict(texture=reference(tex), texcoord=0,
                      channels='rgba', scale=[1, 1, 1], offset=[0, 0, 0], rotation_radians=0, strength=1)
        base['dependencies'] = [reference(tex)]
        with self.assertRaises(RigError): validate_resource(base)
        base['texture_slots']['roughness']['channels'] = 'r'
        Registry([('ceramic.json', canonical_bytes(base)), ('albedo.json', canonical_bytes(tex))],
                 payloads={'textures/albedo.png': image_bytes()})

    def test_review_schema_rejects_lexically_unsafe_paths_and_timestamps(self):
        schema = schemas()['lookdev-texture3d.schema.json']
        validator = Draft202012Validator(schema)
        paths = ['../x', '/x', 'a//b', 'a/../b', 'a/./b', 'a\\b', 'a/b.', 'a/b ', 'a/b\n',
                 'NUL.png', 'conIN$.json', 'CONOUT$', 'CoM¹.png', 'cOm².json', 'com³.txt',
                 'lPt¹.png', 'Lpt².json', 'lPT³.txt']
        for path in paths:
            value = texture(); value['file']['path'] = path
            with self.subTest(path=path):
                self.assertFalse(validator.is_valid(value))
                with self.assertRaises(RigError): validate_resource(value)
        value = texture(); value['provenance']['created_at'] += '\n'
        self.assertFalse(validator.is_valid(value))
        with self.assertRaises(RigError): validate_resource(value)

    def test_bounded_sources_and_resource_inventory(self):
        with self.assertRaises(RigError): Registry([('x.json', b'x'*(2*1024**2+1))])
        with self.assertRaises(RigError): Registry([('x.json', b'{}')]*1025)
        value = material(); value['dependencies'] = [reference(texture())]*257
        with self.assertRaises(RigError): validate_resource(value)

    def test_import_has_no_optional_dependency_side_effect(self):
        code = '''
import sys
class Block:
 def find_spec(self, fullname, *args):
  if fullname.split('.')[0] in {'numpy','PIL','jsonschema','pygltflib'}:
   raise RuntimeError('unexpected optional import: '+fullname)
sys.meta_path.insert(0, Block())
import lookdev_contracts
assert len(lookdev_contracts.schemas()) == 9
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT/'scripts', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_retained_real_file_fixture_and_evidence_are_reproducible(self):
        root = ROOT/'examples/lookdev-contracts'
        evidence = json.loads((root/'evidence.json').read_bytes())
        entries = [(ref['path'], (root/ref['path']).read_bytes()) for ref in evidence['resources']]
        registry = Registry(entries, payloads={'textures/albedo.png': (root/'textures/albedo.png').read_bytes()})
        for ref in evidence['resources']:
            self.assertEqual(registry.reference(ref['id'], ref['version']), ref)
        actual = registry.effective_material(registry.reference('wet-coat', '1.0.0'))
        self.assertEqual(actual, evidence['effective_material'])
        self.assertFalse(evidence['executed_shaders'])
        self.assertFalse(evidence['production_approval'])
        with Image.open(root/'textures/albedo.png') as image:
            self.assertEqual(image.size, (2, 2))
            image.load()

    def test_cli_reader_rejects_links_and_special_files(self):
        from lookdev_contracts import read_document
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); real = root/'material.json'
            real.write_bytes(canonical_bytes(material()))
            self.assertEqual(read_document(real), material())
            if hasattr(os, 'mkfifo'):
                pipe = root/'pipe.json'; os.mkfifo(pipe)
                with self.assertRaises(RigError): read_document(pipe)
            link = root/'link.json'
            link.symlink_to(real)
            with self.assertRaises(RigError): read_document(link)

    def test_central_schema_generator_keeps_model_and_lookdev_hooks(self):
        from generate_schemas import build
        from model3d_contracts import schemas as model_schemas
        generated = build()
        for name, expected in {**model_schemas(), **schemas()}.items():
            with self.subTest(schema=name):
                self.assertIn(ROOT/'profiles'/name, generated)
                self.assertEqual(generated[ROOT/'profiles'/name], expected)
                self.assertEqual(json.loads((ROOT/'profiles'/name).read_text(encoding='utf-8')), expected)

    def test_generated_schema_check_and_cli_validation(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/lookdev_contracts.py'), '--check'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'material.json'; path.write_bytes(canonical_bytes(material()))
            result = subprocess.run([sys.executable, str(ROOT/'scripts/lookdev_contracts.py'), '--validate', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'structurally-valid')
            self.assertFalse(json.loads(result.stdout)['executed'])


if __name__ == '__main__':
    unittest.main()
