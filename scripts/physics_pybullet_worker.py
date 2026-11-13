"""Isolated optional PyBullet DIRECT worker; never imported by ordinary 2D code.

Launched with Python -I, so a scene directory or PYTHONPATH cannot supply a
replacement pybullet module. No scene strings, uploaded scripts or .blend load.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import math
from pathlib import Path
import sys
from fractions import Fraction


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def discovery():
    try:
        import pybullet as p
        version=importlib.metadata.version('pybullet')
        location=Path(p.__file__).resolve()
        value=dict(available=True,solver_version=version,api_version=p.getAPIVersion(),
                   module_path=str(location),module_sha256=hashlib.sha256(location.read_bytes()).hexdigest(),
                   python_version=sys.version.split()[0],platform=sys.platform)
        return p,value
    except (ImportError,importlib.metadata.PackageNotFoundError,OSError) as exc:
        return None,dict(available=False,message='Optional PyBullet is not installed in the isolated interpreter: '+str(exc))


def interpolate(left, right, alpha):
    """Interpolate states already computed by a solver, never create dynamics."""
    a = float(alpha)
    position = [x+(y-x)*a for x,y in zip(left['translation'],right['translation'])]
    q, r = left['rotation'], right['rotation']
    dot = sum(x*y for x,y in zip(q,r))
    if dot < 0:
        r = [-x for x in r]; dot = -dot
    dot = min(1., max(-1., dot))
    if dot > .9995:
        rotation = [x+(y-x)*a for x,y in zip(q,r)]
    else:
        theta=math.acos(dot)
        rotation=[(math.sin((1-a)*theta)*x+math.sin(a*theta)*y)/math.sin(theta) for x,y in zip(q,r)]
    norm=math.sqrt(sum(x*x for x in rotation))
    return dict(id=left['id'],translation=position,rotation=[x/norm for x in rotation])


def main():
    if sys.argv[1:] == ['--discover']:
        _,value=discovery()
        print(json.dumps(value))
        return
    path=Path(sys.argv[-1])
    if path.name!='worker-job.json' or path.is_symlink() or path.stat().st_size>32*1024*1024:
        raise ValueError('Invalid private PyBullet job.')
    root=path.resolve().parent
    job=json.loads(path.read_bytes())
    if set(job)!={'protocol','mode','world','world_sha256','meshes','worker_sha256','fixture'} or job['protocol']!=1 or job['mode']!='simulate':
        raise ValueError('Unsupported PyBullet worker operation.')
    if hashlib.sha256(canonical(job['world'])).hexdigest()!=job['world_sha256'] or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=job['worker_sha256']:
        raise ValueError('Input/worker digest changed.')
    p,identity=discovery()
    if p is None:
        raise RuntimeError(identity['message'])
    world=job['world']
    explicit_proxies=world.get('collision_profile')=='explicit-proxies-v1'
    if world['components'] or world['joints'] or world['events'] or not 1<=len(world['bodies'])<=32:
        raise ValueError('Unsupported category/force/joint/event.')
    frames=int(world['frame_range']['end'])
    if world['frame_range']['start']!=0 or not 2<=frames<=1024:
        raise ValueError('Unsupported frame range.')
    fps=Fraction(world['fps']['numerator'],world['fps']['denominator'])
    step=Fraction(world['solver']['fixed_step']['numerator'],world['solver']['fixed_step']['denominator'])/world['solver']['substeps']
    per_frame=math.ceil(1/(fps*step))
    client=p.connect(p.DIRECT)
    if client<0:
        raise RuntimeError('PyBullet DIRECT connection failed.')
    try:
        p.setGravity(*world['gravity'],physicsClientId=client)
        p.setTimeStep(float(step),physicsClientId=client)
        p.setPhysicsEngineParameter(numSolverIterations=int(world['solver']['iterations']),
            deterministicOverlappingPairs=1,physicsClientId=client)
        stability=world['solver'].get('stability',{})
        if 'contact_penetration_tolerance' in stability:
            p.setPhysicsEngineParameter(contactSlop=stability['contact_penetration_tolerance'],physicsClientId=client)
        engine=p.getPhysicsEngineParameters(physicsClientId=client)
        effective_engine=dict(fixed_time_step=engine['fixedTimeStep'],internal_substeps=engine['numSubSteps'],iterations=engine['numSolverIterations'])
        if effective_engine!=dict(fixed_time_step=float(step),internal_substeps=0,iterations=world['solver']['iterations']):
            raise ValueError('Native engine changed requested fixed timestep/iterations/substep policy.')
        materials={x['id']:x for x in world['materials']}
        identifiers={}
        inertial_frames={}
        for body in world['bodies']:
            import numpy as np
            moments,axes=np.linalg.eigh(np.asarray(body['mass']['inertia'],dtype=float))
            if np.linalg.det(axes)<0:axes[:,0]*=-1
            # Convert the proper principal-axis matrix to native XYZW rotation.
            trace=float(np.trace(axes))
            if trace>0:
                factor=math.sqrt(trace+1)*2
                inertial_rotation=[(axes[2,1]-axes[1,2])/factor,(axes[0,2]-axes[2,0])/factor,(axes[1,0]-axes[0,1])/factor,factor/4]
            else:
                i=int(np.argmax(np.diag(axes)));j,k=(i+1)%3,(i+2)%3
                factor=math.sqrt(1+axes[i,i]-axes[j,j]-axes[k,k])*2
                inertial_rotation=[0.,0.,0.,(axes[k,j]-axes[j,k])/factor]
                inertial_rotation[i]=factor/4;inertial_rotation[j]=(axes[j,i]+axes[i,j])/factor;inertial_rotation[k]=(axes[k,i]+axes[i,k])/factor
            inertial_frames[body['id']]=(moments.tolist(),inertial_rotation)
            collider=body['colliders'][0]
            shape=collider['shape']
            if explicit_proxies:
                collision=collision_build_shape(p,client,collider,root)
            elif shape['type']=='sphere':
                collision=p.createCollisionShape(p.GEOM_SPHERE,radius=shape['radius'],physicsClientId=client)
            elif shape['type']=='box':
                collision=p.createCollisionShape(p.GEOM_BOX,halfExtents=[v/2 for v in shape['size']],physicsClientId=client)
            else:
                raise ValueError('Unsupported primitive.')
            value=p.createMultiBody(baseMass=body['mass']['kilograms'],baseCollisionShapeIndex=collision,
                basePosition=body['transform']['translation'],baseOrientation=body['transform']['rotation'],
                baseInertialFramePosition=body['mass']['center_of_mass'],
                baseInertialFrameOrientation=inertial_rotation,physicsClientId=client)
            identifiers[body['id']]=value
            material=materials[collider['material']]
            p.changeDynamics(value,-1,lateralFriction=material['dynamic_friction'],restitution=material['restitution'],
                linearDamping=material['linear_damping'],angularDamping=material['angular_damping'],
                collisionMargin=material['collision_margin'],physicsClientId=client)
            if body['mode']=='dynamic':
                p.changeDynamics(value,-1,localInertiaDiagonal=moments.tolist(),physicsClientId=client)
                p.resetBaseVelocity(value,linearVelocity=body['linear_velocity'],angularVelocity=body['angular_velocity'],physicsClientId=client)
            sleep=body.get('sleep',dict(enabled=False,initially_sleeping=False))
            activation=p.ACTIVATION_STATE_ENABLE_SLEEPING if sleep['enabled'] else p.ACTIVATION_STATE_DISABLE_SLEEPING
            p.changeDynamics(value,-1,activationState=activation,physicsClientId=client)
            if sleep['initially_sleeping']:
                p.changeDynamics(value,-1,activationState=p.ACTIVATION_STATE_SLEEP,physicsClientId=client)
        if explicit_proxies:
            p.setPhysicsEngineParameter(collisionFilterMode=1,physicsClientId=client)
            for body in world['bodies']:
                collider=body['colliders'][0]
                p.setCollisionFilterGroupMask(identifiers[body['id']],-1,collision_signed_mask(collider['group']),collision_signed_mask(collider['mask']),physicsClientId=client)
        for left,right in world.get('collision_exclusions',[]):
            p.setCollisionFilterPair(identifiers[left],identifiers[right],-1,-1,enableCollision=0,physicsClientId=client)
        reverse={v:k for k,v in identifiers.items()}
        effective=[]
        for body in world['bodies']:
            observed=p.getDynamicsInfo(identifiers[body['id']],-1,physicsClientId=client)
            native_shape=p.getCollisionShapeData(identifiers[body['id']],-1,physicsClientId=client)
            proxy_wrapped=any(abs(x)>1e-10 for x in native_shape[0][5]) or abs(abs(native_shape[0][6][3])-1)>1e-10
            if not explicit_proxies and (len(native_shape)!=1 or native_shape[0][2] not in (p.GEOM_SPHERE,p.GEOM_BOX)):raise ValueError('Native primitive collision child disappeared.')
            effective.append(dict(id=body['id'],mass=observed[0],friction=observed[1],
                inertia=list(observed[2]),center_of_mass=list(observed[3]),restitution=observed[5],
                native_margin=observed[11],margin_semantics=('inherent sphere radius' if body['colliders'][0]['shape']['type']=='sphere' and not proxy_wrapped else 'explicit proxy margin') if explicit_proxies else (('transformed primitive wrapper margin' if any(observed[3]) or abs(abs(observed[4][3])-1)>1e-10 else 'inherent sphere radius') if body['colliders'][0]['shape']['type']=='sphere' else 'explicit box collision margin')))
        control_observations=[]
        for body in world['bodies']:
            value=identifiers[body['id']]
            observed=p.getDynamicsInfo(value,-1,physicsClientId=client)
            linear,angular=p.getBaseVelocity(value,physicsClientId=client)
            native_shape=p.getCollisionShapeData(value,-1,physicsClientId=client)[0]
            control_observations.append(dict(id=body['id'],mode=body['mode'],inertial_position=list(observed[3]),
                inertial_rotation=list(observed[4]),principal_inertia=list(observed[2]),
                initial_linear_velocity=list(linear),initial_angular_velocity=list(angular),
                collision=dict(type=({p.GEOM_SPHERE:'sphere',p.GEOM_BOX:'box',p.GEOM_CAPSULE:'capsule',p.GEOM_CYLINDER:'cylinder',p.GEOM_MESH:'mesh'}[native_shape[2]] if explicit_proxies else ('sphere' if native_shape[2]==p.GEOM_SPHERE else 'box')),dimensions=list(native_shape[3]),translation=list(native_shape[5]),rotation=list(native_shape[6])),
                sleep=dict(enabled=body.get('sleep',{}).get('enabled',False),initially_sleeping=body.get('sleep',{}).get('initially_sleeping',False),linear_threshold=None,angular_threshold=None)))
        if explicit_proxies:
            for body,observed in zip(world['bodies'],control_observations):
                observed['collision_children']=collision_native_observations(p,client,identifiers[body['id']])
        def body_pose(body_index):
            position,rotation=p.getBasePositionAndOrientation(body_index,physicsClientId=client)
            inertia=p.getDynamicsInfo(body_index,-1,physicsClientId=client)
            if list(inertia[3])==[0,0,0] and list(inertia[4])==[0,0,0,1]:return position,rotation
            def multiply(q,r):
                x,y,z,w=q;a,b,c,d=r
                return [w*a+x*d+y*c-z*b,w*b-x*c+y*d+z*a,w*c+x*b-y*a+z*d,w*d-x*a-y*b-z*c]
            x,y,z,w=inertia[4]
            body_rotation=multiply(rotation,[-x,-y,-z,w])
            norm=math.sqrt(sum(x*x for x in body_rotation));body_rotation=[x/norm for x in body_rotation]
            # Preserve double precision for large translated COM frames. Native
            # multiplyTransforms helpers round through their float transform API.
            shifted=multiply(multiply(body_rotation,[*inertia[3],0]),[-body_rotation[0],-body_rotation[1],-body_rotation[2],body_rotation[3]])[:3]
            return [x-y for x,y in zip(position,shifted)],body_rotation
        def motion_at(motion,time):
            coordinate=time*Fraction(motion['fps']['numerator'],motion['fps']['denominator'])
            keys=motion['keys']
            if coordinate<=keys[0]['frame']:return list(keys[0]['value'])
            for left,right in zip(keys,keys[1:]):
                if coordinate<=right['frame']:
                    if motion['interpolation']=='step':return list(right['value'] if coordinate==right['frame'] else left['value'])
                    alpha=float((coordinate-left['frame'])/(right['frame']-left['frame']))
                    return [a+(b-a)*alpha for a,b in zip(left['value'],right['value'])]
            return list(keys[-1]['value'])
        def apply_controls(index):
            start=index*step;end=start+step
            for body in world['bodies']:
                if body['mode']=='kinematic':
                    motion=job['meshes'][body['id']]['motion']
                    before=motion_at(motion,start);after=motion_at(motion,end)
                    identifier=identifiers[body['id']]
                    # Kinematic placement is explicitly authored; dynamic bodies
                    # still evolve solely through the real collision solver.
                    p.resetBasePositionAndOrientation(identifier,before,body['transform']['rotation'],physicsClientId=client)
                    p.resetBaseVelocity(identifier,linearVelocity=[(b-a)/float(step) for a,b in zip(before,after)],angularVelocity=[0,0,0],physicsClientId=client)
            for force in world['forces']:
                first=Fraction(force['frame_range']['start'],1)/fps
                last=Fraction(force['frame_range']['end'],1)/fps
                impulse=force['type'] in ('impulse','angular-impulse')
                overlap=max(Fraction(0), min(end,last)-max(start,first))
                if impulse:
                    if start!=first:continue
                    factor=1/float(step)
                else:
                    if not overlap:continue
                    factor=float(overlap/step)
                vector=[v*force['strength']*factor for v in force['vector']]
                for name in force['bodies']:
                    identifier=identifiers[name]
                    p.changeDynamics(identifier,-1,activationState=p.ACTIVATION_STATE_WAKE_UP,physicsClientId=client)
                    if force['type'] in ('torque','angular-impulse'):
                        p.applyExternalTorque(identifier,-1,vector,p.WORLD_FRAME,physicsClientId=client)
                    else:
                        point=p.getBasePositionAndOrientation(identifier,physicsClientId=client)[0] if force.get('application','world-point')=='center' else force['point']
                        p.applyExternalForce(identifier,-1,vector,point,p.WORLD_FRAME,physicsClientId=client)
        def state():
            values=[]
            for identifier,body_index in identifiers.items():
                translation,rotation=body_pose(body_index)
                linear,angular=p.getBaseVelocity(body_index,physicsClientId=client)
                for key,velocity in [('max_linear_velocity',linear),('max_angular_velocity',angular)]:
                    limit=min(stability.get(key,50.),50.)
                    if math.sqrt(sum(v*v for v in velocity))>limit:
                        failure=dict(code='PHYSICS_NUMERICAL_INSTABILITY',world_sha256=job['world_sha256'],
                            solver_step=index,solver_time=dict(numerator=(index*step).numerator,denominator=(index*step).denominator),
                            setting=key,limit=limit,observed=math.sqrt(sum(v*v for v in velocity)))
                        temporary=root/'worker-failure.json.tmp'
                        temporary.write_bytes(canonical(failure));temporary.replace(root/'worker-failure.json')
                        raise ValueError('Numerical stability velocity limit exceeded: '+key)
                if not all(math.isfinite(v) for v in [*translation,*rotation]):
                    raise ValueError('Nonfinite PyBullet state.')
                values.append(dict(id=identifier,translation=list(translation),rotation=list(rotation)))
            return values
        samples=[]; contacts=[]
        index=0
        observed_steps=[]
        left=right=state()
        solved=[dict(step=0,bodies=right)]
        for frame in range(frames):
            target=Fraction(frame,1)/fps/step
            upper=math.ceil(target)
            previous=index
            while index<upper:
                left=right
                apply_controls(index)
                p.stepSimulation(physicsClientId=client)
                index+=1
                right=state()
                solved.append(dict(step=index,bodies=right))
            alpha=target-(upper-1) if target.denominator!=1 else Fraction(1)
            bodies=right if target.denominator==1 else [interpolate(a,b,alpha) for a,b in zip(left,right)]
            values=[]
            for contact in p.getContactPoints(physicsClientId=client):
                if len(values)>=4096:raise ValueError('Contact output bound exceeded.')
                values.append(dict(body_a=reverse[contact[1]],body_b=reverse[contact[2]],
                    position_a=list(contact[5]),position_b=list(contact[6]),normal_b=list(contact[7]),
                    distance=contact[8],normal_force=contact[9]))
                if explicit_proxies:
                    values[-1].update(normal_impulse=contact[9]*float(step),
                        lateral_force_1=contact[10],lateral_direction_1=list(contact[11]),
                        lateral_force_2=contact[12],lateral_direction_2=list(contact[13]))
            observed_steps.append(index-previous)
            contacts.append(dict(frame=frame,solver_step=index,solver_time=dict(numerator=(index*step).numerator,denominator=(index*step).denominator),contacts=values))
            samples.append(dict(frame=frame,bodies=bodies))
        (root/'solver-states.json').write_bytes(canonical(dict(schema_version=1,
            kind='hatch-physics-solver-states',world_sha256=job['world_sha256'],
            effective_step=dict(numerator=step.numerator,denominator=step.denominator),samples=solved)))
        (root/'states.json').write_bytes(canonical(dict(schema_version=1,kind='hatch-physics-samples',
            world_sha256=job['world_sha256'],frame_range=world['frame_range'],fps=world['fps'],fixture=job['fixture'],samples=samples)))
        (root/'contacts.json').write_bytes(canonical(dict(schema_version=1,kind='hatch-pybullet-contacts',
            world_sha256=job['world_sha256'],samples=contacts)))
        (root/'worker-report.json').write_bytes(canonical(dict(ok=True,operation='simulate',
            backend=identity,connection_mode='DIRECT',sample_count=frames,object_count=len(identifiers),
            maximum_steps_per_output_interval=max(observed_steps),steps_per_output_interval=observed_steps,actual_solver_steps=index,
            actual_bodies=effective,effective_engine=effective_engine,rigid_controls=dict(version=1,bodies=control_observations,
                force_policy='native external force/torque per fixed step; discrete impulse is integrated wrench over one complete native step',
                forces=world['forces']),
            cache_storage='immutable native transform/contact samples',restart='clean-restart-only',
            contacts_queried=True)))
    finally:
        p.disconnect(physicsClientId=client)


if __name__=='__main__':main()
