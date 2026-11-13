"""Actual native liquid lifecycle; opt-in, no synthetic success evidence."""
from pathlib import Path
import os
import sys
import struct
import zlib
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as core
from physics_liquid import simulate,verify_cache
from physics_liquid_example import build_liquid_inputs
from physics_liquid_acceptance import evaluate_caches


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1','Native backend opt-in required')
class NativeLiquidTests(unittest.TestCase):
    @contextmanager
    def workspace(self,case):
        evidence=os.environ.get('HATCH_LIQUID_EVIDENCE')
        if evidence:
            parent=Path(evidence);parent.mkdir(parents=True,exist_ok=True)
            yield parent/case
        else:
            with tempfile.TemporaryDirectory() as name:yield Path(name)/case

    def test_real_bake_reopen_and_corruption(self):
        with self.workspace('pouring') as root:
            world,payloads,settings=build_liquid_inputs()
            original=dict(payloads)
            report=simulate(world,payloads,root,settings=settings)
            self.assertEqual(payloads,original)
            self.assertEqual(report['status'],'verified')
            self.assertTrue(report['geometry']['reopened_geometry_verified'])
            self.assertLessEqual(report['geometry']['maximum_native_vertex_error'],2e-5)
            self.assertEqual(len(report['geometry']['samples']),48)
            # Updating inventory hashes cannot conceal stale source/worker/replay bindings.
            manifest_path=root/'liquid-cache.json';manifest_bytes=manifest_path.read_bytes()
            mutations={'inputs/inlet.obj':b'v 99 99 99\n',
                'worker.py':b'# changed worker\n',
                'replay-report.json':core.canonical(dict(mode='reopen',blend_sha256='0'*64,frames=list(range(48))))}
            report_data=core.parse((root/'liquid-report.json').read_bytes())
            report_data['observed']['resolution']+=1
            mutations['liquid-report.json']=core.canonical(report_data)
            png=(root/'frame-0000.png').read_bytes()
            ihdr=struct.pack('>II',50000,50000)+png[24:29]
            mutations['frame-0000.png']=png[:16]+ihdr+struct.pack('>I',zlib.crc32(b'IHDR'+ihdr))+png[33:]
            for relative,replacement in mutations.items():
                path=root/relative;original_bytes=path.read_bytes()
                try:
                    path.write_bytes(replacement)
                    manifest=core.parse(manifest_bytes)
                    entry=next(x for x in manifest['files'] if x['path']==relative)
                    entry.update(bytes=len(replacement),sha256=core.digest(replacement))
                    manifest_path.write_bytes(core.canonical(manifest))
                    with self.assertRaises(core.BackendError):verify_cache(root)
                finally:
                    path.write_bytes(original_bytes);manifest_path.write_bytes(manifest_bytes)
            before=(root/'scene.blend').read_bytes()
            with self.assertRaises(core.BackendError):simulate(world,payloads,root,settings=settings)
            self.assertEqual((root/'scene.blend').read_bytes(),before)
            mesh=next((root/'cache'/'mesh').glob('*.bobj.gz'));original=mesh.read_bytes()
            try:
                mesh.write_bytes(b'corrupted')
                with self.assertRaises(core.BackendError):verify_cache(root)
            finally:mesh.write_bytes(original)

    def test_actual_viscosity_moving_obstacle_and_open_boundary(self):
        results={}
        caches={}
        with ExitStack() as stack:
            for case,options in [('baseline',{}),('viscous',dict(viscosity=.01)),('stationary',dict(stationary_paddle=True)),('moving',dict(moving=True)),('uncontained',dict(container=False))]:
                with self.subTest(case=case):
                    root=stack.enter_context(self.workspace(case))
                    world,payloads,settings=build_liquid_inputs(**options)
                    if case=='uncontained':settings['boundaries']['bottom']=False
                    report=simulate(world,payloads,root,settings=settings)
                    self.assertTrue(report['geometry']['reopened_geometry_verified'])
                    results[case]=[x['mesh_sha256'] for x in report['geometry']['samples']]
                    if case in ('baseline','viscous','uncontained'):caches[case]=root
            acceptance=evaluate_caches(caches)
            evidence=os.environ.get('HATCH_LIQUID_EVIDENCE')
            if evidence:
                # Keep the full diagnostics even if an actual physical check fails.
                (Path(evidence)/'surface-acceptance.json').write_bytes(core.canonical(acceptance))
            self.assertEqual(acceptance['assessment']['status'],'passed',acceptance['assessment'])
        self.assertNotEqual(results['stationary'],results['moving'])

    def test_missing_solver_never_publishes_success(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)/'liquid';world,payloads,settings=build_liquid_inputs()
            with self.assertRaises(core.BackendError):simulate(world,payloads,root,settings=settings,executable='/absent-blender')
            self.assertFalse(root.exists())


if __name__=='__main__':unittest.main()
