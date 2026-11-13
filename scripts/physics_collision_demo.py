"""Build, simulate, verify and render the native collision playground."""
import argparse
from io import BytesIO
from pathlib import Path
import shutil
import tempfile

import physics_backends as physics
from physics_collision_example import build_collision_inputs
from physics_collision_query import raycast_cache


def demonstrate(destination, *, interpreter=None):
    target=physics._new_destination(destination)
    stage=Path(tempfile.mkdtemp(prefix='.collision-playground-',dir=target.parent))
    backend=physics.PyBulletBackend(interpreter)
    prepared=None
    try:
        world,payloads=build_collision_inputs()
        prepared=backend.prepare(world,payloads)
        result=backend.simulate(prepared)
        cache=stage/'cache';backend.bake(result,cache)
        verification=physics.verify_cache(cache)
        states=physics.parse((cache/'states.json').read_bytes())['samples']
        contacts=physics.parse((cache/'contacts.json').read_bytes())['samples']
        trajectories={body['id']:[sample['bodies'][index]['translation'] for sample in states]
                      for index,body in enumerate(world['bodies'])}
        excluded=[contact for frame in contacts for contact in frame['contacts'] if 'excluded' in (contact['body_a'],contact['body_b'])]
        physics.require(not excluded,'PHYSICS_DEMO','Excluded body unexpectedly collided.')
        physics.require(trajectories['excluded'][-1][1]<-.5,'PHYSICS_DEMO','Excluded body did not fall through the ground.')
        observed=[contact for frame in contacts for contact in frame['contacts']]
        physics.require(observed,'PHYSICS_DEMO','Playground produced no real contacts.')
        previews=[];frames=[]
        from PIL import Image
        for frame in range(0,len(states),8):
            query=raycast_cache(cache,frame,[dict(start=[0,8,0],end=[0,-1,0],mask=0xffffffff)],interpreter=interpreter,preview=True)
            payload=query.pop('preview_png');name=f'frame-{frame:04d}.png'
            physics._write(stage/name,payload)
            with Image.open(BytesIO(payload)) as image:frames.append(image.convert('RGB').copy())
            previews.append(dict(frame=frame,path=name,sha256=physics.digest(payload),query=query))
        frames[0].save(stage/'playground.gif',save_all=True,append_images=frames[1:],duration=round(1000*8/24),loop=0)
        report=dict(schema_version=1,kind='physics-collision-playground',fixture=True,artistic_review='not-performed',
            verification=verification,world_sha256=result.world_sha256,backend=result.backend,usage=result.usage,
            contact_count=len(observed),minimum_contact_distance=min(contact['distance'] for contact in observed),
            excluded_pair_contacts=len(excluded),excluded_final_position=trajectories['excluded'][-1],
            projectile_trajectory=trajectories['projectile'],high_speed_policy='50 m/s projectile with discrete stepping; observed contacts and tunneling retained without a CCD guarantee',
            contact_impulse_policy='instantaneous native normal force multiplied by one effective solver step; not accumulated frame impulse',
            previews=previews,source='actual Bullet rigid-body states rendered at every eighth cached output frame')
        physics._write(stage/'acceptance.json',physics.canonical(report))
        physics._publish(stage,target)
        return report
    finally:
        if prepared is not None:backend.cleanup(prepared)
        if stage.exists():shutil.rmtree(stage)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    parser.add_argument('--python',dest='interpreter')
    args=parser.parse_args()
    print(physics.canonical(demonstrate(args.destination,interpreter=args.interpreter)).decode())


if __name__=='__main__':main()
