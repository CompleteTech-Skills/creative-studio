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
    if world['components'] or world['joints'] or world['forces'] or world['events'] or not 1<=len(world['bodies'])<=32:
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
        for body in world['bodies']:
            collider=body['colliders'][0]
            shape=collider['shape']
            if shape['type']=='sphere':
                collision=p.createCollisionShape(p.GEOM_SPHERE,radius=shape['radius'],physicsClientId=client)
            elif shape['type']=='box':
                collision=p.createCollisionShape(p.GEOM_BOX,halfExtents=[v/2 for v in shape['size']],physicsClientId=client)
            else:
                raise ValueError('Unsupported primitive.')
            value=p.createMultiBody(baseMass=body['mass']['kilograms'],baseCollisionShapeIndex=collision,
                basePosition=body['transform']['translation'],baseOrientation=body['transform']['rotation'],physicsClientId=client)
            identifiers[body['id']]=value
            material=materials[collider['material']]
            p.changeDynamics(value,-1,lateralFriction=material['dynamic_friction'],restitution=material['restitution'],
                linearDamping=material['linear_damping'],angularDamping=material['angular_damping'],
                collisionMargin=material['collision_margin'],physicsClientId=client)
        reverse={v:k for k,v in identifiers.items()}
        effective=[]
        for body in world['bodies']:
            observed=p.getDynamicsInfo(identifiers[body['id']],-1,physicsClientId=client)
            effective.append(dict(id=body['id'],mass=observed[0],friction=observed[1],
                inertia=list(observed[2]),center_of_mass=list(observed[3]),restitution=observed[5],
                native_margin=observed[11],margin_semantics='inherent sphere radius' if body['colliders'][0]['shape']['type']=='sphere' else 'explicit box collision margin'))
        def state():
            values=[]
            for identifier,body_index in identifiers.items():
                translation,rotation=p.getBasePositionAndOrientation(body_index,physicsClientId=client)
                linear,angular=p.getBaseVelocity(body_index,physicsClientId=client)
                for key,velocity in [('max_linear_velocity',linear),('max_angular_velocity',angular)]:
                    if key in stability and math.sqrt(sum(v*v for v in velocity))>stability[key]:
                        failure=dict(code='PHYSICS_NUMERICAL_INSTABILITY',world_sha256=job['world_sha256'],
                            solver_step=index,solver_time=dict(numerator=(index*step).numerator,denominator=(index*step).denominator),
                            setting=key,limit=stability[key],observed=math.sqrt(sum(v*v for v in velocity)))
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
            actual_bodies=effective,effective_engine=effective_engine,
            cache_storage='immutable native transform/contact samples',restart='clean-restart-only',
            contacts_queried=True)))
    finally:
        p.disconnect(physicsClientId=client)


if __name__=='__main__':main()
