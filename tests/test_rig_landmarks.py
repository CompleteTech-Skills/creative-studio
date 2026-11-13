"""Admission and persistence regressions for authored landmark fitting (#52)."""
from copy import deepcopy
from pathlib import Path
import tempfile
import json
import subprocess
import sys
import unittest

import numpy as np
from rig_test_helpers import fixture
from rig_demo import make_geometry
from rig_errors import RigError, read_json, sha256
from rig_models import load_rig
from rig_operations import mutate
from rig_skeleton import validate_skeleton
from rig_templates import generate_skeleton, load_rig_template, MAX_TEMPLATE_DEPTH


class LandmarkAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geometry, cls.template, cls.landmarks, _ = make_geometry('robotic')
        cls.name = next(iter(cls.landmarks))

    def fit(self, landmarks):
        return generate_skeleton(self.geometry, self.template, landmarks=landmarks)

    def test_authored_landmarks_keep_exact_endpoints_and_evidence(self):
        before = deepcopy(self.landmarks)
        result = self.fit(self.landmarks)
        validate_skeleton(result['skeleton'])
        for bone in result['skeleton']['bones']:
            source = self.landmarks[bone['id']]
            np.testing.assert_allclose(bone['head'], source['head'], atol=1e-12)
            np.testing.assert_allclose(bone['tail'], source['tail'], atol=1e-12)
            self.assertEqual(bone['evidence']['reference'], source['reference'])
        self.assertEqual(result['status'], 'skeleton-generated')
        self.assertEqual(self.landmarks, before)

    def test_malformed_landmarks_fail_with_typed_diagnostics(self):
        good = self.landmarks[self.name]
        values = [[], False, '', {self.name: None}, {self.name: {'approved': True}},
                  {self.name: {**good, 'head': [0, 0]}},
                  {self.name: {**good, 'tail': [0, '1', 0]}},
                  {self.name: {**good, 'head': [False, 0, 0]}},
                  {self.name: {**good, 'approved': 1}},
                  {self.name: {**good, 'unknown': 'field'}},
                  {'missing-bone': good}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(RigError):
                self.fit(value)

    def test_invalid_evidence_and_nonfinite_geometry_never_return_ready(self):
        good = self.landmarks[self.name]
        patches = [{'source': 'invented-authority'}, {'confidence': 'high'},
                   {'confidence': True}, {'confidence': -0.1}, {'confidence': 1.1},
                   {'confidence': float('inf')}, {'reference': ''}, {'reference': []},
                   {'head': [float('nan'), 0, 0]}, {'tail': [float('inf'), 0, 0]},
                   {'head': [10**309, 0, 0]}, {'tail': [-(10**400), 0, 0]},
                   {'tail': good['head']}]
        for patch in patches:
            with self.subTest(patch=patch), self.assertRaises(RigError):
                self.fit({self.name: {**good, **patch}})

    def test_low_confidence_and_geometric_sources_remain_uncertain(self):
        result = self.fit({self.name: {**self.landmarks[self.name], 'confidence': .5}})
        self.assertIn(self.name, [u['element'] for u in result['uncertainties']])
        result = self.fit({self.name: {**self.landmarks[self.name], 'source': 'geometry-fit'}})
        self.assertIn(self.name, [u['element'] for u in result['uncertainties']])
        self.assertEqual(result['status'], 'needs-correction')

    def test_dispatcher_rejection_preserves_active_rig_revisions_and_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            rig = fixture(directory, animated=False)
            before = (rig.root/'rig.json').read_bytes()
            source_hashes = {p: sha256(p) for p in (rig.root/'source').rglob('*') if p.is_file()}
            revisions = {p: sha256(p) for p in (rig.root/'rig/revisions').glob('*')}
            good = deepcopy(self.landmarks[self.name])
            for payload in ({}, [], False, None,
                            {'source_sha256': '0'*64, 'landmarks': {}},
                            {'source_sha256': rig.manifest['source']['sha256'], 'landmarks': None},
                            {'source_sha256': rig.manifest['source']['sha256'],
                             'landmarks': {self.name: {**good, 'confidence': 'high'}}}):
                with self.subTest(payload=payload), self.assertRaises(RigError):
                    mutate(rig.root, 'auto', {'replace': True, 'template': 'robotic', 'landmarks': payload})
                self.assertEqual((rig.root/'rig.json').read_bytes(), before)
                self.assertEqual({p: sha256(p) for p in (rig.root/'rig/revisions').glob('*')}, revisions)
                self.assertEqual({p: sha256(p) for p in (rig.root/'source').rglob('*') if p.is_file()}, source_hashes)
                self.assertEqual(read_json(rig.root/'job.json')['steps']['auto']['status'], 'failed')
                load_rig(rig.root)

    def test_cli_rejects_explicit_null_without_discarding_skin_or_animation(self):
        with tempfile.TemporaryDirectory() as directory:
            rig=fixture(directory)
            before=(rig.root/'rig.json').read_bytes()
            revisions={p:sha256(p) for p in (rig.root/'rig/revisions').glob('*')}
            sources={p:sha256(p) for p in (rig.root/'source').rglob('*') if p.is_file()}
            path=Path(directory)/'landmarks.json';path.write_text('null')
            command=[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/rig.py'),
                'auto','--run-dir',str(rig.root),'--template','robotic','--replace']
            result=subprocess.run([*command,'--landmarks',str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            self.assertEqual(json.loads(result.stdout)['error']['code'],'RIG_LANDMARK_SOURCE')
            self.assertNotIn('Traceback',result.stderr)
            self.assertEqual((rig.root/'rig.json').read_bytes(),before)
            self.assertEqual({p:sha256(p) for p in (rig.root/'rig/revisions').glob('*')},revisions)
            self.assertEqual({p:sha256(p) for p in (rig.root/'source').rglob('*') if p.is_file()},sources)
            loaded=load_rig(rig.root)
            self.assertTrue(loaded.weights);self.assertTrue(loaded.clips)
            # Omitting the flag still permits the explicitly requested refit.
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            validate_skeleton(load_rig(rig.root).skeleton)

    def test_dispatcher_accepts_hash_bound_landmarks_and_can_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            rig = fixture(directory, animated=False)
            payload = {'source_sha256': rig.manifest['source']['sha256'], 'landmarks': self.landmarks}
            result = mutate(rig.root, 'auto', {'replace': True, 'template': 'robotic', 'landmarks': payload})
            self.assertEqual(result['status'], 'succeeded')
            loaded = load_rig(rig.root)
            validate_skeleton(loaded.skeleton)
            self.assertEqual(loaded.skeleton['bones'][0]['evidence']['source'], 'procedural-fixture')


class TemplateAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geometry, _, _, _ = make_geometry('robotic')
        cls.base_template = load_rig_template('robotic')

    def write(self, directory, value):
        (Path(directory)/(value['id']+'.json')).write_text(json.dumps(value))

    def test_custom_template_dictionary_has_the_same_strict_admission_as_file(self):
        for update in ({'unknown': 1}, {'bones': None}, {'rest_bounds': [[0, 0, 0]]},
                       {'extends': ['robotic']*9}):
            value = deepcopy(self.base_template);value.update(update)
            with self.subTest(update=update), self.assertRaises(RigError):
                generate_skeleton(self.geometry, value)

    def test_preserved_existing_skeleton_is_validated_before_returning_ready(self):
        from rig_skeleton import skeleton_from_heads
        valid = skeleton_from_heads([dict(id='root', role='root', parent=None,
            head=[0, 0, 0], tail=[0, 1, 0], deform=True)])
        invalid = deepcopy(valid);invalid['bones'][0]['rest']['rotation']=[0, 0, 0, 0]
        with self.assertRaises(RigError):
            generate_skeleton(self.geometry, self.base_template, existing=invalid)
        result = generate_skeleton(self.geometry, self.base_template, existing=valid)
        self.assertEqual(result['skeleton'], valid)
        self.assertIsNot(result['skeleton'], valid)

    def test_composed_templates_cannot_exceed_the_published_bone_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = deepcopy(self.base_template['bones'][0])
            for part in ('a', 'b'):
                bones = []
                for index in range(129):
                    bone=deepcopy(base);bone.update(id=f'{part}{index}', role=f'{part}{index}',
                                                  parent=None, mirror=None)
                    bones.append(bone)
                self.write(directory, self.template_value(part, bones=bones))
            self.write(directory, self.template_value('child', extends=['a', 'b']))
            with self.assertRaises(RigError) as caught:
                load_rig_template('child', directory=Path(directory))
            self.assertEqual(caught.exception.report['code'], 'RIG_SCHEMA')

    def template_value(self, identifier, **kwargs):
        value=deepcopy(self.base_template)
        value.update(id=identifier, extends=list(kwargs.get('extends', ())),
                     bones=list(kwargs.get('bones', ())), constraints=[], ik=[],
                     sockets=[], capabilities=[])
        return value

    def test_deep_inheritance_fails_before_python_recursion_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            for index in range(MAX_TEMPLATE_DEPTH+1):
                parents=[f'n{index+1}'] if index < MAX_TEMPLATE_DEPTH else []
                self.write(directory, self.template_value(f'n{index}', extends=parents))
            with self.assertRaises(RigError) as caught:
                load_rig_template('n0', directory=Path(directory))
            self.assertEqual(caught.exception.report['code'], 'RIG_TEMPLATE_DEPTH')

    def test_repeated_acyclic_inheritance_has_a_total_work_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            for level in range(9):
                parents=[] if level == 0 else [f'a{level-1}', f'b{level-1}']
                for prefix in ('a', 'b'):
                    self.write(directory, self.template_value(f'{prefix}{level}', extends=parents))
            with self.assertRaises(RigError) as caught:
                load_rig_template('a8', directory=Path(directory))
            self.assertEqual(caught.exception.report['code'], 'RIG_TEMPLATE_WORK')
