"""Soft-body admission uses actual source geometry; solver cases are explicit."""
from copy import deepcopy
from pathlib import Path
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_backends import BackendError,canonical,parse,digest,_run_process,_inventory
from physics_softbody import SoftBodyBackend,admit,_component_meshes,verify_cache,_validate_output
from physics_softbody_example import build_softbody_inputs


class SoftBodyAdmissionTests(unittest.TestCase):
    def test_strict_world_admission_retains_input_and_explicit_capabilities(self):
        world,_=build_softbody_inputs();before=deepcopy(world)
        self.assertEqual(admit(world),world);self.assertEqual(world,before)
        report=SoftBodyBackend('/missing').validate_scene(world)
        self.assertFalse(report['solver_executed']);self.assertFalse(report['native_scene_verified'])
        for edit in (lambda w:w.update(script='untrusted'),lambda w:w['components'][0].update(stiffness=2),
                     lambda w:w['components'][0].update(parent='floor'),lambda w:w['components'][0].update(vertex_count=100000),
                     lambda w:w['solver'].update(iterations=2),lambda w:w['solver'].update(stability={'max_linear_velocity':2}),
                     lambda w:w['bodies'][0].update(mode='dynamic'),lambda w:w['gravity'].__setitem__(0,float('nan'))):
            changed=deepcopy(world);edit(changed)
            with self.assertRaises(BackendError):admit(changed)

    def test_native_clock_limits_and_integer_pin_normalization(self):
        world,_=build_softbody_inputs(pins=True)
        world['components'][0]['pins']=[2.0]
        self.assertIs(type(admit(world)['components'][0]['pins'][0]),int)
        world['fps']={'numerator':48000,'denominator':1001}
        world['solver']['fixed_step']={'numerator':1001,'denominator':4800000}
        with self.assertRaises(BackendError):admit(world)

    def test_native_report_rejects_false_clock_and_requested_properties(self):
        world,_=build_softbody_inputs();world['frame_range']['end']=2
        comp=world['components'][0]
        report=dict(ok=True,is_baked=True,world_sha256=digest(canonical(world)),application_version='4.5.3 LTS',application_build='67807e1800cc',operation='simulate',sample_count=2,vertex_count=66,
            fps=24,fps_base=1,time_scale=1,disk_cache=True,native_settings=dict(mass=comp['mass']/comp['vertex_count'],
            friction=0,pull=comp['stiffness'],push=comp['stiffness'],damping=comp['damping']*50,
            step_min=1000,step_max=1000,use_auto_step=False,use_goal=False))
        data=dict(component='ball',samples=[dict(frame=f,positions=[[0,0,0]]*66) for f in range(2)])
        payloads={'simulate-report.json':canonical(report),'deformation.json':canonical(data)}
        _validate_output(None,world,payloads)
        for key,value in [('fps',23),('disk_cache',False),('time_scale',.5)]:
            altered=deepcopy(report);altered[key]=value
            with self.assertRaises(BackendError):_validate_output(None,world,dict(payloads,**{'simulate-report.json':canonical(altered)}))
        altered=deepcopy(report);altered['native_settings']['pull']=.1
        with self.assertRaises(BackendError):_validate_output(None,world,dict(payloads,**{'simulate-report.json':canonical(altered)}))

    def test_actual_component_geometry_and_unchanged_sources(self):
        world,payloads=build_softbody_inputs()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            for path,data in payloads.items():(root/path).write_bytes(data)
            bundle,meshes=_component_meshes(root,world)
            self.assertEqual(len(meshes['ball']['positions']),66)
            self.assertEqual(len(meshes['ball']['triangles']),128)
            self.assertGreater(min(p[1] for p in meshes['ball']['positions']),2)
            self.assertEqual({p:(root/p).read_bytes() for p in payloads},payloads)
            (root/'ball.obj').write_bytes(b'changed')
            with self.assertRaises(BackendError):_component_meshes(root,world)

    def test_declared_vertex_count_cannot_replace_real_mesh_count(self):
        world,payloads=build_softbody_inputs();world['components'][0]['vertex_count']+=1
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            for path,data in payloads.items():(root/path).write_bytes(data)
            with self.assertRaises(BackendError):_component_meshes(root,world)

    def test_softbody_protocol_reports_own_category_and_rejects_export(self):
        backend=SoftBodyBackend('/missing');world,_=build_softbody_inputs()
        with patch.object(backend,'_discover',return_value={'id':'blender','application_version':'test','executable_sha256':'0'*64}):
            report=backend.probe()
        self.assertEqual(report.categories['soft-body'].state,'experimental')
        self.assertEqual(report.categories['rigid-body'].state,'unsupported')
        self.assertFalse(report.headless_smoke_verified)
        self.assertGreater(backend.estimate_resources(world,mesh_vertices=66).solver_steps,0)
        low=deepcopy(world);low['budgets']['bake_file_bytes']=1
        with self.assertRaises(BackendError):backend.estimate_resources(low,mesh_vertices=66)
        with self.assertRaises(BackendError):backend.export_animation('/missing','/missing')
        with patch('physics_softbody.verify_cache',return_value={'status':'ok'}) as verify:
            backend.verify('cache',expected_world_sha256='expected')
            verify.assert_called_once_with('cache',expected_world_sha256='expected')
        with patch('physics_softbody.inspect_cache',return_value={'frame':2}) as inspect:
            self.assertEqual(backend.inspect('cache',2),{'frame':2})
            inspect.assert_called_once_with('cache',2)

    def test_mutated_prepared_worker_is_rejected_before_launch(self):
        world,payloads=build_softbody_inputs();backend=SoftBodyBackend('/missing')
        with patch.object(backend,'_discover',return_value={'id':'negative-test-only'}):
            prepared=backend.prepare(world,payloads)
        try:
            (prepared.root/'worker.py').write_bytes(b'untrusted modification')
            with patch('physics_softbody._run_process') as worker:
                with self.assertRaises(BackendError):backend.simulate(prepared)
                worker.assert_not_called()
        finally:backend.cleanup(prepared)


@unittest.skipUnless(os.environ.get('HATCH_SOFTBODY_TEST')=='1','Explicit Blender soft-body gate')
class NativeSoftBodyTests(unittest.TestCase):
    @staticmethod
    def _change_json(path,key,value):
        data=parse(path.read_bytes());data[key]=value;path.write_bytes(canonical(data))

    @staticmethod
    def _collapse_initial(path):
        data=parse(path.read_bytes());data['samples'][0]['positions']=[[0,0,0] for _ in data['samples'][0]['positions']]
        path.write_bytes(canonical(data))

    def test_real_deformation_and_reopened_cache(self):
        backend=SoftBodyBackend(os.environ['HATCH_BLENDER']);world,payloads=build_softbody_inputs()
        prepared=backend.prepare(world,payloads)
        with tempfile.TemporaryDirectory() as name:
            try:
                result=backend.simulate(prepared)
                self.assertLessEqual(result.verification['maximum_vertex_error'],1e-5)
                destination=Path(name)/'cache';backend.bake(result,destination)
                self.assertEqual(verify_cache(destination)['sample_count'],100)
                samples=parse((destination/'deformation.json').read_bytes())['samples']
                initial=[p[1] for p in samples[0]['positions']]
                final=[p[1] for p in samples[-1]['positions']]
                self.assertGreater(sum(initial)/len(initial)-sum(final)/len(final),1)
                self.assertGreater(min(final),-.05)
                self.assertLess(abs(sum(p[0] for p in samples[-1]['positions'])/66),.1)
                self.assertLess(abs(sum(p[2] for p in samples[-1]['positions'])/66),.1)
                radii=[]
                for sample in samples:
                    points=sample['positions'];center=[sum(p[a] for p in points)/len(points) for a in range(3)]
                    radii.append([sum((p[a]-center[a])**2 for a in range(3))**.5 for p in points])
                self.assertGreater(max(abs(r-.5) for values in radii for r in values),.02)
                # Published native files must replay after relocation, with changed gravity.
                relocated=Path(name)/'relocated';shutil.copytree(destination,relocated)
                _run_process(backend._command(relocated,reopen=True),relocated,timeout=30,
                    memory_bytes=world['budgets']['memory_bytes'],output_bytes=world['budgets']['temporary_bytes'],label='relocation')
                self.assertEqual(parse((relocated/'reopen-report.json').read_bytes())['maximum_vertex_error'],0)
                # Rehashed edits must still fail semantic/source consistency checks.
                for label,edit in [
                    ('fixture',lambda path,m:m.update(fixture=False)),
                    ('native-missing',lambda path,m:(path/'softbody.blend').unlink()),
                    ('native-clock',lambda path,m:self._change_json(path/'simulate-report.json','fps',12)),
                    ('initial-geometry',lambda path,m:self._collapse_initial(path/'deformation.json'))]:
                    clone=Path(name)/label;shutil.copytree(destination,clone)
                    manifest=parse((clone/'cache.json').read_bytes());edit(clone,manifest)
                    manifest['files']=[f for f in _inventory(clone) if f['path']!='cache.json']
                    manifest['usage']['output_bytes']=sum(f['bytes'] for f in manifest['files'])
                    (clone/'cache.json').write_bytes(canonical(manifest))
                    with self.assertRaises(BackendError,msg=label):verify_cache(clone)
                # Publication must preserve a preexisting user directory.
                occupied=Path(name)/'occupied';occupied.mkdir();(occupied/'sentinel').write_bytes(b'keep')
                with self.assertRaises(BackendError):backend.bake(result,occupied)
                self.assertEqual((occupied/'sentinel').read_bytes(),b'keep')
                (destination/'deformation.json').write_bytes(b'{}')
                with self.assertRaises(BackendError):verify_cache(destination)
            finally:backend.cleanup(prepared)
