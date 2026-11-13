"""Read-only browser reports delegated to the canonical asset subsystem owners.

These reports do not publish versions, grant visual approval, repair payloads or
apply cleanup. Locations always contain a configured alias and an exact reference.
"""
from __future__ import annotations

from copy import deepcopy
import io
from typing import Callable
import warnings

from asset_library_contracts import validate
from rig_errors import RigError

BROWSER_LIMITS = dict(source_bytes=32*1024**2, dependency_bytes=32*1024**2,
                      vertices=100000, triangles=200000, memory_bytes=128*1024**2,
                      keyframes=25000, clips=32, influences=4)
MAX_THUMBNAIL_BYTES = 4*1024**2
MAX_THUMBNAIL_PIXELS = 1024**2
IMAGE_MIMES = {'png':'image/png', 'jpeg':'image/jpeg', 'webp':'image/webp'}


def fail(code, message):
    raise RigError('LIBRARY_BROWSER_DETAILS_'+code, message, stage='asset-browser')


class BrowserDetails:
    """Use the same explicitly scoped libraries and native index as the browser."""
    def __init__(self, libraries, index):
        from asset_library_dependencies import DependencyGraph
        if not isinstance(libraries, dict) or not 1 <= len(libraries) <= 32:
            fail('SCOPE', 'Configure one to 32 library aliases.')
        if getattr(index, 'libraries', None) != libraries:
            fail('SCOPE', 'Browser details and search must use identical library scopes.')
        self.libraries = dict(libraries)
        self.index = index
        self.graph = DependencyGraph(self.libraries, max_nodes=256, max_edges=1024)

    def _location(self, value):
        from asset_library_dependencies import location
        value = location(value)
        if value['library'] not in self.libraries:
            fail('SCOPE', 'The library alias is not configured.')
        return deepcopy(value)

    def _report(self, name, value, action: Callable):
        value = self._location(value)
        base = dict(schema_version=1, kind='asset-browser-'+name, location=value,
                    publication_changed=False, production_approved=False)
        try:
            report = action(self.libraries[value['library']], value)
            return dict(base, status='verified', report=report)
        except ImportError as exc:
            return dict(base, status='unavailable', reason='Install the declared optional dependency: '+str(exc.name))
        except RigError as exc:
            return dict(base, status='blocked', error=deepcopy(exc.report))

    def dependency_graph(self, value):
        result = self._report('dependency-graph', value,
                              lambda _, loc: self.graph.closure([loc], strict=False))
        if result['status'] == 'verified' and not result['report']['complete']:
            result['status'] = 'incomplete'
        return result

    def usage(self, value):
        value = self._location(value)
        resolver = getattr(self.index, 'usage_resolver', None)
        projects = tuple(getattr(self.index, 'project_ids', ()))
        if resolver is None or not projects:
            return dict(schema_version=1, kind='asset-browser-usage', location=value,
                        status='unavailable', reason='No explicit project binding scopes were configured.',
                        publication_changed=False, production_approved=False)
        def inspect(library, location):
            library.inspect(location['reference'])
            found=[]
            for project in projects:
                state=resolver.bindings(project)
                slots=sorted(name for name,binding in state['bindings'].items() if binding == location)
                if slots:
                    found.append(dict(project=project, revision=state['revision'], slots=slots))
            return dict(projects=found, inspected_projects=list(projects), scope='explicit-project-bindings')
        return self._report('usage', value, inspect)

    def validation(self, value):
        def inspect(library, location):
            from asset_library_validation import validate_asset
            return validate_asset(library, location['reference'], limits=dict(BROWSER_LIMITS))
        return self._report('validation', value, inspect)

    def exports(self, value):
        def inspect(library, location):
            from asset_library_export import ExportPipeline
            manifest=library.resolve(location['reference'])
            adapter=ExportPipeline(library).capabilities()
            runtime=sorted(f['path'] for f in manifest['files'] if f['format']=='glb')
            paths={f['path'] for f in manifest['files']}
            native={'native/asset.blend','evidence/import.json'} <= paths
            formats=[]
            for name in adapter['formats']:
                candidate=bool(runtime) and (name!='blend' or native)
                formats.append(dict(format=name, adapter_available=True,
                    asset_status='candidate' if candidate else 'unavailable', runtime_paths=runtime,
                    verification_required=True, explicit_native_trust_required=name=='blend',
                    reason=('Actual export validation and loss-policy checks are required.' if candidate else
                            'No eligible registered runtime GLB or required packed native evidence.')))
            return dict(adapter=adapter, formats=formats, export_executed=False,
                        visual_review=manifest['metadata']['review_status'],
                        distribution=manifest['publication']['distribution'])
        return self._report('exports', value, inspect)

    def compare(self, value, other_exact_ref):
        validate(other_exact_ref, 'AssetRef')
        candidate=deepcopy(other_exact_ref)
        def inspect(library, location):
            from asset_library_versions import AssetVersions
            return dict(previous=location['reference'], candidate=candidate,
                        comparison=AssetVersions(library).compare(location['reference'], candidate))
        return self._report('version-comparison', value, inspect)

    def diagnose(self, value):
        """Inspect broken payloads without requiring a successful resolve first."""
        value=self._location(value)
        library=self.libraries[value['library']]
        base=dict(schema_version=1, kind='asset-browser-diagnosis', location=value,
                  publication_changed=False, production_approved=False, repair_executed=False)
        metadata=None
        errors=[]
        try:
            metadata=library.inspect(value['reference'], verify_payloads=False)
            library.inspect(value['reference'], verify_payloads=True)
        except RigError as exc:
            errors.append(deepcopy(exc.report))
        graph=self.dependency_graph(value)
        complete=not errors and graph['status']=='verified'
        return dict(base, status='verified' if complete else 'incomplete',
                    metadata_available=metadata is not None,
                    files=deepcopy(metadata['files']) if metadata else [],
                    errors=errors, dependency_graph=graph, payload_complete=complete,
                    visual_review=metadata['metadata']['review_status'] if metadata else 'unknown')

    def cleanup_analysis(self, alias):
        if not isinstance(alias,str) or alias not in self.libraries:
            fail('SCOPE','Choose an explicitly configured library alias.')
        base=dict(schema_version=1, kind='asset-browser-cleanup-analysis', library=alias,
                  applied=False, publication_changed=False, production_approved=False)
        try:
            return dict(base, status='verified', plan=self.libraries[alias].analyze_gc())
        except RigError as exc:
            return dict(base, status='blocked', error=deepcopy(exc.report))

    def thumbnail(self, value):
        """Return exact registered preview bytes, MIME and bound provenance, or None.

        Only explicit thumbnail/material preview descriptors qualify. An ordinary
        texture is never promoted to an asset thumbnail. Decoding does not supply
        visual approval or upgrade a generated preview to externally verified.
        """
        value=self._location(value)
        library=self.libraries[value['library']]
        manifest=library.resolve(value['reference'])
        candidates=sorted((p for p in manifest['previews'] if p['type'] in ('thumbnail','material')),
                          key=lambda p:(p['type']!='thumbnail',p['file']['path']))
        for preview in candidates:
            record=preview['file']
            if record['format'] not in IMAGE_MIMES:
                continue
            if record['bytes']>MAX_THUMBNAIL_BYTES or preview['width']*preview['height']>MAX_THUMBNAIL_PIXELS:
                fail('THUMBNAIL_LIMIT','Registered thumbnail exceeds the browser image budget.')
            payload=library.read_file(value['reference'],record['path'])
            if len(payload)>MAX_THUMBNAIL_BYTES:
                fail('THUMBNAIL_LIMIT','Encoded thumbnail exceeds four MiB.')
            from PIL import Image, ImageFile
            if ImageFile.LOAD_TRUNCATED_IMAGES:
                fail('THUMBNAIL_POLICY','Strict thumbnail decoding requires truncated-image loading to remain disabled.')
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(payload)) as image:
                        if (image.size!=(preview['width'],preview['height']) or image.width*image.height>MAX_THUMBNAIL_PIXELS):
                            fail('THUMBNAIL_DIMENSIONS','Actual thumbnail dimensions differ or exceed the pixel budget.')
                        if getattr(image,'n_frames',1)!=1:
                            fail('THUMBNAIL_ANIMATION','Asset list thumbnails must be static images.')
                        expected={'png':'PNG','jpeg':'JPEG','webp':'WEBP'}[record['format']]
                        if image.format!=expected:
                            fail('THUMBNAIL_FORMAT','Decoded thumbnail format differs from its exact record.')
                        image.verify()
                    with Image.open(io.BytesIO(payload)) as decoded:
                        decoded.load()
            except RigError:
                raise
            except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc:
                raise RigError('LIBRARY_BROWSER_DETAILS_THUMBNAIL_DECODE', 'Registered thumbnail failed strict image decoding.',
                               stage='asset-browser',asset=record['path']) from exc
            provenance=dict(location=value, path=record['path'], sha256=record['sha256'], bytes=record['bytes'],
                width=preview['width'], height=preview['height'], type=preview['type'],
                source_version_sha256=preview['source_version_sha256'], status=preview['status'],
                backend=preview['backend'],backend_version=preview['backend_version'],
                visual_review=manifest['metadata']['review_status'],fixture=manifest['provenance']['fixture'])
            return payload, IMAGE_MIMES[record['format']], provenance
        return None
