"""Director scheduling adapters over existing native execution authorities.

These adapters do not launch providers, issue replacement ownership tokens, or
turn a native job's status label into artifact verification. Unknown resource
usage stays unknown. Resource reservations belong to the scheduler's JobStore.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import io
from pathlib import Path
import re

from rig_errors import RigError, object_hash


def _need(value, message, code='INPUT'):
    if not value:
        raise RigError('DIRECTOR_DRIVER_' + code, message, stage='execution-driver')


def _binding(value, fields):
    _need(isinstance(value, dict) and set(value) == set(fields), 'Unexpected native binding fields.')
    for key in fields:
        _need(isinstance(value[key], str) and 0 < len(value[key]) <= 128,
              'Native binding values must be bounded nonempty strings.')
        if key.endswith('sha256'):
            _need(re.fullmatch('[0-9a-f]{64}', value[key]) is not None, 'Expected an exact native SHA-256 binding.')
    return value


def _artifact(identifier, path, digest, size, *, fixture, kind='native-output', media='application/octet-stream'):
    return dict(artifact_id=identifier, kind=kind, schema_version=1, semantic_version='1.0.0',
                path=path, sha256=digest, byte_size=size, media_type=media,
                provenance_ref=None, fixture=fixture, dependencies=[])


class RenderJobDriver:
    """Use one existing render job, its leases, verifier and retry policy."""
    owner = 'RenderAdapter'
    operations = ('RenderAdapter.submit',)
    binding_fields = ('job', 'request_sha256', 'backend_sha256', 'snapshot_sha256')

    def __init__(self, store, backend, verifier):
        self.store, self.backend, self.verifier = store, backend, verifier

    def _state(self, binding):
        _binding(binding, self.binding_fields)
        state = self.store.status(binding['job'], event_limit=1000)
        _need(object_hash(state['request']) == binding['request_sha256'] and
              object_hash(state['backend']) == binding['backend_sha256'] and
              state['request']['snapshot']['sha256'] == binding['snapshot_sha256'],
              'The persisted native request/backend/snapshot binding changed.', 'BINDING')
        from render3d_contracts import make_frame_spec, load
        from render3d_snapshot import verify_snapshot, Scene3DAdapter, NativeRigAdapter
        # Metadata identity alone is insufficient: admission must also re-read
        # every pinned source byte through the existing snapshot authority.
        snapshot = load(Path(state['snapshot']) / 'snapshot.json', 'RenderSnapshot')
        adapter = Scene3DAdapter() if snapshot['scope'] == 'scene3d' else NativeRigAdapter()
        verified = verify_snapshot(state['snapshot'], adapter)
        _need(verified == snapshot, 'Native snapshot changed during admission.', 'BINDING')
        state['_snapshot_fixture'] = verified['fixture']
        make_frame_spec(state['request'], state['request']['frame_range']['start'],
                        Path(state['snapshot']) / 'snapshot.json')
        return state

    def validate(self, binding, node, input_root):
        _need(node['operation'] in self.operations, 'Driver does not implement this operation.')
        state = self._state(binding)
        _need(any(ref['sha256'] == binding['snapshot_sha256'] and
                  ref['byte_size'] == state['request']['snapshot']['bytes']
                  for ref in node['inputs']),
              'The graph must pin the native render snapshot.', 'BINDING')

    def required_capabilities(self, binding):
        state = self._state(binding)
        return ['blender.cpu' if state['backend']['device'] == 'cpu' else 'blender.gpu']

    def admission_resources(self, binding):
        resources = self._state(binding)['request']['settings']['resources']
        # timeout_seconds is wall time, not a CPU/GPU consumption measurement.
        return dict(max_memory_bytes=resources['memory_bytes'],
                    max_storage_bytes=resources['storage_bytes'])

    def inspect(self, binding):
        state = self._state(binding)
        artifacts = []
        records = []
        if state['state'] == 'complete':
            from render3d_contracts import make_frame_spec
            from director_contracts import _read_artifact
            for frame in state['frames']:
                _need(frame['state'] == 'complete', 'Native completion has unfinished frames.', 'INCOMPLETE')
                spec = make_frame_spec(state['request'], frame['frame'], Path(state['snapshot']) / 'snapshot.json')
                lease = dict(request=state['request'], frame=frame['frame'], cache_key=frame['cache_key'],
                             spec=spec, backend=state['backend'])
                record = self.store._verify_cache(lease, self.verifier)
                records.append(dict(frame=frame['frame'], cache_key=frame['cache_key'],
                                    native_record_sha256=object_hash(record)))
                for image in record['result']['images']:
                    native = image['file']
                    artifact = _artifact('render-' + frame['cache_key'] + '-' + image['pass_name'],
                        'cache/' + frame['cache_key'] + '/' + native['path'],
                        native['sha256'], native['bytes'], fixture=state['_snapshot_fixture'],
                        kind='render-frame', media='image/' + state['request']['settings']['output']['format'])
                    # The verifier checks decoding; this independent no-follow
                    # read also binds the public reference's exact bytes/path.
                    _read_artifact(self.store.root, artifact)
                    artifacts.append(artifact)
            _need(bool(artifacts), 'Completed render has no verified image artifacts.', 'INCOMPLETE')
        return dict(state=state['state'], receipt=dict(native_owner='render3d', job=binding['job'],
                    request_sha256=binding['request_sha256'], backend_sha256=binding['backend_sha256'],
                    snapshot_sha256=binding['snapshot_sha256'], frames=deepcopy(state['frames']),
                    events=deepcopy(state['events']), verified_records=records),
                    artifacts=artifacts, artifact_root=str(self.store.root), actual_usage=None)

    def dispatch(self, binding, *, admit):
        self._state(binding)
        _need('admit' in inspect.signature(self.store.run_one).parameters,
              'This native JobStore lacks atomic DAG admission; no work was claimed.', 'UNAVAILABLE')
        return self.store.run_one(self.backend, self.verifier, job=binding['job'], admit=admit)

    def cancel(self, binding):
        state = self._state(binding)
        self.store.cancel(binding['job'], expected_revision=state['revision'])
        return self.inspect(binding)

    def resume(self, binding):
        state = self._state(binding)
        return self.store.resume(binding['job'], retry_failed=True,
                                 expected_revision=state['revision'], verifier=self.verifier)

    def heartbeat(self, binding, lease):
        self._state(binding)
        _need(lease.get('job') == binding['job'], 'Lease belongs to another native job.', 'BINDING')
        return self.store.heartbeat(lease)


class NativeAcquisitionDriver:
    """Bridge one native raster acquisition claim without calling a provider.

    An active token has an uncertain external outcome until the native owner
    records a generated source or a known failure. Cancellation cannot invent
    proof that an external call stopped.
    """
    owner = 'AcquisitionGateway'
    operations = ('AcquisitionGateway.claim',)
    binding_fields = ('job', 'input_sha256')

    def __init__(self, gateway, store, *, max_image_pixels=16_777_216):
        _need(type(max_image_pixels) is int and 1 <= max_image_pixels <= 100_000_000,
              'Invalid acquisition image decode bound.')
        self.gateway, self.store = gateway, store
        self.max_image_pixels = max_image_pixels

    def _check(self, binding, admit=None):
        _binding(binding, self.binding_fields)
        def check(data):
            import hatch
            selected = hatch.job_for(data, binding['job'])
            # Native source reads occur under the native ownership lock and
            # before entering the shared SQLite admission transaction.
            current = object_hash(hatch.inputs(self.gateway.root, selected))
            _need(current == binding['input_sha256'], 'Native acquisition inputs changed.', 'BINDING')
            if admit is not None:
                record = dict(native_owner=self.owner, job=binding['job'], input_sha256=current,
                              binding=deepcopy(self.gateway.binding))
                with self.store._transaction() as db:
                    allowed = admit(db, record)
                    _need(type(allowed) is bool, 'Admission callback must return a strict bool.')
                    _need(allowed, 'Native acquisition admission was deferred.', 'DEFERRED')
        return check

    def validate(self, binding, node, input_root):
        _need(node['operation'] in self.operations, 'Driver does not implement this operation.')
        self.gateway.prepare(binding['job'], _check=self._check(binding))

    def required_capabilities(self, binding):
        _binding(binding, self.binding_fields)
        return ['native.acquisition']

    def admission_resources(self, binding):
        _binding(binding, self.binding_fields)
        # Claiming the existing token does not invoke a provider or establish
        # provider usage. The canonical owner exposes no request memory budget.
        return {}

    def inspect(self, binding):
        report = self.gateway.reconcile(binding['job'], _check=self._check(binding))
        native = report['native_state']
        artifacts = []
        if native == 'generated':
            import hatch
            from asset_library import read_source_bytes
            from PIL import Image
            selected = hatch.job_for(report['native_manifest'], binding['job'])
            _need(not report['stale'] and selected.get('generation_receipt'),
                  'Native generated output lacks a fresh provenance receipt.', 'INCOMPLETE')
            payload = read_source_bytes(self.gateway.root / selected['output_path'])
            _need(hashlib.sha256(payload).hexdigest() == selected['output_hash'],
                  'Native generated output bytes changed.', 'BINDING')
            with Image.open(io.BytesIO(payload)) as image:
                _need(image.width * image.height <= self.max_image_pixels, 'Native image exceeds decode bound.', 'LIMIT')
                image.verify()
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
            artifact = _artifact('acquisition-' + object_hash(dict(binding=binding, output=selected['output_hash']))[:64],
                selected['output_path'], selected['output_hash'], len(payload),
                fixture=self.gateway.binding['fixture'], kind='raster-acquisition-output', media='image/png')
            from director_contracts import _read_artifact
            _read_artifact(self.gateway.root, artifact)
            artifacts.append(artifact)
        state = ('complete' if native == 'generated' else 'uncertain' if native == 'running'
                 else 'failed' if native == 'failed' else 'queued')
        return dict(state=state, receipt=report, artifacts=artifacts,
                    artifact_root=str(self.gateway.root), actual_usage=None)

    def dispatch(self, binding, *, admit):
        return self.gateway.claim(binding['job'], _check=self._check(binding, admit))

    def cancel(self, binding):
        report = self.gateway.reconcile(binding['job'], _check=self._check(binding))
        if report['native_state'] == 'running':
            self.gateway.fail(binding['job'], report['claim_id'],
                              'Director cancellation requested; external outcome remains unknown.',
                              outcome='uncertain', _check=self._check(binding))
            return self.inspect(binding)
        if report['native_state'] == 'generated':
            return self.inspect(binding)
        raise RigError('DIRECTOR_DRIVER_UNAVAILABLE',
                       'The native acquisition owner has no cancellation transition for unstarted work.',
                       stage='execution-driver')

    def resume(self, binding):
        # Reconciliation never grants a new provider call or ownership token.
        return self.inspect(binding)

    def heartbeat(self, binding, lease):
        raise RigError('DIRECTOR_DRIVER_UNAVAILABLE',
                       'Native acquisition tokens have no expiring heartbeat lease; reconcile the original claim.',
                       stage='execution-driver')
