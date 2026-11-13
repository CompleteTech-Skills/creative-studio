"""Director reliability adapters over native project and render lifecycle owners.

This module has no project, job, worker, or cache storage format of its own.
Receipts describe exact revisions; they do not confer visual approval.
"""
from __future__ import annotations
from copy import deepcopy

from rig_errors import RigError


def _require(condition, message):
    if not condition:
        raise RigError('DIRECTOR_RELIABILITY', message, stage='director-reliability')


def _asset_identity(snapshot):
    """Retain exact authored asset references and independent review records."""
    assets = {}
    for path, document in snapshot['documents'].items():
        if document.get('kind') == 'scene3d-asset':
            assets[path] = deepcopy(document)
    return assets


def apply_camera_or_light_patch(repository, expected_revision, expected_sha256,
                                identifier, component, properties, transaction_id):
    """Apply a typed canonical component patch, reporting conservative rework.

    A camera invalidates its scene's shots using that camera; a light invalidates
    all shots of its scene. Native commands validate units, scalar bounds, locks,
    dependency references and atomic publication. Existing asset reviews survive.
    """
    _require(component in ('camera', 'light'), 'Select the camera or light component.')
    _require(isinstance(identifier, str) and isinstance(properties, dict), 'Invalid typed component patch.')
    before = repository.snapshot()
    _require(before['revision'] == expected_revision and before['sha256'] == expected_sha256,
             'Patch revision or snapshot hash is stale.')
    nodes = [node for node in before['scene']['nodes'] if node['id'] == identifier]
    _require(len(nodes) == 1 and nodes[0]['type'] == component and component in nodes[0],
             'Patch target is not the requested native component.')
    affected = []
    scene_id = (before['scene']['id'], before['scene']['version'])
    for path, document in before['documents'].items():
        if document.get('kind') != 'scene3d-shot':
            continue
        reference = document['scene']
        if (reference['id'], reference['version']) != scene_id or reference['path'] != before['scene_path']:
            continue
        if component == 'camera' and document['camera'] != identifier:
            continue
        affected.append(dict(path=path, id=document['id'], version=document['version'],
                             frame_range=dict(start=0, end=document['duration_frames'])))
    assets = _asset_identity(before)
    batch = dict(schema_version=1, commands=[dict(operation='configure-node', parameters=dict(
        identifier=identifier, properties={component: deepcopy(properties)}))])
    after = repository.apply(expected_revision, batch, transaction_id,
                             expected_sha256=expected_sha256, label='Director '+component+' revision')
    _require(_asset_identity(after) == assets, 'Native component edit changed asset identity or review records.')
    # Reopen the committed revision through the public owner before reporting it.
    from editor3d_repository import EditorRepository
    reopened = EditorRepository(repository.root).snapshot(after['revision'])
    _require(reopened == after, 'Reopened project differs from committed component edit.')
    return dict(schema_version=1, kind='director-component-revision',
                before=dict(revision=before['revision'], sha256=before['sha256']),
                after=dict(revision=after['revision'], sha256=after['sha256']),
                target=identifier, component=component,
                affected_shots=sorted(affected, key=lambda item: item['path']),
                asset_identities_preserved=True, independent_review_records_preserved=True,
                visual_review='not-performed')


def resume_verified_job(store, job, verifier, *, expected_revision, retry_failed=True):
    """Resume the existing identity after owner-side verification of every cache.

    Completed valid frames remain complete; missing/invalid frames return to the
    native queue. No 'verified' flag substitutes for decoding the current bytes.
    """
    before = store.status(job)
    _require(before['revision'] == expected_revision, 'Render job revision is stale.')
    store.resume(job, retry_failed=retry_failed, expected_revision=expected_revision, verifier=verifier)
    after = store.status(job)
    _require(after['id'] == before['id'] and after['request'] == before['request'],
             'Resume changed native job identity or render inputs.')
    preserved = [frame['frame'] for frame in after['frames'] if frame['state'] == 'complete']
    queued = [frame['frame'] for frame in after['frames'] if frame['state'] == 'queued']
    return dict(schema_version=1, kind='director-job-resume', job=job,
                revision=after['revision'], verified_preserved_frames=preserved,
                pending_frames=queued, visual_review='not-performed')
