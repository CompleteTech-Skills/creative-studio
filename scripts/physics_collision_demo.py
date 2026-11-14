"""Build, simulate, verify and render the native collision playground."""
import argparse
from io import BytesIO
from pathlib import Path
import os

import physics_backends as physics
from physics_collision_example import build_collision_inputs
from physics_collision_query import _raycast_cache_at


def demonstrate(destination, *, interpreter=None):
    from atomic_publish import private_stage_fd,rename_no_replace_at
    target=physics._new_destination(destination)
    backend=physics.PyBulletBackend(interpreter);prepared=None
    with physics._Reader(target.parent) as parent, private_stage_fd(parent.fd,prefix='.collision-playground-') as stage_fd:
        os.mkdir('payload',mode=0o700,dir_fd=stage_fd)
        payload_fd=os.open('payload',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=stage_fd)
        cache_fd=None
        try:
            world,payloads=build_collision_inputs()
            prepared=backend.prepare(world,payloads);result=backend.simulate(prepared)
            os.mkdir('cache',mode=0o700,dir_fd=payload_fd)
            cache_fd=os.open('cache',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=payload_fd)
            backend._stage_result_at(result,cache_fd)
            captured=physics._read_cache(None,directory_fd=cache_fd)
            verification=physics._cache_verification_report(*captured)
            states=captured[2]['samples']
            # Contact diagnostics must be drawn from the same verified snapshot.
            contacts=captured[3]['_contacts']['samples']
            trajectories={body['id']:[sample['bodies'][index]['translation'] for sample in states]
                          for index,body in enumerate(world['bodies'])}
            excluded=[contact for frame in contacts for contact in frame['contacts'] if 'excluded' in (contact['body_a'],contact['body_b'])]
            physics.require(not excluded,'PHYSICS_DEMO','Excluded body unexpectedly collided.')
            physics.require(trajectories['excluded'][-1][1]<-.5,'PHYSICS_DEMO','Excluded body did not fall through the ground.')
            observed=[contact for frame in contacts for contact in frame['contacts']]
            physics.require(observed,'PHYSICS_DEMO','Playground produced no real contacts.')
            previews=[];frames=[];query_usage=[]
            from PIL import Image
            for frame in range(0,len(states),8):
                query=_raycast_cache_at(cache_fd,frame,[dict(start=[0,8,0],end=[0,-1,0],mask=1)],interpreter=interpreter,preview=True)
                payload=query.pop('preview_png');name=f'frame-{frame:04d}.png'
                physics._write_relative(payload_fd,name,payload)
                with Image.open(BytesIO(payload)) as image:frames.append(image.convert('RGB').copy())
                query_usage.append(query['worker_usage'])
                previews.append(dict(frame=frame,path=name,sha256=physics.digest(payload),query=query))
            gif=BytesIO();frames[0].save(gif,format='GIF',save_all=True,append_images=frames[1:],duration=round(1000*8/24),loop=0)
            physics._write_relative(payload_fd,'playground.gif',gif.getvalue())
            report=dict(schema_version=1,kind='physics-collision-playground',fixture=True,artistic_review='not-performed',
                verification=verification,world_sha256=result.world_sha256,backend=result.backend,usage=result.usage,
                preview_worker_usage=dict(elapsed_seconds=sum(x['elapsed_seconds'] for x in query_usage),
                    peak_memory_bytes=max(x['peak_memory_bytes'] for x in query_usage)),
                contact_count=len(observed),minimum_contact_distance=min(contact['distance'] for contact in observed),
                excluded_pair_contacts=len(excluded),excluded_final_position=trajectories['excluded'][-1],
                projectile_trajectory=trajectories['projectile'],high_speed_policy='25 m/s projectile with discrete stepping; observed contacts and tunneling retained without a CCD guarantee',
                contact_impulse_policy='instantaneous native normal force multiplied by one effective solver step; not accumulated frame impulse',
                previews=previews,source='actual Bullet rigid-body states rendered at every eighth cached output frame')
            physics._write_relative(payload_fd,'acceptance.json',physics.canonical(report))
            inventory=physics._inventory_relative(payload_fd)
            physics.require(sum(x['bytes'] for x in inventory)<=min(physics.MAX_CACHE_BYTES,world['budgets']['cache_bytes'],world['budgets']['temporary_bytes']) and
                all(x['bytes']<=world['budgets']['bake_file_bytes'] for x in inventory),
                'PHYSICS_RESOURCE_LIMIT','Collision cache and previews exceed their retained storage budgets.')
            actual={x['path']:x for x in inventory}
            physics.require(all(actual[item['path']]['sha256']==item['sha256'] for item in previews),
                'PHYSICS_DEMO','Stored preview bytes changed before publication.')
            physics._verify_cache_at(cache_fd,expected_world_sha256=result.world_sha256)
            try:rename_no_replace_at(stage_fd,'payload',parent.fd,target.name)
            except FileExistsError as exc:
                raise physics.BackendError('PHYSICS_DESTINATION_EXISTS','A destination created during staging was preserved.',category='invalid-input') from exc
            except OSError as exc:
                raise physics.BackendError('PHYSICS_PUBLICATION_UNAVAILABLE','Exclusive output publication failed safely: '+str(exc)) from exc
            return report
        finally:
            if cache_fd is not None:os.close(cache_fd)
            os.close(payload_fd)
            if prepared is not None:backend.cleanup(prepared)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    parser.add_argument('--python',dest='interpreter')
    args=parser.parse_args()
    print(physics.canonical(demonstrate(args.destination,interpreter=args.interpreter)).decode())


if __name__=='__main__':main()
