"""Genuine, explicitly synthetic native Scene3D sphere/floor engineering input.

The source OBJ meshes and native manifests are independent inspectable geometry,
not prerecorded simulation. The Blender/PyBullet adapters compute all motion.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _box(size):
    x, y, z = [v/2 for v in size]
    vertices = [(-x,-y,-z),(x,-y,-z),(x,y,-z),(-x,y,-z),
                (-x,-y,z),(x,-y,z),(x,y,z),(-x,y,z)]
    triangles = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
                 (3,7,6),(3,6,2),(0,4,7),(0,7,3),(1,2,6),(1,6,5)]
    return vertices, triangles


def _sphere(radius):
    # An independently inspectable low-poly sphere surface. Collision is the
    # declared analytic sphere; its visual tessellation is explicitly coarse.
    vertices = [(radius,0,0),(-radius,0,0),(0,radius,0),(0,-radius,0),(0,0,radius),(0,0,-radius)]
    triangles = [(0,2,4),(4,2,1),(1,2,5),(5,2,0),(4,3,0),(1,3,4),(5,3,1),(0,3,5)]
    return vertices, triangles


def build_inputs(*, backend='blender', frames=49, dynamic_shape='sphere', floor_shape='box'):
    from physics_contracts import COORDINATES, PHYSICAL_UNITS, validate_contract, definitions
    from scene3d_contracts import validate_document
    from scene3d_example import node, SETTINGS, PROVENANCE
    require_frames = type(frames) is int and 25 <= frames <= 1024
    if not require_frames:
        raise ValueError('The gravity/contact fixture requires 25..1024 frames.')
    if dynamic_shape not in ('sphere','box') or floor_shape not in ('sphere','box'):
        raise ValueError('The engineering fixture implements only genuine sphere/box geometry.')
    payloads = {}
    def write(kind, identifier, **fields):
        value = dict(schema_version=1, kind='scene3d-'+kind, id=identifier, version='1.0.0', **fields)
        validate_document(value, kind)
        path = identifier+'.json'
        data = _canonical(value)
        payloads[path] = data
        return dict(id=identifier, version='1.0.0', path=path, sha256=hashlib.sha256(data).hexdigest(), kind=value['kind'])
    bodies = []
    nodes = []
    native_assets = []
    physical_assets = []
    for identifier, shape, translation, dynamic in [
        ('ball', dict(type='sphere', radius=.5) if dynamic_shape=='sphere' else dict(type='box',size=[1,1,1]), [0,3,0], True),
        ('floor', dict(type='box', size=[10,.2,10]) if floor_shape=='box' else dict(type='sphere',radius=1), [0,-.1,0] if floor_shape=='box' else [0,-1,0], False),
    ]:
        points, triangles = _sphere(shape['radius']) if shape['type'] == 'sphere' else _box(shape['size'])
        obj = '# Procedural engineering fixture; not production artwork.\n'
        obj += ''.join('v %.9g %.9g %.9g\n' % v for v in points)
        obj += ''.join('f %d %d %d\n' % tuple(v+1 for v in face) for face in triangles)
        data = obj.encode()
        path = identifier+'.obj'
        payloads[path] = data
        file = dict(path=path, sha256=hashlib.sha256(data).hexdigest())
        lower = [min(v[i] for v in points) for i in range(3)]
        upper = [max(v[i] for v in points) for i in range(3)]
        asset_id = identifier+'-asset'
        asset = write('asset', asset_id, name=identifier+' engineering mesh', asset_type='mesh',
            content=file, source=file, provenance=deepcopy(PROVENANCE),
            coordinate_system=COORDINATES, original_coordinate_system='right-handed Y-up meters',
            pivot=[0,0,0], meshes=[file], materials=[], bounds=dict(minimum=lower, maximum=upper),
            collision=None, sockets=[], components={}, skeleton=None, animations=[], lods=[], dependencies=[],
            resources=dict(vertices=len(points), triangles=len(triangles), bytes=len(data)), validation=[],
            review=dict(status='unreviewed', receipts=[]))
        native_assets.append(asset)
        physical_assets.append(dict(id=asset_id, version='1.0.0', path=path, sha256=file['sha256'], kind='geometry'))
        n = node(identifier, 'mesh_instance')
        n['asset'] = asset
        n['transform']['translation'] = translation
        nodes.append(n)
        mass = 1. if dynamic else 0.
        moment = (.4*mass*.5*.5 if dynamic_shape=='sphere' else mass/6) if dynamic else 0.
        bodies.append(dict(id=identifier, category='rigid-body', mode='dynamic' if dynamic else 'static',
            authority='physics' if dynamic else 'static', parent=None, asset=asset_id,
            transform=dict(translation=translation, rotation=[0,0,0,1]),
            mass=dict(kilograms=mass, center_of_mass=[0,0,0], inertia=[[moment,0,0],[0,moment,0],[0,0,moment]]),
            linear_velocity=[0,0,0], angular_velocity=[0,0,0], animation=None,
            colliders=[dict(id=identifier+'-collider', transform=dict(translation=[0,0,0],rotation=[0,0,0,1]),
                shape=shape, material='engineering', group=1, mask=2**32-1, sensor=False)]))
    scene = write('scene', 'physics-scene', name='Genuine primitive meshes',
        description='Native '+dynamic_shape+' and '+floor_shape+' meshes; analytic collision proxies; motion computed by a real solver.',
        revision=0, coordinate_system=COORDINATES, roots=['ball','floor'], nodes=nodes,
        world=dict(coordinate_system=COORDINATES, background=[.1,.1,.1], exposure=0, environment=None, metadata={}),
        collections=[], layers=[], cameras=[], lights=[], recipes=[], dependencies=native_assets,
        bounds=None, shots=[], continuity=[], metadata={}, provenance=deepcopy(PROVENANCE))
    write('project', 'project', name='Physics backend engineering fixture', status='fixture',
        coordinate_system=COORDINATES, asset_catalog=None, assets=native_assets, characters=[], scenes=[scene],
        shots=[], prefabs=[], animations=[], render_defaults=SETTINGS,
        budgets=dict(max_nodes=32,max_files=64,max_bytes=1024*1024,max_frames=1024),
        created_at='2026-10-24T00:00:00Z', modified_at='2026-10-24T00:00:00Z', linked_2d_project=None,
        dependency_lock=[*native_assets,scene], validation=[], provenance=deepcopy(PROVENANCE), metadata={})
    properties = definitions()['PhysicsBudgets']['properties']
    budgets = {key: (1024 if schema.get('type') == 'integer' else 120) for key, schema in properties.items()}
    budgets.update(bodies=32, dynamic_bodies=32, colliders=32, constraints=32, vertices=10000,triangles=10000,
                   output_frames=1024,steps=20000,memory_bytes=1024**3,vram_bytes=0,
                   cache_bytes=64*1024**2,bake_file_bytes=64*1024**2,temporary_bytes=64*1024**2,
                   cpu_workers=1,concurrent_jobs=1,restart_attempts=0,timeout_seconds=60,duration_seconds=60)
    world = dict(schema_version=1,kind='hatch-3d-physics-world',id='backend-engineering-fixture',version='1.0.0',
        coordinate_system=COORDINATES,physical_units=PHYSICAL_UNITS,
        scene={**scene, 'kind':'scene'}, fps=dict(numerator=24,denominator=1),frame_range=dict(start=0,end=frames),
        gravity=[0,-9.81,0], solver=dict(backend=backend,adapter_version='1.0.0',fixed_step=dict(numerator=1,denominator=240),
            substeps=1,iterations=20,seed=1,quality='preview',required_categories=['rigid-body'],reproducibility='verified-cache-replay'),
        budgets=budgets, assets=physical_assets,
        materials=[dict(id='engineering',density=1000,static_friction=.5,dynamic_friction=.5,restitution=0,
            linear_damping=0,angular_damping=0,collision_margin=0,pbr_material=None,combination='bullet-product')],
        bodies=bodies,components=[],joints=[],forces=[],events=[])
    validate_contract(world)
    return world,payloads


def write_inputs(destination, *, backend='blender', frames=49):
    from scene3d_contracts import load_project
    root=Path(destination)
    root.mkdir(parents=True,exist_ok=False)
    world,payloads=build_inputs(backend=backend,frames=frames)
    for path,payload in payloads.items():
        (root/path).write_bytes(payload)
    report=load_project(root)
    (root/'physics-world.json').write_bytes(_canonical(world))
    return world,payloads,report
