"""Owned isolated query worker; geometry comes from a verified cache closure."""
import importlib.metadata
import sys


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def main():
    path = Path(sys.argv[-1])
    if path.name != 'worker-job.json' or path.is_symlink() or path.stat().st_size > 32*1024*1024:
        raise ValueError('Invalid private query job.')
    root = path.resolve().parent
    job = json.loads(path.read_bytes())
    if set(job) != {'protocol','mode','world','world_sha256','frame','state','queries','preview','worker_sha256'} or job['protocol'] != 1 or job['mode'] != 'collision-query':
        raise ValueError('Unsupported query operation.')
    if hashlib.sha256(canonical(job['world'])).hexdigest() != job['world_sha256'] or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != job['worker_sha256']:
        raise ValueError('Query source digest changed.')
    import pybullet as p
    location = Path(p.__file__).resolve()
    identity = dict(available=True,solver_version=importlib.metadata.version('pybullet'),api_version=p.getAPIVersion(),
        module_path=str(location),module_sha256=hashlib.sha256(location.read_bytes()).hexdigest(),
        python_version=sys.version.split()[0],platform=sys.platform)
    if identity['solver_version'] != '3.2.7':
        raise ValueError('Unsupported query backend version.')
    client = p.connect(p.DIRECT)
    if client < 0:
        raise RuntimeError('Query connection failed.')
    try:
        p.setPhysicsEngineParameter(collisionFilterMode=0,physicsClientId=client)
        world = job['world']
        poses = {body['id']:body for body in job['state']['bodies']}
        materials = {material['id']:material for material in world['materials']}
        identifiers = {}
        for index, body in enumerate(world['bodies']):
            collider = body['colliders'][0]
            shapes = collision_build_query_shapes(p,client,collider,root)
            visual = -1
            if job['preview']:
                leaves = collision_leaves(collider)
                kinds = dict(sphere=p.GEOM_SPHERE,box=p.GEOM_BOX,capsule=p.GEOM_CAPSULE,cylinder=p.GEOM_CYLINDER,
                    **{'convex-hull':p.GEOM_MESH,'triangle-mesh':p.GEOM_MESH})
                visual = p.createVisualShapeArray(shapeTypes=[kinds[x['shape']['type']] for x in leaves],
                    radii=[x['shape'].get('radius',1.) for x in leaves],lengths=[x['shape'].get('height',1.) for x in leaves],
                    halfExtents=[[v/2 for v in x['shape']['size']] if x['shape']['type']=='box' else [1,1,1] for x in leaves],
                    fileNames=[_collision_mesh_file(root,x['shape']) if x['shape']['type'] in ('convex-hull','triangle-mesh') else '' for x in leaves],
                    meshScales=[[1,1,1] for _ in leaves],visualFramePositions=[x['translation'] for x in leaves],
                    visualFrameOrientations=[x['rotation'] for x in leaves],
                    rgbaColors=[[.25+.15*(index%4),.35+.1*(index%3),.7,1] for _ in leaves],physicsClientId=client)
            pose = poses[body['id']]
            for shape_index, shape in enumerate(shapes):
                native = p.createMultiBody(baseMass=0,baseCollisionShapeIndex=shape,baseVisualShapeIndex=visual if shape_index==0 else -1,
                    basePosition=pose['translation'],baseOrientation=pose['rotation'],physicsClientId=client)
                p.changeDynamics(native,-1,collisionMargin=materials[collider['material']]['collision_margin'],physicsClientId=client)
                identifiers[native] = body['id']
                p.setCollisionFilterGroupMask(native,-1,collision_signed_mask(collider['group']),collision_signed_mask(collider['mask']),physicsClientId=client)
        # Synchronize broadphase AABBs for newly reconstructed static poses.
        p.performCollisionDetection(physicsClientId=client)
        results = []
        for ray in job['queries']:
            hit = collision_raycast(p,client,ray['start'],ray['end'],ray['mask'])
            results.append(dict(hit=hit['hit'],body=identifiers[hit['body_index']] if hit['hit'] else None,
                fraction=hit['fraction'],position=hit['position'],normal=hit['normal']))
        if job['preview']:
            view = p.computeViewMatrix([12,9,15],[0,1,0],[0,1,0])
            projection = p.computeProjectionMatrixFOV(55,320/240,.1,100)
            _,_,pixels,_,_ = p.getCameraImage(320,240,viewMatrix=view,projectionMatrix=projection,
                renderer=p.ER_TINY_RENDERER,physicsClientId=client)
            rgb = bytes(channel for row in pixels for pixel in row for channel in pixel[:3])
            (root/'query-preview.ppm').write_bytes(b'P6\n320 240\n255\n'+rgb)
        output = dict(ok=True,operation='collision-query',world_sha256=job['world_sha256'],
            worker_sha256=job['worker_sha256'],frame=job['frame'],backend=identity,results=results,
            representation='exact-box-triangle-surface-v1;native-other-proxies-v1')
        (root/'query-result.json').write_bytes(canonical(output))
    finally:
        p.disconnect(client)


if __name__ == '__main__':
    main()
