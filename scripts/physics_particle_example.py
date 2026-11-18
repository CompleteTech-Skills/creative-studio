"""Synthetic fountain and sparks; actual numerical state, no visual approval."""
from pathlib import Path
import argparse
import json

from physics_backend_example import build_inputs as native_inputs
from physics_particles import ParticleBackend, VERSION, schemas
import physics_backends as core


def build_inputs(kind='fountain', *, collision='bounce', frames=73):
    if kind not in ('fountain','sparks'):
        raise ValueError('Select fountain or sparks.')
    world,payloads=native_inputs(frames=frames)
    world['bodies']=[world['bodies'][1]]
    world['solver'].update(backend='particles-native',adapter_version=VERSION,required_categories=['particles','rigid-body'])
    world['components']=[dict(id='ball',category='particles',geometry='ball-asset',count=64,lifetime_seconds=2.5,
        seed=917,mass=.01,radius=.025,initial_velocity=[0.,4. if kind=='fountain' else 1.,0.],
        colliders=[] if collision=='none' else ['floor'])]
    world['budgets']['particles']=64
    settings=dict(schema_version=1,kind='hatch-physics-particle-settings',world_sha256=core.digest(core.canonical(world)),
        emitters=[dict(component='ball',shape='point' if kind=='fountain' else 'mesh-surface',
            rate=dict(numerator=24 if kind=='fountain' else 0,denominator=1),burst=1 if kind=='fountain' else 64,
            start=dict(numerator=0,denominator=1),end=dict(numerator=2,denominator=1),spread=.3 if kind=='fountain' else 2.,
            drag=0. if kind=='fountain' else .004,restitution=.6,collision=collision)])
    return world,settings,payloads


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination');parser.add_argument('--kind',choices=['fountain','sparks'],default='fountain')
    parser.add_argument('--collision',choices=['none','bounce','remove'],default='bounce')
    parser.add_argument('--python',help='Python interpreter with PyBullet 3.2.7 for collisions')
    args=parser.parse_args()
    world,settings,payloads=build_inputs(args.kind,collision=args.collision)
    backend=ParticleBackend(args.python);prepared=backend.prepare(world,settings,payloads)
    try:report=backend.bake(backend.simulate(prepared),args.destination)
    finally:backend.cleanup(prepared)
    print(json.dumps(report,sort_keys=True,indent=2))


if __name__=='__main__':main()
