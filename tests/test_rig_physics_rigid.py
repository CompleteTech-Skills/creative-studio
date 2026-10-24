"""Real rigid controls: no authored dynamic trajectories and no fake solver."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as physics
from physics_backend_example import build_inputs
from physics_contracts import validate_contract
from physics_rigid import density_mass, sample_translation


def force(kind='force',vector=None,*,point=None,start=0,end=12,application='center'):
    return dict(id='physical-input',type=kind,bodies=['ball'],vector=vector or [2,0,0],point=point or [0,0,0],
        frame_range=dict(start=start,end=end),strength=1,seed=0,categories=['rigid-body'],application=application)


def kinematic_floor(world,payloads):
    """Bind the actual authoritative native Scene3D clip in its hash closure."""
    from scene3d_contracts import validate_document
    from scene3d_example import PROVENANCE
    clip=dict(schema_version=1,kind='scene3d-animation',id='floor-motion',version='1.0.0',
        duration_frames=48,fps=dict(numerator=24,denominator=1),
        tracks=[dict(target='floor',property='translation',interpolation='linear',
            keys=[dict(frame=0,value=[0,-.1,0]),dict(frame=48,value=[2,-.1,0])])],
        bindings=[],dependencies=[],provenance=deepcopy(PROVENANCE))
    validate_document(clip,'animation')
    payloads['floor-motion.json']=physics.canonical(clip)
    reference=dict(id=clip['id'],version=clip['version'],kind=clip['kind'],path='floor-motion.json',sha256=physics.digest(payloads['floor-motion.json']))
    project=json.loads(payloads['project.json']);project['animations']=[reference];project['dependency_lock'].append(reference)
    payloads['project.json']=physics.canonical(project)
    body=world['bodies'][1];body.update(mode='kinematic',authority='animation',animation=clip['id'])
    world['assets'].append({**reference,'kind':'animation'})
    validate_contract(world)


class RigidContractTests(unittest.TestCase):
    def test_density_assignment_is_explicit_and_immutable(self):
        world,_=build_inputs();body=world['bodies'][0];before=deepcopy(body)
        derived=density_mass(body,1000)
        self.assertAlmostEqual(derived['mass']['kilograms'],1000*4*math.pi*.5**3/3)
        self.assertEqual(body,before)
        for density in [True,0,-1,float('inf'),1e100]:
            with self.assertRaises(ValueError):density_mass(body,density)

    def test_force_scope_and_real_backend_limits(self):
        world,_=build_inputs();world['forces']=[force()]
        physics.BlenderBackend('/missing').validate_scene(world)
        for mutation in [lambda f:f.update(type='torque'),lambda f:f.update(application='world-point'),lambda f:f.update(bodies=['floor']),lambda f:f.update(seed=3)]:
            value=deepcopy(world);mutation(value['forces'][0])
            with self.assertRaises(physics.BackendError):physics.BlenderBackend('/missing').validate_scene(value)
        world['solver']['backend']='pybullet'
        for kind in ['force','torque','impulse','angular-impulse']:
            world['forces']=[force(kind,end=1)]
            physics.PyBulletBackend('/missing').validate_scene(world)
        world['fps']=dict(numerator=30000,denominator=1001);world['forces']=[force('impulse',start=1,end=2)]
        with self.assertRaises(physics.BackendError):physics.PyBulletBackend('/missing').validate_scene(world)

    def test_integral_float_force_clocks_and_native_clip_preserve_inputs(self):
        from physics_rigid import compile_motion
        from scene3d_contracts import validate_document
        world,payloads=build_inputs(backend='pybullet');world['forces']=[force('impulse',end=1)]
        world['forces'][0]['frame_range']={'start':0.0,'end':1.0};world['forces'][0]['seed']=0.0
        before=deepcopy(world)
        physics.PyBulletBackend('/missing').validate_scene(world)
        self.assertEqual(world,before)
        kinematic_floor(world,payloads)
        clip=json.loads(payloads['floor-motion.json']);clip['fps']={'numerator':24.0,'denominator':1.0}
        clip['duration_frames']=48.0
        for key in clip['tracks'][0]['keys']:key['frame']=float(key['frame'])
        validate_document(clip,'animation');original=deepcopy(clip)
        motion=compile_motion(world['bodies'][1],clip,physics._world(world),lambda okay,message:self.assertTrue(okay,message))
        self.assertEqual(motion['fps'],{'numerator':24,'denominator':1});self.assertEqual(clip,original)
        self.assertTrue(all(type(key['frame']) is int for key in motion['keys']))

    def test_zero_native_inertia_cannot_certify_small_positive_tensor(self):
        root=Path(__file__).resolve().parents[1]/'tests/results/physics-timing-20261024/executed/artifacts/24-1-0'
        report=json.loads((root/'simulation-report.json').read_bytes())
        world=json.loads((root/'world.json').read_bytes())
        world['bodies'][0]['mass']['inertia']=[[1e-9,0,0],[0,1e-9,0],[0,0,1e-9]]
        admitted=physics._admit_world(world,'pybullet')[0]
        report['rigid_controls']=dict(version=1,forces=[],force_policy='native external force/torque per fixed step; discrete impulse is integrated wrench over one complete native step',bodies=[])
        for body in admitted['bodies']:
            report['rigid_controls']['bodies'].append(dict(id=body['id'],mode=body['mode'],inertial_position=[0,0,0],inertial_rotation=[0,0,0,1],principal_inertia=[0,0,0],initial_linear_velocity=[0,0,0],initial_angular_velocity=[0,0,0],sleep=dict(enabled=False,initially_sleeping=False,linear_threshold=None,angular_threshold=None)))
        report['actual_bodies'][0]['inertia']=[0,0,0]
        with self.assertRaises(physics.BackendError) as raised:physics._check_pybullet_report(report,admitted,report['backend'])
        self.assertIn('inertia',str(raised.exception))
        world['bodies'][0]['mass']['inertia']=[[1e-9,0,0],[0,.1,0],[0,0,.1]]
        admitted=physics._admit_world(world,'pybullet')[0]
        report['rigid_controls']['bodies'][0]['principal_inertia']=[1e-100,.1,.1]
        report['actual_bodies'][0]['inertia']=[1e-100,.1,.1]
        with self.assertRaises(physics.BackendError):physics._check_pybullet_report(report,admitted,report['backend'])
        world['bodies'][0]['mass']['inertia']=[[1e-50,0,0],[0,1e-50,0],[0,0,1e-50]]
        with self.assertRaises(physics.BackendError):physics._admit_world(world,'pybullet')

    def test_sleep_conflicts_and_unexposed_thresholds_fail(self):
        world,_=build_inputs();body=world['bodies'][0]
        body['sleep']=dict(enabled=False,initially_sleeping=True)
        with self.assertRaises(Exception):validate_contract(world)
        body['sleep']=dict(enabled=True,initially_sleeping=True);body['linear_velocity']=[1,0,0]
        with self.assertRaises(Exception):validate_contract(world)
        body['linear_velocity']=[0,0,0];body['sleep']['linear_threshold']=.4
        world['solver']['backend']='pybullet'
        with self.assertRaises(physics.BackendError):physics.PyBulletBackend('/missing').validate_scene(world)

    def test_motion_exact_linear_step_and_authority(self):
        motion=dict(fps=dict(numerator=30000,denominator=1001),interpolation='linear',keys=[dict(frame=0,value=[0,0,0]),dict(frame=30,value=[3,0,0])])
        self.assertEqual(sample_translation(motion,Fraction(1001,2000)),[1.5,0,0])
        motion['interpolation']='step';self.assertEqual(sample_translation(motion,Fraction(1,10)),[0,0,0])
        world,payloads=build_inputs();kinematic_floor(world,payloads)
        physics.BlenderBackend('/missing').validate_scene(world)
        world['bodies'][1]['authority']='physics'
        with self.assertRaises(physics.BackendError):physics.BlenderBackend('/missing').validate_scene(world)


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1','Explicit actual solver gate required')
class NativeRigidTests(unittest.TestCase):
    def run_world(self,backend,world,payloads):
        adapter=physics.BlenderBackend(os.environ['HATCH_BLENDER']) if backend=='blender' else physics.PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
        before=deepcopy(payloads)
        prepared=adapter.prepare(world,payloads)
        try:
            result=adapter.simulate(prepared)
            states=physics.parse((prepared.root/'states.json').read_bytes())
            report=physics.parse((prepared.root/'simulation-report.json').read_bytes())
            output=os.environ.get('HATCH_RIGID_EVIDENCE')
            if output:
                destination=Path(output)/self._testMethodName/(backend+'-'+str(self.counter))
                destination.parent.mkdir(parents=True,exist_ok=True);adapter.bake(result,destination)
                self.assertEqual(physics.verify_cache(destination)['sample_count'],world['frame_range']['end'])
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    destination=Path(tmp)/'cache';adapter.bake(result,destination)
                    self.assertEqual(physics.verify_cache(destination)['sample_count'],world['frame_range']['end'])
            if output:
                index=Path(output)/'executions.json'
                records=json.loads(index.read_bytes()) if index.exists() else []
                records.append(dict(test=self._testMethodName,backend=backend,world_sha256=result.world_sha256,
                    states_sha256=physics.digest((prepared.root/'states.json').read_bytes()),backend_identity=result.backend,
                    usage=result.usage,solver=world['solver'],gravity=world['gravity'],mass=[b['mass'] for b in world['bodies']],
                    forces=world['forces'],first=states['samples'][0],sixth=states['samples'][min(6,len(states['samples'])-1)],last=states['samples'][-1]))
                index.write_bytes(physics.canonical(records))
            self.counter+=1
            self.assertEqual(payloads,before)
            return states,report
        except Exception as exc:
            output=os.environ.get('HATCH_RIGID_EVIDENCE')
            if output:
                failed=Path(output)/'failed-attempts'/self._testMethodName/(backend+'-'+str(self.counter))
                failed.parent.mkdir(parents=True,exist_ok=True)
                shutil.copytree(prepared.root,failed,symlinks=True)
                (failed/'failure.json').write_bytes(physics.canonical(dict(status='FAILED; not a verified physics cache',exception=type(exc).__name__,message=str(exc),diagnostic=getattr(exc,'report',None))))
            raise
        finally:adapter.cleanup(prepared)

    def setUp(self):self.counter=0
    @staticmethod
    def pose(states,frame,body='ball'):
        return next(item for item in states['samples'][frame]['bodies'] if item['id']==body)

    def test_real_blender_and_direct_freefall_zero_g_and_mass(self):
        for backend in ['blender','pybullet']:
            for shape,mass in [('sphere',1),('box',10)]:
                world,payloads=build_inputs(backend=backend,dynamic_shape=shape)
                body=world['bodies'][0];ratio=mass/body['mass']['kilograms'];body['mass']['kilograms']=mass
                body['mass']['inertia']=[[v*ratio for v in row] for row in body['mass']['inertia']]
                states,report=self.run_world(backend,world,payloads)
                for frame in range(1,7):
                    self.assertLess(abs(self.pose(states,frame)['translation'][1]-(3-9.81*(frame/24)**2/2)),.03)
                self.assertLess(abs(self.pose(states,48)['translation'][1]-.5),.03)
            world,payloads=build_inputs(backend=backend);world['gravity']=[0,0,0]
            states,_=self.run_world(backend,world,payloads)
            self.assertLess(max(abs(a-b) for a,b in zip(self.pose(states,48)['translation'],[0,3,0])),1e-5)

    def test_real_native_central_force_is_mass_dependent(self):
        for backend in ['blender','pybullet']:
            for numerator,denominator in [(24,1),(30000,1001)]:
                observed=[]
                for mass in [1,2]:
                    world,payloads=build_inputs(backend=backend);world['gravity']=[0,0,0];world['forces']=[force()]
                    world['fps']=dict(numerator=numerator,denominator=denominator)
                    if backend=='blender' and denominator!=1:world['solver']['fixed_step']=dict(numerator=1001,denominator=240000)
                    body=world['bodies'][0];body['mass']['kilograms']=mass;body['mass']['inertia']=[[x*mass for x in row] for row in body['mass']['inertia']]
                    states,_=self.run_world(backend,world,payloads)
                    time=6*denominator/numerator;x=self.pose(states,6)['translation'][0]
                    self.assertLess(abs(x-(2/mass)*(time**2)/2),.003)
                    self.assertAlmostEqual(self.pose(states,6)['translation'][1],3,delta=1e-5)
                    observed.append(x)
                self.assertAlmostEqual(observed[0]/observed[1],2,delta=.01)

    def test_real_direct_impulse_torque_initial_velocity_and_custom_inertia(self):
        for kind,vector in [('impulse',[1,0,0]),('angular-impulse',[0,0,.1]),('torque',[0,0,.1])]:
            world,payloads=build_inputs(backend='pybullet');world['gravity']=[0,0,0]
            world['forces']=[force(kind,vector,end=1 if 'impulse' in kind else 12)]
            states,_=self.run_world('pybullet',world,payloads)
            if kind=='impulse':self.assertAlmostEqual(self.pose(states,12)['translation'][0],.5,delta=.005)
            elif kind=='angular-impulse':self.assertAlmostEqual(abs(self.pose(states,12)['rotation'][2]),math.sin(.25),delta=.005)
            else:self.assertAlmostEqual(abs(self.pose(states,12)['rotation'][2]),math.sin(.0625),delta=.01)
        for tensor in [[[.2,.02,0],[.02,.3,0],[0,0,.25]], [[.12,.02,0],[.02,.14,0],[0,0,.15]]]:
            world,payloads=build_inputs(backend='pybullet');world['gravity']=[0,0,0]
            body=world['bodies'][0];body['linear_velocity']=[1,0,0];body['angular_velocity']=[0,0,.3]
            body['mass']['center_of_mass']=[.1,0,0];body['mass']['inertia']=tensor
            states,report=self.run_world('pybullet',world,payloads)
            self.assertLess(max(abs(a-b) for a,b in zip(self.pose(states,0)['translation'],body['transform']['translation'])),1e-6)
            self.assertLess(abs(abs(self.pose(states,0)['rotation'][3])-1),1e-6)
            self.assertGreater(self.pose(states,24)['translation'][0],.9)
            self.assertGreater(abs(self.pose(states,24)['rotation'][2]),.1)
            self.assertEqual(report['rigid_controls']['bodies'][0]['inertial_position'],[.1,0,0])

    def test_real_direct_offcenter_force_and_damping(self):
        world,payloads=build_inputs(backend='pybullet');world['gravity']=[0,0,0]
        world['forces']=[force('impulse',[1,0,0],point=[0,3.5,0],application='world-point',end=1)]
        states,_=self.run_world('pybullet',world,payloads)
        self.assertGreater(abs(self.pose(states,12)['rotation'][2]),.2)
        distances=[]
        for damping in [0,.8]:
            world,payloads=build_inputs(backend='pybullet');world['gravity']=[0,0,0]
            world['bodies'][0]['linear_velocity']=[1,0,0];world['materials'][0]['linear_damping']=damping
            states,_=self.run_world('pybullet',world,payloads);distances.append(self.pose(states,48)['translation'][0])
        self.assertGreater(distances[0]-distances[1],.5)

    def test_real_kinematic_native_clip_and_dynamic_authorities(self):
        for backend in ['blender','pybullet']:
            world,payloads=build_inputs(backend=backend);kinematic_floor(world,payloads)
            states,_=self.run_world(backend,world,payloads)
            self.assertAlmostEqual(self.pose(states,24,'floor')['translation'][0],1,delta=.01)
            self.assertLess(self.pose(states,12)['translation'][1],2)

    def test_real_sleep_and_force_wake(self):
        for backend in ['blender','pybullet']:
            world,payloads=build_inputs(backend=backend);world['bodies'][0]['sleep']=dict(enabled=True,initially_sleeping=True)
            if backend=='blender':world['bodies'][0]['sleep'].update(linear_threshold=.1,angular_threshold=.1)
            world['forces']=[force(start=8,end=20)]
            states,_=self.run_world(backend,world,payloads)
            self.assertAlmostEqual(self.pose(states,6)['translation'][1],3,delta=.001)
            self.assertLess(self.pose(states,18)['translation'][1],2.8)

    def test_real_blender_damping_reduces_force_driven_motion(self):
        values=[]
        for damping in [0,.8]:
            world,payloads=build_inputs();world['gravity']=[0,0,0];world['forces']=[force(end=24)]
            world['materials'][0]['linear_damping']=damping
            states,_=self.run_world('blender',world,payloads);values.append(self.pose(states,24)['translation'][0])
        self.assertGreater(values[0]-values[1],.1)
