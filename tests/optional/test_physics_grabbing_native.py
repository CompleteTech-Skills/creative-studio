"""Explicit native rig-socket release gate; excluded from ordinary2D discovery."""
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from physics_backends import PyBulletBackend, canonical, digest, parse
from physics_grabbing_example import build_release_inputs
from physics_rigid_render import render_direct_cache
from rig_demo import create_demo
from rig_errors import sha256
from rig_models import load_rig
from rig_skeleton import neutral_pose


@unittest.skipUnless(os.environ.get('HATCH_GRABBING_TEST') == '1', 'Explicit optional native grip gate required')
class NativeGrabReleaseTests(unittest.TestCase):
    def test_real_rig_socket_velocity_impulse_ballistics_contact_and_native_reopen(self):
        owned = os.environ.get('HATCH_GRABBING_EVIDENCE')
        context = tempfile.TemporaryDirectory(prefix='hatch-grab-') if not owned else None
        root = Path(owned if owned else context.name)
        root.mkdir(parents=True, exist_ok=True)
        try:
            create_demo(root/'character', 'humanoid', identifier='grip-character', animate=False)
            rig = load_rig(root/'character')
            original_rig_hash = sha256(root/'character'/'rig.json')
            observed = {}
            for name, impulse, spin in [('drop', [0, 0, 0], [0, 0, 0]), ('throw', [2, 0, 0], [0, 0, .1])]:
                world, payloads, preparation = build_release_inputs(rig, impulse=impulse, angular_impulse=spin)
                # Input-only carry is clearly separated from computed dynamics.
                self.assertIn('uncommitted', preparation['status'])
                for measured, expected in zip(preparation['release']['linear_velocity'], [1., 0., 0.]):
                    self.assertAlmostEqual(measured, expected, delta=1e-12)
                backend = PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
                prepared = backend.prepare(world, payloads)
                try:
                    result = backend.simulate(prepared)
                    backend.bake(result, root/name)
                finally:
                    backend.cleanup(prepared)
                verification = backend.verify(root/name)
                self.assertEqual(verification['status'], 'hash-and-contract-verified')
                states = parse((root/name/'states.json').read_bytes())['samples']
                first = next(b for b in states[0]['bodies'] if b['id'] == 'ball')
                quarter = next(b for b in states[6]['bodies'] if b['id'] == 'ball')
                last = next(b for b in states[-1]['bodies'] if b['id'] == 'ball')
                self.assertAlmostEqual(quarter['translation'][0]-first['translation'][0], (1+impulse[0])*.25, delta=.005)
                self.assertAlmostEqual(quarter['translation'][1], 3-9.81*.25**2/2, delta=.01)
                self.assertAlmostEqual(last['translation'][1], .5, delta=.03)
                contacts = parse((root/name/'contacts.json').read_bytes())
                self.assertTrue(contacts['samples'])
                self.assertTrue(any(sample['contacts'] for sample in contacts['samples']))
                observed[name] = dict(verification=verification, first=first, quarter=quarter, final=last,
                    release=preparation, actual_usage=result.usage,
                    states_sha256=sha256(root/name/'states.json'), cache_sha256=sha256(root/name/'cache.json'),
                    world_sha256=digest(canonical(world)))
            self.assertGreater(observed['throw']['quarter']['translation'][0], observed['drop']['quarter']['translation'][0]+.45)
            # The native angular impulse must change the independently retained
            # orientation; it is never baked into an authored prop track.
            a = observed['drop']['quarter']['rotation'];b = observed['throw']['quarter']['rotation']
            spin_angle = 2*math.acos(min(1., abs(sum(x*y for x, y in zip(a, b)))))
            self.assertGreater(spin_angle, .1)
            observed['spin_difference_radians'] = spin_angle
            replay = render_direct_cache(root/'throw', root/'rendered-throw', os.environ['HATCH_BLENDER'])
            self.assertEqual(len(replay['reports'][0]['previews']), 3)
            observed['native_replay'] = replay
            self.assertEqual(sha256(root/'character'/'rig.json'), original_rig_hash)
            observed.update(rig_sha256=original_rig_hash, fixture=True,
                artistic_review='not-performed', ownership='not-committed; continuity118 unavailable',
                fixed_spring_attachments='unsupported; joint-owner event integration required',
                scope='actual weighted rig socket kinematics; native dynamic release, impulse, gravity, contacts and Blender reopened replay')
            (root/'acceptance.json').write_bytes(canonical(observed))
        finally:
            if context:context.cleanup()


if __name__ == '__main__':unittest.main()
