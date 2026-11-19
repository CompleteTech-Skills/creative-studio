from copy import deepcopy
import json
import math
import numpy as np
from rig_animation import evaluate_animation, validate_animation
from rig_contracts import validate_contract
from rig_skeleton import evaluate_fk
from rig_skinning import evaluate_deformation
from rig_state_runtime import RigStateRuntime
from rig_validation import validate_rig, contact_metrics
from test_rig_runtime_clock_validation import RigRuntimeClockValidationTests

outcomes = []
for conflicting in (False, True):
    case = RigRuntimeClockValidationTests()
    case.setUp()
    try:
        clip = case.contact_clip(oriented=False)
        rig = case.rig
        for chain in rig.manifest['controllers']['ik']:
            chain['solver'] = 'two-bone'
        prior, later = clip['contacts']
        prior['target'] = [0., 2., 0.]
        later.update(start=.04, end=.06, target=[1., 1., 0.] if conflicting else prior['target'])
        validate_animation(rig, clip)
        state = 'static-positive'
        runtime = RigStateRuntime(rig, state, recipes={state:clip['id']}, selected=[state])
        runtime.evaluate(.01)
        fk_error = 0.
        for t in np.linspace(later['start'], later['end'], 7, endpoint=False):
            native = evaluate_animation(rig, clip, t)
            fk = evaluate_fk(rig.skeleton, native['pose'])
            final = fk[rig.manifest['controllers']['ik'][0]['bones'][-1]][:3,3]
            skin = evaluate_deformation(rig, native['pose'])
            assert all(np.isfinite(m['positions']).all() for m in skin.values())
            fk_error=max(fk_error,math.dist(final,prior['target']))
            assert abs(math.dist(final,prior['target'])-native['final_contacts'][0]['residual_m']) < 1e-12
        played = runtime.evaluate(.05)
        actual = contact_metrics(rig, clip)
        report = validate_rig(rig)
        validate_contract(report, 'RigValidationReport')
        json.dumps(report,allow_nan=False)
        row = next(e for e in report['checks'] if e['name']=='contacts')
        print(dict(conflicting=conflicting, actual_fk_error_m=fk_error, runtime_status=played['status'], runtime_residuals=[e['residual_m'] for e in played['contacts']], validator_error_m=actual['max_contact_error_m'], validator_drift_m=actual['max_planted_drift_m'], validator_contact=row, ok=report['ok']))
        outcomes.append((fk_error>1.) == conflicting and (played['status']=='controls-unsatisfied')==conflicting and (row['status']=='failed')==conflicting and (not report['ok'])==conflicting)
    finally:
        case.doCleanups()
raise SystemExit(0 if all(outcomes) else 1)
