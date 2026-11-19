"""Canonical native folded-chain witness under a rigid oblique change of frame."""
from copy import deepcopy
import json, math
import numpy as np
from rig_animation import evaluate_animation
from rig_ik import solve_ik
from rig_math import axis_angle, qmatrix
from rig_errors import RigError
try:
    from rig_state_runtime import RigStateRuntime
except ModuleNotFoundError as error:
    if error.name != 'rig_state_runtime':
        raise
    RigStateRuntime = None
from test_rig_contact_numeric_boundaries import RigContactNumericBoundaryTests, step

fields = ('state', 'entered', 'time', 'source_pose', '_input_pose', '_source_input_pose',
          '_source_contact_correction', 'root_offset', 'transition_count')
def snapshot(runtime):
    return {k:deepcopy(getattr(runtime,k).tolist() if k == 'root_offset' else getattr(runtime,k)) for k in fields}

def run(count):
    helper = RigContactNumericBoundaryTests()
    helper.setUp()
    try:
        helper.bind_root(count+3)
        rig = helper.rig
        names = [f'b{i}' for i in range(count,count+3)]
        chain = rig.manifest['controllers']['ik'][0]
        orient = axis_angle([1.,1.,1.], math.pi/4)
        R = qmatrix(orient)
        chain.update(bones=names, solver='two-bone', pole=(R @ [0.,0.,1.]).tolist())
        tracks = [step('b0','rotation',[0.,.5],[[0.,0.,0.,1.],orient.tolist()])]
        for i in range(count+1):
            tracks.append(step(f'b{i}','translation',[0.,.5],[[0.,float(i>0),0.],[0.,0.,0.]]))
        for i in range(count):
            tracks.append(step(f'b{i}','scale',[0.,.5],[[1.]*3,[2.**29,2.**-3,2.**29]]))
        for name,sign in zip(names[1:],[1.,-1.]):
            tracks.append(step(name,'translation',[0.,.5],[[0.,1.,0.],[math.ldexp(sign,-29*count),0.,0.]]))
        goal=dict(schema_version=1,id='plant',chain=chain['id'],start=.5,end=1.,target=[0.]*3,kind='hand')
        clip=helper.install_clip(tracks,[goal],'oblique-fold')
        pose=evaluate_animation(rig,clip,.5,contacts=False)['pose']
        before=helper.assert_geometry(pose)
        parent=before[names[0]][:3,:3]
        lengths=[math.hypot(*parent[:,j]) for j in range(3)]
        unit_columns = parent / lengths
        np.testing.assert_allclose(unit_columns, R, rtol=0., atol=1e-14)
        np.testing.assert_allclose(np.linalg.det(unit_columns), 1., rtol=0., atol=1e-14)
        witness=deepcopy(pose)
        quarter=axis_angle([0.,1.,0.],-math.pi/2)
        witness['bones'][names[0]]['rotation']=quarter.tolist()
        after=helper.assert_geometry(witness)
        world_turn = R @ qmatrix(quarter) @ R.T
        for name in names:
            original = before[name][:3,:3]
            actual = after[name][:3,:3]
            norms = [math.hypot(*original[:,j]) for j in range(3)]
            np.testing.assert_allclose(actual / norms, world_turn @ (original / norms), rtol=0., atol=1e-13)
        np.testing.assert_allclose(after[names[1]][:3,3],chain['pole'],rtol=0.,atol=1e-13)
        np.testing.assert_array_equal(after[names[-1]][:3,3], [0.]*3)
        state = 'static-positive'
        actions = {'direct':lambda:solve_ik(rig.skeleton,pose,chain,[0.]*3).converged,
                   'native':lambda:evaluate_animation(rig,clip,.5)['contacts'][0]['converged']}
        if RigStateRuntime is not None:
            runtime=RigStateRuntime(rig,state,recipes={state:clip['id']},selected=[state])
            assert runtime.evaluate(.25)['status'] == 'playing'
            previous=snapshot(runtime)
            actions['runtime']=lambda:runtime.evaluate(.5)['status'] == 'playing'
        ok=True
        results={}
        for operation, call in actions.items():
            try:
                value=call()
                results[operation] = bool(value)
                ok = ok and bool(value)
            except RigError as error:
                results[operation] = error.report['code']
                if operation == 'runtime':
                    assert previous == snapshot(runtime)
                    results['rollback_fields_preserved'] = len(previous)
                ok = False
        print(json.dumps(dict(parents=count, column_lengths=lengths,
             determinant_of_normalized_columns=np.linalg.det(unit_columns),
             unscaled_svd_smallest=np.linalg.svd(parent,compute_uv=False)[-1],
             rigid_turn_witness=True, native_fk_skin_pole_closure_witness=True,
             results=results),allow_nan=False))
        return ok
    finally:
        helper.doCleanups()
with np.errstate(all='ignore'):
    results=[run(n) for n in (1,17,35)]
raise SystemExit(not all(results))
