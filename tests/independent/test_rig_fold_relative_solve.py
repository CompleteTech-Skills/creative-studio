"""Independent native fixtures for large IK lengths and unavailable contact metrics.

These are synthetic engineering geometry tests; they exercise native FK, skin,
contacts and runtime playback without claiming visual or production approval.
"""
from copy import deepcopy
import json
import math
import tempfile
import unittest

import numpy as np

from rig_animation import evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_errors import RigError
from rig_ik import solve_ik, validate_controllers
from rig_models import SkinWeights, validate_geometry
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin

from rig_test_helpers import chain_skeleton, fixture
from rig_validation import contact_metrics, validate_rig


def step(bone, path, times, values):
    return dict(schema_version=1, target=bone, path=path, interpolation='STEP',
                times=times, values=values)


class RigContactNumericBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rig = fixture(directory.name)
        self.mesh = self.rig.geometry.meshes[0]
        self.neutral = deepcopy(self.rig.clips['neutral'])
    def bind_root(self, count):
        self.rig.manifest['skeleton'] = chain_skeleton(count)
        validate_skeleton(self.rig.skeleton)
        validate_geometry(self.rig.geometry)
        vertices = len(self.mesh.positions)
        skin = SkinWeights(self.mesh.id, ['b0'], np.zeros((vertices, 4), dtype=np.uint16),
                           np.tile([1., 0., 0., 0.], (vertices, 1)), np.eye(4)[None])
        validate_skin(skin, self.mesh, self.rig.skeleton)
        self.rig.weights = {self.mesh.id: skin}
    def install_clip(self, tracks, contacts, name):
        clip = dict(deepcopy(self.neutral), id=name, tracks=tracks, contacts=contacts)
        validate_controllers(self.rig.skeleton, self.rig.manifest['controllers'])
        validate_animation(self.rig, clip)
        validate_contract(clip, 'AnimationClip')
        self.rig.clips = {name: clip}
        return clip
    def assert_geometry(self, pose):
        validate_contract(pose, 'Pose')
        world = evaluate_fk(self.rig.skeleton, pose)
        for matrix in world.values():
            self.assertTrue(np.isfinite(matrix).all())
        deformed = evaluate_deformation(self.rig, pose)
        for mesh in self.rig.geometry.meshes:
            self.assertTrue(np.isfinite(deformed[mesh.id]['positions']).all())
        return world

import math
from copy import deepcopy
import numpy as np
from rig_math import axis_angle, qmatrix
from rig_animation import evaluate_animation
from rig_ik import solve_ik
try:
    from rig_state_runtime import RigStateRuntime
except ModuleNotFoundError as err:
    if err.name != 'rig_state_runtime':
        raise
    RigStateRuntime = None
from rig_errors import RigError

failures = 0
for parents in (17,35):
    case=RigContactNumericBoundaryTests()
    case.setUp()
    case.bind_root(parents+4)
    rig=case.rig
    chain=rig.manifest['controllers']['ik'][0]
    names=[f'b{i}' for i in range(parents+1,parents+4)]
    quat=axis_angle([0,0,1],math.pi/4)
    rot=qmatrix(quat)
    chain.update(bones=names,solver='two-bone',pole=(rot @ [-2.,3.,1.]).tolist())
    tracks=[]
    for i in range(parents+2):
        tracks.append(step(f'b{i}','translation',[0.,.5],[[0.,float(i>0),0.],[0.]*3]))
    tracks.append(step('b0','rotation',[0.,.5],[[0.,0.,0.,1.],quat.tolist()]))
    for i in range(parents+1):
        scales=[2.**29]*3 if i<parents else [480.,320.,160.]
        tracks.append(step(f'b{i}','scale',[0.,.5],[[1.]*3,scales]))
    for name,sign in zip(names[1:],[1.,-1.]):
        local=(math.ldexp(1.,-29*parents)/160)*sign
        tracks.append(step(name,'translation',[0.,.5],[[0.,1.,0.],[local]*3]))
    goal=dict(schema_version=1,id='plant',chain=chain['id'],start=.5,end=1.,target=[0.]*3,kind='hand')
    clip=case.install_clip(tracks,[goal],'fold-control')
    with np.errstate(all='ignore'):
        pose=evaluate_animation(rig,clip,.5,contacts=False)['pose']
        world=case.assert_geometry(pose)
        witness=deepcopy(pose)
        witness['bones'][names[0]].update(rotation=[0.,0.,2**-.5,2**-.5],scale=[1.5,2./3,1.])
        after=case.assert_geometry(witness)
        np.testing.assert_allclose(after[names[1]][:3,3],chain['pole'],rtol=0.,atol=1e-12)
        np.testing.assert_allclose(after[names[-1]][:3,3],[0.,0.,0.],rtol=0.,atol=1e-12)
        print('parents',parents,'finite poses and real geometry, witness matches desired elbow and closed target')
        print('column lengths', [math.hypot(*world[names[0]][:3,k]) for k in range(3)])
        state='static-positive'
        runtime=RigStateRuntime(rig,state,recipes={state:clip['id']},selected=[state]) if RigStateRuntime else None
        if runtime:
            print('before',runtime.evaluate(.25)['status'])
        for op,fn in [('solve',lambda:solve_ik(rig.skeleton,pose,chain,[0.,0.,0.]).converged),
                      ('animation',lambda:evaluate_animation(rig,clip,.5)['contacts'][0]['converged']),
                      ('runtime',lambda:runtime.evaluate(.5)['status'])]:
            if op == 'runtime' and not runtime:
                print('runtime unavailable on base')
                continue
            try:
                val = fn()
                assert val in (True, 'playing')
                print(op,val)
            except Exception as err:
                print(op,type(err).__name__,getattr(err,'report',{}).get('code'),str(err),'clock',runtime.time if runtime else None)
                failures += 1
                if op == 'solve':
                    import traceback
                    traceback.print_exc()
                    trace=err.__traceback__
                    while trace and trace.tb_frame.f_code.co_name != 'try_world_turn':
                        trace=trace.tb_next
                    if trace:
                        vals=trace.tb_frame.f_locals
                        rhs=vals['rotation'] @ vals['root']
                        parent=vals['parent']
                        bound=np.max(np.abs(parent))
                        normalized=np.linalg.solve(parent/bound,rhs/bound)
                        print('parent finite',np.isfinite(parent).all(),'RHS finite',np.isfinite(rhs).all(),'unscaled solution finite',np.isfinite(vals['value']).all(),'normalized solution finite',np.isfinite(normalized).all(),'normalized parent condition',np.linalg.cond(parent/bound))
                        print('unscaled solution',vals['value'][:3,:3].tolist(),'normalized solution',normalized.tolist())
    case.doCleanups()

print("Failed admitted operations",failures)
raise SystemExit(1 if failures else 0)
