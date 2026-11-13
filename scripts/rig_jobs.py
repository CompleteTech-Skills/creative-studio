"""Transactional local rig runs, immutable revisions and bounded resumable steps."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import uuid

from atomic_publish import _rename_no_replace

from rig_contracts import COORDINATES, DEFAULT_LIMITS, validate_contract
from rig_errors import (VERSION, RigError, atomic_bytes, atomic_json, job_lock,
                        object_hash, read_json, require, safe_path, sha256, slug, timestamp)
from rig_models import RigAsset, load_rig, rig_signature, save_geometry, save_weights


def provenance(operation, inputs=None, ancestry=None):
    return dict(schema_version=1, tool_version=VERSION, operation=operation,
                created_at=timestamp(), input_hashes=inputs or {}, ancestry=ancestry or [])


def next_version(version):
    a, b, c = map(int, version.split('.'));return f'{a}.{b}.{c+1}'


def save_rig(rig: RigAsset, *, bump=True, operation='save') -> dict:
    """Write immutable blobs first and atomically promote the one active manifest last."""
    require(rig.root is not None, 'RIG_RUN', 'The rig needs a run directory.')
    root = Path(rig.root);old = read_json(root/'rig.json') if (root/'rig.json').exists() else None
    manifest = deepcopy(rig.manifest)
    if old and bump:
        manifest['version'] = next_version(old['version']);manifest['revision'] = old['revision']+1
        manifest['review'] = dict(status='pending', receipts=[]);manifest['validation'] = []
        manifest['provenance'] = provenance(operation, {'previous': rig_signature(old)},
                                            [*old['provenance']['ancestry'], rig_signature(old)][-1000:])
    manifest['geometry'] = save_geometry(root, rig.geometry, manifest['limits'])
    if old:
        manifest['geometry']['version']=(next_version(old['geometry']['version']) if
            manifest['geometry']['data']['sha256']!=old['geometry']['data']['sha256'] else old['geometry']['version'])
    manifest['skins'] = [save_weights(root, w, manifest['version']) for _, w in sorted(rig.weights.items())]
    manifest['animations'] = []
    for key, clip in sorted(rig.clips.items()):
        require(key == clip['id'], 'RIG_ANIMATION_ID', 'Clip dictionary key differs from its identifier.')
        validate_contract(clip, 'AnimationClip')
        digest = object_hash(clip);name = f'animations/{slug(key)}-{digest}.json'
        target = safe_path(root, name)
        if not target.exists():atomic_json(target, clip)
        manifest['animations'].append(dict(id=key, version=clip['version'], path=name, sha256=sha256(target)))
    validate_contract(manifest, 'Rig')
    if old:
        snapshot = f'rig/revisions/{old["revision"]:06d}-{rig_signature(old)}.json'
        target = safe_path(root, snapshot)
        if target.exists():require(read_json(target) == old, 'RIG_REVISION_COLLISION', 'Immutable revision was modified.')
        else:atomic_json(target, old)
    atomic_json(root/'rig.json', manifest)
    rig.manifest = manifest
    if (root/'job.json').exists():
        job=read_json(root/'job.json')
        if not any(s['status']=='running' for s in job['steps'].values()):
            job['rig_sha256']=rig_signature(manifest);job['limits']=manifest['limits']
            job['status']=('animation-ready' if rig.clips and rig.weights else 'weights-generated' if rig.weights
                           else 'needs-correction' if any(u['resolution']=='pending' for u in manifest['uncertainties'])
                           else 'skeleton-generated' if manifest['skeleton'] else 'imported')
            for step in job['steps'].values():
                if step['output_sha256']!=job['rig_sha256'] and step['status']=='succeeded':step['status']='stale'
            job['history'].append(dict(operation=operation,at=timestamp(),signature=job['rig_sha256']))
            job['history']=job['history'][-10000:];atomic_json(root/'job.json',validate_contract(job,'RigJob'))
    return manifest


def create_run(model, root, *, identifier=None, scale=1., limits=None, fixture=False,
               origin='import', attribution='User-supplied model; see original source files.', blender=None) -> RigAsset:
    """Publish a validated run whose geometry and provenance share frozen bytes."""
    from rig_source_snapshot import import_snapshot
    root = Path(root).absolute();model = Path(model).absolute()
    require(not root.is_symlink(), 'RIG_RUN_PATH', 'Run directories cannot be symbolic links.')
    limits = {**DEFAULT_LIMITS, **(limits or {})};validate_contract(limits, 'Limits')
    require(limits['influences'] in (4, 8), 'RIG_INFLUENCES', 'Choose four or eight influences.')
    with job_lock(root):
        require(not root.exists() and not root.is_symlink(), 'RIG_RUN_EXISTS',
                'Import into a new destination; existing directories are preserved.', preserved=str(root))
        with import_snapshot(model, scale=scale, limits=limits, blender=blender) as snapshot:
            imported = snapshot.model
            fixture = bool(fixture or imported.report.get('fixture'))
            if fixture:origin = 'procedural-fixture'
            identifier = slug(identifier or model.stem)
            records = {record['path']: record for record in snapshot.source_records}
            entry = snapshot.provenance['source_entry']
            require(entry in records, 'RIG_SOURCE_INVENTORY', 'Import did not retain the original model.')
            source_hash = records[entry]['sha256']
            # The original source can change now without altering the captured
            # model or the provenance published for it.
            with tempfile.TemporaryDirectory(prefix='.rig-import-', dir=root.parent) as temporary:
                stage = Path(temporary)/'run';stage.mkdir()
                dependencies = []
                for relative, record in sorted(records.items()):
                    name = 'source/'+relative
                    source = safe_path(snapshot.root, relative, exists=True)
                    target = safe_path(stage, name);target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    require(target.stat().st_size == record['bytes'] and sha256(target) == record['sha256'],
                            'RIG_SOURCE_COPY', 'Captured source copy failed its byte hash.', asset=relative)
                    dependencies.append(dict(path=name, sha256=record['sha256']))
                source_name = 'source/'+entry
                identity = dict(schema_version=1, id=identifier, source_sha256=source_hash, fixture=fixture,
                                origin=origin, attribution=attribution, coordinate_system=COORDINATES,
                                import_scale=scale, approved_artwork=False)
                atomic_json(stage/'identity.json', identity)
                facial = dict(schema_version=1, eyes=[], morph_targets={m.id:list(m.morphs) for m in imported.geometry.meshes if m.morphs}, expressions={})
                manifest = dict(schema_version=1, kind='hatch-3d-rig', id=identifier, version='1.0.0', revision=0,
                    character=dict(schema_version=1, id=identifier, version='1.0.0', identity_sha256=sha256(stage/'identity.json')),
                    source=dict(schema_version=1, path=source_name, sha256=source_hash, origin=origin,
                                fixture=fixture, attribution=attribution, dependencies=dependencies),
                    coordinate_system=deepcopy(COORDINATES), archetype='custom', geometry={}, skeleton=imported.skeleton,
                    skins=[], controllers=dict(ik=[], constraints=[]), facial=facial, attachments=[], animations=[],
                    state_mapping={}, uncertainties=[], provenance=provenance('import', {'source':source_hash}),
                    limits=limits, review=dict(status='pending', receipts=[]), validation=[])
                rig = RigAsset(manifest, imported.geometry, imported.weights, imported.clips, stage)
                for clip in rig.clips.values():
                    clip['rig_id']=identifier
                    if rig.manifest['skeleton']:clip['skeleton_version']=rig.skeleton['version']
                save_rig(rig, bump=False)
                atomic_json(stage/'validation/import.json', imported.report)
                job = dict(schema_version=1, kind='hatch-3d-rig-job', id=identifier, status='imported',
                           rig_sha256=rig_signature(rig.manifest), steps={}, limits=limits,
                           history=[dict(operation='import', at=timestamp(), signature=rig_signature(rig.manifest))])
                atomic_json(stage/'job.json', validate_contract(job, 'RigJob'))
                # Reopen the complete staged run before the only publication.
                load_rig(stage)
                try:
                    _rename_no_replace(stage, root)
                except FileExistsError as exc:
                    raise RigError('RIG_RUN_EXISTS', 'Another writer created the destination; it was preserved.',
                                   preserved=str(root)) from exc
                return load_rig(root)


def code_signature():
    return object_hash({p.name:sha256(p) for p in sorted(Path(__file__).parent.glob('rig_*.py'))})


def run_step(root, name, parameters, action, *, status, retry=False):
    """Single writer, content/parameter/code cache and preserved failure diagnostics."""
    root=Path(root).absolute()
    with job_lock(root):
        rig=load_rig(root);job=validate_contract(read_json(root/'job.json'), 'RigJob')
        current=rig_signature(rig.manifest);param_hash=object_hash(parameters);code_hash=code_signature()
        previous=job['steps'].get(name)
        same=previous and previous['parameters_sha256']==param_hash and previous['code_sha256']==code_hash
        if same and previous['status']=='succeeded' and previous['output_sha256']==current:
            intact=all(sha256(safe_path(root, d['path'], exists=True))==d['sha256'] for d in previous['artifacts'])
            if intact:return dict(status='cached', step=name, rig_sha256=current, artifacts=previous['artifacts'])
        attempts=previous['attempts'] if same and previous['input_sha256']==current else 0
        require(attempts < rig.manifest['limits']['attempts'], 'RIG_ATTEMPTS', 'Step attempt budget is exhausted; correct the input or parameters.')
        if previous and previous['status']=='running':
            previous['status']='interrupted';job['history'].append(dict(operation=name, status='interrupted', at=timestamp()))
        step=dict(status='running', input_sha256=current, output_sha256=None, parameters=deepcopy(parameters),
                  parameters_sha256=param_hash, code_sha256=code_hash, attempts=attempts+1,
                  token=str(uuid.uuid4()), started_at=timestamp(), finished_at=None, error=None, artifacts=[])
        job['steps'][name]=step;atomic_json(root/'job.json', validate_contract(job, 'RigJob'))
        try:
            result=action(rig) or {}
            active=load_rig(root);step['output_sha256']=rig_signature(active.manifest)
            for name_ in result.get('artifact_paths', []):
                path=safe_path(root, name_, exists=True);step['artifacts'].append(dict(path=name_, sha256=sha256(path)))
            step['status']='succeeded';job['status']=result.get('job_status', status)
            job['rig_sha256']=step['output_sha256'];job['limits']=active.manifest['limits']
            # Only steps bound to earlier active inputs become stale; immutable artifacts remain.
            if step['output_sha256'] != current:
                for key, other in job['steps'].items():
                    if key != name and other['status']=='succeeded':other['status']='stale'
            return dict(status='succeeded', step=name, rig_sha256=step['output_sha256'], **result)
        except Exception as exc:
            error=exc.report if isinstance(exc, RigError) else RigError('RIG_STEP_FAILED', str(exc), stage=name,
                retryable=True, preserved=str(root)).report
            step['status']='failed';step['error']=error;job['status']='failed'
            if isinstance(exc, RigError):raise
            raise RigError(**{k:v for k,v in error.items() if k not in ('expected','observed')}) from exc
        finally:
            step['finished_at']=timestamp()
            job['history'].append(dict(operation=name, status=step['status'], at=step['finished_at'], token=step['token']))
            job['history']=job['history'][-10000:]
            atomic_json(root/'job.json', validate_contract(job, 'RigJob'))


def configure_limits(rig, changes):
    value={**rig.manifest['limits'], **changes};validate_contract(value, 'Limits')
    require(value['influences'] in (4,8), 'RIG_INFLUENCES', 'Choose four or eight influences.')
    require(rig.geometry.vertex_count <= value['vertices'] and rig.geometry.triangle_count <= value['triangles'],
            'RIG_LIMIT_GEOMETRY', 'Existing geometry exceeds the proposed limits.')
    if rig.manifest['skeleton']:require(len(rig.skeleton['bones']) <= value['bones'], 'RIG_LIMIT_BONES', 'Existing skeleton exceeds the proposed limits.')
    rig.manifest['limits']=value;save_rig(rig, operation='configure-limits')


def status(root):
    rig=load_rig(root);job=validate_contract(read_json(Path(root)/'job.json'), 'RigJob')
    return dict(id=rig.manifest['id'], version=rig.manifest['version'], rig_sha256=rig_signature(rig.manifest),
                status=job['status'], review=rig.manifest['review']['status'], fixture=rig.manifest['source']['fixture'],
                meshes=len(rig.geometry.meshes), vertices=rig.geometry.vertex_count,
                bones=len(rig.skeleton['bones']) if rig.manifest['skeleton'] else 0,
                skins=len(rig.weights), clips=list(rig.clips), steps=job['steps'])
