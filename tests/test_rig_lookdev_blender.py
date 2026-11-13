"""Surface admission plus opt-in actual Cycles rendering/reopen tests."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from lookdev_contracts import Registry, canonical_bytes, content_hash
from lookdev_blender import _features, _validate_plan, gltf_material, material_plan
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]


def surface_resource(identifier='surface', **changes):
    result = json.loads((ROOT / 'examples/lookdev-contracts/ceramic.json').read_text())
    result['id'] = identifier
    result['surface'].update(changes)
    result['surface']['double_sided'] = True
    return result


def plan(value):
    registry = Registry([(value['id'] + '.json', canonical_bytes(value))])
    return material_plan(registry, registry.reference(value['id'], value['version']))


class BlenderMaterialAdmissionTests(unittest.TestCase):
    def test_plan_resolves_instance_without_mutating_or_aliasing_base(self):
        base = surface_resource()
        base_payload = canonical_bytes(base)
        registry = Registry([('surface.json', base_payload)])
        instance = {k: deepcopy(v) for k, v in base.items() if k not in
                    ('surface', 'texture_slots', 'variants', 'workflow')}
        instance.update(id='rough', kind='material-instance3d',
                        base_material=registry.reference('surface', '1.0.0'),
                        overrides={'roughness': 0.95})
        instance['dependencies'] = [instance['base_material']]
        registry = Registry([('surface.json', base_payload), ('rough.json', canonical_bytes(instance))])
        resolved = material_plan(registry, registry.reference('rough', '1.0.0'))
        self.assertEqual(resolved['surface']['roughness'], 0.95)
        self.assertEqual(registry.get(registry.reference('surface', '1.0.0'))['surface']['roughness'], 0.24)
        resolved['surface']['roughness'] = 0
        self.assertEqual(material_plan(registry, registry.reference('rough', '1.0.0'))['surface']['roughness'], 0.95)
        self.assertEqual(canonical_bytes(base), base_payload)

    def test_valid_plan_hash_and_factor_changes(self):
        first = plan(surface_resource(metallic=0, roughness=0.1))
        second = plan(surface_resource(metallic=1, roughness=0.9))
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(_validate_plan(first), first['surface'])
        self.assertEqual(first['sha256'], content_hash({k: v for k, v in first.items() if k != 'sha256'}))

    def test_unsupported_required_feature_cannot_be_accepted_as_flat_fallback(self):
        for permissive in (False, True):
            value = surface_resource()
            value['render_policy'].update(strict=not permissive, allow_approximations=permissive,
                                          fallback='explicit-approximation' if permissive else 'reject',
                                          required_features=['imaginary-volume'])
            with self.subTest(permissive=permissive), self.assertRaises(RigError):
                plan(value)

    def test_volume_is_rejected_before_shader_execution(self):
        value = surface_resource(volume={'thickness_meters': 0.2, 'attenuation_distance_meters': 1,
                                         'attenuation_color': [0.2, 0.3, 0.4]})
        with self.assertRaises(RigError):
            plan(value)

    def test_eevee_rejects_strict_physical_scattering_and_transmission(self):
        for changes in ({'transmission': 0.8}, {'subsurface': {'weight': 0.8,
                'radius_meters': [0.1, 0.2, 0.3], 'color': [0.2, 0.3, 0.4], 'method': 'random-walk'}}):
            resource = surface_resource(**changes)
            registry = Registry([('surface.json', canonical_bytes(resource))])
            with self.subTest(changes=changes), self.assertRaises(RigError):
                material_plan(registry, registry.reference('surface', '1.0.0'), engine='BLENDER_EEVEE_NEXT')

    def test_plan_tampering_is_detected(self):
        for key, value in [('roughness', 0.9), ('metallic', 0.5), ('base_color', [1, 1, 1, 1])]:
            altered = plan(surface_resource())
            altered['surface'][key] = value
            with self.subTest(key=key), self.assertRaises(RigError):
                _validate_plan(altered)

    def test_bad_physical_values_reject_even_with_recomputed_digest(self):
        for key, value in [('roughness', -1), ('metallic', True), ('ior', 0), ('opacity', 2),
                           ('base_color', [0, 0, 0]), ('double_sided', 1), ('alpha_mode', 'OPAQUE\n')]:
            altered = plan(surface_resource())
            altered['surface'][key] = value
            altered['sha256'] = content_hash({k: v for k, v in altered.items() if k != 'sha256'})
            with self.subTest(key=key), self.assertRaises(RigError):
                _validate_plan(altered)

    def test_engine_capabilities_match_admission(self):
        self.assertTrue({'subsurface', 'transmission', 'iridescence'} <= _features('CYCLES'))
        self.assertFalse({'subsurface', 'transmission', 'iridescence'} & _features('BLENDER_EEVEE_NEXT'))
        with self.assertRaises(RigError):
            _features('UNKNOWN')

    def test_subsurface_mixtures_reject_instead_of_changing_other_responses(self):
        for changes in ({'metallic': 0.5}, {'transmission': 0.5}):
            with self.subTest(changes=changes), self.assertRaises(RigError):
                plan(surface_resource(**changes, subsurface={'weight': 0.8,
                    'radius_meters': [0.1, 0.2, 0.3], 'color': [0.2, 0.3, 0.4], 'method': 'random-walk'}))

    def test_gltf_factors_and_extensions_preserve_supported_values(self):
        value = surface_resource(base_color=[0.2, 0.3, 0.4, 0.5], opacity=0.6,
            alpha_mode='MASK', alpha_cutoff=0.4, ior=1.7,
            specular={'strength': 0.6, 'color': [0.8, 0.7, 0.6]}, transmission=0.2,
            clearcoat={'weight': 0.7, 'roughness': 0.3},
            sheen={'weight': 0.5, 'color': [0.8, 0.4, 0.2], 'roughness': 0.4},
            emission={'color': [0.1, 0.2, 0.3], 'strength_watts_per_square_meter': 4})
        result = gltf_material(plan(value))
        self.assertEqual(result['pbrMetallicRoughness']['baseColorFactor'], [0.2, 0.3, 0.4, 0.3])
        self.assertEqual(result['alphaCutoff'], 0.4)
        extensions = result['extensions']
        self.assertEqual(extensions['KHR_materials_ior'], {'ior': 1.7})
        self.assertEqual(extensions['KHR_materials_transmission'], {'transmissionFactor': 0.2})
        self.assertEqual(extensions['KHR_materials_specular']['specularFactor'], 0.6)
        self.assertEqual(extensions['KHR_materials_sheen']['sheenColorFactor'], [0.4, 0.2, 0.1])
        self.assertEqual(extensions['KHR_materials_emissive_strength']['emissiveStrength'], 4)
        self.assertEqual(extensions['KHR_materials_clearcoat']['clearcoatFactor'], 0.7)
        with self.assertRaises(RigError):
            gltf_material(plan(surface_resource(subsurface={'weight': 0.8,
                'radius_meters': [0.1, 0.2, 0.3], 'color': [0.2, 0.3, 0.4], 'method': 'random-walk'})))

    def test_malformed_and_nonfinite_plans_fail(self):
        for value in (None, {}, [], {'adapter_version': '1.0.0'}):
            with self.subTest(value=value), self.assertRaises(RigError):
                _validate_plan(value)
        altered = plan(surface_resource())
        altered['surface']['roughness'] = float('nan')
        with self.assertRaises(RigError):
            _validate_plan(altered)


@unittest.skipUnless(os.environ.get('HATCH_LOOKDEV_BLENDER_TEST') == '1',
                     'Enable HATCH_LOOKDEV_BLENDER_TEST=1 for actual pinned Blender rendering.')
class ActualBlenderMaterialTests(unittest.TestCase):
    def test_actual_eevee_supported_surfaces_and_capabilities(self):
        from PIL import Image, ImageChops, ImageStat
        blender = os.environ.get('HATCH_BLENDER')
        self.assertTrue(blender and Path(blender).is_file())
        evidence = os.environ.get('HATCH_LOOKDEV_EVIDENCE')
        temporary = None if evidence else tempfile.TemporaryDirectory(prefix='lookdev-eevee-')
        if temporary:
            self.addCleanup(temporary.cleanup)
        target = Path(evidence) / 'eevee' if evidence else Path(temporary.name)
        target.mkdir(parents=True, exist_ok=True)
        self.assertFalse((target / 'job.json').exists(), 'Use a new evidence directory.')
        values = [surface_resource('glossy', roughness=0.08), surface_resource('rough', roughness=0.9)]
        def eevee_plan(value):
            registry = Registry([(value['id'] + '.json', canonical_bytes(value))])
            return material_plan(registry, registry.reference(value['id'], value['version']), engine='BLENDER_EEVEE_NEXT')
        job = {'scripts': str(ROOT / 'scripts'), 'destination': str(target),
               'engine': 'BLENDER_EEVEE_NEXT', 'plans': [eevee_plan(v) for v in values],
               'advanced': eevee_plan(surface_resource('advanced'))}
        job_path = target / 'job.json'
        job_path.write_text(json.dumps(job), encoding='utf-8')
        command = [blender, '--background', '--factory-startup', '--disable-autoexec', '--threads', '1',
                   '--python-exit-code', '1', '--python', str(ROOT / 'tests/fixtures/lookdev_blender_probe.py'),
                   '--', str(job_path), 'render']
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=240, check=False)
        (target / 'render.log').write_text(result.stdout, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout[-8000:])
        report = json.loads((target / 'report.json').read_text())
        self.assertEqual(report['engine'], 'BLENDER_EEVEE_NEXT')
        self.assertTrue(report['linked_instances_isolated'])
        self.assertFalse({'subsurface', 'transmission', 'iridescence'} & set(report['probe']['features']))
        a = Image.open(target / 'glossy.png').convert('RGB')
        b = Image.open(target / 'rough.png').convert('RGB')
        mean = sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
        self.assertGreater(mean, 0.5)
        (target / 'measured-comparisons.json').write_text(json.dumps({'glossy/rough': mean}), encoding='utf-8')

    def test_actual_cycles_material_changes_and_saved_scene_reopen(self):
        from PIL import Image, ImageChops, ImageStat
        blender = os.environ.get('HATCH_BLENDER')
        self.assertTrue(blender and Path(blender).is_file(), 'Set HATCH_BLENDER to Blender 4.5 LTS.')
        evidence = os.environ.get('HATCH_LOOKDEV_EVIDENCE')
        temporary = None if evidence else tempfile.TemporaryDirectory(prefix='lookdev-render-')
        if temporary:
            self.addCleanup(temporary.cleanup)
        target = Path(evidence or temporary.name)
        target.mkdir(parents=True, exist_ok=True)
        self.assertFalse((target / 'job.json').exists(), 'Use a new evidence directory.')
        values = [surface_resource('glossy', roughness=0.08),
                  surface_resource('rough', roughness=0.9),
                  surface_resource('metal', metallic=1, roughness=0.08),
                  surface_resource('sss-off', roughness=0.5, base_color=[0.7, 0.15, 0.06, 1]),
                  surface_resource('sss-on', roughness=0.5, base_color=[0.7, 0.15, 0.06, 1],
                      subsurface={'weight': 0.8, 'radius_meters': [0.5, 0.15, 0.08],
                                  'color': [0.7, 0.15, 0.06], 'method': 'random-walk'})]
        job = {'scripts': str(ROOT / 'scripts'), 'destination': str(target),
               'plans': [plan(value) for value in values],
               'advanced': plan(surface_resource('advanced',
                   specular={'strength': 0.6, 'color': [0.8, 0.6, 0.4]},
                   clearcoat={'weight': 0.7, 'roughness': 0.2},
                   sheen={'weight': 0.3, 'color': [0.4, 0.2, 0.1], 'roughness': 0.6},
                   emission={'color': [0.1, 0.4, 0.8], 'strength_watts_per_square_meter': 2.0},
                   transmission=0.25,
                   anisotropy={'strength': 0.4, 'direction': [1.0, 0.0], 'rotation_radians': 0.5}))}
        job_path = target / 'job.json'
        job_path.write_text(json.dumps(job), encoding='utf-8')
        fixture = ROOT / 'tests/fixtures/lookdev_blender_probe.py'
        for mode in ('render', 'reopen'):
            command = [blender, '--background', '--factory-startup', '--disable-autoexec', '--threads', '1',
                       '--python-exit-code', '1', '--python', str(fixture), '--', str(job_path), mode]
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=240, check=False)
            (target / (mode + '.log')).write_text(result.stdout, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout[-8000:])
        report = json.loads((target / 'report.json').read_text())
        reopened = json.loads((target / 'reopen.json').read_text())
        self.assertEqual(report['engine'], 'CYCLES')
        self.assertTrue(report['linked_instances_isolated'])
        self.assertTrue(report['blender'].startswith('4.5.'))
        self.assertEqual(report['samples'], 8)
        self.assertGreater(report['displacement']['maximum_vertex_z_change'], 0.01)
        self.assertTrue(report['displacement']['source_data_preserved'])
        self.assertEqual(reopened['material_count'], len(values) + 1)
        self.assertEqual(reopened['glossy_plan_sha256'], job['plans'][0]['sha256'])
        for identifier in ('glossy', 'rough', 'metal', 'sss-off', 'sss-on'):
            image = Image.open(target / (identifier + '.png')).convert('RGB')
            self.assertEqual(image.size, (128, 128))
            self.assertGreater(max(ImageStat.Stat(image).stddev), 5, identifier)
        differences = {}
        for first, second in [('glossy', 'rough'), ('glossy', 'metal'), ('sss-off', 'sss-on')]:
            a = Image.open(target / (first + '.png')).convert('RGB')
            b = Image.open(target / (second + '.png')).convert('RGB')
            mean = sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
            differences[first + '/' + second] = mean
            self.assertGreater(mean, 0.5, (first, second, mean))
        a = Image.open(target / 'glossy.png').convert('RGB')
        b = Image.open(target / 'reopened-glossy.png').convert('RGB')
        self.assertIsNone(ImageChops.difference(a, b).getbbox(), 'Saved/reopened scene changed actual pixels.')
        (target / 'measured-comparisons.json').write_text(json.dumps(differences, indent=2), encoding='utf-8')
