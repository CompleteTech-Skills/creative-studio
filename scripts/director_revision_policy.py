"""Prepare immutable, UNSELECTED logical revisions; never activate native state.

Bindings and publication are installed application owners. This helper provides
neither native snapshot registration nor the Root preactivation/ONE HEAD hook.
"""
from copy import deepcopy
import hashlib
import unicodedata

from jsonschema import Draft202012Validator
import director_contracts as contracts
from director_revision import MAX_PATCH_BYTES, _bytes, _hash, _keys, _constraints
from rig_errors import RigError, decode_json

_INVALIDATED = frozenset(('scene', 'rendering', 'video', 'native_editability',
                          'visual_qa', 'delivery'))
_FIELDS = frozenset(('scenes', 'timelines', 'render_configuration'))


def _need(ok, code, message):
    if not ok:
        raise RigError('DIRECTOR_LOGICAL_' + code, message, stage='director-revision-policy')


def _version(value):
    # The canonical ref/document validators validate syntax before this helper.
    return tuple(int(part) for part in value.split('.'))


class LogicalRevisionPolicy:
    """Trusted-owner policy, independent of mutable EditorRepository state.

    ``binding_resolver(project_ref, patch_ref)`` must resolve exact registered
    inputs and return logical_project, patch, replacements, invalidated_reviews,
    invalidated_exports. Each replacement contains field, before and after.
    No binding or approval boolean is accepted from a serialized request.
    Resolver authority does not itself authorize a protected native edit.
    """
    def __init__(self, artifact_root, *, binding_resolver=None, publish_artifact=None):
        _need(binding_resolver is None or callable(binding_resolver), 'DEPENDENCY',
              'Install the trusted native/logical binding owner.')
        _need(publish_artifact is None or callable(publish_artifact), 'DEPENDENCY',
              'Install the canonical immutable document publisher.')
        self.artifact_root = artifact_root
        self.binding_resolver = binding_resolver
        self.publish_artifact = publish_artifact

    def _ref(self, reference):
        schema = contracts.schemas()['director.schema.json']
        validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/artifact-ref'})
        _need(next(validator.iter_errors(reference), None) is None, 'REFERENCE',
              'Use a canonical exact ArtifactRef.')
        path = contracts.portable_name(reference['path'])
        _need(path == unicodedata.normalize('NFC', path), 'REFERENCE', 'Artifact paths must use Unicode NFC.')
        return reference

    def _read(self, reference):
        self._ref(reference)
        return contracts._read_artifact(self.artifact_root, reference)

    def _document(self, reference):
        document = contracts.validate_document(decode_json(self._read(reference)))
        _need((document.get('id'), document.get('kind'), document.get('schema_version'),
               document.get('semantic_version')) ==
              (reference['artifact_id'], reference['kind'], reference['schema_version'],
               reference['semantic_version']), 'IDENTITY', 'Document identity differs from its exact reference.')
        return document

    def _closure(self, value):
        refs = list(contracts.artifact_references(value))
        unique = {}
        paths = {}
        while refs:
            _need(len(refs) <= contracts.MAX_FILES * 8, 'LIMIT', 'Reference closure exceeds its bound.')
            ref = refs.pop()
            self._ref(ref)
            key = (ref['artifact_id'], ref['semantic_version'])
            _need(key not in unique or unique[key] == ref, 'REFERENCE', 'Ambiguous artifact version binding.')
            alias = ref['path'].casefold()
            _need(alias not in paths or paths[alias] == ref, 'REFERENCE', 'Conflicting artifact path or alias.')
            if key in unique:
                continue
            unique[key] = ref
            paths[alias] = ref
            _need(len(unique) <= contracts.MAX_FILES and
                  sum(r['byte_size'] for r in unique.values()) <= contracts.MAX_TOTAL_BYTES,
                  'LIMIT', 'Reference byte closure exceeds its bound.')
            self._read(ref)
            refs.extend(contracts.artifact_references(ref['dependencies']))
            if ref['kind'].removeprefix('director-') in contracts.DOCUMENTS:
                refs.extend(contracts.artifact_references(self._document(ref)))
        contracts._acyclic({key: {(d['artifact_id'], d['semantic_version']) for d in ref['dependencies']}
                            for key, ref in unique.items()}, 'logical revision artifact')
        return list(unique.values())

    def prepare(self, project_ref, patch_ref, *, expected_project_sha256, next_version):
        """Read exact immutable inputs and return a candidate; no bytes are written."""
        _need(callable(self.binding_resolver), 'DEPENDENCY',
              'Canonical full native snapshot registration and exact binding owner are required.')
        project = self._document(project_ref)
        _need(project['kind'] == 'director-project' and project['schema_version'] == 1,
              'DEPENDENCY', 'This bounded policy accepts DirectorProject v1; v2 needs owner integration.')
        _need(project_ref['sha256'] == expected_project_sha256, 'STALE',
              'Expected logical project hash differs from the immutable input.')
        self._ref(patch_ref)
        _need(patch_ref['kind'] == 'director-revision-patch' and patch_ref['byte_size'] <= MAX_PATCH_BYTES,
              'PATCH', 'Use an exact bounded native revision patch.')
        patch = decode_json(self._read(patch_ref), max_bytes=MAX_PATCH_BYTES)
        _keys(patch, {'schema_version', 'kind', 'instruction', 'parent', 'change', 'locked_constraints', 'sha256'})
        _need(patch['kind'] == 'director-revision-patch' and patch['schema_version'] == 1 and
              patch['sha256'] == _hash({k: v for k, v in patch.items() if k != 'sha256'}),
              'PATCH', 'Malformed native revision patch digest.')
        _need(isinstance(patch['change'], dict), 'PATCH', 'A typed native operation is required.')
        operation = patch['change'].get('operation')
        _need(operation in ('camera', 'camera-pose', 'material', 'world'), 'DEPENDENCY',
              'Duration, ending and story revisions require exact logical story/shot owner integration.')
        required = {'camera': {'operation', 'target', 'properties'},
                    'camera-pose': {'operation', 'target', 'transform'},
                    'material': {'operation', 'target', 'slot', 'surface'},
                    'world': {'operation', 'target', 'properties'}}[operation]
        _keys(patch['change'], required)
        details = patch['change'].get('transform', patch['change'].get('surface', patch['change'].get('properties')))
        _need(isinstance(patch['change']['target'], str) and 0 < len(patch['change']['target']) <= 256 and
              isinstance(details, dict) and bool(details), 'PATCH', 'Use an explicit native target and change object.')
        _need(isinstance(patch['instruction'], str) and 0 < len(patch['instruction']) <= 16384 and
              isinstance(patch['locked_constraints'], list) and len(patch['locked_constraints']) <= 256 and
              all(isinstance(lock, str) and 0 < len(lock) <= 1024 for lock in patch['locked_constraints']),
              'PATCH', 'Malformed original instruction or locked constraints.')
        old_refs = self._closure(project)
        self._closure(patch_ref)
        _need(isinstance(patch['parent'], dict) and 'artifact' in patch['parent'], 'BINDING',
              'Patch requires its exact registered native parent ArtifactRef.')
        native_parent = patch['parent']['artifact']
        _keys(patch['parent'], {'revision', 'sha256', 'project_sha256', 'scene_path', 'artifact'})
        self._read(native_parent)
        _need(native_parent in old_refs and native_parent['sha256'] == patch['parent'].get('project_sha256'),
              'BINDING', 'Patch native parent is absent from the logical project or has a different hash.')
        brief = self._document(project['brief'])
        _need(brief['kind'] == 'director-creative-brief' and brief['project_id'] == project['id'],
              'IDENTITY', 'Original brief must bind this exact logical project identity.')
        locks = _constraints({'project': {'metadata': {}}}, brief['requirements']['locked'])
        _need(set(locks) <= set(patch['locked_constraints']), 'IDENTITY',
              'Patch omitted locked requirements from the exact original brief.')
        binding = deepcopy(self.binding_resolver(deepcopy(project_ref), deepcopy(patch_ref)))
        _need(isinstance(binding, dict) and set(binding) == {
            'logical_project', 'patch', 'replacements', 'invalidated_reviews', 'invalidated_exports'},
            'BINDING', 'Trusted owner must provide a complete exact replacement binding.')
        _need(binding['logical_project'] == project_ref and binding['patch'] == patch_ref,
              'STALE', 'Trusted binding belongs to different immutable project or patch bytes.')
        replacements = binding['replacements']
        _need(isinstance(replacements, list) and 0 < len(replacements) <= 256,
              'BINDING', 'Supply bounded actual changed native references.')
        candidate = deepcopy(project)
        seen = set()
        for replacement in replacements:
            _need(isinstance(replacement, dict) and set(replacement) == {'field', 'before', 'after'},
                  'BINDING', 'Use an exact field/before/after replacement.')
            field, before, after = (replacement[k] for k in ('field', 'before', 'after'))
            _need(isinstance(field, str) and field in _FIELDS, 'IDENTITY',
                  'Original request, brief, story, identity and asset registry are immutable here.')
            self._read(before); self._read(after)
            _need(before['artifact_id'] == after['artifact_id'] and before['kind'] == after['kind'] and
                  before['schema_version'] == after['schema_version'] and
                  _version(after['semantic_version']) > _version(before['semantic_version']) and
                  before['sha256'] != after['sha256'], 'VERSION',
                  'Changed native references must retain identity and advance immutable versions and bytes.')
            key = (field, before['artifact_id'])
            _need(key not in seen, 'BINDING', 'Ambiguous duplicate replacement.')
            seen.add(key)
            if isinstance(project[field], list):
                _need(project[field].count(before) == 1 and
                      sum(r['artifact_id'] == before['artifact_id'] for r in project[field]) == 1,
                      'BINDING', 'Before reference must occur exactly once in its selected logical field.')
                candidate[field][project[field].index(before)] = deepcopy(after)
            else:
                _need(project[field] == before, 'BINDING', 'Scalar native binding does not match the logical document.')
                candidate[field] = deepcopy(after)
        for field in ('reviews', 'exports'):
            invalid = binding['invalidated_' + field]
            _need(isinstance(invalid, list) and len(invalid) <= 256 and
                  len({_hash(r) for r in invalid}) == len(invalid) and
                  all(project[field].count(r) == 1 for r in invalid),
                  'BINDING', 'Invalidated ' + field + ' must be exact unique existing references.')
            candidate[field] = [r for r in candidate[field] if r not in invalid]
        # Existing inventory is historical closure, never a guessed active map.
        # Keep old bytes there and register changed resources separately.
        for replacement in replacements:
            if replacement['after'] not in candidate['inventory']:
                candidate['inventory'].append(deepcopy(replacement['after']))
        for ref in (project_ref, patch_ref):
            if ref not in candidate['dependencies']:
                candidate['dependencies'].append(deepcopy(ref))
        invalidated = _INVALIDATED | ({'assets'} if operation == 'material' else set())
        for milestone in candidate['milestones']:
            if milestone['name'] in invalidated:
                milestone.update(status='unverified', evidence=[], diagnostics=[], authority='automated',
                                 scope='Changed native inputs; regeneration and current review are pending.')
        candidate.update(semantic_version=next_version, status='in-progress')
        contracts.validate_document(candidate)
        _need(_version(next_version) > _version(project['semantic_version']), 'VERSION',
              'Logical project semantic version must advance.')
        self._closure(candidate)
        dependencies = [deepcopy(project_ref), deepcopy(patch_ref)]
        return dict(state='immutable-candidate-unselected', project=candidate,
                    project_ref=deepcopy(project_ref), patch_ref=deepcopy(patch_ref),
                    expected_project_sha256=expected_project_sha256, next_version=next_version,
                    dependencies=dependencies, sha256=hashlib.sha256(_bytes(candidate)).hexdigest())

    def publish(self, prepared):
        """Publish immutable bytes only; Root must separately activate the pair."""
        _need(callable(self.publish_artifact), 'DEPENDENCY', 'Canonical DocumentPublisher is required.')
        _need(isinstance(prepared, dict) and set(prepared) == {'state', 'project', 'project_ref', 'patch_ref',
              'expected_project_sha256', 'next_version', 'dependencies', 'sha256'},
              'CANDIDATE', 'Use the complete prepared immutable candidate.')
        checked = self.prepare(prepared['project_ref'], prepared['patch_ref'],
                               expected_project_sha256=prepared['expected_project_sha256'],
                               next_version=prepared['next_version'])
        _need(prepared == checked, 'CANDIDATE', 'Candidate or trusted bindings changed before publication.')
        project = checked['project']; payload = _bytes(project)
        fixture = checked['project_ref']['fixture'] or checked['patch_ref']['fixture'] or any(
            ref['fixture'] for ref in self._closure(project))
        reference = self.publish_artifact(payload, artifact_id=project['id'], kind='director-project',
            schema_version=1, semantic_version=project['semantic_version'],
            dependencies=deepcopy(checked['dependencies']), fixture=fixture)
        self._ref(reference)
        _need(reference['artifact_id'] == project['id'] and reference['kind'] == 'director-project' and
              reference['schema_version'] == 1 and reference['semantic_version'] == project['semantic_version'] and
              reference['dependencies'] == checked['dependencies'] and reference['fixture'] == fixture and
              self._read(reference) == payload, 'PUBLICATION',
              'Canonical publisher did not preserve exact project bytes and predecessor/patch dependencies.')
        self._document(reference)
        return dict(state='published-unselected', project=deepcopy(reference),
                    native_mutation=False, activation='requires-root-preactivation-one-head')

    def rollback(self, publication):
        """Retain an unselected immutable candidate; never undo native state."""
        _need(isinstance(publication, dict) and publication.get('state') == 'published-unselected' and
              publication.get('native_mutation') is False, 'CANDIDATE', 'Only an unselected publication can be abandoned.')
        self._document(publication['project'])
        return dict(state='abandoned-unselected', retained_artifact=deepcopy(publication['project']),
                    native_mutation=False)
