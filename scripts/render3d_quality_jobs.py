"""Bound quality preflight around the existing JobStore consumer API.

No scheduler, cache implementation, process loop or pixel decoder is defined here.
"""
from copy import deepcopy
from pathlib import Path

from render3d_contracts import require, validate
from render3d_quality_binding import validate_quality_settings


class BoundQualityBackend:
    def __init__(self, backend, request, executable):
        self.backend, self.settings, self.executable = backend, request['settings'], executable
    def render_frames(self, request, snapshot_root, destination, **kwargs):
        require(request['settings'] == self.settings, 'stale-cache', 'Frame changed its bound quality settings.')
        validate_quality_settings(request['settings'], executable=self.executable)
        return self.backend.render_frames(request, snapshot_root, destination, **kwargs)


class BoundQualityJob:
    """Validate current helper/profile bytes even on an already-complete cache path.

    Supply an already-bound request. Other authoring owners may provide their
    existing backend/verifier wrappers and an additional preflight callback.
    """
    def __init__(self, store, backend, request, snapshot_root, verifier, *,
                 executable=None, identity_provider=None, preflight=None):
        validate(request, 'RenderRequest')
        require('quality_sha256' in request['settings'], 'invalid-input',
                'Bind the quality request before constructing a cache consumer.')
        self.store, self.request = store, deepcopy(request)
        self.snapshot_root = Path(snapshot_root)
        self.executable = executable or getattr(backend, 'executable', None)
        self.identity_provider = identity_provider or backend.inspect_version
        self.preflight, self.verifier = preflight, verifier
        self.backend = BoundQualityBackend(backend, self.request, self.executable)
    def _validate(self, job=None):
        validate_quality_settings(self.request['settings'], executable=self.executable)
        if self.preflight is not None:self.preflight()
        identity = self.identity_provider()
        validate(identity, 'BackendIdentity')
        if job is not None:
            context = self.store.status(job)
            require(context['request'] == self.request, 'stale-cache', 'Job belongs to another bound request.')
            require(context['backend'] == identity, 'stale-cache', 'Current backend differs from cached execution identity.')
            require(Path(context['snapshot']).resolve() == self.snapshot_root.resolve(),
                    'stale-asset', 'Job belongs to another snapshot root.')
        return identity
    def _verify(self, request, result, root):
        self._validate()
        require(request['settings'] == self.request['settings'], 'stale-cache', 'Cached frame changed its bound quality settings.')
        validate_quality_settings(request['settings'], executable=self.executable)
        return self.verifier(request, result, root)
    def submit(self):
        identity = self._validate()
        return self.store.submit(self.request, self.snapshot_root, identity)
    def status(self, job):
        self._validate(job)
        return self.store.status(job)
    def run_one(self, job):
        self._validate(job)
        return self.store.run_one(self.backend, self._verify, job=job)
    def resume(self, job, **kwargs):
        self._validate(job)
        return self.store.resume(job, verifier=self._verify, **kwargs)
    def materialize(self, job, destination):
        self._validate(job)
        return self.store.materialize(job, destination, self._verify)
