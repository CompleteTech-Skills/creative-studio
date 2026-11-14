"""Paired object-local contacts against actual native skeleton sockets."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import numpy as np

from rig_attachments import attach_prop, prop_world, socket_world, transfer_prop
from rig_demo import create_demo
from rig_errors import RigError
from rig_math import axis_angle, compose, decompose
from rig_skeleton import neutral_pose


class PairedGripTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.source, _ = create_demo(Path(cls.temporary.name)/'source', 'humanoid', animate=False)
        cls.target = deepcopy(cls.source)
        cls.target.manifest['character']['id'] = 'receiver'
        cls.pose = neutral_pose(cls.source.skeleton)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def state(self):
        # Distinct native hands hold differently oriented points on one rotated,
        # translated prop. No artificial coincident-socket substitution.
        world = compose([.12, .8, -.3], axis_angle([0, 1, 0], .4))
        source_hand = socket_world(self.source, self.pose, 'grip.left')
        target_hand = socket_world(self.target, self.pose, 'grip.right')
        prop = dict(id='key', owner=self.source.manifest['character']['id'], socket='grip.left',
                    transform=decompose(np.linalg.inv(source_hand) @ world), provenance={'original': 'immutable'})
        return prop, world, decompose(np.linalg.inv(world) @ source_hand), decompose(np.linalg.inv(world) @ target_hand)

    def transfer(self, prop, source_grip, target_grip, **kwargs):
        return transfer_prop(self.source, self.pose, self.target, self.pose, prop, 'grip.right',
                             source_grip=source_grip, target_grip=target_grip, **kwargs)

    def test_separated_native_hands_transfer_one_owner_preserving_full_world_pose(self):
        prop, world, source_grip, target_grip = self.state()
        original = deepcopy(prop)
        self.assertGreater(np.linalg.norm(socket_world(self.source, self.pose, 'grip.left')[:3, 3]
                                          - socket_world(self.target, self.pose, 'grip.right')[:3, 3]), .1)
        moved = self.transfer(prop, source_grip, target_grip)
        self.assertEqual(moved['owner'], 'receiver')
        self.assertEqual(moved['socket'], 'grip.right')
        self.assertEqual(prop, original)
        self.assertEqual(moved['provenance'], prop['provenance'])
        np.testing.assert_allclose(prop_world(self.target, self.pose, moved), world, atol=1e-12, rtol=0)
        with self.assertRaises(RigError):
            self.transfer(moved, source_grip, target_grip)

    def test_legacy_coincident_contact_succeeds_and_separated_hands_still_fail(self):
        hand = socket_world(self.source, self.pose, 'grip.right')
        free = dict(id='key', owner=None, socket=None, transform=decompose(hand))
        prop = attach_prop(self.source, self.pose, free, 'grip.right')
        moved = transfer_prop(self.source, self.pose, self.target, self.pose, prop, 'grip.right')
        np.testing.assert_allclose(prop_world(self.target, self.pose, moved), hand, atol=1e-12, rtol=0)
        prop, _, _, _ = self.state()
        with self.assertRaises(RigError) as failure:
            transfer_prop(self.source, self.pose, self.target, self.pose, prop, 'grip.right')
        self.assertEqual(failure.exception.report['code'], 'RIG_HANDOFF_CONTACT')

    def test_each_grip_is_checked_independently_at_original_centimeter_tolerance(self):
        prop, _, source_grip, target_grip = self.state()
        for side in ('source', 'target'):
            with self.subTest(side=side):
                grips = [deepcopy(source_grip), deepcopy(target_grip)]
                grips[0 if side == 'source' else 1]['translation'][0] += .011
                with self.assertRaises(RigError) as failure:
                    self.transfer(prop, *grips)
                self.assertEqual(failure.exception.report['code'], 'RIG_HANDOFF_CONTACT')
                self.assertIn(side, str(failure.exception))

    def test_own_grip_orientation_cannot_be_wrong_even_when_position_meets(self):
        prop, _, source_grip, target_grip = self.state()
        for index in (0, 1):
            with self.subTest(index=index):
                grips = [deepcopy(source_grip), deepcopy(target_grip)]
                grips[index]['rotation'] = axis_angle([1, 0, 0], 1.1).tolist()
                with self.assertRaises(RigError) as failure:
                    self.transfer(prop, *grips)
                self.assertEqual(failure.exception.report['code'], 'RIG_HANDOFF_CONTACT')
                self.assertLess(failure.exception.report['observed']['position_m'], 1e-12)

    def test_partial_pairs_and_malformed_nonfinite_singular_grips_fail_typed(self):
        prop, _, source_grip, target_grip = self.state()
        cases = [(source_grip, None), (None, target_grip), ({}, target_grip), ([], target_grip)]
        for field, value in [('translation', [float('nan'), 0, 0]), ('rotation', [0, 0, 0, 0]),
                             ('rotation', [0, 0, 0, 2]), ('scale', [1, 0, 1]),
                             ('translation', [True, 0, 0]), ('translation', ['1', 0, 0]),
                             ('translation', [10**400, 0, 0]),
                             ('translation', ['not-a-coordinate', 0, 0])]:
            invalid = deepcopy(source_grip)
            invalid[field] = value
            cases.append((invalid, target_grip))
        for index, grips in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(RigError):
                self.transfer(prop, *grips)

    def test_paired_tolerances_are_finite_nonnegative_numbers(self):
        prop, _, source_grip, target_grip = self.state()
        for name in ('tolerance', 'orientation_tolerance'):
            for value in (-1, float('nan'), float('inf'), True, '0.01', 10**400):
                with self.subTest(name=name, value=value), self.assertRaises(RigError) as failure:
                    self.transfer(prop, source_grip, target_grip, **{name: value})
                self.assertEqual(failure.exception.report['code'], 'RIG_HANDOFF_TOLERANCE')


if __name__ == '__main__':
    unittest.main()
