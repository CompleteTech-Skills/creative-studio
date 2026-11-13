"""Exercise real local mesh jobs, immutable retries and portable restoration."""
from __future__ import annotations
import argparse
from pathlib import Path

from model3d_jobs import archive_job, create_job, restore_job, run_batch, run_job
from model3d_job_cli import handler_version, native_primitive
from rig_errors import atomic_json, require, sha256
from rig_import import load_character_model


def demonstrate(destination):
    root = Path(destination).absolute()
    require(not root.exists() and not root.is_symlink(), 'MODEL3D_DEMO_EXISTS', 'Choose a new demo destination.')
    root.mkdir(parents=True)
    paths = []
    for kind in ('box', 'sphere', 'torus'):
        job = root/'jobs'/kind
        create_job(job, dict(engine='native-primitive', parameters=dict(kind=kind, parameters={},
                    name=kind, normal_mode='smooth', format='glb')))
        paths.append(job)
    version = handler_version()
    report = run_batch(paths, native_primitive, handler_version=version)
    require(report['succeeded'] == 3, 'MODEL3D_DEMO_BATCH', 'Native primitive batch failed.')
    assets = []
    for job in paths:
        before = run_job(job, lambda _:require(False, 'MODEL3D_DEMO_CACHE', 'Successful output was regenerated.'),
                         handler_version=version)
        output = job/before['current_revision']/'output/model/model.glb'
        geometry = load_character_model(output).geometry
        require(geometry.vertex_count > 0 and geometry.triangle_count > 0,
                'MODEL3D_DEMO_GEOMETRY', 'Native job did not produce real geometry.')
        archive = root/'archives'/(job.name+'.zip')
        archive_result = archive_job(job, archive)
        restored_root = root/'restored'/job.name
        restored = restore_job(archive, restored_root)
        restored_output = restored_root/restored['current_revision']/'output/model/model.glb'
        require(sha256(output) == sha256(restored_output), 'MODEL3D_DEMO_RESTORE', 'Restored mesh bytes differ.')
        assets.append(dict(name=job.name, vertices=geometry.vertex_count, triangles=geometry.triangle_count,
                           geometry_sha256=sha256(output), archive_sha256=archive_result['sha256'],
                           attempts=before['attempts'], restored=True, cached_without_reexecution=True))
    files = {path.relative_to(root).as_posix():sha256(path) for path in sorted(root.rglob('*'))
             if path.is_file() and not path.name.endswith('.lock')}
    result = dict(schema_version=1, fixture=True, provider_calls=0, handler_version=version,
                  assets=assets, files=files, review_state='visually-unreviewed',
                  scope='Native primitive local-job/ZIP lifecycle engineering evidence.',
                  not_executed=['Blender', 'browser', 'rendered appearance', 'provider inference',
                                'power-loss recovery', 'complete parent demonstrations A-M'])
    atomic_json(root/'acceptance.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    demonstrate(args.destination)
    print(Path(args.destination)/'acceptance.json')
