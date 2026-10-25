"""Admission and persistence regressions for authored landmark fitting (#52)."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import numpy as np
from rig_test_helpers import fixture
from rig_demo import make_geometry
from rig_errors import RigError, read_json, sha256
from rig_models import load_rig
from rig_operations import mutate
from rig_skeleton import validate_skeleton
from rig_templates import generate_skeleton


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
            for payload in ({}, [], False,
                            {'source_sha256': '0'*64, 'landmarks': {}},
                            {'source_sha256': rig.manifest['source']['sha256'],
                             'landmarks': {self.name: {**good, 'confidence': 'high'}}}):
                with self.subTest(payload=payload), self.assertRaises(RigError):
                    mutate(rig.root, 'auto', {'replace': True, 'template': 'robotic', 'landmarks': payload})
                self.assertEqual((rig.root/'rig.json').read_bytes(), before)
                self.assertEqual({p: sha256(p) for p in (rig.root/'rig/revisions').glob('*')}, revisions)
                self.assertEqual({p: sha256(p) for p in (rig.root/'source').rglob('*') if p.is_file()}, source_hashes)
                self.assertEqual(read_json(rig.root/'job.json')['steps']['auto']['status'], 'failed')
                load_rig(rig.root)

    def test_dispatcher_accepts_hash_bound_landmarks_and_can_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            rig = fixture(directory, animated=False)
            payload = {'source_sha256': rig.manifest['source']['sha256'], 'landmarks': self.landmarks}
            result = mutate(rig.root, 'auto', {'replace': True, 'template': 'robotic', 'landmarks': payload})
            self.assertEqual(result['status'], 'succeeded')
            loaded = load_rig(rig.root)
            validate_skeleton(loaded.skeleton)
            self.assertEqual(loaded.skeleton['bones'][0]['evidence']['source'], 'procedural-fixture')
