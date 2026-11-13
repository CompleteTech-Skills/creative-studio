"""Append-only, locally persisted Director budget accounting.

No provider is invoked. Reservations bound worst-case work before it starts;
uncertain outcomes retain capacity, and reported usage never becomes approval.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from atomic_publish import _rename_no_replace
from asset_library import _read_source_bytes
from rig_errors import RigError, decode_json, job_lock, object_hash

METRICS = ('provider_calls', 'input_tokens', 'output_tokens', 'cpu_seconds', 'gpu_seconds', 'storage_bytes', 'amount')
COUNTS = frozenset(('provider_calls', 'input_tokens', 'output_tokens', 'storage_bytes'))
MAX_REVISIONS = 2048
MAX_RESERVATIONS = 512
MAX_BYTES = 4 * 1024**2


def _exact(function):
    """All supported quantities and bounded aggregates fit exactly in 128 digits."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 128
            return function(*args, **kwargs)
    return wrapped


def _need(ok, code, message):
    if not ok:
        raise RigError('DIRECTOR_BUDGET_' + code, message, stage='budget')


def _id(value):
    _need(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value), 'INPUT', 'Invalid operation/provider identity.')
    return value


def _number(value, metric, *, maximum=10**15):
    _need(type(value) in (int, float, str, Decimal), 'INPUT', 'Budget numbers must be finite nonnegative decimal quantities.')
    _need(not isinstance(value,str) or len(value)<=256, 'INPUT', 'Decimal text exceeds bound.')
    _need(type(value) is not int or 0 <= value <= maximum, 'INPUT', 'Integer quantity exceeds bound.')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise RigError('DIRECTOR_BUDGET_INPUT', 'Invalid decimal quantity.', stage='budget') from exc
    _need(result.is_finite() and 0 <= result <= maximum, 'INPUT', 'Quantity exceeds the finite budget bound.')
    _need(result.as_tuple().exponent >= -64 and len(result.as_tuple().digits) <= len(str(maximum)) + 64, 'INPUT', 'Quantities exceed the bounded precision or 64 fractional places.')
    _need(metric not in COUNTS or result == result.to_integral_value(), 'INPUT', 'Count quantities must be integral.')
    return result


def _quantities(value, *, unknown=False, maximum=10**15):
    _need(isinstance(value, dict) and set(value) == set(METRICS), 'INPUT', 'Every budget metric must be explicitly provided.')
    return {key: None if unknown and value[key] is None else str(_number(value[key], key, maximum=maximum)) for key in METRICS}


def _decimal_map(value):
    return {key: Decimal(value[key]) for key in METRICS}


@_exact
def _totals(state, provider=None):
    spent = {key: Decimal(0) for key in METRICS}
    held = {key: Decimal(0) for key in METRICS}
    for row in state['reservations'].values():
        if provider is not None and row['provider'] != provider:
            continue
        for key in METRICS:
            spent[key] += Decimal(row['spent'][key])
            held[key] += Decimal(row['held'][key])
    return spent, held


@_exact
def _validate_state(state):
    _need(isinstance(state, dict) and set(state) == {'schema_version', 'kind', 'revision', 'parent', 'limits', 'provider_limits', 'reservations', 'events', 'currency'}, 'CORRUPT', 'Unknown ledger fields.')
    _need(state['schema_version'] in (1, 2) and state['kind'] == 'director-budget-ledger', 'CORRUPT', 'Unsupported ledger version.')
    _need(type(state['revision']) is int and 0 <= state['revision'] < MAX_REVISIONS, 'CORRUPT', 'Invalid ledger revision.')
    _quantities(state['limits'])
    _need(state['currency'] is None or (isinstance(state['currency'], str) and re.fullmatch(r'[A-Z]{3}', state['currency'])), 'CORRUPT', 'Invalid ledger currency.')
    _need(Decimal(state['limits']['amount']) == 0 or state['currency'] is not None, 'CURRENCY', 'Monetary budgets require one explicit currency.')
    _need(isinstance(state['provider_limits'], dict) and len(state['provider_limits']) <= 256, 'CORRUPT', 'Invalid provider limits.')
    for name, limits in state['provider_limits'].items():
        _id(name); _quantities(limits)
    _need(isinstance(state['reservations'], dict) and len(state['reservations']) <= MAX_RESERVATIONS, 'CORRUPT', 'Too many reservations.')
    for identifier, row in state['reservations'].items():
        _id(identifier)
        _need(set(row) in ({'id', 'operation_id', 'provider', 'reserved', 'spent', 'held', 'state', 'unknown_price', 'attempts'}, {'id', 'operation_id', 'provider', 'reserved', 'spent', 'held', 'state', 'unknown_price', 'attempts', 'dispatches'}), 'CORRUPT', 'Invalid reservation fields.')
        dispatches = row.get('dispatches', [])
        _need(not dispatches or state['schema_version'] == 2, 'CORRUPT', 'Dispatch records require explicit ledger version two.')
        _need(isinstance(dispatches, list) and len(dispatches) <= 17, 'CORRUPT', 'Invalid dispatch history.')
        for dispatch in dispatches:
            _need(isinstance(dispatch, dict) and set(dispatch) == {'id', 'native_owner', 'operation_sha256', 'state', 'native_evidence', 'usage_accounted'}, 'CORRUPT', 'Invalid dispatch record.')
            _id(dispatch['id']); _id(dispatch['native_owner'])
            _need(isinstance(dispatch['operation_sha256'], str) and re.fullmatch('[0-9a-f]{64}', dispatch['operation_sha256']) and dispatch['state'] in ('pending', 'claimed', 'uncertain', 'complete', 'failed'), 'CORRUPT', 'Invalid dispatch identity/state.')
            _evidence(dispatch['native_evidence'])
            _need(type(dispatch['usage_accounted']) is bool and (not dispatch['usage_accounted'] or dispatch['state'] in ('complete', 'failed')), 'CORRUPT', 'Invalid dispatch usage accounting.')
        _need(len({entry['id'] for entry in dispatches}) == len(dispatches), 'CORRUPT', 'Duplicate dispatch identity.')
        _need(row['id'] == identifier and row['state'] in ('reserved', 'uncertain', 'settled', 'released'), 'CORRUPT', 'Invalid reservation identity/state.')
        _id(row['operation_id']); _id(row['provider'])
        for field in ('reserved', 'spent', 'held'):
            # Accepted usage deltas may cumulatively exceed the admission limit.
            _quantities(row[field], maximum=128 * 10**15 if field == 'spent' else 10**15)
        _need(type(row['unknown_price']) is bool and isinstance(row['attempts'], list) and len(row['attempts']) <= 128, 'CORRUPT', 'Invalid reservation attempts.')
        for key in METRICS:
            _need(Decimal(row['held'][key]) <= max(Decimal(row['reserved'][key]) - Decimal(row['spent'][key]), Decimal(0)), 'CORRUPT', 'Held capacity exceeds unconsumed reservation.')
    _need(isinstance(state['events'], list) and len(state['events']) <= MAX_REVISIONS, 'CORRUPT', 'Invalid event history.')
    _need(len(state['events']) == state['revision'], 'CORRUPT', 'Event sequence does not match revision.')
    for index,event in enumerate(state['events']):
        _need(isinstance(event,dict) and set(event)=={'idempotency_key','action','input_digest','payload','receipt'}, 'CORRUPT', 'Invalid event record.')
        _id(event['idempotency_key'])
        _need(event['action'] in ('reserve','settle','release','dispatch','dispatch-outcome') and isinstance(event['payload'],dict) and isinstance(event['receipt'],dict), 'CORRUPT', 'Invalid event action.')
        _need(event['input_digest']==object_hash({'action':event['action'],'payload':event['payload']}), 'CORRUPT', 'Event inputs changed.')
        _need(event['receipt'].get('revision')==index+1 and event['receipt'].get('idempotency_key')==event['idempotency_key'], 'CORRUPT', 'Event receipt changed.')
        _evidence(event['payload'].get('evidence'))
    _need(len({event['idempotency_key'] for event in state['events']})==len(state['events']), 'CORRUPT', 'Duplicate idempotency identity.')
    for provider in [None, *state['provider_limits']]:
        spent, held = _totals(state, provider)
        limits = state['limits'] if provider is None else state['provider_limits'][provider]
        # Unexpected provider overrun is real expenditure, never discarded.
        # Admission checks spent+held; overdrawn ledgers therefore block new work.
        _need(all(held[key] >= 0 for key in METRICS), 'CORRUPT', 'Negative held budget.')
    return state


class BudgetLedger:
    """Immutable snapshots under one ownership lock; expected SHA is the CAS token."""

    def __init__(self, root):
        self.root = Path(root).absolute()
        _need(self.root.parent.is_dir() and self.root.parent.resolve() == self.root.parent, 'PATH', 'Ledger parent must be a canonical existing directory.')
        _need(not self.root.is_symlink(), 'PATH', 'Ledger root cannot be a symlink.')

    @classmethod
    def create(cls, root, limits, *, provider_limits=None, currency=None):
        ledger = cls(root)
        state = {'schema_version': 1, 'kind': 'director-budget-ledger', 'revision': 0, 'parent': None,
                 'limits': _quantities(limits), 'provider_limits': {_id(k): _quantities(v) for k,v in (provider_limits or {}).items()},
                 'reservations': {}, 'events': [], 'currency': currency}
        _validate_state(state)
        with tempfile.TemporaryDirectory(prefix='.director-budget-', dir=ledger.root.parent) as tmp:
            stage = Path(tmp) / 'ledger'; stage.mkdir(); (stage / 'evidence').mkdir()
            ledger._publish(state, stage)
            _rename_no_replace(stage, ledger.root)
        return ledger

    def retain_evidence(self, source):
        """Copy bounded exact bytes to immutable owned storage before accounting."""
        data = _read_source_bytes(source, MAX_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        root = self.root / 'evidence'
        _need(root.is_dir() and not root.is_symlink(), 'PATH', 'Unsafe evidence storage.')
        destination = root / digest
        if not destination.exists():
            fd, name = tempfile.mkstemp(prefix='.staged-', dir=root)
            temporary = Path(name)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                try: _rename_no_replace(temporary, destination)
                except FileExistsError: pass
            finally: temporary.unlink(missing_ok=True)
        proof = {'path': 'evidence/' + digest, 'sha256': digest}
        _verify_evidence(self.root, [proof])
        return proof

    def _publish(self, state, root=None):
        root = self.root if root is None else root
        _validate_state(state)
        digest = object_hash(state)
        data = (json.dumps(state, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
        _need(len(data) <= MAX_BYTES, 'LIMIT', 'Ledger snapshot exceeds byte bound.')
        final = root / f"{state['revision']:08d}-{digest}.json"
        fd, name = tempfile.mkstemp(prefix='.staged-', dir=root)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            _rename_no_replace(temporary, final)
            if os.name != 'nt':
                directory = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try: os.fsync(directory)
                finally: os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def snapshot(self):
        _need(self.root.is_dir() and not self.root.is_symlink(), 'PATH', 'Ledger is absent or unsafe.')
        files = sorted(self.root.iterdir())
        _need(len(files) <= MAX_REVISIONS + 1, 'LIMIT', 'Too many ledger files.')
        prior = None; latest = None
        # An interrupted unpublished staging file is ignored, never trusted/deleted.
        for path in files:
            if path.name == 'evidence':
                _need(path.is_dir() and not path.is_symlink(), 'PATH', 'Unsafe evidence directory.')
                continue
            if path.name.startswith('.staged-'):
                continue
            match = re.fullmatch(r'([0-9]{8})-([0-9a-f]{64})\.json', path.name)
            _need(match is not None and path.is_file() and not path.is_symlink(), 'CORRUPT', 'Unexpected ledger entry.')
            _need(path.stat().st_size <= MAX_BYTES, 'LIMIT', 'Ledger snapshot exceeds byte bound.')
            state = _validate_state(decode_json(_read_source_bytes(path, MAX_BYTES)))
            digest = object_hash(state)
            _need(digest == match[2] and state['revision'] == int(match[1]), 'CORRUPT', 'Ledger snapshot hash/sequence changed.')
            _need(state['revision'] == (0 if latest is None else latest['revision'] + 1) and state['parent'] == prior,
                  'CORRUPT', 'Ledger history is missing or rebound.')
            if latest is not None:
                _need(state['events'][:-1]==latest['events'] and all(state[key]==latest[key] for key in ('limits','provider_limits','currency')), 'CORRUPT', 'Immutable history or budget policy changed.')
            prior, latest = digest, state
        _need(latest is not None, 'CORRUPT', 'No valid ledger snapshot.')
        proofs = {item['path']: item for event in latest['events'] for item in event['payload']['evidence']}
        _verify_evidence(self.root, list(proofs.values()))
        return {'sha256': prior, 'state': deepcopy(latest)}

    def _change(self, expected_revision, key, action, payload, mutate, *, replay_indicator=False):
        _id(key)
        with job_lock(self.root):
            snapshot = self.snapshot(); state = snapshot['state']
            _verify_evidence(self.root, payload['evidence'])
            digest = object_hash({'action': action, 'payload': payload})
            existing = [event for event in state['events'] if event['idempotency_key'] == key]
            if existing:
                _need(len(existing) == 1 and existing[0]['input_digest'] == digest, 'IDEMPOTENCY', 'Idempotency key reused for different work.')
                return dict(deepcopy(existing[0]['receipt']), replayed=True) if replay_indicator else deepcopy(existing[0]['receipt'])
            _need(snapshot['sha256'] == expected_revision, 'STALE', 'Ledger revision changed; read current state before retrying.')
            _need(state['revision'] + 1 < MAX_REVISIONS, 'LIMIT', 'Ledger history is full.')
            receipt = mutate(state)
            state['revision'] += 1; state['parent'] = snapshot['sha256']
            receipt.update(revision=state['revision'], idempotency_key=key)
            state['events'].append({'idempotency_key': key, 'action': action, 'input_digest': digest, 'payload': payload, 'receipt': deepcopy(receipt)})
            self._publish(state)
            return dict(receipt, replayed=False) if replay_indicator else receipt

    @_exact
    def reserve(self, *, operation_id, provider, estimate, retry_allowance, expected_revision, idempotency_key,
                unknown_price=False, unknown_price_cap=None, evidence=None):
        _id(operation_id); _id(provider)
        _need(type(retry_allowance) is int and 0 <= retry_allowance <= 16, 'INPUT', 'Retries must be bounded from zero to sixteen.')
        _need(type(unknown_price) is bool, 'INPUT', 'Unknown-price policy must be explicit.')
        amounts = _quantities(estimate, unknown=True)
        missing_price = amounts['amount'] is None
        if missing_price:
            _need(unknown_price and unknown_price_cap is not None, 'PRICE', 'Unknown-priced work requires explicit policy and monetary cap.')
            cap = _number(unknown_price_cap, 'amount')
            _need(cap > 0, 'PRICE', 'Unknown-priced work requires a positive monetary cap.')
            amounts['amount'] = str(cap)
        _need(all(value is not None for value in amounts.values()), 'ESTIMATE', 'Unknown nonmonetary estimates cannot be reserved.')
        bound = {key: str(Decimal(value) * (retry_allowance + 1)) for key,value in amounts.items()}
        proof = _evidence(evidence)
        _need(not (unknown_price or missing_price or Decimal(amounts['amount']) > 0) or proof, 'EVIDENCE', 'Monetary estimates require retained rate or unknown-price policy evidence.')
        payload = dict(operation_id=operation_id, provider=provider, estimate=amounts, retry_allowance=retry_allowance,
                       unknown_price=unknown_price, evidence=proof)
        def mutate(state):
            _need(provider in state['provider_limits'], 'PROVIDER', 'Provider has no explicit budget policy.')
            _need(len(state['reservations']) < MAX_RESERVATIONS, 'LIMIT', 'Too many reservations.')
            _need(not any(row['operation_id'] == operation_id for row in state['reservations'].values()), 'DUPLICATE', 'Operation already owns a reservation.')
            for scope in (None, provider):
                spent,held = _totals(state,scope)
                limits = state['limits'] if scope is None else state['provider_limits'][scope]
                _need(all(spent[key]+held[key]+Decimal(bound[key]) <= Decimal(limits[key]) for key in METRICS), 'EXCEEDED', 'Reservation exceeds project or provider hard limits.')
            identifier='reservation-'+object_hash({'operation':operation_id,'key':idempotency_key})[:24]
            state['reservations'][identifier] = dict(id=identifier,operation_id=operation_id,provider=provider,
                reserved=bound,spent={key:'0' for key in METRICS},held=deepcopy(bound),state='reserved',unknown_price=unknown_price,attempts=[])
            return {'reservation_id':identifier,'state':'reserved','reserved':deepcopy(bound)}
        return self._change(expected_revision,idempotency_key,'reserve',payload,mutate)

    @_exact
    def settle(self, reservation_id, *, usage, outcome, expected_revision, idempotency_key, evidence, dispatch_id=None):
        _id(reservation_id)
        _need(outcome in ('complete','failed','uncertain','retry'), 'INPUT', 'Unsupported settlement outcome.')
        amounts = _quantities(usage, unknown=True)
        proof = _evidence(evidence); _need(bool(proof), 'EVIDENCE', 'Settlement requires retained usage evidence.')
        _need(outcome == 'uncertain' or all(value is not None for value in amounts.values()), 'USAGE', 'Unknown usage must retain uncertain accounting.')
        payload = dict(reservation_id=reservation_id,usage=amounts,outcome=outcome,evidence=proof)
        if dispatch_id is not None:
            _id(dispatch_id); payload['dispatch_id'] = dispatch_id
        def mutate(state):
            _need(reservation_id in state['reservations'], 'MISSING', 'Reservation does not exist.')
            row = state['reservations'][reservation_id]
            _need(row['state'] in ('reserved','uncertain'), 'STATE', 'Reservation is already final.')
            _need(outcome not in ('complete', 'failed') or not any(d['state'] in ('pending', 'claimed', 'uncertain') for d in row.get('dispatches', [])), 'DISPATCH', 'An active or uncertain native dispatch must be reconciled before final settlement.')
            if row.get('dispatches'):
                pending = [d for d in row['dispatches'] if not d['usage_accounted'] and
                           (dispatch_id is None or d['id'] == dispatch_id)]
                _need(len(pending) == 1 and pending[0]['state'] in ('complete', 'failed'),
                      'DISPATCH', 'Settlement needs one original known native outcome.')
                _need(outcome != 'uncertain' and all(value is not None for value in amounts.values()),
                      'DISPATCH', 'Unknown native usage keeps the original dispatch and reservation held.')
                _need((outcome == 'complete') == (pending[0]['state'] == 'complete'),
                      'DISPATCH', 'Settlement outcome must preserve the actual native outcome.')
                _need(outcome not in ('complete', 'failed') or all(d is pending[0] or d['usage_accounted'] for d in row['dispatches']),
                      'DISPATCH', 'Every native attempt must have actual usage before final settlement.')
                pending[0]['usage_accounted'] = True
            _need(len(row['attempts']) < 128, 'LIMIT', 'Too many usage attempts.')
            for key in METRICS:
                if amounts[key] is not None:
                    spent = Decimal(row['spent'][key]) + Decimal(amounts[key])
                    row['spent'][key] = str(spent)
                    row['held'][key] = str(max(Decimal(row['reserved'][key]) - spent, Decimal(0)))
            row['attempts'].append(dict(usage=amounts,outcome=outcome,evidence=proof,idempotency_key=idempotency_key))
            if outcome in ('complete','failed'):
                row['state']='settled';row['held']={key:'0' for key in METRICS}
            elif outcome=='uncertain':row['state']='uncertain'
            return {'reservation_id':reservation_id,'state':row['state'],'spent':deepcopy(row['spent']),'held':deepcopy(row['held']),
                    'over_budget':any(Decimal(row['spent'][key]) > Decimal(row['reserved'][key]) for key in METRICS)}
        return self._change(expected_revision,idempotency_key,'settle',payload,mutate)

    def release(self, reservation_id, *, outcome, expected_revision, idempotency_key, evidence):
        _id(reservation_id)
        _need(outcome in ('unstarted','canceled-known'), 'INPUT', 'Release requires known nonexecuted remainder.')
        proof = _evidence(evidence);_need(bool(proof), 'EVIDENCE', 'Release requires evidence that remaining work is not running.')
        payload=dict(reservation_id=reservation_id,outcome=outcome,evidence=proof)
        def mutate(state):
            _need(reservation_id in state['reservations'], 'MISSING', 'Reservation does not exist.')
            row=state['reservations'][reservation_id]
            _need(row['state']=='reserved', 'UNCERTAIN', 'Uncertain/final reservations cannot be released.')
            _need(not any(d['state'] in ('pending', 'claimed', 'uncertain') or not d['usage_accounted'] for d in row.get('dispatches', [])), 'DISPATCH', 'An admitted or unaccounted native dispatch still owns this reservation.')
            _need(outcome!='unstarted' or not row['attempts'], 'STATE', 'Started work cannot be released as unstarted.')
            row['state']='released';row['held']={key:'0' for key in METRICS}
            return {'reservation_id':reservation_id,'state':'released','spent':deepcopy(row['spent'])}
        return self._change(expected_revision,idempotency_key,'release',payload,mutate)


    def begin_dispatch(self, reservation_id, *, native_owner, operation_sha256,
                       expected_revision, idempotency_key, evidence):
        """Durably consume one dispatch allowance BEFORE asking a native owner.

        Only ``replayed=False`` grants a new native handoff. Replays and crashes
        reconcile the original native owner; they never authorize a repeat paid
        call. This is a budget fence, not an execution lease or generation claim.
        """
        _id(reservation_id); _id(native_owner)
        _need(isinstance(operation_sha256, str) and re.fullmatch('[0-9a-f]{64}', operation_sha256),
              'INPUT', 'Dispatch must pin its exact operation digest.')
        proof = _evidence(evidence)
        _need(bool(proof), 'EVIDENCE', 'Dispatch requires retained native input evidence.')
        payload = dict(reservation_id=reservation_id, native_owner=native_owner,
                       operation_sha256=operation_sha256, evidence=proof)
        def mutate(state):
            _need(reservation_id in state['reservations'], 'MISSING', 'Reservation does not exist.')
            row = state['reservations'][reservation_id]
            _need(row['state'] == 'reserved', 'STATE', 'Only an active known reservation may dispatch.')
            prior = row.get('dispatches', [])
            _need(not any(d['state'] in ('pending', 'claimed', 'uncertain') or not d['usage_accounted'] for d in prior),
                  'DISPATCH', 'Reconcile the existing native dispatch before another attempt.')
            reservation = next(event for event in state['events'] if event['action'] == 'reserve'
                               and event['receipt']['reservation_id'] == reservation_id)
            _need(len(prior) < reservation['payload']['retry_allowance'] + 1,
                  'ATTEMPTS', 'The reserved bounded attempt allowance is exhausted.')
            identifier = 'dispatch-' + object_hash(dict(reservation=reservation_id, key=idempotency_key))[:24]
            state['schema_version'] = 2
            row.setdefault('dispatches', []).append(dict(id=identifier, native_owner=native_owner,
                operation_sha256=operation_sha256, state='pending', native_evidence=[], usage_accounted=False))
            return dict(reservation_id=reservation_id, dispatch_id=identifier, state='pending',
                        native_owner=native_owner, operation_sha256=operation_sha256)
        return self._change(expected_revision, idempotency_key, 'dispatch', payload, mutate,
                            replay_indicator=True)

    def record_dispatch_outcome(self, reservation_id, dispatch_id, *, outcome,
                                expected_revision, idempotency_key, evidence):
        """Attach exact native owner evidence without releasing budget capacity.

        Unknown outcomes retain the original dispatch and reservation. Native
        claim/job identities live in their owning evidence; this ledger does not
        invent or supersede them. Usage is settled separately from actual data.
        """
        _id(reservation_id); _id(dispatch_id)
        _need(outcome in ('claimed', 'uncertain', 'complete', 'failed'), 'INPUT', 'Invalid native dispatch outcome.')
        proof = _evidence(evidence)
        _need(bool(proof), 'EVIDENCE', 'Native dispatch outcome requires retained exact evidence.')
        payload = dict(reservation_id=reservation_id, dispatch_id=dispatch_id, outcome=outcome, evidence=proof)
        def mutate(state):
            _need(reservation_id in state['reservations'], 'MISSING', 'Reservation does not exist.')
            row = state['reservations'][reservation_id]
            _need(row['state'] in ('reserved', 'uncertain'), 'STATE', 'Reservation is already final.')
            records = [d for d in row.get('dispatches', []) if d['id'] == dispatch_id]
            _need(len(records) == 1, 'MISSING', 'Original native dispatch is absent.')
            record = records[0]
            allowed = {'pending': {'claimed', 'uncertain', 'complete', 'failed'},
                       'claimed': {'uncertain', 'complete', 'failed'},
                       'uncertain': {'claimed', 'complete', 'failed'},
                       'complete': set(), 'failed': set()}
            _need(outcome in allowed[record['state']], 'STATE', 'Illegal native dispatch transition.')
            record['state'] = outcome
            record['native_evidence'] = deepcopy(proof)
            return dict(reservation_id=reservation_id, dispatch_id=dispatch_id, state=outcome,
                        native_owner=record['native_owner'], operation_sha256=record['operation_sha256'])
        return self._change(expected_revision, idempotency_key, 'dispatch-outcome', payload, mutate)


def _evidence(evidence):
    _need(isinstance(evidence, list) and len(evidence) <= 128 and all(isinstance(item, dict) and
        set(item)=={'path','sha256'} and isinstance(item['path'],str) and 0<len(item['path'])<=1024 and
        isinstance(item['sha256'],str) and re.fullmatch('[0-9a-f]{64}',item['sha256']) for item in evidence),
        'EVIDENCE', 'Evidence must contain bounded exact path/hash references.')
    return deepcopy(evidence)


def _verify_evidence(root, evidence):
    for item in _evidence(evidence):
        _need(item['path'] == 'evidence/' + item['sha256'], 'EVIDENCE', 'Evidence must be retained in this ledger.')
        data = _read_source_bytes(root / item['path'], MAX_BYTES)
        _need(hashlib.sha256(data).hexdigest() == item['sha256'], 'EVIDENCE', 'Retained accounting evidence changed.')


@_exact
def execute(ledger, request, *, evidence_root, provider='local', unknown_price_cap=None):
    """Adapt a strict Director BudgetLedger request to this native ledger.

    Evidence is copied from exact ArtifactRefs into immutable local storage. The
    accounting operation never invokes the referenced provider or other worker.
    """
    from director_contracts import validate_method_request, validate_method_result, _read_artifact
    method = request.get('method')
    _need(method in ('BudgetLedger.reserve', 'BudgetLedger.settle', 'BudgetLedger.release'), 'METHOD', 'Unknown budget method.')
    validate_method_request(method, request)
    data = request['input']; context = request['context']
    proof = []
    sources = data.get('rate_evidence', data.get('evidence', []))
    with tempfile.TemporaryDirectory(prefix='budget-evidence-') as temporary:
        for index, reference in enumerate(sources):
            payload = _read_artifact(evidence_root, reference)
            path = Path(temporary).resolve() / str(index); path.write_bytes(payload)
            proof.append(ledger.retain_evidence(path))
    common = dict(expected_revision=data['expected_ledger_revision'], idempotency_key=context['idempotency_key'])
    if method == 'BudgetLedger.reserve':
        usage = data['estimated_bound']
        policy = context['policy']
        _need(provider in policy['providers'], 'POLICY', 'Provider is not allowed by this request.')
        amount = usage['amount']['value']
        _need(policy['paid'] != 'deny' or (amount is not None and _number(amount, 'amount') == 0),
              'POLICY', 'Paid or unpriced work is denied by this request.')
        _need(usage['classification']=='estimated', 'USAGE', 'Reservation requires estimated usage.')
        _need(usage['currency'] in (None, ledger.snapshot()['state']['currency']), 'CURRENCY', 'Usage currency differs from ledger.')
        receipt = ledger.reserve(operation_id=data['operation'],provider=provider,
            estimate={key:usage[key]['value'] for key in METRICS},retry_allowance=data['retry_allowance'],
            unknown_price=context['policy']['paid']=='allow-unknown-price',unknown_price_cap=unknown_price_cap,evidence=proof,**common)
    elif method == 'BudgetLedger.settle':
        usage = data['actual_usage']
        _need(usage['classification']=='actual', 'USAGE', 'Settlement requires actual usage.')
        _need(usage['currency'] in (None, ledger.snapshot()['state']['currency']), 'CURRENCY', 'Usage currency differs from ledger.')
        receipt = ledger.settle(data['reservation_id'], usage={key:usage[key]['value'] for key in METRICS},
            outcome=data['outcome'], evidence=proof, **common)
    else:
        receipt = ledger.release(data['reservation_id'],outcome='canceled-known',evidence=proof,**common)
    snapshot = ledger.snapshot();state=snapshot['state'];spent,held=_totals(state)
    available={key:max(Decimal(state['limits'][key])-spent[key]-held[key],Decimal(0)) for key in METRICS}
    remaining=dict(max_provider_calls=int(available['provider_calls']),max_cpu_seconds=float(available['cpu_seconds']),
        max_gpu_seconds=float(available['gpu_seconds']),max_storage_bytes=int(available['storage_bytes']),
        max_memory_bytes=0,currency=state['currency'],max_amount=float(available['amount']) if state['currency'] else None,policy_ref=None)
    references=[]
    for item in proof:
        path=ledger.root/item['path']
        references.append(dict(artifact_id='budget-'+item['sha256'][:24],kind='budget-accounting-evidence',
            schema_version=1,semantic_version='1.0.0',path=item['path'],sha256=item['sha256'],byte_size=path.stat().st_size,
            media_type='application/octet-stream',provenance_ref=None,
            fixture=any(source['fixture'] for source in sources if source['sha256']==item['sha256']),dependencies=[]))
    result=dict(schema_version=1,kind='director-result',method=method,status='succeeded',operation_id=context['request_id'],
        input_digest=object_hash(request),value=dict(reservation_id=receipt['reservation_id'],ledger_revision=snapshot['sha256'],
            remaining=remaining,state=receipt['state'],evidence=references),artifacts=references,diagnostics=[],
        usage=dict(classification='unavailable',**{key:{'value':None,'explanation':'Accounting overhead is not measured.'} for key in METRICS},
                   currency=None,measurement_source='director-budget-ledger'),provenance_receipts=references)
    validate_method_result(method,result,request)
    return result
