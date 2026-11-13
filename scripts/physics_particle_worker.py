"""Package-owned CPU particle integration worker.

No scene-provided code is evaluated. The host validates source geometry and
contracts; this worker independently bounds its loops and output. Dynamics use
exact event times and an analytic constant-force/linear-drag update between
events. This is a particle solver, not a rigid-body or fluid solver.
"""
from __future__ import annotations

from bisect import bisect_left
from fractions import Fraction
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import sys
import time


VERSION = '1.0.0'
MAX_PARTICLES = 10000
MAX_FRAMES = 1024
MAX_INTERVALS = 100000
MAX_PARTICLE_UPDATES = 2000000
MAX_CONTACT_QUERIES = 2000000
MAX_OUTPUT_BYTES = 256 * 1024 * 1024


class ParticleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ParticleError(code, message)


def rational(value):
    value = Fraction(value)
    return {'numerator': value.numerator, 'denominator': value.denominator}


def ratio(value):
    require(type(value) is dict and set(value) == {'numerator', 'denominator'} and
            type(value['numerator']) is int and type(value['denominator']) is int and
            value['denominator'] > 0, 'PARTICLE_TIME', 'Expected an exact rational time.')
    return Fraction(value['numerator'], value['denominator'])


def vector(value):
    require(type(value) is list and len(value) == 3 and all(
        type(x) in (float, int) and math.isfinite(x) and abs(x) <= 1e12 for x in value),
        'PARTICLE_VECTOR', 'Expected three bounded finite numeric coordinates.')
    return list(map(float, value))


def _positive(value, name, *, zero=False):
    require(type(value) in (int, float) and math.isfinite(value) and
            (value >= 0 if zero else value > 0) and value <= 1e12,
            'PARTICLE_SETTING', 'Invalid ' + name + '.')
    return float(value)


def _integer(value, name, maximum, *, zero=False):
    require(type(value) is int and (value >= 0 if zero else value > 0) and value <= maximum,
            'PARTICLE_RESOURCE_LIMIT', 'Invalid or excessive ' + name + '.')
    return value


def _sample_position(rng, shape, mesh):
    origin = mesh['origin']
    if shape == 'point':
        return origin[:]
    vertices = mesh['vertices']
    if shape == 'box':
        return [rng.uniform(min(p[i] for p in vertices), max(p[i] for p in vertices)) for i in range(3)]
    if shape == 'sphere':
        radius = max(math.dist(p, origin) for p in vertices)
        z = rng.uniform(-1., 1.)
        angle = rng.random() * 2 * math.pi
        radial = math.sqrt(max(0., 1-z*z))
        distance = radius * rng.random() ** (1/3)
        direction = [radial*math.cos(angle), z, radial*math.sin(angle)]
        return [origin[i] + distance*direction[i] for i in range(3)]
    require(shape == 'mesh-surface', 'PARTICLE_SHAPE', 'Unsupported emitter shape.')
    cumulative = mesh['_areas']
    index = min(bisect_left(cumulative, rng.random()*cumulative[-1]), len(cumulative)-1)
    triangle = mesh['triangles'][index]
    a, b, c = [vertices[i] for i in triangle]
    u = math.sqrt(rng.random())
    v = rng.random()
    return [(1-u)*a[i] + u*(1-v)*b[i] + u*v*c[i] for i in range(3)]


def _sample_velocity(rng, initial, spread):
    speed = math.sqrt(sum(x*x for x in initial))
    if spread == 0 or speed == 0:
        return initial[:]
    axis = [x/speed for x in initial]
    reference = [1., 0., 0.] if abs(axis[0]) < .9 else [0., 1., 0.]
    tangent = [axis[1]*reference[2]-axis[2]*reference[1],
               axis[2]*reference[0]-axis[0]*reference[2],
               axis[0]*reference[1]-axis[1]*reference[0]]
    norm = math.sqrt(sum(x*x for x in tangent))
    tangent = [x/norm for x in tangent]
    other = [axis[1]*tangent[2]-axis[2]*tangent[1],
             axis[2]*tangent[0]-axis[0]*tangent[2],
             axis[0]*tangent[1]-axis[1]*tangent[0]]
    cosine = rng.uniform(math.cos(spread), 1.)
    sine = math.sqrt(max(0., 1-cosine*cosine))
    angle = rng.random()*2*math.pi
    return [speed*(axis[i]*cosine + sine*(tangent[i]*math.cos(angle)+other[i]*math.sin(angle)))
            for i in range(3)]


def _mesh(value):
    require(type(value) is dict, 'PARTICLE_GEOMETRY', 'Missing admitted emitter geometry.')
    origin = vector(value.get('origin'))
    vertices = value.get('vertices')
    triangles = value.get('triangles')
    require(type(vertices) is list and 1 <= len(vertices) <= 100000 and
            type(triangles) is list and len(triangles) <= 200000,
            'PARTICLE_RESOURCE_LIMIT', 'Emitter mesh exceeds worker geometry limits.')
    vertices = [vector(p) for p in vertices]
    clean_triangles = []
    cumulative = []
    area = 0.
    for face in triangles:
        require(type(face) is list and len(face) == 3 and all(
            type(i) is int and 0 <= i < len(vertices) for i in face),
            'PARTICLE_GEOMETRY', 'Invalid emitter triangle.')
        a, b, c = [vertices[i] for i in face]
        ab, ac = [[p[i]-a[i] for i in range(3)] for p in (b, c)]
        cross = [ab[1]*ac[2]-ab[2]*ac[1], ab[2]*ac[0]-ab[0]*ac[2], ab[0]*ac[1]-ab[1]*ac[0]]
        weight = math.sqrt(sum(x*x for x in cross))/2
        require(math.isfinite(weight), 'PARTICLE_GEOMETRY', 'Nonfinite emitter area.')
        if weight > 0:
            area += weight
            cumulative.append(area)
            clean_triangles.append(face[:])
    return {'origin': origin, 'vertices': vertices, 'triangles': clean_triangles, '_areas': cumulative}


def _acceleration(world, component, setting, at, fixed_step):
    mass = component['mass']
    acceleration = list(world['gravity'])
    drag_rate = setting['drag']/mass
    for force in world['forces']:
        if component['id'] not in force['bodies']:
            continue
        first = Fraction(force['frame_range']['start'], 1)/ratio(world['fps'])
        last = Fraction(force['frame_range']['end'], 1)/ratio(world['fps'])
        if not first <= at < last:
            continue
        strength = force['strength']
        direction = force['vector']
        if force['type'] == 'drag':
            rate = strength/mass
            drag_rate += rate
            acceleration = [acceleration[i] + rate*direction[i] for i in range(3)]
        elif force['type'] in ('force', 'wind'):
            acceleration = [acceleration[i] + strength*direction[i]/mass for i in range(3)]
        elif force['type'] == 'turbulence':
            tick = at // fixed_step
            key = json.dumps([force['seed'], force['id'], component['id'], tick], separators=(',', ':'))
            rng = random.Random(int.from_bytes(hashlib.sha256(key.encode()).digest(), 'big'))
            acceleration = [acceleration[i] + strength*direction[i]*rng.uniform(-1., 1.)/mass for i in range(3)]
        else:
            raise ParticleError('PARTICLE_FORCE_UNSUPPORTED', 'Unsupported particle force: '+str(force['type']))
    return acceleration, drag_rate


def _integrate(particle, acceleration, drag_rate, elapsed):
    position, velocity = particle['position'], particle['velocity']
    dt = float(elapsed)
    if drag_rate == 0:
        particle['position'] = [position[i]+velocity[i]*dt+.5*acceleration[i]*dt*dt for i in range(3)]
        particle['velocity'] = [velocity[i]+acceleration[i]*dt for i in range(3)]
    else:
        decay = math.exp(-drag_rate*dt)
        integral = -math.expm1(-drag_rate*dt)/drag_rate
        terminal = [a/drag_rate for a in acceleration]
        particle['position'] = [position[i]+terminal[i]*dt+(velocity[i]-terminal[i])*integral for i in range(3)]
        particle['velocity'] = [terminal[i]+(velocity[i]-terminal[i])*decay for i in range(3)]
    require(all(math.isfinite(x) and abs(x) <= 1e8 for x in particle['position']+particle['velocity']),
            'PARTICLE_NUMERICAL_INSTABILITY', 'Particle position or velocity exceeded the finite solver bound.')


class _Contacts:
    """Discrete finite-radius overlap contacts against static native geometry.

    Positions are integrated by the particle solver; Bullet computes actual
    closest features, penetration and normals. There is no swept collision,
    rigid-body response, particle/particle contact or two-way force transfer.
    """
    def __init__(self, job, settings, components):
        self.p = None
        self.client = None
        self.bodies = {}
        self.spheres = {}
        self.queries = 0
        self.count = 0
        self.pairs = set()
        self.identity = None
        enabled = any(value['collision'] != 'none' for value in settings.values())
        if not enabled:
            return
        try:
            version = importlib.metadata.version('pybullet')
            require(version == '3.2.7', 'PARTICLE_COLLISION_VERSION',
                    'Collision support requires the admitted PyBullet 3.2.7 distribution.')
            import pybullet as p
            # The host stages the exact package helper beside this worker.
            # -I deliberately excludes the current directory from sys.path.
            helper_path = Path(__file__).resolve().with_name('physics_collision_native.py')
            require(helper_path.is_file() and not helper_path.is_symlink(),
                    'PARTICLE_COLLISION_HELPER', 'Missing trusted collision helper.')
            spec = importlib.util.spec_from_file_location('_particle_collision_native', helper_path)
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            self.p = p
            self.client = p.connect(p.DIRECT)
            require(type(self.client) is int and self.client >= 0, 'PARTICLE_COLLISION_BACKEND',
                    'Cannot open an isolated Bullet DIRECT connection.')
            body_records = job.get('colliders', [])
            require(type(body_records) is list and 1 <= len(body_records) <= 32,
                    'PARTICLE_COLLISION_BODIES', 'Collision jobs require 1..32 admitted static bodies.')
            root = Path(__file__).resolve().parent
            for body in body_records:
                identifier = body['id']
                require(type(identifier) is str and identifier not in self.bodies and
                        body['mode'] == 'static' and body['authority'] == 'static' and
                        body['parent'] is None and body['animation'] is None and len(body['colliders']) == 1,
                        'PARTICLE_COLLISION_BODIES', 'Only uniquely identified static single-collider bodies are supported.')
                collider = body['colliders'][0]
                require(collider['group'] == 1 and collider['mask'] == 0xffffffff and not collider['sensor'],
                        'PARTICLE_COLLISION_FILTER', 'Particle contacts currently require default nonsensor filters.')
                shape = helper.collision_build_shape(p, self.client, collider, root)
                translation = vector(body['transform']['translation'])
                rotation = body['transform']['rotation']
                require(type(rotation) is list and len(rotation) == 4 and all(
                    type(x) in (int,float) and math.isfinite(x) for x in rotation) and
                    abs(sum(x*x for x in rotation)-1) < 1e-6,
                    'PARTICLE_COLLISION_BODIES', 'Collider rotation must be a unit XYZW quaternion.')
                native = p.createMultiBody(baseMass=0,baseCollisionShapeIndex=shape,
                    basePosition=translation,baseOrientation=rotation,physicsClientId=self.client)
                require(type(native) is int and native >= 0, 'PARTICLE_COLLISION_BACKEND',
                        'Bullet rejected a static collision body.')
                self.bodies[identifier] = native
            requested = {body for key,setting in settings.items() if setting['collision'] != 'none'
                         for body in components[key]['colliders']}
            require(requested == set(self.bodies), 'PARTICLE_COLLISION_BODIES',
                    'Collision payload must exactly match referenced static bodies.')
            binary = Path(p.__file__).resolve()
            hasher = hashlib.sha256()
            with binary.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    hasher.update(chunk)
            self.identity = {'distribution':'pybullet','version':version,'api_version':p.getAPIVersion(),
                             'binary_sha256':hasher.hexdigest(),'helper_sha256':hashlib.sha256(helper_path.read_bytes()).hexdigest(),
                             'mode':'DIRECT','device':'cpu'}
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.p is not None and self.client is not None:
            try:
                self.p.disconnect(physicsClientId=self.client)
            finally:
                self.client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def resolve(self, particle, component, setting, at):
        if setting['collision'] == 'none':
            return [], False
        p = self.p
        radius = particle['radius']
        require(0 < radius <= 1000, 'PARTICLE_COLLISION_RADIUS', 'Particle radius exceeds native bounds.')
        if radius not in self.spheres:
            shape = p.createCollisionShape(p.GEOM_SPHERE,radius=radius,physicsClientId=self.client)
            sphere = p.createMultiBody(baseMass=0,baseCollisionShapeIndex=shape,
                basePosition=particle['position'],physicsClientId=self.client)
            require(type(sphere) is int and sphere >= 0, 'PARTICLE_COLLISION_BACKEND',
                    'Bullet rejected particle query sphere.')
            self.spheres[radius] = sphere
        sphere = self.spheres[radius]
        events = []
        for pass_index in range(4):
            penetrated = False
            for identifier in component['colliders']:
                self.queries += 1
                require(self.queries <= MAX_CONTACT_QUERIES, 'PARTICLE_RESOURCE_LIMIT',
                        'Particle collision query budget exceeded.')
                p.resetBasePositionAndOrientation(sphere,particle['position'],[0,0,0,1],physicsClientId=self.client)
                contacts = p.getClosestPoints(sphere,self.bodies[identifier],distance=0,physicsClientId=self.client)
                if not contacts:
                    continue
                contact = min(contacts,key=lambda value:value[8])
                distance = contact[8]
                normal = vector(list(contact[7]))
                require(math.isfinite(distance) and distance <= 1e-9 and
                        abs(sum(x*x for x in normal)-1) <= 1e-5,
                        'PARTICLE_COLLISION_BACKEND', 'Bullet returned invalid contact geometry.')
                # Tiny numerical residuals are accepted after an earlier pass;
                # fresh overlaps still resolve even when depth is very small.
                if pass_index and distance >= -1e-7:
                    continue
                penetrated = distance < -1e-7 or penetrated
                self.count += 1
                self.pairs.add((particle['id'],identifier))
                events.append({'type':'collision','particle':particle['id'],'component':particle['component'],
                    'body':identifier,'time':rational(at),'normal':normal,'penetration':max(0.,-distance)})
                if setting['collision'] == 'remove':
                    return events, True
                particle['position'] = [particle['position'][i]+normal[i]*(-distance+1e-9) for i in range(3)]
                inward = sum(particle['velocity'][i]*normal[i] for i in range(3))
                if inward < 0:
                    particle['velocity'] = [particle['velocity'][i]-(1+setting['restitution'])*inward*normal[i]
                                            for i in range(3)]
            if not penetrated:
                break
        else:
            raise ParticleError('PARTICLE_COLLISION_UNRESOLVED',
                                'Static contacts did not settle within four bounded projection passes.')
        return events, False


def run(job, *, cancel=None):
    """Compute real particle evolution with optional isolated native contact queries.

    Time intervals are subdivisions of the requested fixed grid at exact birth,
    death, force and output events. ``fixed_max_step`` is a maximum, not a claim
    that every numerical interval has that length. Drag is kg/s; force/wind and
    seeded turbulence are Newtons. World gravity is acceleration in m/s².
    """
    require(type(job) is dict, 'PARTICLE_JOB', 'Expected a worker job object.')
    world, settings = job['world'], job['settings']
    components = {c['id']: dict(c) for c in world['components']}
    require(1 <= len(components) <= 64 and len(components) == len(world['components']),
            'PARTICLE_RESOURCE_LIMIT', 'Invalid component inventory.')
    fps = ratio(world['fps'])
    step = ratio(world['solver']['fixed_step'])/_integer(world['solver']['substeps'], 'substeps', 1024)
    require(fps > 0 and step > 0, 'PARTICLE_TIME', 'Rates must be positive.')
    first, stop = world['frame_range']['start'], world['frame_range']['end']
    require(type(first) is int and first == 0 and type(stop) is int and 2 <= stop <= MAX_FRAMES,
            'PARTICLE_TIME', 'Particle output must use a zero-origin bounded frame range.')
    terminal = Fraction(stop-1, 1)/fps
    ticks = math.ceil(terminal/step)
    require(ticks <= min(MAX_INTERVALS, world['budgets']['steps']), 'PARTICLE_RESOURCE_LIMIT',
            'Requested fixed grid exceeds the step budget.')
    cap = min(MAX_PARTICLES, world['budgets']['particles'])
    require(sum(_integer(c['count'], 'component count', MAX_PARTICLES) for c in components.values()) <= cap,
            'PARTICLE_RESOURCE_LIMIT', 'Total possible births exceed the particle budget.')
    require(not world.get('joints') and not world.get('events'), 'PARTICLE_CAPABILITY',
            'Particle joints and scene interaction events are unsupported.')
    require(cancel is None or callable(cancel), 'PARTICLE_JOB', 'Invalid cancellation callback.')
    deadline = time.monotonic() + _positive(world['budgets']['timeout_seconds'], 'timeout')
    byte_cap = min(MAX_OUTPUT_BYTES, world['budgets']['cache_bytes'], world['budgets']['temporary_bytes'])
    require(byte_cap > 0, 'PARTICLE_RESOURCE_LIMIT', 'Output budget must be positive.')
    setting_by_id = {}
    meshes = {}
    generators = {}
    schedule = {}
    death_schedule = {}
    capped = []
    for setting in settings['emitters']:
        identifier = setting['component']
        require(identifier in components and identifier not in setting_by_id,
                'PARTICLE_SETTING', 'Emitter settings must uniquely reference particle components.')
        component = components[identifier]
        require(component['category'] == 'particles', 'PARTICLE_CAPABILITY', 'Only particle components are supported.')
        require(setting['collision'] in ('none', 'bounce', 'remove') and
                ((setting['collision'] == 'none') == (not component['colliders'])),
                'PARTICLE_COLLISION_UNSUPPORTED', 'Collision mode and explicit body references must agree.')
        component['mass'] = _positive(component['mass'], 'particle mass')
        component['radius'] = _positive(component['radius'], 'particle radius')
        component['initial_velocity'] = vector(component['initial_velocity'])
        lifetime = Fraction(str(_positive(component['lifetime_seconds'], 'particle lifetime')))
        _integer(component['seed'], 'emitter seed', 2**31-1, zero=True)
        spread = _positive(setting['spread'], 'spread', zero=True)
        require(spread <= math.pi, 'PARTICLE_SETTING', 'Spread must not exceed pi radians.')
        setting = dict(setting, spread=spread, drag=_positive(setting['drag'], 'drag', zero=True))
        _positive(setting['restitution'], 'restitution', zero=True)
        require(setting['restitution'] <= 1, 'PARTICLE_SETTING', 'Restitution exceeds one.')
        rate, start, end = ratio(setting['rate']), ratio(setting['start']), ratio(setting['end'])
        require(rate >= 0 and 0 <= start < end, 'PARTICLE_TIME', 'Invalid emission interval or rate.')
        burst = _integer(setting['burst'], 'burst', MAX_PARTICLES, zero=True)
        mesh = _mesh(job['emitters'][identifier])
        require(setting['shape'] in ('point', 'box', 'sphere', 'mesh-surface'), 'PARTICLE_SHAPE',
                'Unknown emitter shape.')
        require(setting['shape'] != 'mesh-surface' or mesh['_areas'], 'PARTICLE_GEOMETRY',
                'Mesh-surface emission requires nonzero triangle area.')
        meshes[identifier] = mesh
        setting_by_id[identifier] = setting
        generators[identifier] = random.Random(component['seed'])
        limit = component['count']
        birth_times = [start]*min(burst, limit) if start <= terminal else []
        remaining = limit-len(birth_times)
        if rate and remaining and start < terminal:
            # End is half-open; rate emissions start one period after start.
            possible = max(0, math.ceil((min(end, terminal+step)-start)*rate)-1)
            possible = min(possible, math.floor((terminal-start)*rate))
            birth_times.extend(start+Fraction(i,1)/rate for i in range(1, min(remaining,possible)+1))
            if possible > remaining:
                capped.append(identifier)
        if burst > limit:
            capped.append(identifier)
        for index, born in enumerate(birth_times):
            if born >= end or born > terminal:
                continue
            particle_id = identifier+':'+str(index)
            schedule.setdefault(born, []).append((particle_id, identifier))
            if born+lifetime <= terminal:
                death_schedule.setdefault(born+lifetime, []).append(particle_id)
        component['_lifetime'] = lifetime
    require(set(setting_by_id) == set(components), 'PARTICLE_SETTING', 'Every component requires emitter settings.')
    boundaries = {Fraction(i,1)*step for i in range(ticks+1) if i*step <= terminal}
    frame_times = {Fraction(i,1)/fps: i for i in range(stop)}
    boundaries.update(frame_times)
    boundaries.update(schedule)
    boundaries.update(death_schedule)
    for force in world['forces']:
        require(force['type'] in ('force','wind','drag','turbulence'), 'PARTICLE_FORCE_UNSUPPORTED',
                'Unsupported particle force category.')
        vector(force['vector'])
        _positive(force['strength'], 'force strength', zero=True)
        require(set(force['bodies']) <= set(components), 'PARTICLE_FORCE_UNSUPPORTED',
                'Forces may target only admitted particle components.')
        for frame in force['frame_range'].values():
            at = Fraction(frame,1)/fps
            if 0 <= at <= terminal:
                boundaries.add(at)
    ordered = sorted(boundaries)
    require(len(ordered) <= MAX_INTERVALS and len(ordered)-1 <= world['budgets']['steps'],
            'PARTICLE_RESOURCE_LIMIT', 'Event subdivisions exceed configured step or worker bound.')
    require(len(ordered)*cap <= MAX_PARTICLE_UPDATES, 'PARTICLE_RESOURCE_LIMIT',
            'Worst-case particle updates exceed the worker operation budget.')
    active = {}
    samples = []
    events = []
    previous = Fraction(0)
    peak = updates = 0
    max_interval = Fraction(0)
    encoded_bytes = 0
    started = time.monotonic()
    with _Contacts(job, setting_by_id, components) as contacts:
        for at in ordered:
            require(time.monotonic() < deadline, 'PARTICLE_TIMEOUT', 'Particle worker exceeded wall-clock budget.')
            require(cancel is None or not cancel(), 'PARTICLE_CANCELLED', 'Particle computation was cancelled.')
            elapsed = at-previous
            require(0 <= elapsed <= step, 'PARTICLE_TIME', 'Numerical interval exceeded fixed maximum step.')
            max_interval = max(max_interval, elapsed)
            for particle in list(active.values()):
                identifier = particle['component']
                acceleration, drag = _acceleration(world, components[identifier], setting_by_id[identifier], previous, step)
                _integrate(particle, acceleration, drag, elapsed)
                updates += 1
                contact_events, removed = contacts.resolve(particle, components[identifier], setting_by_id[identifier], at)
                events.extend(contact_events)
                if removed:
                    del active[particle['id']]
                    events.append({'type':'death','id':particle['id'],'component':identifier,
                                   'time':rational(at),'reason':'collision'})
            for particle_id in death_schedule.get(at, []):
                particle = active.pop(particle_id, None)
                if particle is not None:
                    events.append({'type':'death','id':particle_id,'component':particle['component'],
                                   'time':rational(at),'reason':'lifetime'})
            for particle_id, identifier in schedule.get(at, []):
                component = components[identifier]
                rng = generators[identifier]
                setting = setting_by_id[identifier]
                active[particle_id] = {'id':particle_id,'component':identifier,
                    'position':_sample_position(rng,setting['shape'],meshes[identifier]),
                    'velocity':_sample_velocity(rng,component['initial_velocity'],setting['spread']),
                    'radius':component['radius'],'mass':component['mass'],
                    'birth_time':rational(at),'_birth':at}
                events.append({'type':'birth','id':particle_id,'component':identifier,'time':rational(at)})
                contact_events, removed = contacts.resolve(active[particle_id], component, setting, at)
                events.extend(contact_events)
                if removed:
                    del active[particle_id]
                    events.append({'type':'death','id':particle_id,'component':identifier,
                                   'time':rational(at),'reason':'collision'})
            peak = max(peak,len(active))
            if at in frame_times:
                sample = {'frame':frame_times[at],'time':rational(at),'particles':[
                    {**{key:value for key,value in particle.items() if key!='_birth'},
                     'position':particle['position'][:],'velocity':particle['velocity'][:],
                     'age':float(at-particle['_birth'])}
                    for particle in sorted(active.values(),key=lambda value:value['id'])]}
                encoded_bytes += len(json.dumps(sample, separators=(',', ':'), allow_nan=False).encode())
                require(encoded_bytes <= byte_cap//2, 'PARTICLE_RESOURCE_LIMIT', 'Particle state cache exceeds output budget.')
                samples.append(sample)
            previous = at
    particles = {'schema_version':1,'kind':'hatch-particle-states','fps':world['fps'],
                 'frame_range':world['frame_range'],'samples':samples}
    event_result = {'schema_version':1,'kind':'hatch-particle-events','events':events}
    report = {'ok':True,'solver':'hatch-particles-cpu','version':VERSION,'python':sys.version.split()[0],'device':'cpu','precision':'float64',
        'method':'analytic constant force and linear drag between exact events',
        'force_units':'force/wind/turbulence newtons; drag kg/s; world gravity m/s^2',
        'fixed_max_step':rational(step),'actual_substep_count':max(0,len(ordered)-1),
        'max_observed_step':rational(max_interval),'particle_updates':updates,
        'births':sum(event['type']=='birth' for event in events),
        'deaths':sum(event['type']=='death' for event in events),'peak_particles':peak,
        'emission_capped':sorted(set(capped)),
        'collision':{'method':'discrete sphere/static overlap projection; no CCD',
                     'backend':contacts.identity,'queries':contacts.queries,'contacts':contacts.count,
                     'pairs':[list(pair) for pair in sorted(contacts.pairs)]},
        'elapsed_seconds':time.monotonic()-started,
        'reproduction':'same Python implementation and inputs; no cross-platform bitwise guarantee',
        'subdivision_policy':'fixed grid split at births, deaths, force boundaries and exact output times',
        'rate_policy':'burst at start; regular births one period after start; end excluded; count caps lifetime births'}
    total = sum(len(json.dumps(value,separators=(',', ':'),allow_nan=False).encode())
                for value in (particles,event_result,report))
    require(total <= byte_cap, 'PARTICLE_RESOURCE_LIMIT', 'Combined worker output exceeds byte budget.')
    return {'particles':particles,'events':event_result,'report':report}


def export_obj(sample):
    """Generate actual octahedra from evaluated particle positions and radii."""
    rows = ['# Evaluated CPU particle geometry; octahedral display surfaces, not a fluid mesh.']
    directions = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]
    faces = [(0,2,4),(4,2,1),(1,2,5),(5,2,0),(4,3,0),(1,3,4),(5,3,1),(0,3,5)]
    for index,particle in enumerate(sample['particles']):
        center = vector(particle['position'])
        radius = _positive(particle['radius'],'display radius')
        rows.append('o particle_'+str(index))
        for direction in directions:
            rows.append('v '+' '.join(format(center[i]+radius*direction[i],'.17g') for i in range(3)))
        for face in faces:
            rows.append('f '+' '.join(str(index*6+i+1) for i in face))
    return ('\n'.join(rows)+'\n').encode('ascii')


def _pairs(items):
    result = {}
    for key,value in items:
        require(key not in result,'PARTICLE_JOB','Duplicate JSON key.')
        result[key] = value
    return result


def _write(name,value):
    payload = json.dumps(value,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()
    fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(payload)


def main():
    try:
        require(len(sys.argv)==2,'PARTICLE_JOB','Pass one trusted worker-job.json path.')
        path = Path(sys.argv[1])
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 32*1024*1024,
                'PARTICLE_JOB','Worker job must be a bounded regular file.')
        job = json.loads(path.read_bytes(),object_pairs_hook=_pairs,
                         parse_constant=lambda value:(_ for _ in ()).throw(ParticleError('PARTICLE_JOB','Nonfinite JSON constant.')))
        result = run(job)
        for key in ('particles','events','report'):
            _write(key+'.json',result[key])
        return 0
    except (ParticleError,ValueError,TypeError,KeyError,OverflowError,RecursionError,OSError) as exc:
        print(json.dumps({'ok':False,'code':getattr(exc,'code','PARTICLE_WORKER_FAILURE'),'message':str(exc)[:2048]}),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
