"""Admission and measured diagnostics for solver-owned mechanical joints.

Angles are radians, local anchors are rigid transforms, and ``axis`` is in
anchor A's local frame. No function here creates animation or substitutes for
native solver execution. Geometry and transforms keep their existing owners.
"""
from __future__ import annotations
import math

TYPES = ('fixed', 'hinge', 'slider', 'ball', 'point', 'spring', 'generic-6dof')


def _error(message):
    from physics_backends import BackendError
    raise BackendError('PHYSICS_JOINT_UNSUPPORTED', message, category='capability-gap')


def admit(world, backend):
    """Fail explicitly before launch for controls the native mapper cannot honor."""
    if not world['joints']:
        return
    if backend != 'blender':
        _error('This joint adapter uses Blender Bullet; DIRECT joints are not yet mapped.')
    if len(world['joints']) > 64:
        _error('At most 64 native constraints are admitted.')
    from rig_math import compose
    import numpy as np
    bodies = {item['id']: item for item in world['bodies']}
    for joint in world['joints']:
        kind = joint['type']
        if kind not in TYPES:
            _error('Unsupported joint type: ' + str(kind))
        if len(joint['id'].encode()) > 40:
            _error('Joint IDs must fit stable native constraint names (40 UTF-8 bytes).')
        if joint['stiffness'] > 1e6 or not 0 <= joint['damping'] <= 1:
            _error('Native spring stiffness is bounded to 1e6 and damping to 0..1.')
        if joint['stiffness'] and kind not in ('spring', 'generic-6dof'):
            _error('Spring stiffness only applies to spring/generic-6dof constraints.')
        if joint['damping'] and kind not in ('spring', 'generic-6dof'):
            _error('Spring damping only applies to spring/generic-6dof constraints.')
        if joint['damping'] and not joint['stiffness']:
            _error('Damping without an enabled positive-stiffness spring is unsupported.')
        if joint['motor_max_force'] or joint['motor_velocity']:
            _error('Native joint motors are not yet verified.')
        if 'break_force' in joint:
            _error('Native break force conversion is not yet verified.')
        if kind == 'hinge' and any(abs(x)>math.pi for x in joint['limits']):
            _error('Angular limits must lie within -pi..pi.')
        for key in ('linear_limits','angular_limits'):
            if key in joint and kind != 'generic-6dof':
                _error('Per-axis limits apply only to generic-6dof constraints.')
            if key in joint:
                for lo,hi in joint[key]:
                    if lo>hi or max(abs(lo),abs(hi))>(math.pi if key=='angular_limits' else 1000):
                        _error('Invalid bounded per-axis joint limits.')
        # Blender stores a single world constraint frame. Two independently
        # authored local frames must coincide before that mapping is legitimate.
        frames = []
        for suffix in ('a','b'):
            body=bodies[joint['body_'+suffix]];anchor=joint['anchor_'+suffix]
            frames.append(compose(**body['transform']) @ compose(**anchor))
        if not np.allclose(frames[0],frames[1],atol=1e-6,rtol=0):
            _error('Local anchor frames must coincide in world space at preparation.')
        if max(abs(x) for x in frames[0][:3,3]) > 10000:
            _error('Joint anchor exceeds the native spatial bound.')
        if 'break_force' in joint and joint['break_force'] > 1e9:
            _error('Break force exceeds 1e9 N.')


def measurements(world, states):
    """Measure native sampled body transforms with shared rig mathematics."""
    from rig_math import compose, decompose, qmul, qinverse, qangle, signed_twist, qmatrix
    import numpy as np
    records=[]
    for sample in states['samples']:
        bodies={item['id']:item for item in sample['bodies']}
        joints=[]
        for joint in world['joints']:
            frames=[]
            for suffix in ('a','b'):
                body=bodies[joint['body_'+suffix]]
                frames.append(compose(body['translation'],body['rotation']) @ compose(**joint['anchor_'+suffix]))
            a,b=map(decompose,frames)
            relative=qmul(qinverse(a['rotation']),b['rotation'])
            axis=np.asarray(joint['axis'],dtype=float)
            offset=qmatrix(qinverse(a['rotation'])) @ (np.asarray(b['translation'])-a['translation'])
            axial=float(np.dot(offset,axis))
            perpendicular=float(np.linalg.norm(offset-axis*axial))
            angle=float(signed_twist(relative,axis))
            joints.append(dict(id=joint['id'],anchor_separation=float(np.linalg.norm(offset)),
                axial_displacement=axial,perpendicular_separation=perpendicular,
                relative_angle=float(qangle(relative)),hinge_angle=angle))
        records.append(dict(frame=sample['frame'],joints=joints))
    return dict(schema_version=1,kind='hatch-physics-joint-measurements',
        semantics='measured native body poses; no authored constraint motion',samples=records)


def validate_native_report(value, world):
    """Verify exact admitted controls against bounded native property readback."""
    from physics_backend_evidence import need, object_fields, number
    need(type(value) is list and len(value)==len(world['joints']), 'Native joint inventory differs.')
    types=dict(fixed='FIXED',hinge='HINGE',slider='SLIDER',ball='POINT',point='POINT',spring='GENERIC_SPRING',**{'generic-6dof':'GENERIC_SPRING'})
    for observed,joint in zip(value,world['joints']):
        object_fields(observed,('id','type','body_a','body_b','anchor','axis','spring_enabled','disable_collisions','enabled','use_breaking','solver_iterations','linear_limits','angular_limits','stiffness','damping'), 'Native joint')
        need(observed['id']==joint['id'] and observed['type']==types[joint['type']] and observed['body_a']==joint['body_a'] and observed['body_b']==joint['body_b'] and observed['disable_collisions']==(not joint['collide_connected']) and observed['enabled'] is True and observed['use_breaking'] is False and observed['solver_iterations']==world['solver']['iterations'], 'Native constraint identity/settings differ.')
        from rig_math import compose
        import numpy as np
        body=next(b for b in world['bodies'] if b['id']==joint['body_a'])
        frame=compose(**body['transform']) @ compose(**joint['anchor_a'])
        for key,expected in [('anchor',frame[:3,3]),('axis',frame[:3,:3] @ np.asarray(joint['axis']))]:
            need(type(observed[key]) is list and len(observed[key])==3 and all(number(v) for v in observed[key]) and np.allclose(observed[key],expected,atol=1e-5,rtol=1e-6),'Native joint frame differs.')
        expected_spring=joint['type'] in ('spring','generic-6dof') and joint['stiffness']>0
        need(type(observed['spring_enabled']) is list and len(observed['spring_enabled'])==3 and all(type(v) is bool and v==expected_spring for v in observed['spring_enabled']),'Native spring enable flags differ.')
        for name in ('linear_limits','angular_limits'):
            rows=observed[name]
            need(type(rows) is list and len(rows)==3 and all(type(r) is list and len(r)==3 and type(r[0]) is bool and all(number(v) for v in r[1:]) for r in rows),'Malformed native limits.')
            for i,row in enumerate(rows):
                active=(joint['type']=='hinge' and name=='angular_limits' and i==2) or (joint['type']=='slider' and name=='linear_limits' and i==0) or joint['type'] in ('spring','generic-6dof')
                expected=((joint['limits'] if name=='linear_limits' and i==2 else [0,0]) if joint['type']=='spring' else joint.get(name,[[0,0]]*3)[i]) if joint['type'] in ('spring','generic-6dof') else joint['limits']
                need(row[0] is active and (not active or all(abs(a-b)<=max(1e-6,abs(b)*1e-6) for a,b in zip(row[1:],expected))), 'Native joint limit differs.')
        for name in ('stiffness','damping'):
            expected=joint[name] if joint['type'] in ('spring','generic-6dof') else 0
            need(type(observed[name]) is list and len(observed[name])==3 and all(number(v) and abs(v-expected)<=max(1e-6,abs(expected)*1e-6) for v in observed[name]),'Native spring setting differs.')
