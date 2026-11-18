from fractions import Fraction
import math
from contextlib import nullcontext
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_timing import clock, bracket, interpolate
from physics_backend_example import build_inputs
import physics_backends as physics

class TimingTests(unittest.TestCase):
    def test_exact_ntsc_terminal_brackets_without_drift(self):
        world,_=build_inputs(backend='pybullet')
        world['fps']=dict(numerator=30000,denominator=1001)
        value=clock(world)
        self.assertEqual(value['last_sample_time'],dict(numerator=1001,denominator=625))
        self.assertEqual(value['solver_steps'],385)
        self.assertEqual(value['computed_terminal_time'],dict(numerator=77,denominator=48))
        self.assertEqual(bracket(Fraction(1001,30000),Fraction(1,240)),(8,9,Fraction(1,125)))

    def test_quaternion_shortest_arc_and_endpoints(self):
        a=dict(id='x',translation=[0,0,0],rotation=[0,0,0,1])
        b=dict(id='x',translation=[2,0,0],rotation=[0,0,0,-1])
        self.assertEqual(interpolate(a,b,Fraction(1,2))['translation'],[1,0,0])
        self.assertEqual(interpolate(a,b,Fraction(1,2))['rotation'],[0,0,0,1])

    def test_backend_restriction_and_stability_are_explicit(self):
        world,_=build_inputs()
        world['fps']=dict(numerator=30000,denominator=1001)
        with self.assertRaises(physics.BackendError):physics.BlenderBackend('/missing').validate_scene(world)
        world['solver']['backend']='pybullet'
        report=physics.PyBulletBackend('/missing').validate_scene(world)
        self.assertEqual(report['timing']['effective_solver_step'],dict(numerator=1,denominator=240))
        world['solver']['stability']=dict(continuous_collision_detection=True)
        with self.assertRaises(physics.BackendError) as raised:physics.PyBulletBackend('/missing').validate_scene(world)
        self.assertEqual(raised.exception.report['code'],'PHYSICS_STABILITY_UNSUPPORTED')
