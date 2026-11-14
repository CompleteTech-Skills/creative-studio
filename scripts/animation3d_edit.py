"""Bounded detached clip edit commands for transactional editor adapters.

The caller owns publication, exact file references and project revision CAS.
Document hashes here are canonical object hashes, not serialized file hashes.
"""
from copy import deepcopy
from fractions import Fraction
from functools import lru_cache
import json
from pathlib import Path

from animation3d_contracts import _definitions, validate_contract, KINDS
from animation3d_time import frame_position
from animation3d_clips import (edit_key, edit_clip, copy_keys, paste_keys, move_keys,
                               retime_clip, reverse_clip, splice_frames, set_region, remove_region)
from rig_contracts import obj, arr, ref
from rig_errors import RigError, require, finite_json, object_hash, open_payload, decode_json

MAX_COMMANDS = 128
MAX_COPIED_KEYS = 4096
MAX_DOCUMENT_NODES = 100000
MAX_TEXT_CHARACTERS = 2*1024*1024
MAX_JSON_BYTES = 2*1024*1024


@lru_cache(maxsize=1)
def _request_schema():
    """Closed transport schema using the canonical clip/key/track definitions."""
    defs = deepcopy(_definitions())
    identifier = defs['track']['properties']['id']
    number = {'type':'integer','minimum':-10_000_000,'maximum':10_000_000}
    positive = {'type':'integer','minimum':1,'maximum':10_000_000}
    rational = obj(dict(numerator=number, denominator=positive))
    scale = obj(dict(numerator=positive, denominator=positive))
    def command(op, fields=None, optional=()):
        fields = dict(op={'const':op}, **(fields or {}))
        return obj(fields, [name for name in fields if name not in optional])
    commands = [
        command('insert-key', dict(track_id=identifier, key=ref('key'))),
        command('remove-key', dict(track_id=identifier, time=ref('time'))),
        command('replace-key', dict(track_id=identifier, time=ref('time'), key=ref('key'))),
        command('replace-track', dict(track=ref('track'))),
        command('copy-keys', dict(track_id=identifier, start=ref('time'), end=ref('time'))),
        command('paste-keys', dict(track_id=identifier, keys=arr(ref('key'),1,4096), offset=rational), ['offset']),
        command('move-keys', dict(track_id=identifier, start=ref('time'), end=ref('time'), scale=scale, offset=rational), ['scale','offset']),
        command('edit-clip', dict(name={'type':'string','minLength':1,'maxLength':4096},
            duration={'type':'integer','minimum':1,'maximum':10_000_000},
            fps=obj(dict(numerator=positive, denominator=positive)), add_track=ref('track'), remove_track=identifier,
            identifier=identifier, version=defs['clip']['properties']['version'],
            **{name:deepcopy(defs['clip']['properties'][name]) for name in ('tags','categories','loop_mode','completion_mode','default_speed')},
            add_event=ref('event'), remove_event=identifier),
            ['name','duration','fps','add_track','remove_track','identifier','version','tags','categories','loop_mode','completion_mode','default_speed','add_event','remove_event']),
        command('set-region',dict(identifier=identifier,name={'type':'string','minLength':1,'maxLength':4090},
            start_frame={'type':'integer','minimum':0,'maximum':10_000_000},end_frame=positive)),
        command('remove-region',dict(identifier=identifier)),
        command('retime-clip', dict(scale=scale, offset=rational), ['scale','offset']),
        command('reverse-clip'),
        command('splice-frames', dict(start={'type':'integer','minimum':0,'maximum':10_000_000}, count=number)),
    ]
    schema = obj(dict(schema_version={'const':1}, kind={'const':'animation3d-edit-request'},
        expected_document_hash={'type':'string','pattern':r'^[0-9a-f]{64}(?![\s\S])'},
        commands=arr({'oneOf':commands},1,MAX_COMMANDS)))
    return dict(schema, **{'$schema':'https://json-schema.org/draft/2020-12/schema', '$defs':defs})


def request_schema():
    return deepcopy(_request_schema())


def _bounded_document(value):
    # Bound clone/hash work even for programmatic callers, before deepcopy.
    stack = [(value,0)]
    nodes = text = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        require(depth <= 64 and nodes <= MAX_DOCUMENT_NODES, 'ANIMATION_LIMIT', 'Edit document exceeds node/depth policy.')
        if isinstance(item, dict):
            require(len(item) <= MAX_DOCUMENT_NODES-nodes, 'ANIMATION_LIMIT', 'Edit document has too many fields.')
            stack.extend((part,depth+1) for pair in item.items() for part in pair)
        elif isinstance(item, list):
            require(len(item) <= MAX_DOCUMENT_NODES-nodes, 'ANIMATION_LIMIT', 'Edit document has too many entries.')
            stack.extend((part,depth+1) for part in item)
        elif isinstance(item, str):
            text += len(item)
            require(text <= MAX_TEXT_CHARACTERS, 'ANIMATION_LIMIT', 'Edit document exceeds text policy.')
    finite_json(value)


def validate_request(request):
    _bounded_document(request)
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RigError('ANIMATION_DEPENDENCY', 'Install the optional native schema validator.') from exc
    error = next(Draft202012Validator(_request_schema()).iter_errors(request), None)
    if error:
        raise RigError('ANIMATION_EDIT_SCHEMA', error.message[:2048], asset='/'+'/'.join(map(str,error.absolute_path)))
    return request


def _fraction(value, default):
    return default if value is None else Fraction(int(value['numerator']), int(value['denominator']))


def _validate_native(clip, rig_root, actor_id):
    from animation3d_evaluator import RigClipEvaluator
    evaluator = RigClipEvaluator(rig_root, actor_id)
    evaluator.validate_clip(clip)


def apply_clip_commands(clip, request, *, rig_root=None, actor_id=None):
    """Apply the complete batch to detached values, returning a replacement.

    Invalid later commands discard the batch. The original clip/request and all
    native files remain untouched. A caller may require native bone/morph target
    and actual geometry validation by providing both native context arguments.
    """
    _bounded_document(clip)
    validate_contract(clip)
    require(clip['kind']==KINDS['clip'], 'ANIMATION_KIND', 'Clip editing requires a clip document.')
    validate_request(request)
    before = object_hash(clip)
    require(request['expected_document_hash']==before, 'ANIMATION_EDIT_STALE', 'Clip document changed before the edit batch.')
    require((rig_root is None)==(actor_id is None), 'ANIMATION_EDIT_CONTEXT', 'Native context requires both rig root and actor ID.')
    value = deepcopy(clip)
    copied = []
    copied_count = 0
    for index, command in enumerate(request['commands']):
        op = command['op']
        if op=='insert-key':
            value = edit_key(value, command['track_id'], key=command['key'])
        elif op=='remove-key':
            value = edit_key(value, command['track_id'], remove=command['time'])
        elif op=='replace-key':
            track = next((t for t in value['tracks'] if t['id']==command['track_id']), None)
            require(track is not None, 'ANIMATION_REFERENCE', 'Track does not exist.')
            position = frame_position(command['time'])
            found = next((i for i,k in enumerate(track['keys']) if frame_position(k['time'])==position), None)
            require(found is not None, 'ANIMATION_REFERENCE', 'No key occupies the requested time.')
            track['keys'][found] = deepcopy(command['key'])
            track['keys'].sort(key=lambda key:frame_position(key['time']))
            validate_contract(value)
        elif op=='replace-track':
            target = next((i for i,t in enumerate(value['tracks']) if t['id']==command['track']['id']), None)
            require(target is not None, 'ANIMATION_REFERENCE', 'Track replacement must name an existing track.')
            value['tracks'][target] = deepcopy(command['track'])
            validate_contract(value)
        elif op=='copy-keys':
            low,high=frame_position(command['start']),frame_position(command['end'])
            require(low<=high, 'ANIMATION_INTERVAL', 'Copy range is reversed.')
            track=next((t for t in value['tracks'] if t['id']==command['track_id']), None)
            require(track is not None, 'ANIMATION_REFERENCE', 'Track does not exist.')
            selection=[]
            for key in track['keys']:
                if low<=frame_position(key['time'])<=high:
                    require(copied_count+len(selection)<MAX_COPIED_KEYS, 'ANIMATION_LIMIT', 'Aggregate clipboard key capacity exceeded.')
                    selection.append(key)
            proposal=dict(command_index=index,track_id=command['track_id'],keys=selection)
            _bounded_document([value, copied+[proposal]])
            keys=copy_keys(value,command['track_id'],command['start'],command['end'])
            copied.append(dict(command_index=index,track_id=command['track_id'],keys=keys))
            copied_count+=len(keys)
        elif op=='paste-keys':
            value = paste_keys(value, command['track_id'], command['keys'], _fraction(command.get('offset'), 0))
        elif op=='move-keys':
            value = move_keys(value, command['track_id'], command['start'], command['end'],
                scale=_fraction(command.get('scale'), 1), offset=_fraction(command.get('offset'), 0))
        elif op=='edit-clip':
            fields = {k:deepcopy(v) for k,v in command.items() if k!='op'}
            if 'duration' in fields: fields['duration']=int(fields['duration'])
            if 'fps' in fields: fields['fps']={k:int(v) for k,v in fields['fps'].items()}
            value = edit_clip(value, **fields)
        elif op=='set-region':
            value=set_region(value,command['identifier'],command['name'],int(command['start_frame']),int(command['end_frame']))
        elif op=='remove-region':
            value=remove_region(value,command['identifier'])
        elif op=='retime-clip':
            value = retime_clip(value, _fraction(command.get('scale'), 1), _fraction(command.get('offset'), 0))
        elif op=='reverse-clip':
            value = reverse_clip(value)
        elif op=='splice-frames':
            value = splice_frames(value, int(command['start']), int(command['count']))
        _bounded_document([value,copied])
    if rig_root is not None:
        _validate_native(value, rig_root, actor_id)
    after = object_hash(value)
    result=dict(clip=value, before_document_hash=before, after_document_hash=after,
                changed=before!=after, applied_count=len(request['commands']),
                copied_keys=copied, native_validated=rig_root is not None,
                publication_scope='validated detached document; caller owns exact file references and revision transaction')
    _bounded_document(result)
    return result


def read_edit_document(path):
    path = Path(path).absolute()
    with open_payload(path.parent.resolve(strict=True), path.name) as (stream, metadata):
        require(metadata.st_size<=MAX_JSON_BYTES, 'ANIMATION_LIMIT', 'Edit input exceeds JSON byte policy.')
        payload = stream.read(MAX_JSON_BYTES+1)
        require(len(payload)==metadata.st_size, 'ANIMATION_HASH', 'Edit input changed during descriptor read.')
    return decode_json(payload)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clip', type=Path)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--rig-root', type=Path)
    parser.add_argument('--actor')
    parser.add_argument('--schema', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.schema:
            result = request_schema()
        else:
            require(args.clip is not None and args.request is not None, 'ANIMATION_EDIT_CONTEXT', 'Provide clip and request inputs.')
            result = apply_clip_commands(read_edit_document(args.clip), read_edit_document(args.request),
                                         rig_root=args.rig_root, actor_id=args.actor)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (RigError, OSError) as exc:
        error = exc.report if isinstance(exc,RigError) else dict(code='ANIMATION_IO',message=str(exc))
        print(json.dumps(dict(error=error), sort_keys=True, allow_nan=False))
        return 2


if __name__=='__main__': raise SystemExit(main())
