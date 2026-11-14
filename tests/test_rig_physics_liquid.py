"""Actual native liquid lifecycle; opt-in, no synthetic success evidence."""
from pathlib import Path
import os
import sys
import struct
import zlib
import tempfile
import unittest
from unittest.mock import patch
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
            world['fps']=dict(numerator=30000,denominator=1001)
            world['solver']['fixed_step']=dict(numerator=1001,denominator=30000)
            world['components'][0]['resolution']=[float(v) for v in world['components'][0]['resolution']]
            original_world=core.canonical(world)
            original=dict(payloads)
            import atomic_publish
            original_rename=atomic_publish.rename_no_replace_at
            changed=[]
            def rename(source_fd,source_name,destination_fd,destination_name):
                if destination_name==root.name:
                    info=os.fstat(source_fd)
                    stage=next(p for p in root.parent.iterdir() if
                        (p.stat().st_dev,p.stat().st_ino)==(info.st_dev,info.st_ino))
                    stage.rename(root.parent/(stage.name+'.owned'))
                    stage.mkdir();(stage/'foreign-marker').write_bytes(b'preserve replacement')
                    changed.append(stage)
                return original_rename(source_fd,source_name,destination_fd,destination_name)
            with patch.object(atomic_publish,'rename_no_replace_at',side_effect=rename):
                report=simulate(world,payloads,root,settings=settings)
            self.assertEqual(len(changed),1)
            self.assertEqual((changed[0]/'foreign-marker').read_bytes(),b'preserve replacement')
            self.assertFalse((root/'foreign-marker').exists())
            self.assertEqual(payloads,original)
            self.assertEqual(core.canonical(world),original_world)
            self.assertEqual((root/'world.json').read_bytes(),original_world)
            self.assertEqual(report['status'],'verified')
            self.assertTrue(report['geometry']['reopened_geometry_verified'])
            self.assertLessEqual(report['geometry']['maximum_native_vertex_error'],2e-5)
            self.assertEqual(len(report['geometry']['samples']),48)
            observed=core.parse((root/'liquid-report.json').read_bytes())['observed']
            self.assertEqual((observed['fps_numerator'],observed['fps_base']),(30000,1001))
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
            # Rebinding unsigned hashes cannot raise the world's declared storage limits.
            paths=['world.json','liquid-job.json','liquid-report.json','liquid-cache.json']
            pristine={name:(root/name).read_bytes() for name in paths}
            for field,manifest_only in [('cache_bytes',False),('temporary_bytes',False),('bake_file_bytes',False),
                ('cache_bytes',True),('temporary_bytes',True)]:
                with self.subTest(field=field,manifest_only=manifest_only):
                    try:
                        changed=core.parse(pristine['world.json']);changed['budgets'][field]=1
                        manifest=core.parse(pristine['liquid-cache.json'])
                        for _ in range(8):
                            world_data=core.canonical(changed);world_hash=core.digest(world_data)
                            job=core.parse(pristine['liquid-job.json']);job.update(world=changed,world_sha256=world_hash)
                            native_report=core.parse(pristine['liquid-report.json']);native_report['world_sha256']=world_hash
                            replacement={'world.json':world_data,'liquid-job.json':core.canonical(job),
                                'liquid-report.json':core.canonical(native_report)}
                            manifest['world_sha256']=world_hash
                            for entry in manifest['files']:
                                if entry['path'] in replacement:
                                    data=replacement[entry['path']];entry.update(bytes=len(data),sha256=core.digest(data))
                            capacity=sum(entry['bytes'] for entry in manifest['files'])
                            if not manifest_only or changed['budgets'][field]==capacity:break
                            changed['budgets'][field]=capacity
                        else:self.fail('Storage boundary did not stabilize.')
                        for name,data in replacement.items():(root/name).write_bytes(data)
                        (root/'liquid-cache.json').write_bytes(core.canonical(manifest))
                        with self.assertRaises(core.BackendError) as raised:verify_cache(root)
                        self.assertEqual(raised.exception.report['code'],'PHYSICS_CACHE_LIMIT')
                    finally:
                        for name,data in pristine.items():(root/name).write_bytes(data)

    def test_actual_viscosity_moving_obstacle_and_open_boundary(self):
        results={}
        caches={}
        with ExitStack() as stack:
            for case,options in [('baseline',{}),('viscous',dict(viscosity=.01)),('stationary',dict(stationary_paddle=True)),('moving',dict(moving=True)),('uncontained',dict(container=False)),('geometry',{})]:
                with self.subTest(case=case):
                    root=stack.enter_context(self.workspace(case))
                    world,payloads,settings=build_liquid_inputs(**options)
                    if case in ('baseline','viscous','uncontained'):settings['boundaries']['bottom']=False
                    if case=='geometry':settings['flow_behavior']='GEOMETRY'
                    report=simulate(world,payloads,root,settings=settings)
                    self.assertTrue(report['geometry']['reopened_geometry_verified'])
                    results[case]=[x['mesh_sha256'] for x in report['geometry']['samples']]
                    if case=='geometry':
                        retained=core.parse((root/'liquid-settings.json').read_bytes())
                        self.assertEqual(retained['flow_behavior'],'GEOMETRY')
                        self.assertTrue(any(x['vertex_count']>8 for x in report['geometry']['samples']))
                        self.assertGreater(len(set(results[case])),1)
                    if case in ('baseline','viscous','uncontained'):caches[case]=root
            acceptance=evaluate_caches(caches)
            evidence=os.environ.get('HATCH_LIQUID_EVIDENCE')
            if evidence:
                # Keep the full diagnostics even if an actual physical check fails.
                (Path(evidence)/'surface-acceptance.json').write_bytes(core.canonical(acceptance))
            self.assertEqual(acceptance['assessment']['status'],'passed',acceptance['assessment'])
            # A writer may change caller paths immediately after verification.
            # The assessor must consume the exact verified bytes for metrics and
            # identities, including the manifest, source/settings and native mesh.
            import physics_liquid_acceptance as assessor
            original_snapshot = assessor._verify_cache_snapshot
            originals = {}
            def replace_after_snapshot(directory):
                report, payloads = original_snapshot(directory)
                with self.assertRaises(TypeError):
                    payloads['world.json'] = b'changed'
                if Path(directory) == caches['baseline']:
                    for name in ('world.json', 'liquid-settings.json', 'worker.py',
                                 'liquid-cache.json', 'cache/mesh/fluid_mesh_0025.bobj.gz'):
                        path = Path(directory)/name
                        originals[path] = path.read_bytes()
                        path.write_bytes(b'unverified concurrent replacement')
                return report, payloads
            try:
                with patch.object(assessor, '_verify_cache_snapshot', replace_after_snapshot):
                    after_replacement = evaluate_caches(caches)
                self.assertEqual(after_replacement, acceptance)
            finally:
                for path, payload in originals.items():
                    path.write_bytes(payload)
        self.assertNotEqual(results['stationary'],results['moving'])

    def test_missing_solver_never_publishes_success(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)/'liquid';world,payloads,settings=build_liquid_inputs()
            with self.assertRaises(core.BackendError):simulate(world,payloads,root,settings=settings,executable='/absent-blender')
            self.assertFalse(root.exists())


if __name__=='__main__':unittest.main()
