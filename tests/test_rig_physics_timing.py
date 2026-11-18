"""Actual Blender and DIRECT timing gates; excluded from 2D-only discovery."""
from fractions import Fraction
from contextlib import nullcontext
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_timing import clock
from physics_backend_example import build_inputs
import physics_backends as physics

@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1','Explicit real solver gate')
class NativeTimingTests(unittest.TestCase):
    def test_real_blender_integer_and_ntsc_effective_steps(self):
        target=os.environ.get('HATCH_TIMING_EVIDENCE')
        retained=Path(target).parent/'blender-artifacts' if target else None
        if retained:retained.mkdir(parents=True,exist_ok=False)
        records=[]
        with (nullcontext(str(retained)) if retained else tempfile.TemporaryDirectory(prefix='hatch-blender-timing-')) as directory:
            for p,q,denominator in [(24,1,240),(30000,1001,240000)]:
                backend=physics.BlenderBackend(os.environ['HATCH_BLENDER'])
                world,payloads=build_inputs()
                world['fps']=dict(numerator=p,denominator=q)
                world['solver']['fixed_step']=dict(numerator=q,denominator=denominator)
                prepared=backend.prepare(world,payloads)
                try:
                    result=backend.simulate(prepared)
                    destination=Path(directory)/str(p)
                    backend.bake(result,destination)
                    self.assertEqual(physics.verify_cache(destination)['sample_count'],49)
                    errors=[]
                    for frame in range(1,7):
                        sample=physics.inspect_cache(destination,frame)
                        expected=3-9.81*(frame*q/p)**2/2
                        errors.append(abs(sample['bodies'][0]['translation'][1]-expected))
                        self.assertLess(errors[-1],.03)
                        self.assertEqual(sample['sampling']['source'],'direct solved state')
                    records.append(dict(rate=[p,q],backend=result.backend,usage=result.usage,
                        continuous_errors=errors,tolerance=.03,solver=world['solver'],gravity=world['gravity'],materials=world['materials'],
                        timing=clock(world),cache=physics.verify_cache(destination),states_sha256=physics.digest((destination/'states.json').read_bytes())))
                finally:backend.cleanup(prepared)
        if target:Path(target).with_name('blender-numerical.json').write_bytes(physics.canonical(dict(scope='real Blender native integral-step integer and NTSC free fall',records=records)))

    def test_real_contact_slop_and_velocity_limit_failure(self):
        heights=[];records=[]
        target=os.environ.get('HATCH_TIMING_EVIDENCE')
        for slop in [0,.05]:
            backend=physics.PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
            world,payloads=build_inputs(backend='pybullet')
            world['solver']['stability']=dict(contact_penetration_tolerance=slop)
            prepared=backend.prepare(world,payloads)
            try:
                result=backend.simulate(prepared)
                states=physics.parse((prepared.root/'states.json').read_bytes())
                heights.append(states['samples'][-1]['bodies'][0]['translation'][1])
                contacts=physics.parse((prepared.root/'contacts.json').read_bytes())
                self.assertTrue(contacts['samples'][-1]['contacts'])
                if target:
                    destination=Path(target).parent/('contact-'+str(slop))
                    backend.bake(result,destination)
                records.append(dict(slop=slop,height=heights[-1],world_sha256=result.world_sha256,backend=result.backend,usage=result.usage))
            finally:backend.cleanup(prepared)
        self.assertGreater(heights[0]-heights[1],.03)
        self.assertLess(heights[0]-heights[1],.07)
        backend=physics.PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
        world,payloads=build_inputs(backend='pybullet')
        world['solver']['stability']=dict(max_linear_velocity=.001)
        prepared=backend.prepare(world,payloads)
        try:
            with self.assertRaises(physics.BackendError) as raised:backend.simulate(prepared)
            self.assertEqual(raised.exception.report['category'],'numerical-instability')
            self.assertEqual(raised.exception.report['solver_step'],1)
            self.assertEqual(raised.exception.report['solver_time'],dict(numerator=1,denominator=240))
            self.assertEqual(raised.exception.report['last_valid_frame'],0)
            stderr=(prepared.root/'simulation-stderr.log').read_bytes()
            self.assertIn(b'Numerical stability velocity limit exceeded',stderr)
            self.assertFalse((prepared.root/'states.json').exists())
            if target:Path(target).with_name('stability.json').write_bytes(physics.canonical(dict(contact_comparison=records,
                contact_height_difference=heights[0]-heights[1],velocity_failure='observed real solver failure; no clamping',
                stderr=stderr.decode(),diagnostic=raised.exception.report,world_sha256=prepared.world_sha256,limit=.001)))
        finally:backend.cleanup(prepared)

    def test_real_fractional_rates_analytic_and_pinned_repeat(self):
        records=[]
        target=os.environ.get('HATCH_TIMING_EVIDENCE')
        retained=Path(target).parent/'artifacts' if target else None
        if retained:retained.mkdir(parents=True,exist_ok=False)
        with (nullcontext(str(retained)) if retained else tempfile.TemporaryDirectory(prefix='hatch-timing-oracles-')) as directory:
            for p,q in [(24,1),(30000,1001),(47,2)]:
                outputs=[]
                for repeat in range(2):
                    backend=physics.PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
                    world,payloads=build_inputs(backend='pybullet')
                    world['fps']=dict(numerator=p,denominator=q)
                    world['solver']['substeps']=2
                    world['solver']['stability']=dict(contact_penetration_tolerance=.001,max_linear_velocity=20,max_angular_velocity=20)
                    prepared=backend.prepare(world,payloads)
                    try:
                        result=backend.simulate(prepared)
                        destination=Path(directory)/f'{p}-{q}-{repeat}'
                        backend.bake(result,destination)
                        self.assertEqual(physics.verify_cache(destination)['sample_count'],49)
                        values=physics.parse((destination/'states.json').read_bytes())
                        maximum=0.
                        dt=Fraction(1,480)
                        for frame in range(1,7):
                            t=Fraction(frame*q,p)
                            coordinate=t/dt;n=math.floor(coordinate);a=float(coordinate-n)
                            expected=3-9.81*float(dt)**2*((1-a)*n*(n+1)+a*(n+1)*(n+2))/2
                            observed=values['samples'][frame]['bodies'][0]['translation'][1]
                            maximum=max(maximum,abs(expected-observed))
                            self.assertLess(abs(expected-observed),1e-9)
                            # Also bound numerical integration error against continuous free fall.
                            self.assertLess(abs(observed-(3-9.81*float(t)**2/2)),.004)
                        sought=physics.inspect_time(destination,Fraction(q,2*p))
                        self.assertEqual(sought['bracket_frames'],[0,1])
                        self.assertEqual(sought['sample_clock'],'solver')
                        self.assertIn('alpha',sought)
                        self.assertEqual(len(sought['bracket_times']),2)
                        native=physics.parse((destination/'solver-states.json').read_bytes())
                        self.assertEqual(len(native['samples']),clock(world)['solver_steps']+1)
                        self.assertEqual(physics.inspect_time(destination,Fraction(48*q,p))['time'],dict(numerator=Fraction(48*q,p).numerator,denominator=Fraction(48*q,p).denominator))
                        with self.assertRaises(physics.BackendError):physics.inspect_time(destination,Fraction(49*q,p))
                        outputs.append(values)
                        records.append(dict(rate=[p,q],repeat=repeat,discrete_euler_max_error=maximum,
                            continuous_tolerance=.004,timing=clock(world),backend=result.backend,usage=result.usage,
                            hardware=physics.hardware(),solver=world['solver'],gravity=world['gravity'],materials=world['materials'],
                            cache=physics.verify_cache(destination),states_sha256=physics.digest((destination/'states.json').read_bytes())))
                    finally:backend.cleanup(prepared)
                self.assertEqual(outputs[0],outputs[1])
            target=os.environ.get('HATCH_TIMING_EVIDENCE')
            if target:Path(target).write_bytes(physics.canonical(dict(scope='real PyBullet; pinned same-environment paired runs only',classification='pinned-numerical for these paired runs only',maximum_paired_output_difference=0,records=records)))
