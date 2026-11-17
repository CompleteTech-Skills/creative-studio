"""Explicit synthetic continuity documents; never production approval evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from continuity_contracts import definitions, digest, canonical_bytes, VERSION

STAMP = '2026-10-24T00:00:00Z'


def pointer(path='mesh.obj', payload=b'# Synthetic geometry transport fixture\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n', *, kind='mesh', identity='fixture'):
    return dict(id=identity, version='1.0.0', path=path, sha256=hashlib.sha256(payload).hexdigest(), kind=kind)


def sample(kind):
    defs = definitions()
    def build(schema):
        if '$ref' in schema:
            name = schema['$ref'].split('/')[-1]
            if name == 'ref':
                return pointer()
            if name == 'quaternion':
                return [0, 0, 0, 1]
            return build(defs[name])
        if 'const' in schema:
            return schema['const']
        if 'enum' in schema:
            return schema['enum'][0]
        if 'anyOf' in schema:
            return None
        typ = schema['type']
        if typ == 'object':
            return {key: build(value) for key, value in schema.get('properties', {}).items() if key in schema.get('required', [])}
        if typ == 'array':
            return [build(schema['items']) for _ in range(schema.get('minItems', 0))]
        if isinstance(typ, list):
            return None
        if typ == 'boolean':
            return False
        if typ in ('integer', 'number'):
            return max(0, schema.get('minimum', 0), schema.get('exclusiveMinimum', -1) + 1)
        if typ == 'string':
            pattern = schema.get('pattern', '')
            if '0-9a-f' in pattern:
                return '0' * 64
            if '\\.' in pattern:
                return '1.0.0'
            if 'T[0-9]' in pattern:
                return STAMP
            return 'fixture'
        raise ValueError(schema)
    value = build(defs[kind])
    def fix(item):
        if isinstance(item, dict):
            if 'start' in item and 'end' in item:
                item.update(start=dict(numerator=0, denominator=1), end=dict(numerator=1, denominator=1))
            if item.get('kind') == 'continuity-link':
                item['outgoing']['boundary'] = 'terminal'
            for child in item.values():
                fix(child)
        elif isinstance(item, list):
            for child in item:
                fix(child)
    fix(value)
    if kind == 'identity':
        value.update(identity_id='robot', namespace='test', approval_status='unreviewed', archetype='robot',
                     display_name='Fixture robot', description='Synthetic engineering fixture, not production art.')
        value['provenance']['fixture'] = True
        value['provenance']['origin'] = 'procedural-fixture'
    if kind == 'revision':
        value.update(identity_id='robot', revision_id='robot-r1', semantic_version='1.0.0')
        value['source_provenance'].update(fixture=True, origin='procedural-fixture')
        value['components'][0]['parent_id'] = None
        value['bounds'] = None
        value['reference_renders'] = [pointer('reference.ppm', b'P6\n1 1\n255\n\xff\x00\x00', kind='evidence', identity='reference')]
    if kind == 'rig-compatibility':
        value['status'] = 'unverified'
    if kind == 'comparison':
        value['status'] = 'needs_visual_review'
    if kind == 'entity':
        value.update(identity_id='robot', revision=dict(identity_id='robot', revision_id='robot-r1', semantic_version='1.0.0', sha256='0'*64))
    return value


def write_assets(root):
    root = Path(root)
    root.mkdir(exist_ok=True)
    (root/'mesh.obj').write_bytes(b'# Synthetic geometry transport fixture\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
    (root/'reference.ppm').write_bytes(b'P6\n1 1\n255\n\xff\x00\x00')


def review_for(root, revision, *, name='review1'):
    review = sample('review')
    review.update(review_id=name, reviewer='authored test decision supplied to exercise the gate',
        reviewer_kind='assistant', fixture=True, subject_sha256=digest(revision),
        notes='Synthetic gate fixture, not an actual visual review or production approval.',
        inspected=[deepcopy(revision['geometry']), *deepcopy(revision['reference_renders'])])
    raw = {k:v for k,v in review.items() if k != 'native_receipt'}
    payload = canonical_bytes(raw)
    path = name + '.json'
    (Path(root)/path).write_bytes(payload)
    review['native_receipt'] = pointer(path, payload, kind='evidence', identity=name)
    return review
