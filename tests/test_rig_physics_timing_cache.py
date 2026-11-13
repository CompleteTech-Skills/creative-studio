"""Native cache admission requires the optional 3D geometry dependencies."""
from fractions import Fraction
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import physics_backends as physics


class NativeTimingCacheTests(unittest.TestCase):
    def test_retained_predecessor_caches_remain_hash_verified(self):
        root=Path(__file__).resolve().parents[1]/'tests/results/physics-backends-20261024'
        for backend in ['blender','pybullet']:
            report=physics.verify_cache(root/(backend+'-demo/cache'))
            self.assertEqual(report['status'],'hash-and-contract-verified')

    def test_rebound_solver_cache_cannot_disagree_with_output_and_unknown_worker_cannot_downgrade(self):
        source=Path(__file__).resolve().parents[1]/'tests/results/physics-timing-20261024/executed/artifacts/30000-1001-0'
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)/'cache';shutil.copytree(source,cache)
            value=physics.parse((cache/'solver-states.json').read_bytes())
            value['samples'][16]['bodies'][0]['translation'][1]+=10
            (cache/'solver-states.json').write_bytes(physics.canonical(value))
            manifest=physics.parse((cache/'cache.json').read_bytes())
            for item in manifest['files']:
                data=(cache/item['path']).read_bytes();item.update(bytes=len(data),sha256=physics.digest(data))
            manifest['usage']['output_bytes']=sum(item['bytes'] for item in manifest['files'])
            (cache/'cache.json').write_bytes(physics.canonical(manifest))
            with self.assertRaises(physics.BackendError) as raised:
                physics._validate_solver_samples(value,physics.parse((cache/'world.json').read_bytes()),manifest['world_sha256'],manifest['fixture'],physics.parse((cache/'states.json').read_bytes()))
            self.assertIn('disagrees',str(raised.exception))
            for name in ['timing.json','solver-states.json']:(cache/name).unlink()
            manifest['files']=[item for item in manifest['files'] if item['path'] not in ['timing.json','solver-states.json']]
            manifest['usage']['output_bytes']=sum(item['bytes'] for item in manifest['files'])
            (cache/'cache.json').write_bytes(physics.canonical(manifest))
            with self.assertRaises(physics.BackendError) as raised:physics.verify_cache(cache)
            self.assertIn('timing receipt',str(raised.exception))

    def test_blender_rejects_injected_solver_cache_for_verify_and_seek(self):
        source=Path(__file__).resolve().parents[1]/'tests/results/physics-backends-20261024/blender-demo/cache'
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)/'cache';shutil.copytree(source,cache)
            data=physics.canonical(dict(kind='fabricated-adversarial-solver-cache',samples=[]))
            (cache/'solver-states.json').write_bytes(data)
            manifest=physics.parse((cache/'cache.json').read_bytes())
            manifest['files'].append(dict(path='solver-states.json',bytes=len(data),sha256=physics.digest(data)))
            manifest['usage']['output_bytes']=sum(item['bytes'] for item in manifest['files'])
            (cache/'cache.json').write_bytes(physics.canonical(manifest))
            for operation in [lambda:physics.verify_cache(cache),lambda:physics.inspect_time(cache,Fraction(1,24))]:
                with self.assertRaises(physics.BackendError) as raised:operation()
                self.assertIn('unsupported',str(raised.exception))

