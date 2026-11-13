"""Native ray queries against verified, computed collision-cache poses."""
from pathlib import Path
import math
import tempfile

import physics_backends as physics


def raycast_cache(cache, frame, queries, *, interpreter=None, preview=False):
    """Query exact cached output poses; never claim to advance the simulation."""
    physics.require(type(frame) is int and type(preview) is bool,
                    'PHYSICS_QUERY', 'Frame must be an integer and preview a boolean.')
    physics.require(type(queries) is list and 1 <= len(queries) <= 1024,
                    'PHYSICS_QUERY', 'Supply one to 1024 explicit rays.')
    for ray in queries:
        physics.require(type(ray) is dict and set(ray) == {'start','end','mask'} and
            type(ray['mask']) is int and 0 <= ray['mask'] <= 0xffffffff and
            all(type(ray[key]) is list and len(ray[key])==3 and all(type(x) in (int,float) and math.isfinite(x) and abs(x)<=10000 for x in ray[key]) for key in ('start','end')) and
            sum((a-b)**2 for a,b in zip(ray['start'],ray['end']))>1e-18,
            'PHYSICS_QUERY','Rays require finite distinct endpoints and an unsigned 32-bit mask.')
    manifest,world,states,_=physics._read_cache(cache)
    physics.require(manifest['backend']['id']=='pybullet' and world.get('collision_profile')=='explicit-proxies-v1',
                    'PHYSICS_QUERY','Native queries require an explicit DIRECT collision-proxy cache.')
    physics.require(0 <= frame < len(states['samples']), 'PHYSICS_QUERY','Query frame is outside the verified output cache.')
    from physics_collision_native import collision_leaves
    for body in world['bodies']:
        collider=body['colliders'][0]
        if any(leaf['shape']['type'] in ('convex-hull','cylinder') for leaf in collision_leaves(collider)):
            physics.require(not any(ray['mask'] & collider['group'] for ray in queries),
                'PHYSICS_QUERY_UNSUPPORTED','Native zero-margin convex-hull/cylinder ray queries are unsupported; explicitly exclude their collision group.',category='capability-gap')

    # _read_cache re-admits and verifies the exact immutable world/source/state
    # closure. The following worker receives only that captured value, never an
    # unverified path or a second read of the potentially changing source cache.
    captured_hash=physics.digest(physics.canonical(manifest))
    module_root=Path(__file__).parent
    sources=[module_root/'physics_collision_native.py', module_root/'physics_collision_query_worker.py']
    for source in sources:
        physics.require(source.is_file() and not source.is_symlink() and 0 < source.stat().st_size <= 256*1024,
                        'PHYSICS_WORKER_TRUST','Query worker sources must be bounded package-owned files.')
    worker=b'\n'.join(source.read_bytes() for source in sources)
    worker_hash=physics.digest(worker)
    backend=physics.PyBulletBackend(interpreter)
    identity=backend._discover()
    with tempfile.TemporaryDirectory(prefix='hatch-physics-query-') as directory:
        root=Path(directory)
        physics._write(root/'worker.py',worker)
        job=dict(protocol=1,mode='collision-query',world=world,world_sha256=manifest['world_sha256'],
                 frame=frame,state=states['samples'][frame],queries=queries,preview=preview,worker_sha256=worker_hash)
        physics._write(root/'worker-job.json',physics.canonical(job))
        physics._run_process([backend.executable,'-I',str(root/'worker.py'),str(root/'worker-job.json')],root,
            timeout=min(30.,float(world['budgets']['timeout_seconds'])),memory_bytes=world['budgets']['memory_bytes'],
            output_bytes=min(world['budgets']['temporary_bytes'],16*1024*1024),label='query')
        with physics._Reader(root) as reader:
            output=physics.parse(reader.read('query-result.json',2*1024*1024))
        physics.require(type(output) is dict and set(output)=={'ok','operation','world_sha256','worker_sha256','frame','backend','results','representation','query_status','query_tolerance_m'} and output.get('ok') is True and output.get('operation')=='collision-query' and
            output.get('world_sha256')==manifest['world_sha256'] and output.get('worker_sha256')==worker_hash and type(output.get('frame')) is int and output['frame']==frame and
            output.get('representation')=='exact-box-triangle-surface-v1;native-other-proxies-v1' and output.get('backend')==backend._module_record and type(output.get('results')) is list and len(output['results'])==len(queries),
            'PHYSICS_QUERY','Native query response identity/count differs.')
        physics.require(output.get('query_status')=='validated' and output.get('query_tolerance_m')==1e-8,
                        'PHYSICS_QUERY_NUMERICAL','Native box ray tolerance produced a hit outside the declared surface; no intersection or miss is synthesized.',category='capability-gap')
        bodies={body['id'] for body in world['bodies']}
        for ray,result in zip(queries,output['results']):
            physics.require(type(result) is dict and set(result)=={'hit','body','fraction','position','normal'} and type(result.get('hit')) is bool,
                            'PHYSICS_QUERY','Malformed native query hit.')
            if result['hit']:
                physics.require(type(result.get('body')) is str and result['body'] in bodies and type(result.get('fraction')) in (int,float) and 0<=result['fraction']<=1 and
                    all(type(result.get(key)) is list and len(result[key])==3 and all(type(x) in (int,float) and math.isfinite(x) for x in result[key]) for key in ('position','normal')),
                    'PHYSICS_QUERY','Malformed native ray intersection.')
                expected=[a+(b-a)*result['fraction'] for a,b in zip(ray['start'],ray['end'])]
                physics.require(max(abs(a-b) for a,b in zip(expected,result['position']))<=1e-5 and
                    abs(sum(x*x for x in result['normal'])-1)<=1e-5,
                    'PHYSICS_QUERY','Native ray position or normal contradicts the requested segment.')
            else:
                physics.require(result.get('body') is None and result.get('fraction')==1. and result.get('position') is None and result.get('normal') is None,
                                'PHYSICS_QUERY','Malformed native ray miss.')
        output.update(cache_manifest_sha256=captured_hash,scope='native ray queries at verified cached output pose; no dynamics advancement',
                      interpreter_identity=identity)
        if preview:
            from PIL import Image
            from io import BytesIO
            raw=(root/'query-preview.ppm').read_bytes()
            physics.require(len(raw)<=320*240*3+64,'PHYSICS_QUERY','Native preview exceeds expected size.')
            with Image.open(BytesIO(raw)) as image:
                physics.require(image.format=='PPM' and image.size==(320,240),'PHYSICS_QUERY','Unexpected native preview dimensions.')
                image.load();png=BytesIO();image.save(png,format='PNG')
            output['preview_png']=png.getvalue()
        return output
