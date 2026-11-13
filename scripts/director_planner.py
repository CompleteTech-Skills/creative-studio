"""Restricted, deterministic planning for the local two-robot Director MVP.

The result is an intent plan, not native geometry, a provider response, or a
completed production. Exact artifact bindings are created by the native build
and graph adapters after the plan has been admitted.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import re
from functools import lru_cache

from director_contracts import duration_frames
from rig_errors import RigError, finite_json, object_hash

GRAMMAR = r'two robots (?:explore|exploring) (?:an? )?(?:abandoned )?laboratory[.!]?'
DEFAULT_PROMPT = 'Two robots exploring an abandoned laboratory.'


def _require(condition, code, message):
    if not condition:
        raise RigError('DIRECTOR_PLAN_' + code, message, stage='planning')


def allocate_frames(total_frames, weights, minimum_frames=1):
    """Largest-remainder allocation; equal remainders favor original beat order."""
    _require(type(total_frames) is int and total_frames > 0, 'TIME', 'Total frames must be positive integer.')
    _require(type(minimum_frames) is int and minimum_frames > 0, 'TIME', 'Minimum frames must be positive integer.')
    _require(isinstance(weights, (list, tuple)) and 0 < len(weights) <= 256, 'TIME', 'Provide one to 256 beat weights.')
    _require(all(type(w) is int and 0 < w <= 1_000_000 for w in weights), 'TIME', 'Beat weights must be positive bounded integers.')
    remaining = total_frames - len(weights) * minimum_frames
    _require(remaining >= 0, 'TIME', 'Duration cannot satisfy every shot minimum.')
    shares = [Fraction(remaining * weight, sum(weights)) for weight in weights]
    result = [minimum_frames + int(share) for share in shares]
    ranking = sorted(range(len(weights)), key=lambda i: (-(shares[i] - int(shares[i])), i))
    for index in ranking[:total_frames - sum(result)]:
        result[index] += 1
    return result


def parse_request(prompt):
    _require(isinstance(prompt, str) and 0 < len(prompt) <= 4096, 'INPUT', 'Prompt must be one to 4096 characters.')
    _require(re.fullmatch(GRAMMAR, ' '.join(prompt.lower().split())) is not None,
             'UNSUPPORTED', 'Supported grammar: Two robots explore/exploring [an] [abandoned] laboratory. Use a structured plan for other requests.')
    return {'original_prompt': prompt, 'parser': 'restricted-two-robot-laboratory-v1',
            'inference_kind': 'deterministic', 'character_count': 2,
            'setting': 'abandoned laboratory' if 'abandoned' in prompt.lower() else 'laboratory',
            'explicit': ['Two robots', 'Explore a laboratory'],
            'assumptions': ['Five-second duration at 24 frames per second.',
                            'Two shots, persistent robot identities Aster and Moss.',
                            'Aster investigates the access key while Moss examines the console.',
                            'Silent visual storytelling; no sound track is generated.']}


def build_intent(prompt=DEFAULT_PROMPT, *, project_id='robot-laboratory', weights=(1, 1)):
    """Construct a deterministic plan awaiting capability/budget admission."""
    _require(isinstance(project_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', project_id),
             'INPUT', 'Invalid project identity.')
    _require(isinstance(weights, (list, tuple)) and len(weights) == 2, 'TIME', 'The MVP requires exactly two shots.')
    interpreted = parse_request(prompt)
    fps = {'numerator': 24, 'denominator': 1}
    total = duration_frames(5, fps)
    lengths = allocate_frames(total, weights, minimum_frames=24)
    first_end = lengths[0]
    plan = {'schema_version': 1, 'kind': 'director-mvp-intent', 'project_id': project_id,
            'interpretation': interpreted, 'state': 'planned', 'production_complete': False,
            'audio_policy': 'zero-audio-streams', 'frame_rate': fps, 'duration_frames': total,
            'resolution': [640, 360], 'characters': [
                {'id': 'aster', 'role': 'investigator', 'required_action': 'articulated exploration'},
                {'id': 'moss', 'role': 'console investigator', 'required_action': 'independent articulated exploration'}],
            'props': [{'id': 'access-key', 'motion': 'lift and carry by Aster'}],
            'environment': {'id': 'laboratory', 'description': interpreted['setting']},
            'story': {'logline': 'Two robots discover an access key and inspect a laboratory console.',
                      'beginning': 'Both robots enter the laboratory and inspect their surroundings.',
                      'middle': 'Aster locates the key; Moss turns toward the console.',
                      'end': 'Aster lifts the key while Moss signals toward the console.',
                      'communication': ['body pose', 'head direction', 'arm gesture'],
                      'unresolved_assumptions': deepcopy(interpreted['assumptions'])},
            'shots': [
                {'id': 'establish', 'start_frame': 0, 'end_frame': first_end, 'duration_frames': lengths[0],
                 'camera_id': 'wide', 'actors': ['aster', 'moss'], 'props': ['access-key'],
                 'actions': ['aster turns and inspects access-key', 'moss turns toward console'],
                 'composition': 'Wide view keeps both robots, key and console visible.',
                 'transition': 'cut', 'markers': [{'id': 'key-noticed', 'frame': first_end - 1, 'target': 'aster'}]},
                {'id': 'discovery', 'start_frame': first_end, 'end_frame': total, 'duration_frames': lengths[1],
                 'camera_id': 'investigation', 'actors': ['aster', 'moss'], 'props': ['access-key'],
                 'actions': ['aster lifts and carries access-key', 'moss raises arm toward console'],
                 'composition': 'Closer angled view includes the key pickup and Moss gesture.',
                 'transition': 'cut', 'markers': [{'id': 'key-lift', 'frame': first_end + lengths[1] // 3, 'target': 'access-key'}]}],
            'required_capabilities': ['model3d.procedural', 'scene3d.assemble', 'animation3d.evaluate',
                                      'render3d.blender.cpu', 'export.silent.mp4'],
            'estimates': {'provider_calls': 0, 'frames': total, 'pixels': total * 640 * 360},
            'admission': None}
    validate_intent(plan)
    return plan


def validate_intent(plan):
    finite_json(plan)
    error = next(_validator().iter_errors(plan), None)
    _require(error is None, 'SCHEMA', error.message if error else '')
    _require(isinstance(plan, dict) and set(plan) == {'schema_version', 'kind', 'project_id', 'interpretation', 'state',
        'production_complete', 'audio_policy', 'frame_rate', 'duration_frames', 'resolution', 'characters',
        'props', 'environment', 'story', 'shots', 'required_capabilities', 'estimates', 'admission'}, 'SCHEMA', 'Invalid intent fields.')
    _require(plan['schema_version'] == 1 and plan['kind'] == 'director-mvp-intent', 'SCHEMA', 'Unknown intent version.')
    _require(plan['state'] in ('planned', 'admitted') and plan['production_complete'] is False, 'STATE', 'Plans cannot claim production completion.')
    _require(plan['audio_policy'] == 'zero-audio-streams', 'AUDIO', 'MVP delivery must be audio-free.')
    _require(plan['frame_rate'] == {'numerator': 24, 'denominator': 1} and plan['duration_frames'] == 120
             and plan['resolution'] == [640, 360], 'TIME', 'MVP requires exactly 120 frames at 24 fps and 640x360.')
    _require([item['id'] for item in plan['characters']] == ['aster', 'moss'], 'IDENTITY', 'MVP requires exactly Aster and Moss.')
    _require(len(plan['shots']) == 2, 'SHOTS', 'MVP requires exactly two shots.')
    cursor = 0
    ids = set()
    for shot in plan['shots']:
        _require(shot['id'] not in ids, 'SHOTS', 'Duplicate shot identity.')
        ids.add(shot['id'])
        _require(type(shot['start_frame']) is int and type(shot['end_frame']) is int and
                 shot['start_frame'] == cursor and shot['end_frame'] > cursor and
                 shot['duration_frames'] == shot['end_frame'] - cursor, 'TIME', 'Shot gap, overlap or invalid interval.')
        _require(shot['transition'] == 'cut' and shot['actors'] == ['aster', 'moss'], 'SHOTS', 'Unsupported shot transition or missing robot.')
        _require(bool(shot['actions']) and bool(shot['composition']), 'STORY', 'Each shot needs visible actions and composition.')
        for marker in shot['markers']:
            _require(type(marker['frame']) is int and cursor <= marker['frame'] < shot['end_frame'], 'TIME', 'Marker outside shot.')
            _require(marker['target'] in ['aster', 'moss', 'access-key'], 'REFERENCE', 'Unknown marker target.')
        cursor = shot['end_frame']
    _require(cursor == 120, 'TIME', 'Shots do not cover the complete duration.')
    _require((plan['state'] == 'admitted') == (plan['admission'] is not None), 'ADMISSION', 'Plan admission state mismatch.')
    return plan


def intent_digest(plan):
    validate_intent(plan)
    return object_hash(plan)


def admit_intent(plan, snapshot, *, evidence_root, resources, budget_snapshot):
    """Recheck real capability evidence and bind admission to this exact intent."""
    from director_capabilities import assert_admission
    validate_intent(plan)
    _require(plan['state'] == 'planned', 'STATE', 'Only an unadmitted plan may be admitted.')
    validate_budget(budget_snapshot)
    _require(isinstance(resources, dict) and set(resources) == set(budget_snapshot['limits']), 'BUDGET', 'Resource estimates must cover the budget dimensions.')
    for name, amount in resources.items():
        _require(type(amount) in (int, float) and 0 <= amount <= budget_snapshot['limits'][name], 'BUDGET', 'Estimated resources exceed the project budget: ' + name)
    operations = ['blender.cpu', 'codec.h264', 'importer.glb']
    assert_admission(snapshot, operations, evidence_root=evidence_root, resources=resources)
    admitted = deepcopy(plan)
    admitted['state'] = 'admitted'
    admitted['admission'] = {'input_sha256': object_hash(plan), 'capability_sha256': object_hash(snapshot),
                             'required_operations': operations, 'resources': deepcopy(resources), 'budget_snapshot': deepcopy(budget_snapshot)}
    validate_intent(admitted)
    return admitted


def validate_admitted(plan, snapshot, *, evidence_root):
    """Admission does not confer execution: recheck evidence immediately before use."""
    from director_capabilities import assert_admission
    validate_intent(plan)
    _require(plan['state'] == 'admitted', 'ADMISSION', 'Plan was not admitted.')
    unsigned = deepcopy(plan)
    unsigned.update(state='planned', admission=None)
    proof = plan['admission']
    validate_budget(proof['budget_snapshot'])
    _require(all(amount <= proof['budget_snapshot']['limits'][name] for name, amount in proof['resources'].items()), 'BUDGET', 'Resources exceed the project budget.')
    _require(proof['input_sha256'] == object_hash(unsigned) and
             proof['capability_sha256'] == object_hash(snapshot), 'ADMISSION', 'Stale or rebound plan admission.')
    assert_admission(snapshot, proof['required_operations'], evidence_root=evidence_root, resources=proof['resources'])
    return plan


def schemas():
    """Closed schema for intent data; does not alter native Director documents."""
    from rig_contracts import obj, arr
    text = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
    identifier = {'type': 'string', 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(?![\s\S])'}
    integer = {'type': 'integer', 'minimum': 0, 'maximum': 2_000_000_000}
    texts = arr(text, 0, 256)
    marker = obj({'id': identifier, 'frame': integer, 'target': {'enum': ['aster', 'moss', 'access-key']}})
    shot = obj({'id': identifier, 'start_frame': integer, 'end_frame': integer, 'duration_frames': integer,
                'camera_id': identifier, 'actors': {'const': ['aster', 'moss']}, 'props': {'const': ['access-key']},
                'actions': arr(text, 1, 16), 'composition': text, 'transition': {'const': 'cut'},
                'markers': arr(marker, 1, 16)})
    limits = obj({name: {'type': 'number', 'minimum': 0, 'maximum': 1e15}
                  for name in ['max_cpu_seconds', 'max_memory_bytes', 'max_storage_bytes', 'max_provider_calls']})
    budget = obj({'schema_version': {'const': 1}, 'kind': {'const': 'director-local-budget'},
                  'limits': limits, 'revision_sha256': {'type': 'string', 'pattern': r'^[0-9a-f]{64}(?![\s\S])'}})
    admission = obj({'input_sha256': {'type': 'string', 'pattern': r'^[0-9a-f]{64}(?![\s\S])'},
                     'capability_sha256': {'type': 'string', 'pattern': r'^[0-9a-f]{64}(?![\s\S])'},
                     'required_operations': {'const': ['blender.cpu', 'codec.h264', 'importer.glb']},
                     'resources': limits, 'budget_snapshot': budget})
    result = obj({'schema_version': {'const': 1}, 'kind': {'const': 'director-mvp-intent'}, 'project_id': identifier,
                  'interpretation': obj({'original_prompt': text, 'parser': {'const': 'restricted-two-robot-laboratory-v1'},
                    'inference_kind': {'const': 'deterministic'}, 'character_count': {'const': 2}, 'setting': text,
                    'explicit': texts, 'assumptions': texts}),
                  'state': {'enum': ['planned', 'admitted']}, 'production_complete': {'const': False},
                  'audio_policy': {'const': 'zero-audio-streams'}, 'frame_rate': {'const': {'numerator': 24, 'denominator': 1}},
                  'duration_frames': {'const': 120}, 'resolution': {'const': [640, 360]},
                  'characters': arr(obj({'id': {'enum': ['aster', 'moss']}, 'role': text, 'required_action': text}), 2, 2),
                  'props': arr(obj({'id': {'const': 'access-key'}, 'motion': text}), 1, 1),
                  'environment': obj({'id': {'const': 'laboratory'}, 'description': text}),
                  'story': obj({'logline': text, 'beginning': text, 'middle': text, 'end': text,
                                'communication': arr(text, 1, 16), 'unresolved_assumptions': texts}),
                  'shots': arr(shot, 2, 2), 'required_capabilities': {'const': ['model3d.procedural', 'scene3d.assemble', 'animation3d.evaluate', 'render3d.blender.cpu', 'export.silent.mp4']},
                  'estimates': obj({'provider_calls': {'const': 0}, 'frames': {'const': 120}, 'pixels': {'const': 27648000}}),
                  'admission': {'anyOf': [admission, {'type': 'null'}]}})
    result.update({'$schema': 'https://json-schema.org/draft/2020-12/schema', 'title': 'Director local MVP intent v1'})
    return {'director-mvp-intent.schema.json': result}


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(schemas()['director-mvp-intent.schema.json'])


def create_budget(limits):
    budget = {'schema_version': 1, 'kind': 'director-local-budget', 'limits': deepcopy(limits)}
    budget['revision_sha256'] = object_hash(budget)
    validate_budget(budget)
    return budget


def validate_budget(budget):
    import math
    _require(isinstance(budget, dict) and set(budget) == {'schema_version', 'kind', 'limits', 'revision_sha256'} and
             budget['schema_version'] == 1 and budget['kind'] == 'director-local-budget', 'BUDGET', 'Invalid budget snapshot.')
    limits = budget['limits']
    _require(isinstance(limits, dict) and set(limits) == {'max_cpu_seconds', 'max_memory_bytes', 'max_storage_bytes', 'max_provider_calls'}, 'BUDGET', 'Invalid budget dimensions.')
    _require(all(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e15 for value in limits.values()) and
             limits['max_provider_calls'] == 0, 'BUDGET', 'Invalid local budget limits; paid/provider work is not supported.')
    unsigned = {key: value for key, value in budget.items() if key != 'revision_sha256'}
    _require(budget['revision_sha256'] == object_hash(unsigned), 'BUDGET', 'Stale budget revision.')
    return budget


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    from director_contracts import _read_json_file
    from rig_errors import decode_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt', default=DEFAULT_PROMPT)
    parser.add_argument('--project-id', default='robot-laboratory')
    parser.add_argument('--capabilities', type=Path)
    parser.add_argument('--evidence-root', type=Path)
    parser.add_argument('--resources', type=Path)
    parser.add_argument('--budget', type=Path)
    parser.add_argument('--check-schema', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.check_schema:
            path = Path(__file__).resolve().parents[1] / 'profiles/director-mvp-intent.schema.json'
            _require(json.loads(path.read_text(encoding='utf-8')) == schemas()[path.name], 'SCHEMA', 'Generated intent schema drift.')
            return 0
        plan = build_intent(args.prompt, project_id=args.project_id)
        supplied = [args.capabilities is not None, args.evidence_root is not None, args.resources is not None, args.budget is not None]
        _require(not any(supplied) or all(supplied), 'INPUT', 'Admission requires capabilities, evidence root, resources and budget together.')
        if all(supplied):
            plan = admit_intent(plan, decode_json(_read_json_file(args.capabilities)),
                                evidence_root=args.evidence_root, resources=decode_json(_read_json_file(args.resources)),
                                budget_snapshot=decode_json(_read_json_file(args.budget)))
        print(json.dumps(plan, indent=2, allow_nan=False))
        return 0
    except (RigError, OSError) as exc:
        print(json.dumps({'status': 'failed', 'diagnostic': exc.report if isinstance(exc, RigError) else str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
