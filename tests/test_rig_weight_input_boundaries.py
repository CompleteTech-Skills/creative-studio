"""Manual weight operations reject malformed controls without changing the source."""
from copy import deepcopy
from fractions import Fraction
import math
import unittest

import numpy as np

from rig_errors import RigError
from rig_math import axis_angle
from rig_skinning import copy_weights, edit_weights, evaluate_deformation, smooth_weights, validate_skin
from rig_skeleton import neutral_pose, rotate_bone
from test_rig_skinning import bound_fixture


class RigWeightInputBoundaryTests(unittest.TestCase):
    def assert_skin_unchanged(self, actual, before):
        for name in ('indices', 'weights', 'inverse_bind', 'locked'):
            np.testing.assert_array_equal(getattr(actual, name), getattr(before, name))
        self.assertEqual(actual.joints, before.joints)
        self.assertEqual(actual.method, before.method)

    def reject_without_mutation(self, operation, code):
        rig = bound_fixture()
        skin = rig.weights['strip']
        before = deepcopy(skin)
        try:
            with self.assertRaises(RigError) as caught:
                operation(rig, skin)
            self.assertEqual(caught.exception.report['code'], code)
        finally:
            self.assert_skin_unchanged(skin, before)

    def test_edit_influence_limit_is_integer_within_storage_width(self):
        for limit in (True, False, np.bool_(True), np.bool_(False), 1.5, '4', None, -1, 0, 5, 8):
            with self.subTest(limit=limit):
                self.reject_without_mutation(
                    lambda rig, skin: edit_weights(skin, {0: {'b1': 1}}, max_influences=limit),
                    'RIG_WEIGHT_EDIT')

    def test_smoothing_requires_bounded_integer_iterations(self):
        for count in (True, False, np.bool_(True), np.bool_(False), 1.5, '2', None, -1, 65, float('nan'), float('inf')):
            with self.subTest(iterations=count):
                self.reject_without_mutation(
                    lambda rig, skin: smooth_weights(rig.geometry.meshes[0], skin, iterations=count),
                    'RIG_WEIGHT_SMOOTH')

    def test_smoothing_requires_finite_numeric_strength(self):
        for strength in (True, False, np.bool_(True), np.bool_(False), '0.3', None, -.1, 1.1, 10**100, -(10**100), float('nan'), float('inf')):
            with self.subTest(strength=strength):
                self.reject_without_mutation(
                    lambda rig, skin: smooth_weights(rig.geometry.meshes[0], skin, strength=strength),
                    'RIG_WEIGHT_SMOOTH')

    def test_copy_requires_integer_source_and_target_indices(self):
        for vertex in (True, False, np.bool_(True), np.bool_(False), 1.5, '1', None, -1, 4):
            for source in (True, False):
                with self.subTest(vertex=vertex, source=source):
                    pairs = {0: vertex} if source else {vertex: 0}
                    self.reject_without_mutation(lambda rig, skin: copy_weights(skin, pairs),
                                                 'RIG_VERTEX_REFERENCE')

    def test_rejection_after_valid_assignment_preserves_source(self):
        self.reject_without_mutation(
            lambda rig, skin: edit_weights(skin, {0: {'b1': 1}, 4: {'b0': 1}}),
            'RIG_VERTEX_REFERENCE')
        self.reject_without_mutation(
            lambda rig, skin: copy_weights(skin, {0: 2, 1: 4}),
            'RIG_VERTEX_REFERENCE')

    def test_valid_operations_normalize_and_preserve_locked_rows(self):
        rig = bound_fixture()
        original = deepcopy(rig.weights['strip'])
        edited = edit_weights(original, {0: {'b0': 2, 'b1': 6}}, max_influences=2)
        copied = copy_weights(edited, {1: 0})
        smoothed = smooth_weights(rig.geometry.meshes[0], copied, iterations=64, strength=1)
        for skin in (edited, copied, smoothed):
            validate_skin(skin, rig.geometry.meshes[0], rig.skeleton)
            np.testing.assert_allclose(skin.weights.sum(axis=1), 1)
        self.assertEqual(smoothed.locked.tolist(), [0, 1])
        np.testing.assert_array_equal(smoothed.indices[:2], copied.indices[:2])
        np.testing.assert_array_equal(smoothed.weights[:2], copied.weights[:2])
        np.testing.assert_allclose(sorted(edited.weights[0]), [0, 0, .25, .75])
        self.assert_skin_unchanged(rig.weights['strip'], original)

    def test_valid_identity_operations_preserve_actual_deformation(self):
        rig = bound_fixture()
        pose = rotate_bone(rig.skeleton, neutral_pose(rig.skeleton), 'b1',
                           axis_angle([0, 0, 1], math.pi / 2))
        expected = evaluate_deformation(rig, pose)['strip']['positions']
        self.assertGreater(np.linalg.norm(expected - rig.geometry.meshes[0].positions), 1)
        for operation in (lambda skin: edit_weights(skin, {0: {'b0': 3}}, max_influences=1),
                          lambda skin: copy_weights(skin, {1: 0}),
                          lambda skin: smooth_weights(rig.geometry.meshes[0], skin, iterations=0),
                          lambda skin: smooth_weights(rig.geometry.meshes[0], skin, strength=0)):
            with self.subTest(operation=operation):
                clone = deepcopy(rig)
                clone.weights['strip'] = operation(clone.weights['strip'])
                np.testing.assert_allclose(evaluate_deformation(clone, pose)['strip']['positions'], expected)

    def test_numpy_numeric_controls_match_builtin_controls(self):
        rig = bound_fixture()
        skin = rig.weights['strip']
        mesh = rig.geometry.meshes[0]
        for actual, expected in (
            (lambda: edit_weights(skin, {0: {'b1': 1}}, max_influences=np.int64(2)),
             lambda: edit_weights(skin, {0: {'b1': 1}}, max_influences=2)),
            (lambda: copy_weights(skin, {0: np.int64(2)}),
             lambda: copy_weights(skin, {0: 2})),
            (lambda: smooth_weights(mesh, skin, iterations=np.int64(2)),
             lambda: smooth_weights(mesh, skin, iterations=2)),
            (lambda: smooth_weights(mesh, skin, strength=Fraction(1, 4)),
             lambda: smooth_weights(mesh, skin, strength=.25)),
            (lambda: smooth_weights(mesh, skin, strength=np.float32(.3)),
             lambda: smooth_weights(mesh, skin, strength=float(np.float32(.3))))):
            with self.subTest(operation=actual):
                self.assert_skin_unchanged(actual(), expected())
