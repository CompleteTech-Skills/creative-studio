from copy import deepcopy
import json
import math

import numpy as np
from rig_animation import evaluate_animation
from rig_ik import solve_ik
from rig_math import axis_angle
from rig_state_runtime import RigStateRuntime
import test_rig_contact_numeric_boundaries as fixtures

expected_status = []
for count in (17,35):
    helper = fixtures.RigContactNumericBoundaryTests()
    helper.setUp()
    helper.bind_root(count+3)
    rig = helper.rig
    names = [f'b{i}' for i in range(count,count+3)]
    chain = rig.manifest['controllers']['ik'][0]
    chain.update(bones=names,solver='two-bone',pole=[0.,0.,1.])
    tracks = []
    for i in range(count+1):
        tracks.append(fixtures.step(f'b{i}','translation',[0.,.5],[[0.,float(i>0),0.],[0.,0.,0.]]))
    for i in range(count):
        tracks.append(fixtures.step(f'b{i}','scale',[0.,.5],[[1.]*3,[2.**29,2.**-3,2.**29]]))
    for name,sign in zip(names[1:],[1.,-1.]):
        tracks.append(fixtures.step(name,'translation',[0.,.5],[[0.,1.,0.],[math.ldexp(sign,-29*count),0.,0.]]))
    goal=dict(schema_version=1,id='plant',chain=chain['id'],start=.5,end=1.,target=[0.]*3,kind='hand')
    clip=helper.install_clip(tracks,[goal],'fold-axial')
    with np.errstate(all='ignore'):
        pose=evaluate_animation(rig,clip,.5,contacts=False)['pose']
        world=helper.assert_geometry(pose)
        p=world[names[0]][:3,:3]
        print('finite parent diagonal',np.diag(p).tolist(),'svd',np.linalg.svd(p,compute_uv=False).tolist(),'finite positive',np.isfinite(p).all() and np.all(np.diag(p)>0))
        witness=deepcopy(pose)
        witness['bones'][names[0]]['rotation']=axis_angle([0.,1.,0.],-math.pi/2).tolist()
        after=helper.assert_geometry(witness)
        np.testing.assert_allclose(after[names[1]][:3,3],chain['pole'],atol=1e-12,rtol=0.)
        np.testing.assert_array_equal(after[names[-1]][:3,3],[0.]*3)
        print('witness skin and closed endpoint passed')
        state='static-positive'
        runtime=RigStateRuntime(rig,state,recipes={state:clip['id']},selected=[state])
        assert runtime.evaluate(.25)['status']=='playing'
        for op, fn in [('solve',lambda:solve_ik(rig.skeleton,pose,chain,[0.]*3).converged),('animation',lambda:evaluate_animation(rig,clip,.5)['final_contacts'][0]['converged']),('runtime',lambda:runtime.evaluate(.5)['status'])]:
            try:
                val=fn()
                print(count,op,val)
                expected_status.append(val in (True,'playing'))
            except Exception as err:
                print(count,op,type(err).__name__,str(err),'runtime clock',runtime.time)
                expected_status.append(False)
    helper.doCleanups()
raise SystemExit(not all(expected_status))
