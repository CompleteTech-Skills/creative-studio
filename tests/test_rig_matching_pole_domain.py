"""Native FK-to-IK matching at the finite world-coordinate boundary."""
from copy import deepcopy
import json
import math
import unittest

import numpy as np

from rig_contracts import validate_contract
from rig_errors import RigError
from rig_ik import match_fk_to_ik, validate_chain
from rig_skeleton import evaluate_fk, neutral_pose, validate_skeleton
from rig_test_helpers import chain_skeleton


class MatchingPoleDomainTests(unittest.TestCase):
    def make_chain(self, origin_x):
        # Bounded local scales compose into a finite 2**1023 world scale.
        # Restore unit endpoint scale and keep all successive links measurable.
        powers = [24] * 42 + [15]
        start = len(powers)
        skeleton = chain_skeleton(2 * start + 3)
        validate_skeleton(skeleton)
        pose = neutral_pose(skeleton)
        for i, exponent in enumerate(powers):
            pose['bones'][f'b{i}'] = dict(scale=[2.**exponent, 2.**exponent, 1.],
                                           translation=[0., 0., 0.])
        translations = [[origin_x, 0., 0.], [.125, .25, 0.], [-.125, .75, 0.]]
        for i, translation in enumerate(translations, start):
            pose['bones'][f'b{i}'] = dict(translation=translation)
        for i, exponent in enumerate(reversed(powers), start + 3):
            pose['bones'][f'b{i}'] = dict(scale=[2.**-exponent, 2.**-exponent, 1.],
                                          translation=[0., 0., 1.])
        chain = dict(schema_version=1, id='arm', solver='fabrik', pole=[0., 0., 1.],
                     bones=[f'b{i}' for i in range(start, len(skeleton['bones']))],
                     tolerance=1e-6, iterations=32)
        validate_contract(pose, 'Pose')
        validate_chain(skeleton, chain)
        world = evaluate_fk(skeleton, pose)
        self.assertTrue(all(np.isfinite(matrix).all() for matrix in world.values()))
        points = [world[name][:3, 3] for name in chain['bones']]
        self.assertEqual(math.dist(points[0], points[-1]), 2.**1023)
        self.assertTrue(math.isfinite(math.fsum(math.dist(a, b) for a, b
                                               in zip(points, points[1:]))))
        np.testing.assert_array_equal(points[-1], [origin_x * 2.**1023, 2.**1023, 43.])
        return skeleton, pose, chain

    def test_representable_translated_matching_pole(self):
        skeleton, pose, chain = self.make_chain(.5)
        original = deepcopy((skeleton, pose, chain))
        with np.errstate(over='ignore', invalid='ignore'):
            matched = match_fk_to_ik(skeleton, pose, chain)
        json.dumps(matched, allow_nan=False)
        self.assertEqual(matched['pole'][0], 1.5 * 2.**1023)
        self.assertEqual(matched['orientation'], [0., 0., 0., 1.])
        self.assertEqual((skeleton, pose, chain), original)

    def test_unrepresentable_translated_matching_pole_rejects(self):
        skeleton, pose, chain = self.make_chain(1.5)
        original = deepcopy((skeleton, pose, chain))
        # The same finite pole displacement now puts its world X outside
        # binary64. No nonfinite result may leave the native matching owner.
        with np.errstate(over='ignore', invalid='ignore'), self.assertRaises(RigError) as failure:
            match_fk_to_ik(skeleton, pose, chain)
        self.assertTrue(failure.exception.report['code'].startswith('RIG_'))
        json.dumps(failure.exception.report, allow_nan=False)
        self.assertEqual((skeleton, pose, chain), original)


if __name__ == '__main__':
    unittest.main()
