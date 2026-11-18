"""Deterministic, named procedural assembly recipes in canonical Y-up meters.

These are explicit template constructions, not language inference or semantic
segmentation. Components may overlap; no boolean union, hollow lamp shade,
manufacturing clearance, texture inference or artistic approval is implied.
Only the rock recipe uses the seed to vary geometry; other recipes retain it
as reproducibility provenance without introducing hidden geometric variation.
The assembly validator owns admission and aggregate resource budgets.
"""
from __future__ import annotations

import math
import random


_NAMES = ('animal', 'arch', 'building', 'chair', 'doorway', 'lamp', 'mug',
          'rock', 'robot', 'room', 'staircase', 'sword', 'table', 'tree')


def known_recipes():
    """Return stable documented template tokens without geometry dependencies."""
    return tuple(sorted(_NAMES))


def recipe(name, *, height=2., width=1., depth=1., seed=0, material='main', steps=5):
    """Return editable part descriptions, with explicit local parent transforms.

    ``height``, ``width`` and ``depth`` set template proportions in meters.
    Not all templates exactly fill the requested envelope (round components,
    handles and decorative details can extend it); actual bounds are measured
    by the assembly owner. Stable part IDs are independent of dimensions/seed.
    Every mesh has a local origin attachment; selected assembly joints add
    authored anchors. Attachments are metadata, not constraints or a rig.
    """
    if name not in _NAMES:
        raise ValueError('Unknown assembly recipe: ' + str(name))
    # The public assembly validator also checks these values before dispatch.
    # Keep this standalone data function bounded and unambiguous as well.
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 1e-4 <= v <= 10000
           for v in (height, width, depth)):
        raise ValueError('Recipe dimensions must be finite meters in [0.0001, 10000].')
    if type(seed) is not int or not 0 <= seed <= 2**32-1:
        raise ValueError('Seed must be an integer in [0, 4294967295].')
    if type(steps) is not int or not 1 <= steps <= 64:
        raise ValueError('Staircase steps must be an integer in [1, 64].')
    h, w, d = float(height), float(width), float(depth)
    parts, centers = [], {}
    identity = [0., 0., 0., 1.]

    def add(identifier, shape, parameters, position, *, parent=None,
            scale=(1., 1., 1.), rotation=None, semantic=None, anchors=None):
        # Recipes specify world centers for clarity; persisted IR is local.
        center = tuple(float(x) for x in position)
        parent_center = centers[parent] if parent is not None else (0., 0., 0.)
        parts.append(dict(id=identifier, parent=parent, semantic=semantic or identifier,
            primitive=shape, parameters=parameters,
            transform=dict(translation=[a-b for a, b in zip(center, parent_center)],
                           rotation=list(identity if rotation is None else rotation), scale=list(scale)),
            material=material, attachments=dict(origin=[0., 0., 0.], **(anchors or {}))))
        centers[identifier] = center

    def box(identifier, size, position, **kwargs):
        add(identifier, 'box', dict(zip(('width', 'height', 'depth'), size)), position, **kwargs)

    def ball(identifier, radius, position, *, scale=(1., 1., 1.), **kwargs):
        add(identifier, 'ellipsoid', dict(radii=[radius*x for x in scale], segments=16, rings=8),
            position, **kwargs)

    def cylinder(identifier, radius, length, position, **kwargs):
        add(identifier, 'cylinder', dict(radius=radius, height=length, segments=16), position, **kwargs)

    if name == 'robot':
        box('torso', (.52*w, .34*h, .45*d), (0., .57*h, 0.),
            anchors={'neck': [0., .17*h, 0.], 'hip': [0., -.17*h, 0.]})
        box('head', (.42*w, .20*h, .40*d), (0., .85*h, 0.), parent='torso')
        cylinder('neck', .07*min(w, d), .07*h, (0., .725*h, 0.), parent='torso')
        for side, sign in (('left', -1), ('right', 1)):
            box(side+'_arm', (.13*w, .34*h, .18*d), (sign*.36*w, .56*h, 0.), parent='torso')
            ball(side+'_hand', .08, (sign*.36*w, .34*h, 0.), scale=(w, h, d), parent=side+'_arm')
            box(side+'_leg', (.16*w, .29*h, .20*d), (sign*.16*w, .255*h, 0.), parent='torso')
            box(side+'_foot', (.23*w, .10*h, .34*d), (sign*.16*w, .05*h, .06*d), parent=side+'_leg')
            ball(side+'_eye', .035, (sign*.10*w, .86*h, .21*d), scale=(w, h, d), parent='head')
        cylinder('chest_button', .055*min(w, h), .04*d, (0., .62*h, .245*d), parent='torso',
                 rotation=[math.sqrt(.5), 0., 0., math.sqrt(.5)])
    elif name == 'animal':
        ball('body', 1., (0., .48*h, 0.), scale=(.35*w, .24*h, .33*d))
        ball('head', 1., (0., .73*h, .30*d), scale=(.24*w, .18*h, .23*d), parent='body')
        ball('muzzle', 1., (0., .68*h, .48*d), scale=(.15*w, .07*h, .10*d), parent='head')
        for side, sign in (('left', -1), ('right', 1)):
            add(side+'_ear', 'cone', dict(radius=.1*w, height=.24*h, segments=16),
                (sign*.14*w, .94*h, .30*d), parent='head')
            ball(side+'_eye', 1., (sign*.10*w, .77*h, .50*d),
                 scale=(.025*w, .025*h, .025*d), parent='head')
            for end, z in (('front', .20*d), ('back', -.20*d)):
                cylinder(side+'_'+end+'_leg', .075*w, .30*h, (sign*.23*w, .19*h, z), parent='body')
                ball(side+'_'+end+'_paw', 1., (sign*.23*w, .055*h, z+.04*d),
                     scale=(.11*w, .055*h, .12*d), parent=side+'_'+end+'_leg')
        add('tail', 'cone', dict(radius=.075*w, height=.35*d, segments=16), (0., .53*h, -.45*d),
            parent='body', rotation=[-math.sqrt(.5), 0., 0., math.sqrt(.5)])
    elif name == 'mug':
        radius = .30*min(w, d)
        wall = .12*radius
        add('body', 'tube', dict(radius=radius, inner_radius=radius-wall, height=.90*h, segments=32),
            (0., .55*h, 0.), anchors={'handle_mount': [radius, 0., 0.]})
        cylinder('bottom', radius, .10*h, (0., .10*h, 0.), parent='body')
        add('handle', 'torus', dict(major_radius=.23*h, minor_radius=min(.045*h, .25*radius), segments=32, rings=12),
            (radius+.13*h, .53*h, 0.), parent='body',
            rotation=[math.sqrt(.5), 0., 0., math.sqrt(.5)])
    elif name in ('table', 'chair'):
        top_y = .92*h if name == 'table' else .48*h
        top_h = .12*h if name == 'table' else .08*h
        box('top' if name == 'table' else 'seat', (w, top_h, d), (0., top_y, 0.))
        root = 'top' if name == 'table' else 'seat'
        leg_height = top_y-top_h/2
        for side, x in (('left', -.39*w), ('right', .39*w)):
            for end, z in (('front', .39*d), ('back', -.39*d)):
                box(side+'_'+end+'_leg', (.12*w, leg_height, .12*d),
                    (x, leg_height/2, z), parent=root)
        if name == 'chair':
            box('back', (w, .48*h, .12*d), (0., .76*h, -.44*d), parent='seat')
    elif name == 'lamp':
        cylinder('base', .45*min(w, d), .08*h, (0., .04*h, 0.))
        cylinder('stem', .045*min(w, d), .65*h, (0., .405*h, 0.), parent='base')
        add('shade', 'cone', dict(radius=.4*min(w, d), height=.30*h, segments=32),
            (0., .85*h, 0.), parent='stem')
        ball('bulb', .075*min(w, d), (0., .75*h, 0.), parent='shade')
    elif name == 'sword':
        cylinder('grip', .075*min(w, d), .23*h, (0., .16*h, 0.))
        ball('pommel', .11*min(w, d), (0., .055*h, 0.), parent='grip')
        box('guard', (.85*w, .045*h, .15*d), (0., .30*h, 0.), parent='grip')
        box('blade', (.22*w, .60*h, .07*d), (0., .62*h, 0.), parent='guard')
        add('tip', 'cone', dict(radius=.11*w, height=.12*h, segments=4), (0., .98*h, 0.),
            scale=(1., 1., .32*d/w), parent='blade')
    elif name == 'tree':
        cylinder('trunk', .10*min(w, d), .64*h, (0., .32*h, 0.))
        for index, (y, radius) in enumerate(((.59, .47), (.76, .35), (.91, .22))):
            ball('foliage_'+str(index+1), 1., (0., y*h, 0.),
                 scale=(radius*w, .18*h, radius*d), parent='trunk')
    elif name == 'rock':
        rng = random.Random(seed)
        # Four ellipsoidal lobes make an irregular real 3D stone assembly.
        # They are independent intersecting meshes, never a claimed union.
        for index in range(4):
            angle = rng.uniform(-math.pi, math.pi)
            radius = .28 if index else .44
            x = rng.uniform(-.16, .16)*w if index else 0.
            z = rng.uniform(-.16, .16)*d if index else 0.
            sy = (.27+rng.random()*.13)*h
            ball('rock_core' if index == 0 else 'stone_lobe_'+str(index), 1.,
                 (x, sy, z), scale=(radius*w, sy, radius*d),
                 parent=None if index == 0 else 'rock_core',
                 rotation=[0., 0., 0., 1.] if index == 0 else [0., math.sin(angle/2), 0., math.cos(angle/2)])
    elif name in ('room', 'building'):
        thickness = .06*min(w, d, h)
        box('floor', (w, thickness, d), (0., thickness/2, 0.))
        box('back_wall', (w, h, thickness), (0., h/2, -d/2+thickness/2), parent='floor')
        for side, sign in (('left', -1), ('right', 1)):
            box(side+'_wall', (thickness, h, d), (sign*(w/2-thickness/2), h/2, 0.), parent='floor')
        if name == 'building':
            # The front is an actual opening framed by piers/lintel, not a
            # painted door or a claim of boolean subtraction.
            for side, sign in (('left', -1), ('right', 1)):
                box('front_'+side+'_pier', (.28*w, h, thickness),
                    (sign*.36*w, h/2, d/2-thickness/2), parent='floor')
            box('front_lintel', (.44*w, .25*h, thickness), (0., .875*h, d/2-thickness/2), parent='floor')
            box('roof', (w, thickness, d), (0., h+thickness/2, 0.), parent='floor')
    elif name == 'doorway':
        for side, sign in (('left', -1), ('right', 1)):
            box(side+'_jamb', (.18*w, h, d), (sign*.41*w, h/2, 0.))
        box('lintel', (.64*w, .18*h, d), (0., .91*h, 0.), parent='left_jamb')
    elif name == 'arch':
        # Wedge-shaped extrusions are assembled around an open semicircle.
        # Parent/child hierarchy and individual voussoirs remain editable.
        radius, thickness = .39*w, .14*w
        pier_h = max(.15*h, h-radius-thickness)
        for side, sign in (('left', -1), ('right', 1)):
            box(side+'_pier', (thickness, pier_h, d), (sign*radius, pier_h/2, 0.))
        segments = 12
        for index in range(segments):
            a, b = math.pi*index/segments, math.pi*(index+1)/segments
            # Extrusion's polygon is local XZ, swept along Y. Rotate it
            # -90 degrees about X to put its section into the arch's XY plane.
            outer, inner = radius+thickness/2, radius-thickness/2
            polygon = [[inner*math.cos(a), inner*math.sin(a)],
                       [outer*math.cos(a), outer*math.sin(a)],
                       [outer*math.cos(b), outer*math.sin(b)],
                       [inner*math.cos(b), inner*math.sin(b)]]
            add('voussoir_'+str(index+1), 'extrusion', dict(polygon=polygon, height=d),
                (0., pier_h, 0.), parent='left_pier', semantic='arch stone',
                rotation=[-math.sqrt(.5), 0., 0., math.sqrt(.5)])
    else:  # staircase
        step_h, step_d = h/steps, d/steps
        for index in range(steps):
            # Filled blocks provide a continuous supporting stair volume.
            box('step_'+str(index+1), (w, step_h*(index+1), step_d),
                (0., step_h*(index+1)/2, -d/2+step_d*(index+.5)),
                parent=None if index == 0 else 'step_1')
    return parts
