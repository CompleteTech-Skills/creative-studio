"""Director CLI and authenticated extensions of the existing native editor.

The EditorRepository, native services and production review journal remain the
only state authorities. Driver functions are installed by trusted host code;
HTTP clients cannot select Python modules, commands, folders or callbacks.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import MappingProxyType
import uuid

from director_contracts import (_read_json_file, validate_method_request,
                                validate_method_result, artifact_references, parse_rfc3339, _integer)
from director_schema import METHODS
from director_qa import QAService, need, object_hash, reference, validate_profile, _usage
from director_milestones import milestone_report
from rig_errors import RigError, decode_json

MAX_STATUS_BYTES = 2*1024**2
MAX_PREVIEW_BYTES = 16*1024**2


class DashboardService:
    def __init__(self, repository, artifact_root, project_reference, profile_reference,
                 *, drivers=None, driver_contexts=None, milestone_checks=None):
        from editor3d_repository import EditorRepository
        from production_workflow import NativeReviewJournal
        need(isinstance(repository, EditorRepository), 'OWNER', 'Use the authoritative native EditorRepository.')
        self.repository = repository
        self.qa = QAService(artifact_root, project_reference)
        self.profile_reference = deepcopy(reference(profile_reference))
        self.profile = validate_profile(decode_json(self.qa._read(profile_reference)))
        self.journal = NativeReviewJournal(repository.root, project_reference, artifact_root=self.qa.root)
        self.qa.journal = self.journal
        installed = dict(drivers or {})
        need(set(installed) <= set(METHODS) and all(callable(driver) for driver in installed.values()),
             'OWNER', 'Install known native service functions in trusted host code.')
        need(not set(installed) & {'QAService.validate', 'QAService.record_review'},
             'OWNER', 'The canonical QA/review owner cannot be replaced by a dashboard driver.')
        self.drivers = MappingProxyType(installed)
        contexts = dict(driver_contexts or {})
        need(set(contexts) <= set(installed) and all(value in ('director', 'native-scene') for value in contexts.values()),
             'OWNER', 'Declare the actual context owner of each native adapter.')
        self.driver_contexts = MappingProxyType({method: contexts.get(method, 'director') for method in installed})
        self.milestone_checks = dict(milestone_checks or {})

    def _native(self, body):
        need(isinstance(body, dict) and {'native_revision', 'native_sha256'} <= set(body),
             'STALE', 'Reload the saved native revision before this action.')
        snapshot = self.repository.snapshot()
        revision = _integer(body['native_revision'], 'native revision')
        need(revision == snapshot['revision'] and body['native_sha256'] == snapshot['sha256'],
             'STALE', 'Native project changed; reload saved state and reapply.')
        return snapshot

    def _document(self, ref):
        need(ref['byte_size'] <= MAX_STATUS_BYTES, 'LIMIT', 'Use the native owner to inspect this larger artifact.')
        return decode_json(self.qa._read(ref))

    def _operations(self):
        return {method: dict(available=method in self.drivers or method.startswith('QAService.'),
                             execution_verified=False,
                             context_owner=self.driver_contexts.get(method, 'director'),
                             reason='installed-owner' if method in self.drivers or method.startswith('QAService.') else 'owner-not-configured')
                for method in METHODS}

    def _changed_native_dimensions(self, native):
        """Use the native owner's exact active scene hash, never a new signature.

        Profiles may depend on immutable earlier scene versions. Their reviews
        remain history, while the active native scene makes relevant scopes stale.
        Unrelated artifact-only scopes remain reusable. We do not manufacture a
        new profile/reference or silently rebind an old approval to new content.
        """
        changed = set()
        active = native['inventory'][native['scene_path']]['sha256']
        for dimension, dependencies in self.profile['review_dependencies'].items():
            for ref in artifact_references(dependencies):
                if ref['kind'] == 'scene3d-scene' and ref['artifact_id'] == native['scene']['id'] and ref['sha256'] != active:
                    changed.add(dimension)
        return changed

    def _reviews(self, native):
        result = self.journal.status(self.profile['review_dependencies'])
        changed = self._changed_native_dimensions(native)
        for name in changed:
            item = result['dimensions'][name]
            if item['status'] != 'unreviewed':
                item.update(status='stale', stale_reason='The active native scene differs from the exact reviewed scene.')
        result['native_bindings_changed'] = sorted(changed)
        result['current_pass'] = bool(result['dimensions']) and not changed and all(
            item['status'] == 'pass' for item in result['dimensions'].values())
        return result

    def _production(self, reviews):
        if self.qa.project['schema_version'] != 2:
            return dict(schema_version=1, kind='director-production-status', status='partial',
                        milestones=deepcopy(self.qa.project['milestones']), migration_required=True,
                        remaining_work=['Explicitly migrate the selected Director project to v2 for complete evidence fields.'],
                        production_art_acceptance=False)
        return milestone_report(self.qa.project, self.profile_reference,
            owner_checks=self.milestone_checks, review_status=reviews)

    @staticmethod
    def _deadline(request):
        need(parse_rfc3339(request['context']['deadline'], code='CONTEXT') > datetime.now(timezone.utc),
             'DEADLINE', 'Operation deadline expired.')

    def status(self, body):
        need(isinstance(body, dict) and set(body) == {'native_revision', 'native_sha256'}, 'INPUT', 'Invalid status envelope.')
        native = self._native(body)
        self.qa._read(self.qa.project_reference)
        project = self.qa.project
        documents = {}
        for field in ('brief', 'story', 'shot_plan', 'asset_registry', 'execution_graph', 'budget_ledger'):
            ref = project[field]
            if ref is None:
                documents[field] = dict(available=False, reason='not-bound')
                continue
            try:
                documents[field] = dict(available=True, reference=ref, document=self._document(ref))
            except (RigError, ValueError, OSError):
                documents[field] = dict(available=False, reference=ref, reason='missing-modified-or-non-JSON-artifact')
        self.qa._read(self.profile_reference)
        reviews = self._reviews(native)
        value = dict(schema_version=1, kind='director-dashboard', project=self.qa.project_reference,
                     native=dict(revision=native['revision'], sha256=native['sha256'],
                                 project_id=native['project']['id'], scene_id=native['scene']['id']),
                     documents=documents, characters=project['characters'], exports=project['exports'],
                     profile=self.profile_reference, review_dependencies=self.profile['review_dependencies'],
                     inventory=project['inventory'], revisions=self.repository.history(),
                     native_nodes=[dict(id=node['id'], name=node['name'], type=node['type']) for node in native['scene']['nodes']],
                     reviews=reviews,
                     review_state=('reviewed' if reviews['current_pass'] else 'awaiting_review'
                         if any(row['status'] in ('unreviewed', 'stale') for row in reviews['dimensions'].values())
                         else 'changes_requested' if reviews['dimensions'] else 'unconfigured'),
                     validation=self.journal.validation_history(),
                     production=self._production(reviews),
                     operations=self._operations(), production_art_acceptance=False)
        need(len(json.dumps(value, allow_nan=False).encode()) <= MAX_STATUS_BYTES,
             'LIMIT', 'Dashboard response exceeds its bounded metadata profile; use scoped native inspection.')
        return value

    def qa_request(self):
        refs = list(artifact_references(self.profile))
        unique = {object_hash(ref): ref for ref in refs}
        return self.request('QAService.validate', dict(artifacts=list(unique.values()),
                            check_profile=self.profile_reference, thresholds=[]))

    def request(self, method, data):
        identifier = 'director-'+uuid.uuid4().hex
        return dict(schema_version=1, kind='director-request', method=method,
                    context=dict(project_id=self.qa.project['id'], project_revision_sha256=self.qa.project_reference['sha256'],
                        request_id=identifier, idempotency_key=identifier, attempt_id=identifier,
                        capability_snapshot_sha256='0'*64, budget_reservation_id=None, cancellation_token=identifier,
                        deadline=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat(),
                        policy=dict(network='deny', providers=[], paid='deny', policy_ref=None)), input=data)

    def validate(self, body):
        need(isinstance(body, dict) and set(body) in ({'native_revision', 'native_sha256'}, {'native_revision', 'native_sha256', 'request'}),
             'INPUT', 'Invalid QA envelope.')
        native = self._native(body)
        request = body['request'] if 'request' in body else self.qa_request()
        validate_method_request('QAService.validate', request)
        need(request['input']['check_profile'] == self.profile_reference, 'STALE', 'Use the active exact QA profile.')
        return self.qa.validate(request, admit=lambda: self._native(body),
            native_context=dict(revision=native['revision'], sha256=native['sha256'], scene_id=native['scene']['id']))

    def record_review(self, body):
        need(isinstance(body, dict) and set(body) == {'native_revision', 'native_sha256', 'request'}, 'INPUT', 'Invalid review envelope.')
        native = self._native(body)
        request = body['request']
        validate_method_request('QAService.record_review', request)
        self._deadline(request)
        self.qa._read(self.profile_reference)
        data = request['input']
        need(request['context']['project_id'] == self.qa.project['id'] and
             request['context']['project_revision_sha256'] == self.qa.project_reference['sha256'],
             'STALE', 'Review request selects an obsolete or foreign Director project.')
        need(data['authority'] in ('human', 'assistant'), 'REVIEW', 'External review requires its actual identified provider adapter.')
        need(self.profile_reference in data['evidence'], 'REVIEW', 'Review must pin the exact current scope profile.')
        declared = {object_hash(ref): ref for ref in artifact_references(data['evidence'])}
        need(len(declared) <= 4096 and sum(ref['byte_size'] for ref in declared.values()) <= 1024**3,
             'LIMIT', 'Review input evidence exceeds the bounded profile.')
        for ref in declared.values():
            self.qa._read(ref)
        decisions = {row['dimension']: row for row in data['decisions']}
        need(len(decisions) == len(data['decisions']) and len(set(data['scope'])) == len(data['scope'])
             and set(decisions) == set(data['scope'])
             and set(decisions) <= set(self.profile['review_dependencies']), 'REVIEW', 'Review exactly the selected current dimensions.')
        receipt = dict(schema_version=1, kind='director-native-review', project_id=self.qa.project['id'],
                       fixture=self.qa.fixture, reviewer=dict(kind=data['authority'], id=data['reviewer_id'], name=data['reviewer_id']),
                       dimensions={})
        need(not set(decisions) & self._changed_native_dimensions(native), 'STALE',
             'Update the changed native scene dependencies and inspect the new evidence before reviewing this scope.')
        for dimension, decision in decisions.items():
            need(decision['verdict'] in ('approved', 'rejected'), 'REVIEW', 'Record explicit inspected approval or rejection; unreviewed is not a review.')
            dependencies = self.profile['review_dependencies'][dimension]
            inspected = []
            for ref in dependencies:
                if ref not in data['evidence']:
                    continue
                media = ref['media_type']
                method = 'visual_inspection' if media.startswith('image/') else 'timed_playback' if media.startswith('video/') else 'automated_check'
                scopes = [check['frames'] for check in self.profile['checks'] if check['artifact'] == ref and check['frames'] is not None]
                need(len({json.dumps(scope, sort_keys=True) for scope in scopes}) <= 1, 'REVIEW', 'Disambiguate the inspected frame scope.')
                inspected.append(dict(artifact=ref, method=method, frames=scopes[0] if scopes else None,
                                      note=decision['rationale']))
            receipt['dimensions'][dimension] = dict(verdict='pass' if decision['verdict'] == 'approved' else 'fail',
                notes=decision['rationale'], dependencies=dependencies, evidence=inspected)
        def admit():
            self._deadline(request)
            current_native = self._native(body)
            need(not set(decisions) & self._changed_native_dimensions(current_native), 'STALE', 'Native review scope changed while it was being checked.')
        record = self.journal.record(receipt, request['context']['idempotency_key'],
                                     current_dependencies=self.profile['review_dependencies'], admit=admit)
        result = dict(schema_version=1, kind='director-result', method='QAService.record_review', status='succeeded',
                      operation_id=request['context']['request_id'], input_digest=object_hash(request), value=deepcopy(data),
                      artifacts=[], diagnostics=[], usage=_usage(), provenance_receipts=data['evidence'])
        validate_method_result('QAService.record_review', result, request)
        return dict(result=result, record=record, review_status=self._reviews(self.repository.snapshot()))

    def review_form(self, body):
        need(isinstance(body, dict) and set(body) == {'native_revision', 'native_sha256', 'reviewer_id',
             'authority', 'dimension', 'verdict', 'notes', 'inspected', 'transaction_id'},
             'INPUT', 'Invalid scoped review form.')
        need(body['inspected'] is True and body['dimension'] in self.profile['review_dependencies'],
             'REVIEW', 'Inspect the exact supplied evidence before recording this scope.')
        deps = self.profile['review_dependencies'][body['dimension']]
        request = self.request('QAService.record_review', dict(reviewer_id=body['reviewer_id'], authority=body['authority'],
            scope=[body['dimension']], decisions=[dict(dimension=body['dimension'], verdict=body['verdict'], rationale=body['notes'])],
            evidence=[self.profile_reference, *deps]))
        request['context']['idempotency_key'] = body['transaction_id']
        return self.record_review(dict(native_revision=body['native_revision'], native_sha256=body['native_sha256'], request=request))

    def action(self, body):
        need(isinstance(body, dict) and set(body) == {'native_revision', 'native_sha256', 'request'}, 'INPUT', 'Invalid native action envelope.')
        native = self._native(body)
        request = body['request']
        need(isinstance(request, dict) and request.get('method') in self.drivers,
             'ADAPTER_UNAVAILABLE', 'This operation has no configured executable native owner.')
        validate_method_request(request['method'], request)
        self._deadline(request)
        owner = self.driver_contexts[request['method']]
        project_id = native['project']['id'] if owner == 'native-scene' else self.qa.project['id']
        revision_sha = native['inventory']['project.json']['sha256'] if owner == 'native-scene' else self.qa.project_reference['sha256']
        need(request['context']['project_id'] == project_id and
             request['context']['project_revision_sha256'] == revision_sha,
             'STALE', 'Native action selects an obsolete or foreign Director project.')
        result = self.drivers[request['method']](request, deepcopy(native))
        validate_method_result(request['method'], result, request)
        return result

    def preview(self, body):
        need(isinstance(body, dict) and set(body) == {'native_revision', 'native_sha256', 'artifact_id'}, 'INPUT', 'Invalid preview envelope.')
        self._native(body)
        refs = [*self.qa.project['inventory'], *self.qa.project['exports'],
                *(ref for values in self.profile['review_dependencies'].values() for ref in values)]
        candidates = [ref for ref in refs if ref['artifact_id'] == body['artifact_id']]
        unique = {object_hash(ref): ref for ref in candidates}
        need(len(unique) == 1, 'REFERENCE', 'Select one exact current preview artifact.')
        ref = next(iter(unique.values()))
        need(ref['media_type'] in ('image/png', 'image/jpeg', 'video/mp4', 'video/webm') and
             ref['byte_size'] <= MAX_PREVIEW_BYTES, 'LIMIT', 'Preview supports bounded current images and movies.')
        raw = self.qa._read(ref)
        if ref['media_type'].startswith('image/'):
            details = self.qa._check(dict(operation='image.decode', options={}, artifact=ref), raw, None)
            need(details['format'] == ('PNG' if ref['media_type'] == 'image/png' else 'JPEG'),
                 'PREVIEW', 'Image bytes do not match the declared preview format.')
        return dict(artifact=ref, data_base64=base64.b64encode(raw).decode('ascii'), inspected=False)


def routes(service):
    return {'director/status': lambda server, body: service.status(body),
            'director/qa': lambda server, body: service.validate(body),
            'director/review': lambda server, body: service.record_review(body),
            'director/review-form': lambda server, body: service.review_form(body),
            'director/action': lambda server, body: service.action(body),
            'director/preview': lambda server, body: service.preview(body)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('serve', 'status', 'qa', 'review', 'action', 'graph', 'assets', 'storyboard', 'preview', 'capabilities'))
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--artifact-root', type=Path, required=True)
    parser.add_argument('--project-reference', type=Path, required=True)
    parser.add_argument('--profile-reference', type=Path, required=True)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        from editor3d_repository import EditorRepository
        service = DashboardService(EditorRepository(args.repository), args.artifact_root,
            _read_json_file(args.project_reference), _read_json_file(args.profile_reference))
        native = service.repository.snapshot()
        body = dict(native_revision=native['revision'], native_sha256=native['sha256'])
        if args.operation == 'serve':
            from editor3d_viewer import EditorServer
            server = EditorServer(service.repository, port=args.port, routes=routes(service))
            print(json.dumps(dict(status='listening', url=server.url)), flush=True)
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally: server.server_close()
            return 0
        if args.operation in ('qa', 'review', 'action'):
            if args.request is not None:
                body['request'] = _read_json_file(args.request)
            need(args.operation == 'qa' or 'request' in body, 'INPUT', 'This operation requires an exact request file.')
            result = getattr(service, 'validate' if args.operation == 'qa' else 'record_review' if args.operation == 'review' else 'action')(body)
        else:
            status = service.status(body)
            result = (status['operations'] if args.operation == 'capabilities' else
                      status['documents']['execution_graph'] if args.operation == 'graph' else
                      dict(characters=status['characters'], registry=status['documents']['asset_registry']) if args.operation == 'assets' else
                      status['documents']['shot_plan'] if args.operation == 'storyboard' else
                      dict(exports=status['exports'], inspection='Select an exact artifact through the authenticated dashboard.') if args.operation == 'preview' else status)
        print(json.dumps(result, allow_nan=False))
        outcome = result.get('result', result) if isinstance(result, dict) else {}
        return 0 if outcome.get('status') not in ('failed', 'blocked_capability', 'blocked_budget', 'canceled') else 1
    except (RigError, ValueError, OSError) as exc:
        report = exc.report if isinstance(exc, RigError) else dict(code='DIRECTOR_DASHBOARD_INPUT', message=str(exc))
        print(json.dumps(dict(status='failed', diagnostic=report)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
