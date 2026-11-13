"""Soft-body admission uses actual source geometry; solver cases are explicit."""
from copy import deepcopy
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_backends import BackendError,canonical,parse,digest
from physics_softbody import SoftBodyBackend,admit,_component_meshes,verify_cache
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
                radii=[]
                for sample in samples:
                    points=sample['positions'];center=[sum(p[a] for p in points)/len(points) for a in range(3)]
                    radii.append([sum((p[a]-center[a])**2 for a in range(3))**.5 for p in points])
                self.assertGreater(max(abs(r-.5) for values in radii for r in values),.02)
                (destination/'deformation.json').write_bytes(b'{}')
                with self.assertRaises(BackendError):verify_cache(destination)
            finally:backend.cleanup(prepared)
