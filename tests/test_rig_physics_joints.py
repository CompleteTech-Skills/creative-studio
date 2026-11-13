"""Actual Blender constraints and fail-closed admission; no authored trajectories."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as physics
from physics_backend_example import build_inputs
from physics_joints import measurements


def fixture(kind='hinge'):
    world,payloads=build_inputs(frames=48)
    world['gravity']=[2,-9.81,0]
    world['joints']=[dict(id='connection',type=kind,body_a='floor',body_b='ball',
        anchor_a=dict(translation=[0,4.1,0],rotation=[0,0,0,1]),
        anchor_b=dict(translation=[0,1,0],rotation=[0,0,0,1]),axis=[0,0,1],
        limits=[-.5,.5],stiffness=0,damping=0,motor_velocity=0,motor_max_force=0,collide_connected=False)]
    return world,payloads


class JointAdmissionTests(unittest.TestCase):
    def test_native_admission_and_explicit_gaps(self):
        for kind in ['fixed','hinge','slider','ball','point','spring','generic-6dof']:
            world,_=fixture(kind);physics.BlenderBackend('/missing').validate_scene(world)
        for patch in [dict(motor_velocity=1,motor_max_force=1),dict(damping=.5),dict(break_force=10),dict(axis=[0,0,0]),dict(body_b='missing'),dict(limits=[1,-1])]:
            world,_=fixture();world['joints'][0].update(patch)
            with self.assertRaises(Exception):physics.BlenderBackend('/missing').validate_scene(world)
        world,_=fixture();world['joints'][0]['anchor_b']['translation']=[0,0,0]
        with self.assertRaises(physics.BackendError):physics.BlenderBackend('/missing').validate_scene(world)
        world,_=fixture();world['solver']['backend']='pybullet'
        with self.assertRaises(physics.BackendError):physics.PyBulletBackend('/missing').validate_scene(world)


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1','Explicit actual solver gate required')
class NativeJointTests(unittest.TestCase):
    def test_active_spring_restores_displacement(self):
        adapter=physics.BlenderBackend(os.environ['HATCH_BLENDER'])
        displacements=[]
        for stiffness in [0,100]:
            world,payloads=fixture('spring')
            world['gravity']=[0,-9.81,0]
            joint=world['joints'][0]
            joint.update(axis=[0,1,0],limits=[-2,2],stiffness=stiffness,damping=.5 if stiffness else 0)
            prepared=adapter.prepare(world,payloads)
            try:
                adapter.simulate(prepared)
                states=json.loads((prepared.root/'states.json').read_bytes())
                values=measurements(world,states)['samples']
                displacements.append(max(abs(v['joints'][0]['axial_displacement']) for v in values))
                self.assertLess(max(v['joints'][0]['perpendicular_separation'] for v in values),.04)
            finally:adapter.cleanup(prepared)
        self.assertGreater(displacements[0],1.)
        self.assertLess(displacements[1],displacements[0]*.5)

    def test_native_constraints_bake_reopen_and_measured_error(self):
        adapter=physics.BlenderBackend(os.environ['HATCH_BLENDER'])
        for kind in ['fixed','hinge','slider','ball','point','spring','generic-6dof']:
            with self.subTest(kind=kind):
                world,payloads=fixture(kind)
                if kind=='spring':world['joints'][0]['limits']=[0,0]
                prepared=adapter.prepare(world,payloads)
                try:
                    result=adapter.simulate(prepared)
                    states=json.loads((prepared.root/'states.json').read_bytes())
                    diagnostics=measurements(world,states)
                    entries=[s['joints'][0] for s in diagnostics['samples']]
                    self.assertLess(max(s['anchor_separation'] for s in entries),.04)
                    if kind=='hinge':
                        self.assertGreater(max(abs(s['hinge_angle']) for s in entries),.05)
                        self.assertLess(max(abs(s['hinge_angle']) for s in entries),.54)
                    if kind=='fixed':
                        self.assertLess(max(s['relative_angle'] for s in entries),.01)
                    directory=os.environ.get('HATCH_JOINT_EVIDENCE')
                    if directory:
                        target=Path(directory)/kind
                        target.parent.mkdir(parents=True,exist_ok=True)
                        adapter.bake(result,target)
                        (target.parent/(kind+'-measurements.json')).write_text(json.dumps(diagnostics,indent=2))
                        physics.verify_cache(target)
                    else:
                        with tempfile.TemporaryDirectory() as temp:
                            target=Path(temp)/kind;adapter.bake(result,target);physics.verify_cache(target)
                finally:adapter.cleanup(prepared)

if __name__=='__main__':unittest.main()
