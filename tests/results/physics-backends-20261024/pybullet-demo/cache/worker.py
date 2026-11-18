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
    fps=world['fps']['numerator']/world['fps']['denominator']
    step=world['solver']['fixed_step']['numerator']/world['solver']['fixed_step']['denominator']/world['solver']['substeps']
    per_frame=round(1/(fps*step))
    client=p.connect(p.DIRECT)
    if client<0:
        raise RuntimeError('PyBullet DIRECT connection failed.')
    try:
        p.setGravity(*world['gravity'],physicsClientId=client)
        p.setTimeStep(step,physicsClientId=client)
        p.setPhysicsEngineParameter(numSolverIterations=int(world['solver']['iterations']),
            deterministicOverlappingPairs=1,physicsClientId=client)
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
        samples=[]; contacts=[]
        for frame in range(frames):
            if frame:
                for _ in range(per_frame):p.stepSimulation(physicsClientId=client)
            bodies=[]
            for identifier,index in identifiers.items():
                translation,rotation=p.getBasePositionAndOrientation(index,physicsClientId=client)
                if not all(math.isfinite(v) for v in [*translation,*rotation]):
                    raise ValueError('Nonfinite PyBullet state.')
                bodies.append(dict(id=identifier,translation=list(translation),rotation=list(rotation)))
            values=[]
            for contact in p.getContactPoints(physicsClientId=client):
                if len(values)>=4096:raise ValueError('Contact output bound exceeded.')
                values.append(dict(body_a=reverse[contact[1]],body_b=reverse[contact[2]],
                    position_a=list(contact[5]),position_b=list(contact[6]),normal_b=list(contact[7]),
                    distance=contact[8],normal_force=contact[9]))
            contacts.append(dict(frame=frame,contacts=values))
            samples.append(dict(frame=frame,bodies=bodies))
        (root/'states.json').write_bytes(canonical(dict(schema_version=1,kind='hatch-physics-samples',
            world_sha256=job['world_sha256'],frame_range=world['frame_range'],fps=world['fps'],fixture=job['fixture'],samples=samples)))
        (root/'contacts.json').write_bytes(canonical(dict(schema_version=1,kind='hatch-pybullet-contacts',
            world_sha256=job['world_sha256'],samples=contacts)))
        (root/'worker-report.json').write_bytes(canonical(dict(ok=True,operation='simulate',
            backend=identity,connection_mode='DIRECT',sample_count=frames,object_count=len(identifiers),
            actual_steps_per_frame=per_frame,actual_solver_steps=(frames-1)*per_frame,
            actual_bodies=effective,
            cache_storage='immutable native transform/contact samples',restart='clean-restart-only',
            contacts_queried=True)))
    finally:
        p.disconnect(physicsClientId=client)


if __name__=='__main__':main()
