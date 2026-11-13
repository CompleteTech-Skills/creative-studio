"""Package-owned Bullet collision construction, shared by simulation and queries.

This source is embedded in a hash-bound private worker. It imports only the
standard library; the caller supplies its already discovered PyBullet module
and explicit connection. No uploaded code, external URIs or user paths run.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import stat


def collision_signed_mask(value):
    if type(value) is not int or not 0 <= value <= 0xffffffff:
        raise ValueError('Collision mask is outside the unsigned 32-bit range.')
    return value if value < 0x80000000 else value - 0x100000000


def _collision_vector(value, size, bound, label):
    if not isinstance(value, (list, tuple)) or len(value) != size or any(
            type(x) not in (int, float) or not math.isfinite(x) or abs(x) > bound for x in value):
        raise ValueError('Invalid '+label+'.')
    return list(value)


def _collision_quaternion(value):
    value = _collision_vector(value, 4, 1.001, 'collision quaternion')
    norm = math.sqrt(sum(x*x for x in value))
    if abs(norm-1) > 1e-6:
        raise ValueError('Collision quaternion must be normalized.')
    return [x/norm for x in value]


def _collision_multiply(left, right):
    x,y,z,w = left
    a,b,c,d = right
    return [w*a+x*d+y*c-z*b, w*b-x*c+y*d+z*a,
            w*c+x*b-y*a+z*d, w*d-x*a-y*b-z*c]


def _collision_rotate(rotation, vector):
    x,y,z,w = rotation
    a,b,c = vector
    # q*v*q^-1, preserving double precision without a native float adapter.
    tx,ty,tz = 2*(y*c-z*b), 2*(z*a-x*c), 2*(x*b-y*a)
    return [a+w*tx+y*tz-z*ty, b+w*ty+z*tx-x*tz, c+w*tz+x*ty-y*tx]


def _collision_compose(parent, child):
    offset = _collision_rotate(parent[1], child[0])
    position = [a+b for a,b in zip(parent[0],offset)]
    rotation = _collision_quaternion(_collision_multiply(parent[1],child[1]))
    return position, rotation


def collision_leaves(collider):
    """Flatten a bounded compound into its exact local primitive frames."""
    transform = collider['transform']
    frame = (_collision_vector(transform['translation'],3,10000,'collision offset'),
             _collision_quaternion(transform['rotation']))
    leaves = []
    def visit(shape, current, depth):
        if depth > 8:
            raise ValueError('Collision compound exceeds eight nested levels.')
        if shape['type'] == 'compound':
            if not isinstance(shape['children'],list) or not 1 <= len(shape['children']) <= 64:
                raise ValueError('Collision compound child count is out of bounds.')
            for child in shape['children']:
                child_transform = child['transform']
                child_frame = (_collision_vector(child_transform['translation'],3,10000,'child offset'),
                               _collision_quaternion(child_transform['rotation']))
                visit(child['shape'],_collision_compose(current,child_frame),depth+1)
            return
        if len(leaves) >= 64:
            raise ValueError('Collision compound exceeds 64 leaves.')
        kind = shape['type']
        if kind not in ('sphere','box','capsule','cylinder','convex-hull','triangle-mesh'):
            raise ValueError('Unsupported collision shape.')
        if kind in ('capsule','cylinder'):
            # Bullet's primitive axis is local +Z. Canonical axes are explicit.
            half = math.sqrt(.5)
            axes = {'X':[0,half,0,half], 'Y':[-half,0,0,half], 'Z':[0,0,0,1]}
            if shape['axis'] not in axes:
                raise ValueError('Unsupported collision primitive axis.')
            current = _collision_compose(current,([0,0,0],axes[shape['axis']]))
        leaves.append(dict(shape=shape,translation=current[0],rotation=current[1]))
    visit(collider['shape'],frame,0)
    return leaves


def _collision_mesh_file(root, shape):
    vertices = shape['vertices']
    triangles = shape['triangles']
    if not 3 <= len(vertices) <= 4096 or not 1 <= len(triangles) <= 8192:
        raise ValueError('Collision mesh exceeds worker bounds.')
    lines = []
    for point in vertices:
        values = _collision_vector(point,3,1000,'collision vertex')
        lines.append('v '+' '.join(format(float(value),'.17g') for value in values)+'\n')
    for face in triangles:
        if len(face) != 3 or any(type(index) is not int or not 0 <= index < len(vertices) for index in face):
            raise ValueError('Invalid collision mesh triangle.')
        lines.append('f '+' '.join(str(index+1) for index in face)+'\n')
    payload = ''.join(lines).encode('ascii')
    path = Path(root)/('collision-proxy-'+hashlib.sha256(payload).hexdigest()+'.obj')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0)
    try:
        descriptor = os.open(path,flags,0o600)
    except FileExistsError:
        descriptor = os.open(path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(descriptor,'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != len(payload) or stream.read(len(payload)+1) != payload:
                raise ValueError('Collision proxy staging path is occupied by different content.')
    else:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(payload)
    return str(path)


def collision_native_types(p, collider):
    kinds = dict(sphere=p.GEOM_SPHERE,box=p.GEOM_BOX,capsule=p.GEOM_CAPSULE,
                 cylinder=p.GEOM_CYLINDER,**{'convex-hull':p.GEOM_MESH,'triangle-mesh':p.GEOM_MESH})
    return [kinds[entry['shape']['type']] for entry in collision_leaves(collider)]


def collision_build_shape(p, client, collider, root):
    """Create actual Bullet primitives/mesh children with exact local frames.

    Hulls use Bullet convex mesh construction; static triangle meshes explicitly
    request concave triangle representation. The host validates manifold/convex
    geometry, body authority, total resources and source identities beforehand.
    """
    leaves = collision_leaves(collider)
    types=[]; radii=[]; lengths=[]; half_extents=[]; files=[]; flags=[]
    for entry in leaves:
        shape = entry['shape']
        kind = shape['type']
        types.extend(collision_native_types(p,dict(shape=shape,transform=dict(translation=[0,0,0],rotation=[0,0,0,1]))))
        radius = shape.get('radius',1.)
        length = shape.get('height',1.)
        if type(radius) not in (int,float) or not math.isfinite(radius) or not .001 <= radius <= 1000:
            raise ValueError('Collision radius is out of range.')
        if type(length) not in (int,float) or not math.isfinite(length) or not .001 <= length <= 1000:
            raise ValueError('Collision height is out of range.')
        radii.append(radius); lengths.append(length)
        half = [float(x)/2 for x in shape['size']] if kind=='box' else [1,1,1]
        if len(half)!=3 or any(not math.isfinite(x) or not .0005 <= x <= 500 for x in half):
            raise ValueError('Collision box dimensions are out of range.')
        half_extents.append(half)
        files.append(_collision_mesh_file(root,shape) if kind in ('convex-hull','triangle-mesh') else '')
        flags.append(p.GEOM_FORCE_CONCAVE_TRIMESH if kind=='triangle-mesh' else 0)
    shape_id = p.createCollisionShapeArray(shapeTypes=types,radii=radii,lengths=lengths,
        halfExtents=half_extents,fileNames=files,meshScales=[[1,1,1] for _ in leaves],flags=flags,
        collisionFramePositions=[entry['translation'] for entry in leaves],
        collisionFrameOrientations=[entry['rotation'] for entry in leaves],physicsClientId=client)
    if type(shape_id) is not int or shape_id < 0:
        raise ValueError('Bullet rejected the admitted collision proxy.')
    return shape_id


def collision_raycast(p, client, start, end, mask=0xffffffff):
    start = _collision_vector(start,3,10000,'ray origin')
    end = _collision_vector(end,3,10000,'ray endpoint')
    if sum((a-b)**2 for a,b in zip(start,end)) < 1e-18:
        raise ValueError('Ray must have nonzero length.')
    result = p.rayTest(start,end,collisionFilterMask=collision_signed_mask(mask),physicsClientId=client)
    if len(result) != 1:
        raise ValueError('Unexpected Bullet ray result count.')
    body,link,fraction,point,normal = result[0]
    if body < 0:
        return dict(hit=False,body_index=None,link_index=None,fraction=1.,position=None,normal=None)
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError('Invalid Bullet ray hit fraction.')
    return dict(hit=True,body_index=body,link_index=link,fraction=fraction,
                position=_collision_vector(point,3,10000,'ray hit position'),
                normal=_collision_vector(normal,3,1.001,'ray hit normal'))


def collision_native_observations(p, client, body_id):
    """Read actual native children, omitting unreliable compound ID fields."""
    observations=[]
    for child in p.getCollisionShapeData(body_id,-1,physicsClientId=client):
        observations.append(dict(type=child[2],dimensions=list(child[3]),
            filename=child[4].decode('utf-8',errors='replace') if isinstance(child[4],bytes) else str(child[4]),
            translation=list(child[5]),rotation=list(child[6])))
    return observations


def collision_query(p, client, start, end, mask=-1):
    return collision_raycast(p,client,start,end,0xffffffff if mask == -1 else mask)


def collision_query_collider(collider):
    """Exact closed box surfaces for native rays; solver proxies stay unchanged.

    Bullet 3.2.7 zero-margin box support shapes can falsely miss rays. A static
    twelve-triangle surface preserves the declared box boundary, including local
    frames, without adding a collision skin. Other admitted proxies stay native.
    """
    result = json.loads(json.dumps(collider))
    def convert(shape):
        if shape['type'] == 'compound':
            for child in shape['children']:
                convert(child['shape'])
        elif shape['type'] == 'box':
            size = shape['size']
            vertices = [[x*size[0]/2,y*size[1]/2,z*size[2]/2] for x,y,z in
                [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]]
            triangles = [[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],
                         [3,7,6],[3,6,2],[0,4,7],[0,7,3],[1,2,6],[1,6,5]]
            shape.clear()
            shape.update(type='triangle-mesh',source='internal-query-box-surface',vertices=vertices,triangles=triangles)
    convert(result['shape'])
    return result


def collision_build_query_shapes(p, client, collider, root):
    """One native query shape per leaf, preserving double-precision box vertices.

    OBJ import rounds vertices to float32; use the native vertex API for boxes.
    Separate static query bodies allow compounds without that lossy file route.
    """
    shapes=[]
    for leaf in collision_leaves(collider):
        native_shape=json.loads(json.dumps(leaf['shape']))
        # Flattened leaf frames already contain the authored axis conversion.
        if native_shape['type'] in ('capsule','cylinder'):
            native_shape['axis']='Z'
        local=dict(shape=native_shape,transform=dict(translation=leaf['translation'],rotation=leaf['rotation']))
        if leaf['shape']['type']=='box':
            surface=collision_query_collider(local)['shape']
            shape=p.createCollisionShape(p.GEOM_MESH,vertices=surface['vertices'],
                indices=[index for triangle in surface['triangles'] for index in triangle],
                flags=p.GEOM_FORCE_CONCAVE_TRIMESH,collisionFramePosition=leaf['translation'],
                collisionFrameOrientation=leaf['rotation'],physicsClientId=client)
            if type(shape) is not int or shape<0:
                raise ValueError('Bullet rejected the exact query box surface.')
        else:
            shape=collision_build_shape(p,client,local,root)
        shapes.append(shape)
    return shapes
