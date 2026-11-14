"""Evidence-bound structural QA; numerical success never grants visual approval.

Native geometry, rendering, media, acquisition and review owners retain their
formats. Unsupported installed adapters are reported as blocked, never passed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from io import BytesIO
from pathlib import Path
import re
import time

from director_contracts import (_read_artifact, artifact_references, validate_document,
                                validate_method_request, validate_method_result, parse_rfc3339, _integer)
from director_schema import MILESTONES, definitions, ID
from rig_errors import RigError, decode_json, finite_json, object_hash

MAX_BYTES = 1024**3
OPERATIONS = ('artifact.bytes', 'director.document', 'director.bundle',
              'scene3d.project', 'native.rig', 'native.motion', 'image.decode',
              'render3d.frames', 'silent.video', 'native.reopen', 'archive.restore')
# Raw bytes, images or claimed metadata cannot certify unrelated milestones.
MILESTONE_OPERATIONS = {
    'planning': {'director.bundle'}, 'assets': {'native.rig'},
    'scene': {'scene3d.project'}, 'motion': {'native.motion'},
    'rendering': {'render3d.frames'}, 'video': {'silent.video'},
    'native_editability': {'native.reopen'}, 'delivery': {'archive.restore'},
    'visual_qa': set(),
}


def need(ok, code, message):
    if not ok:
        raise RigError('DIRECTOR_QA_' + code, message, stage='qa')


def _keys(value, names, label):
    need(isinstance(value, dict) and set(value) == set(names), 'PROFILE', 'Invalid '+label+' fields.')


def reference(value):
    from jsonschema import Draft202012Validator
    schema = definitions()
    errors = Draft202012Validator({'$defs': schema, '$ref': '#/$defs/artifact-ref'}).iter_errors(value)
    need(next(errors, None) is None, 'REFERENCE', 'Use an exact bounded ArtifactRef.')
    return value


def schemas():
    """Authoritative structural profile; runtime additionally checks references/scopes."""
    from rig_contracts import obj, arr, ref
    identifier = deepcopy(ID)
    integer = lambda maximum: {'type': 'integer', 'minimum': 1, 'maximum': maximum}
    nullable_scope = {'anyOf': [{'type':'string', 'minLength':1, 'maxLength':128}, {'type':'null'}]}
    interval = obj({key: {'type':'integer', 'minimum':0, 'maximum':2_000_000_000} for key in ('start','end')})
    check = obj(dict(id=identifier, operation={'enum': list(OPERATIONS)}, artifact=ref('artifact-ref'),
                     options={'type':'object'}, scene=nullable_scope, shot=nullable_scope,
                     frames={'anyOf':[interval, {'type':'null'}]}))
    selected = arr(identifier, 0, 256)
    selected['uniqueItems'] = True
    profile = obj(dict(schema_version={'type':'integer', 'const':1}, kind={'const':'director-qa-profile'},
                       id=identifier, checks=arr(check,1,256),
                       milestones=obj({name:deepcopy(selected) for name in MILESTONES}),
                       review_dependencies={'type':'object', 'maxProperties':64,
                           'propertyNames':identifier, 'additionalProperties':arr(ref('artifact-ref'),1,128)},
                       limits=obj(dict(max_checks=integer(256),max_total_bytes=integer(MAX_BYTES),max_seconds=integer(300)))))
    profile.update({'$schema':'https://json-schema.org/draft/2020-12/schema',
                    '$defs':definitions(), 'title':'Director exact-artifact QA profile v1',
                    'description':'Structural profile only; runtime checks IDs, frame ordering, milestone operation coverage, exact bytes and resource admission.'})
    return {'director-qa-profile.schema.json':profile}


def validate_profile(value):
    finite_json(value)
    _keys(value, ('schema_version', 'kind', 'id', 'checks', 'milestones', 'review_dependencies', 'limits'), 'QA profile')
    need(not isinstance(value['schema_version'], bool) and value['schema_version'] == 1 and value['kind'] == 'director-qa-profile', 'VERSION', 'Unsupported QA profile.')
    need(isinstance(value['id'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value['id']), 'PROFILE', 'Invalid profile ID.')
    limits = value['limits']
    _keys(limits, ('max_checks', 'max_total_bytes', 'max_seconds'), 'QA limits')
    for name, maximum in (('max_checks', 256), ('max_total_bytes', MAX_BYTES), ('max_seconds', 300)):
        need(_integer(limits[name], name, 1) <= maximum, 'LIMIT', 'Invalid '+name+'.')
    checks = value['checks']
    need(isinstance(checks, list) and 0 < len(checks) <= limits['max_checks'], 'LIMIT', 'QA checks exceed the limit.')
    ids = set()
    for check in checks:
        _keys(check, ('id', 'operation', 'artifact', 'options', 'scene', 'shot', 'frames'), 'QA check')
        need(isinstance(check['id'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', check['id']) and check['id'] not in ids,
             'PROFILE', 'Check IDs must be unique bounded identifiers.')
        ids.add(check['id'])
        need(check['operation'] in OPERATIONS and isinstance(check['options'], dict), 'PROFILE', 'Unsupported QA operation.')
        reference(check['artifact'])
        for field in ('scene', 'shot'):
            need(check[field] is None or isinstance(check[field], str) and 0 < len(check[field]) <= 128, 'PROFILE', 'Invalid check scope.')
        frames = check['frames']
        if frames is not None:
            _keys(frames, ('start', 'end'), 'frame interval')
            need(all(_integer(frames[k], k) >= 0 for k in frames) and frames['start'] < frames['end'],
                 'PROFILE', 'Use a bounded half-open frame interval.')
    need(isinstance(value['milestones'], dict) and set(value['milestones']) == set(MILESTONES), 'PROFILE', 'Declare every independent milestone.')
    for name, selected in value['milestones'].items():
        need(isinstance(selected, list) and all(isinstance(key, str) for key in selected)
             and len(set(selected)) == len(selected) and set(selected) <= ids,
             'PROFILE', 'Milestones must select existing unique check IDs.')
        need(not selected or all(next(c for c in checks if c['id'] == key)['operation'] in MILESTONE_OPERATIONS[name]
                                 for key in selected), 'PROFILE', 'Check type cannot certify milestone '+name+'.')
    need(isinstance(value['review_dependencies'], dict) and len(value['review_dependencies']) <= 64, 'PROFILE', 'Invalid review dependencies.')
    for name, refs in value['review_dependencies'].items():
        need(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', name) and
             isinstance(refs, list) and 0 < len(refs) <= 128, 'PROFILE', 'Invalid review dimension.')
        for ref in refs:
            reference(ref)
    need(len({r['path'] for r in artifact_references(value)}) <= 4096, 'LIMIT', 'QA reference count exceeds the limit.')
    return value


def _diagnostic(check, code, message):
    return dict(code=code, severity='error', stage='qa', entity_id=check['shot'] or check['scene'] or check['id'],
                field_path='checks.'+check['id'], expected='A passing executed native check', observed=message,
                retryable=False, recommended_action='Repair the identified input with its native owner and revalidate.',
                evidence=[check['artifact']])


def _finding(check, code, message, current_sha256=None):
    return dict(category=code, severity='error', scene=check['scene'], shot=check['shot'],
                frames=check['frames'], artifact=check['artifact'], expected_sha256=check['artifact']['sha256'],
                current_sha256=current_sha256,
                observation=message, expected='A passing executed native check', actual=message,
                corrective_action='Repair the input with its authoritative owner; retain this failed report.',
                confidence=None)


def _usage():
    fields = {key: dict(value=None, explanation='Not measured by structural QA.') for key in
              ('provider_calls', 'input_tokens', 'output_tokens', 'cpu_seconds', 'gpu_seconds', 'storage_bytes', 'amount')}
    return dict(classification='unavailable', currency=None, measurement_source='director-qa-v1', **fields)


class QAService:
    def __init__(self, artifact_root, project_reference, *, journal=None):
        self.root = Path(artifact_root).resolve(strict=True)
        self.project_reference = deepcopy(reference(project_reference))
        self.project = validate_document(decode_json(_read_artifact(self.root, self.project_reference)))
        need(self.project['kind'] == 'director-project', 'PROJECT', 'Select an exact Director project.')
        need(all(self.project_reference[key] == self.project[field] for key, field in
                 (('artifact_id', 'id'), ('kind', 'kind'), ('schema_version', 'schema_version'),
                  ('semantic_version', 'semantic_version'))),
             'IDENTITY', 'Director project reference identity/version differs from its actual document.')
        self.fixture = self.project_reference['fixture'] or any(ref['fixture'] for ref in artifact_references(self.project))
        self.journal = journal

    def _read(self, ref):
        return _read_artifact(self.root, reference(ref))

    def _check(self, check, raw, cancel):
        operation, options, ref = check['operation'], check['options'], check['artifact']
        path = self.root/ref['path']
        if operation == 'artifact.bytes':
            need(not options, 'PROFILE', 'Byte checks have no options.')
            return dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        if operation == 'director.document':
            need(not options, 'PROFILE', 'Director document checks have no options.')
            document = validate_document(decode_json(raw))
            need(ref['fixture'] or not any(nested['fixture'] for nested in artifact_references(document)),
                 'PROVENANCE', 'A fixture-derived Director document cannot become production evidence.')
            return dict(kind=document['kind'], id=document.get('id'), source_sha256=ref['sha256'])
        if operation == 'director.bundle':
            need(not options, 'PROFILE', 'Director bundle checks have no options.')
            from director_contracts import validate_bundle
            documents = decode_json(raw)
            need(isinstance(documents, list), 'INPUT', 'A Director bundle is a list of exact documents.')
            validate_bundle(documents, self.root)
            # This validates plans/contracts only. Executable capability admission
            # is checked separately by the scheduler, so no production completion.
            return dict(documents=len(documents), scope='contracts-and-exact-bytes', executable_verified=False)
        if operation == 'scene3d.project':
            need(not options and path.name == 'project.json', 'PROFILE', 'Select the native project manifest.')
            from scene3d_contracts import load_project
            bundle = load_project(path.parent)
            need(bundle['inventory']['project.json']['sha256'] == ref['sha256'], 'CHANGED', 'Native project changed during validation.')
            need(ref['fixture'] or not bundle['project']['provenance']['fixture'],
                 'PROVENANCE', 'Native scene fixture classification must be preserved.')
            return dict(files=len(bundle['inventory']), bytes=bundle['bytes'], project_id=bundle['project']['id'])
        if operation in ('native.rig', 'native.motion'):
            need(not options and path.name == 'rig.json', 'PROFILE', 'Select the native rig manifest.')
            from rig_models import load_rig
            from rig_validation import validate_rig
            rig = load_rig(path.parent)
            need(rig.manifest == decode_json(raw), 'CHANGED', 'Native rig changed during validation.')
            need(ref['fixture'] or not rig.manifest['source']['fixture'],
                 'PROVENANCE', 'Native rig fixture classification must be preserved.')
            report = validate_rig(rig, require_animation=operation == 'native.motion')
            need(report['ok'], 'NATIVE', 'Native rig validation failed.')
            return dict(vertices=rig.geometry.vertex_count, triangles=rig.geometry.triangle_count,
                        clips=len(rig.clips), native_metrics=report['metrics'], native_checks=report['checks'])
        if operation == 'image.decode':
            need(not options, 'PROFILE', 'Image checks have no options.')
            from PIL import Image
            try:
                with Image.open(BytesIO(raw)) as image:
                    need(image.width*image.height <= 64*1024**2, 'LIMIT', 'Image pixel budget exceeded.')
                    image.load()
                    return dict(width=image.width, height=image.height, format=image.format, decoded=True)
            except Image.DecompressionBombError as exc:
                raise RigError('DIRECTOR_QA_LIMIT', 'Image exceeds the safe decoder profile.', stage='qa') from exc
        if operation == 'silent.video':
            _keys(options, ('request',), 'silent video check')
            from silent_video import verify_export
            request = decode_json(self._read(options['request']))
            result = verify_export(path, request, cancel=cancel)
            need(result['artifact_sha256'] == ref['sha256'] and result['audio_stream_count'] == 0,
                 'VIDEO', 'Video differs from the pinned silent output.')
            return {key: result[key] for key in ('frame_count', 'fps', 'duration', 'audio_stream_count', 'artifact_sha256')}
        if operation == 'render3d.frames':
            _keys(options, ('request', 'snapshot', 'backend'), 'render result check')
            from render3d_verify import verify_result
            request = decode_json(self._read(options['request']))
            backend = decode_json(self._read(options['backend']))
            snapshot = options['snapshot']
            self._read(snapshot)
            result = verify_result(request, decode_json(raw), path.parent,
                                   snapshot_root=(self.root/snapshot['path']).parent, expected_backend=backend)
            return dict(native_verification=result)
        # Reopen and restore require actual supported owning runtime adapters.
        # Claimed JSON receipts or a file header cannot substitute for execution.
        raise RigError('DIRECTOR_QA_ADAPTER_UNAVAILABLE', 'No installed actual adapter for '+operation+'.', stage='qa')

    def validate(self, request, *, cancel=None, persist=True, admit=None, native_context=None):
        validate_method_request('QAService.validate', request)
        self._read(self.project_reference)
        need(request['context']['project_id'] == self.project['id'], 'PROJECT', 'Request selects another project.')
        need(request['context']['project_revision_sha256'] == self.project_reference['sha256'],
             'STALE', 'Reload the exact Director project before validating it.')
        need(not request['input']['thresholds'], 'THRESHOLD',
             'Custom thresholds require a registered native measurement adapter; none may be silently ignored.')
        profile_ref = request['input']['check_profile']
        profile = validate_profile(decode_json(self._read(profile_ref)))
        provided = {object_hash(reference(ref)) for ref in request['input']['artifacts']}
        all_refs = list(artifact_references(profile))
        need(all(object_hash(ref) in provided for ref in all_refs), 'REFERENCE', 'Profile reads must be declared exact request artifacts.')
        unique = {object_hash(ref): ref for ref in [self.project_reference, profile_ref, *all_refs]}
        need(sum(ref['byte_size'] for ref in unique.values()) <= profile['limits']['max_total_bytes'], 'LIMIT', 'QA input bytes exceed the limit.')
        # Capture deadline before any check; cancellation never creates passing work.
        deadline = parse_rfc3339(request['context']['deadline'], code='CONTEXT')
        remaining = (deadline-datetime.now(timezone.utc)).total_seconds()
        need(remaining > 0, 'DEADLINE', 'QA deadline expired.')
        seconds = min(remaining, profile['limits']['max_seconds'])
        started = time.monotonic()
        def stopped():
            return bool(cancel and cancel()) or time.monotonic()-started >= seconds
        checks, diagnostics, findings = [], [], []
        canceled = False
        for check in profile['checks']:
            if cancel and cancel():
                canceled = True
                break
            if time.monotonic()-started >= seconds:
                diagnostics.append(_diagnostic(check, 'DIRECTOR_QA_DEADLINE', 'Bounded QA deadline exceeded.'))
                checks.append(dict(id=check['id'], operation=check['operation'], artifact=check['artifact'],
                                   status='blocked', evidence=[], details={}))
                break
            entry = dict(id=check['id'], operation=check['operation'], artifact=check['artifact'],
                         status='passed', evidence=[check['artifact']], details={})
            raw = None
            try:
                raw = self._read(check['artifact'])
                entry['details'] = self._check(check, raw, stopped)
            except (ImportError, RigError, ValueError, OSError) as exc:
                code = exc.report['code'] if isinstance(exc, RigError) else 'DIRECTOR_QA_'+type(exc).__name__.upper()
                message = exc.report['message'] if isinstance(exc, RigError) else str(exc)
                unavailable = isinstance(exc, ImportError) or 'UNAVAILABLE' in code or code == 'DIRECTOR_PLATFORM'
                entry['status'] = 'blocked' if unavailable else 'failed'
                diagnostics.append(_diagnostic(check, code, message))
                findings.append(_finding(check, code, message,
                                         hashlib.sha256(raw).hexdigest() if raw is not None else None))
            checks.append(entry)
        canceled = bool(canceled or cancel and cancel())
        expired = time.monotonic()-started >= seconds
        statuses = [entry['status'] for entry in checks]
        failed = 'failed' in statuses
        complete = len(checks) == len(profile['checks']) and not canceled and not expired
        report_status = 'failed' if failed else 'passed' if complete and all(s == 'passed' for s in statuses) else 'partial' if 'passed' in statuses else 'unverified'
        report = dict(schema_version=1, kind='director-qa-report', project=self.project_reference,
                      project_id=self.project['id'], fixture=self.fixture, inputs=list(unique.values()),
                      native_context=deepcopy(native_context),
                      request_sha256=object_hash(request), profile=profile_ref, status=report_status,
                      canceled=canceled, deadline_exceeded=expired, checks=checks, findings=findings,
                      unmeasured=['Subjective visual quality and artistic acceptance are not structural checks.'],
                      production_art_acceptance=False)
        if canceled:
            report['status'] = 'canceled'
        elif report_status == 'unverified':
            report['status'] = 'unavailable'
        report['sha256'] = object_hash(report)
        if persist and self.journal is not None:
            def storage_admit():
                self._read(self.project_reference)
                if report['status'] == 'passed':
                    need(not stopped(), 'DEADLINE', 'QA stopped before successful report publication.')
                    if admit is not None:
                        admit()
            self.journal.append_validation(report, request['context']['idempotency_key'], admit=storage_admit)
        measured = [dict(name='checks-passed', value=statuses.count('passed'), unit='checks',
                         evidence=[profile_ref])]
        result_status = ('canceled' if canceled else 'failed' if failed else 'succeeded' if report_status == 'passed'
                         else 'partial' if 'passed' in statuses else 'blocked_capability')
        if result_status != 'succeeded' and not diagnostics:
            global_check = dict(id='request', artifact=profile_ref, scene=None, shot=None)
            diagnostics.append(_diagnostic(global_check, 'DIRECTOR_QA_CANCELED' if canceled else 'DIRECTOR_QA_DEADLINE',
                                           'QA was canceled.' if canceled else 'QA did not finish within its deadline.'))
        result = dict(schema_version=1, kind='director-result', method='QAService.validate',
                      status=result_status,
                      operation_id=request['context']['request_id'], input_digest=object_hash(request),
                      value=(dict(measured=measured, unmeasured=report['unmeasured'], findings=diagnostics, status=report_status)
                             if result_status in ('succeeded', 'partial') else None),
                      artifacts=[], diagnostics=diagnostics, usage=_usage(), provenance_receipts=[])
        validate_method_result('QAService.validate', result, request)
        return dict(result=result, report=report, profile=profile)
