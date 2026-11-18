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
import time

from atomic_publish import _rename_no_replace
from asset_library import _read_source_bytes
from rig_errors import RigError, decode_json, job_lock, object_hash

METRICS = ('provider_calls', 'input_tokens', 'output_tokens', 'cpu_seconds', 'gpu_seconds', 'storage_bytes', 'amount')
COUNTS = frozenset(('provider_calls', 'input_tokens', 'output_tokens', 'storage_bytes'))
MAX_REVISIONS = 2048
MAX_RESERVATIONS = 512
MAX_BYTES = 4 * 1024**2
NATIVE_RECOVERY_POLICY = object_hash('native-terminal-recovery-v1')


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
    _need(state['schema_version'] == 1 and state['kind'] == 'director-budget-ledger', 'CORRUPT', 'Unsupported ledger version.')
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
        _need(set(row) == {'id', 'operation_id', 'provider', 'reserved', 'spent', 'held', 'state', 'unknown_price', 'attempts'}, 'CORRUPT', 'Invalid reservation fields.')
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
        _need(event['action'] in ('reserve','settle','release','native-claim','native-finalize') and isinstance(event['payload'],dict) and isinstance(event['receipt'],dict), 'CORRUPT', 'Invalid event action.')
        _need(event['input_digest']==object_hash({'action':event['action'],'payload':event['payload']}), 'CORRUPT', 'Event inputs changed.')
        _need(event['receipt'].get('revision')==index+1 and event['receipt'].get('idempotency_key')==event['idempotency_key'], 'CORRUPT', 'Event receipt changed.')
        _evidence(event['payload'].get('evidence'))
        if event['action'] == 'native-finalize':
            final = event['payload']
            _need(set(final) == {'reservation_id', 'operation_id', 'token', 'observation_key', 'owner_history_sha256', 'evidence'} and
                  isinstance(final['owner_history_sha256'], str) and
                  re.fullmatch(r'[0-9a-f]{64}', final['owner_history_sha256']) and
                  isinstance(final['token'], str) and re.fullmatch(r'[0-9a-f]{32}', final['token']),
                  'CORRUPT', 'Invalid native finalization record.')
            for field in ('reservation_id', 'operation_id', 'observation_key'):
                _id(final[field])
            _need(all(event['receipt'].get(key) == final[key] for key in
                      ('reservation_id', 'operation_id', 'token', 'observation_key', 'owner_history_sha256')),
                  'CORRUPT', 'Native finalization receipt changed.')
        if event['action'] == 'native-claim':
            claim = event['payload']
            _need(set(claim) == {'reservation_id', 'operation_id', 'run_id', 'run_location_sha256',
                                'job', 'token', 'attempt', 'input_hashes', 'evidence'} and
                  type(claim['attempt']) is int and 1 <= claim['attempt'] <= 17 and
                  isinstance(claim['token'], str) and re.fullmatch(r'[0-9a-f]{32}', claim['token']) and
                  isinstance(claim['input_hashes'], dict), 'CORRUPT', 'Invalid native publication record.')
            for field in ('reservation_id', 'operation_id', 'run_id', 'job'):
                _id(claim[field])
            _need(isinstance(claim['run_location_sha256'], str) and
                  re.fullmatch(r'[0-9a-f]{64}', claim['run_location_sha256']) and
                  all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v)
                      for k, v in claim['input_hashes'].items()), 'CORRUPT', 'Invalid native publication identity.')
            _need(all(event['receipt'].get(k) == claim[k] for k in
                      ('reservation_id', 'operation_id', 'token', 'attempt')),
                  'CORRUPT', 'Native publication receipt changed.')
    _need(len({event['idempotency_key'] for event in state['events']})==len(state['events']), 'CORRUPT', 'Duplicate idempotency identity.')
    for provider in [None, *state['provider_limits']]:
        spent, held = _totals(state, provider)
        limits = state['limits'] if provider is None else state['provider_limits'][provider]
        # Unexpected provider overrun is real expenditure, never discarded.
        # Admission checks spent+held; overdrawn ledgers therefore block new work.
        _need(all(held[key] >= 0 for key in METRICS), 'CORRUPT', 'Negative held budget.')
    return state


@_exact
def _reservation_payload(*, operation_id, provider, estimate, retry_allowance,
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
    return payload, bound



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
        # The 128-reference bound applies to one accounting operation. A valid
        # append-only history may retain more distinct proofs across operations.
        # Keep conflicting path/hash pairs visible so deduplication cannot hide
        # a noncanonical reference from an earlier event.
        proofs = list({(item['path'], item['sha256']): item
                       for event in latest['events']
                       for item in event['payload']['evidence']}.values())
        for start in range(0, len(proofs), 128):
            _verify_evidence(self.root, proofs[start:start + 128])
        return {'sha256': prior, 'state': deepcopy(latest)}

    def _change(self, expected_revision, key, action, payload, mutate):
        _id(key)
        with job_lock(self.root):
            snapshot = self.snapshot(); state = snapshot['state']
            _verify_evidence(self.root, payload['evidence'])
            digest = object_hash({'action': action, 'payload': payload})
            existing = [event for event in state['events'] if event['idempotency_key'] == key]
            if existing:
                _need(len(existing) == 1 and existing[0]['input_digest'] == digest, 'IDEMPOTENCY', 'Idempotency key reused for different work.')
                return deepcopy(existing[0]['receipt'])
            _need(snapshot['sha256'] == expected_revision, 'STALE', 'Ledger revision changed; read current state before retrying.')
            _need(state['revision'] + 1 < MAX_REVISIONS, 'LIMIT', 'Ledger history is full.')
            receipt = mutate(state)
            state['revision'] += 1; state['parent'] = snapshot['sha256']
            receipt.update(revision=state['revision'], idempotency_key=key)
            state['events'].append({'idempotency_key': key, 'action': action, 'input_digest': digest, 'payload': payload, 'receipt': deepcopy(receipt)})
            self._publish(state)
            return receipt

    @_exact
    def reserve(self, *, operation_id, provider, estimate, retry_allowance, expected_revision, idempotency_key,
                unknown_price=False, unknown_price_cap=None, evidence=None):
        payload, bound = _reservation_payload(operation_id=operation_id, provider=provider,
            estimate=estimate, retry_allowance=retry_allowance, unknown_price=unknown_price,
            unknown_price_cap=unknown_price_cap, evidence=evidence)
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
    def settle(self, reservation_id, *, usage, outcome, expected_revision, idempotency_key, evidence):
        _id(reservation_id)
        _need(outcome in ('complete','failed','uncertain','retry'), 'INPUT', 'Unsupported settlement outcome.')
        amounts = _quantities(usage, unknown=True)
        proof = _evidence(evidence); _need(bool(proof), 'EVIDENCE', 'Settlement requires retained usage evidence.')
        _need(outcome == 'uncertain' or all(value is not None for value in amounts.values()), 'USAGE', 'Unknown usage must retain uncertain accounting.')
        payload = dict(reservation_id=reservation_id,usage=amounts,outcome=outcome,evidence=proof)
        def mutate(state):
            _need(reservation_id in state['reservations'], 'MISSING', 'Reservation does not exist.')
            row = state['reservations'][reservation_id]
            _need(row['state'] in ('reserved','uncertain'), 'STATE', 'Reservation is already final.')
            _need(len(row['attempts']) < 128, 'LIMIT', 'Too many usage attempts.')
            incoming = dict(usage=amounts, outcome=outcome, evidence=proof, idempotency_key=idempotency_key)
            native = _native_usage(self, row, incoming)
            keep_native_open = False
            if native is not None:
                _need(not _native_finalized(state, row, native.get('token')), 'STATE',
                      'A finalized native token cannot report another observation.')
                if outcome != 'uncertain':
                    _require_generic_reconciliation(self, row)
            elif outcome in ('complete', 'failed'):
                _require_native_reconciliation(self, row)
                # Retain actual generic spend, including overruns, without
                # closing an independently active native worker's accounting.
                keep_native_open = _unfinished_native(state, row)
            if outcome in ('complete', 'failed'):
                _need(not _native_pending_usage(self, row, native), 'UNCERTAIN',
                      'Resolve every token-bound native unknown before final ledger settlement.')
            for key in METRICS:
                if amounts[key] is not None:
                    spent = Decimal(row['spent'][key]) + Decimal(amounts[key])
                    row['spent'][key] = str(spent)
                    row['held'][key] = str(max(Decimal(row['reserved'][key]) - spent, Decimal(0)))
            row['attempts'].append(dict(usage=amounts,outcome=outcome,evidence=proof,idempotency_key=idempotency_key))
            if outcome in ('complete','failed') and not keep_native_open:
                row['state']='settled';row['held']={key:'0' for key in METRICS}
            else:row['state']='uncertain' if outcome=='uncertain' else 'reserved'
            return {'reservation_id':reservation_id,'state':row['state'],'spent':deepcopy(row['spent']),'held':deepcopy(row['held']),
                    'over_budget':any(Decimal(row['spent'][key]) > Decimal(row['reserved'][key]) for key in METRICS)}
        return self._change(expected_revision,idempotency_key,'settle',payload,mutate)

    def _release_row(self, state, reservation_id, outcome, *, check_legacy=None):
        """One eligibility check for preflight and the locked ledger mutation."""
        _need(reservation_id in state['reservations'], 'MISSING', 'Reservation does not exist.')
        row = state['reservations'][reservation_id]
        _need(row['state'] == 'reserved', 'UNCERTAIN', 'Uncertain/final reservations cannot be released.')
        published = [event['payload'] for event in state['events'] if event['action'] == 'native-claim' and
                     event['payload']['reservation_id'] == reservation_id]
        _need(outcome != 'unstarted' or not (row['attempts'] or published),
              'STATE', 'Started or published work cannot be released as unstarted.')
        if published:
            _require_native_finalization(state, row)
            _require_generic_reconciliation(self, row)
            _need(_native_remainder_known(self, row, published[-1]['token']), 'UNCERTAIN',
                  'The latest published native attempt needs reconciled usage before cancellation.')
        if re.fullmatch(r'native-[0-9a-f]{64}', row['operation_id']):
            admission = next(event for event in state['events'] if event['action'] == 'reserve' and
                             event['payload']['operation_id'] == row['operation_id'])
            intent = _native_admission(self, admission)
            if intent is not None and 'publication_protocol' not in intent:
                _need(outcome == 'canceled-known' and check_legacy is not None, 'STATE',
                      'Historical cancellation requires explicit reconciliation by its native owner.')
                check_legacy(row)
        return row

    def release(self, reservation_id, *, outcome, expected_revision, idempotency_key, evidence):
        return self._release(reservation_id, outcome=outcome, expected_revision=expected_revision,
                             idempotency_key=idempotency_key, evidence=evidence, check_legacy=None)

    def _release(self, reservation_id, *, outcome, expected_revision, idempotency_key,
                 evidence, check_legacy):
        _id(reservation_id)
        _need(outcome in ('unstarted','canceled-known'), 'INPUT', 'Release requires known nonexecuted remainder.')
        proof = _evidence(evidence);_need(bool(proof), 'EVIDENCE', 'Release requires evidence that remaining work is not running.')
        payload=dict(reservation_id=reservation_id,outcome=outcome,evidence=proof)
        def mutate(state):
            row = self._release_row(state, reservation_id, outcome, check_legacy=check_legacy)
            row['state']='released';row['held']={key:'0' for key in METRICS}
            return {'reservation_id':reservation_id,'state':'released','spent':deepcopy(row['spent'])}
        return self._change(expected_revision,idempotency_key,'release',payload,mutate)


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



NATIVE_OWNER_FILE = 'director-budget-owner.json'


def _require_unrestored_budget(record):
    """Existing native/project restore receipts never renew accounting authority."""
    history = record.get('history', [])
    _need(isinstance(history, list) and all(isinstance(item, dict) for item in history),
          'RESTORED', 'Accounting requires intact native restoration history.')
    _need(not any(item.get('event') in ('restored', 'project-restored') for item in history),
          'RESTORED', 'Restored runs/projects cannot authorize budgeted work without a reconciliation transaction.')


def _native_owner_identity(owner, ledger_root, genesis):
    return dict(schema_version=1, kind='native-budget-owner',
                owner_location_sha256=object_hash(str(owner)),
                ledger_path=ledger_root.relative_to(owner).as_posix(), genesis=genesis)


def _native_enrollment(root, data, *, recovery=False, policy=None):
    import hatch
    if recovery or policy is not None:
        config = data.get('director_budget') if policy is None else policy
        _need(isinstance(config, dict) and
              isinstance(config.get('request_sha256'), str) and
              re.fullmatch(r'[0-9a-f]{64}', config['request_sha256']),
              'BINDING', 'Recovery requires the original native enrollment digest.')
        request_hash = config['request_sha256']
    else:
        request_hash = hatch.input_digest(Path(root) / 'pet_request.json')
    enrollment = dict(run_id=data['run_id'], run_location_sha256=object_hash(str(Path(root).resolve())),
                      request_sha256=request_hash)
    policy = data.get('director_budget') if policy is None else policy
    if policy is not None:
        enrollment['policy_sha256'] = object_hash(policy)
    return enrollment

def _read_native_owner(path):
    """Read a bounded owner association and its monotonic enrollment identities."""
    _need(not path.is_symlink(), 'PATH', 'Native owner association cannot be a link.')
    value = decode_json(_read_source_bytes(path, MAX_BYTES))
    fields = {'schema_version', 'kind', 'owner_location_sha256', 'ledger_path', 'genesis', 'enrollments'}
    _need(isinstance(value, dict) and set(value) == fields and
          value['schema_version'] == 1 and value['kind'] == 'native-budget-owner',
          'BINDING', 'Malformed native owner association.')
    enrollments = value['enrollments']
    _need(isinstance(enrollments, list) and 0 < len(enrollments) <= MAX_RESERVATIONS,
          'BINDING', 'Native enrollment inventory is missing or exceeds its bound.')
    identities, locations = set(), set()
    for entry in enrollments:
        identity_fields = {'run_id', 'run_location_sha256', 'request_sha256'}
        _need(isinstance(entry, dict) and set(entry) in (
            identity_fields, identity_fields | {'policy_sha256'}),
            'BINDING', 'Malformed native enrollment identity.')
        _id(entry['run_id'])
        _need(all(isinstance(entry[key], str) and re.fullmatch(r'[0-9a-f]{64}', entry[key])
                  for key in set(entry) - {'run_id'}),
              'BINDING', 'Malformed native enrollment digest.')
        _need(entry['run_id'] not in identities and entry['run_location_sha256'] not in locations,
              'BINDING', 'Native enrollment identities cannot be replaced or duplicated.')
        identities.add(entry['run_id']); locations.add(entry['run_location_sha256'])
    return value


def _native_owner_binding(owner, ledger_root, genesis, *, create=False, enrollment=None):
    """One ledger association; enrollment append requires existing Hatch ownership."""
    import hatch
    expected = _native_owner_identity(owner, ledger_root, genesis)
    path = owner / NATIVE_OWNER_FILE
    _need(not path.is_symlink(), 'PATH', 'Native owner association cannot be a link.')
    exists = path.exists()
    if exists:
        stored = _read_native_owner(path)
        _need({key: stored[key] for key in expected} == expected,
              'BINDING', 'Native owner already belongs to a different ledger or location.')
        enrollments = stored['enrollments']
    else:
        _need(create and enrollment is not None, 'BINDING', 'Native owner ledger association is missing.')
        enrollments = []
    changed = not exists
    if enrollment is not None:
        matches = [entry for entry in enrollments if entry['run_id'] == enrollment['run_id']
                   or entry['run_location_sha256'] == enrollment['run_location_sha256']]
        if matches:
            # Completed records predating policy retention remain readable.
            # New records authenticate the complete policy on every use.
            original = matches[0]
            expected_enrollment = enrollment if 'policy_sha256' in original else {
                key: value for key, value in enrollment.items() if key != 'policy_sha256'}
            _need(matches == [expected_enrollment], 'BINDING',
                  'Native enrollment identity or policy cannot be replaced.')
        else:
            _need(create, 'BINDING', 'Native accounting enrollment is missing from its owner.')
            _need(len(enrollments) < MAX_RESERVATIONS, 'BINDING', 'Native enrollment inventory is full.')
            enrollments = enrollments + [deepcopy(enrollment)]
            changed = True
    if changed:
        hatch.atomic(path, dict(expected, enrollments=enrollments))
    return expected


def _native_operation_id(run_id, job, hashes, fixture, policy, handoff=None):
    identity = dict(run_id=run_id, job=job, inputs=hashes, fixture=fixture, policy=policy)
    if handoff is not None:
        identity['handoff'] = handoff
    return 'native-' + object_hash(identity)


def _handoff_hashes(value):
    fields = {'extraction-settings.json', 'profile.json'}
    _need(isinstance(value, dict) and set(value) in (
          fields, fields | {'@generation-job'}, fields | {'@generation-job', '@journal-headroom'}) and
          isinstance(value['profile.json'], str) and re.fullmatch(r'[0-9a-f]{64}', value['profile.json']) and
          ('@generation-job' not in value or isinstance(value['@generation-job'], str) and
           re.fullmatch(r'[0-9a-f]{64}', value['@generation-job'])) and
          ('@journal-headroom' not in value or value['@journal-headroom'] == NATIVE_RECOVERY_POLICY) and
          (value['extraction-settings.json'] is None or
           isinstance(value['extraction-settings.json'], str) and
           re.fullmatch(r'[0-9a-f]{64}', value['extraction-settings.json'])),
          'BINDING', 'Invalid captured native handoff identity.')
    return deepcopy(value)


def native_recovery_envelope(data):
    """Size a terminal recovery path without changing or persisting any field.

    Current admissions bind this policy in their immutable handoff identity.
    Historical tokens retain their existing recovery writes and do not acquire
    a new capacity promise retroactively. The ledger's own bounds still apply.
    """
    active = [index for index, job in enumerate(data['jobs'])
              if job.get('budget_operation', {}).get('handoff_hashes', {}).get('@journal-headroom') == NATIVE_RECOVERY_POLICY]
    if not active:
        return data
    result = deepcopy(data)
    reference = {'path': 'evidence/' + 'f' * 64, 'sha256': 'f' * 64}
    # Maximum accepted normalized delta and cumulative receipt decimal widths.
    delta = '1000000000000000.' + '0' * 64
    aggregate = '128000000000000000.' + '0' * 64
    for index in active:
        job = result['jobs'][index]
        operation = job['budget_operation']
        # A lost reservation acknowledgment is an existing recoverable state.
        # Reserve its fixed-width identity too; only reconciliation may restore
        # the actual immutable receipt in the real journal.
        operation.setdefault('reservation_id', 'reservation-' + 'f' * 24)
        def reserve_receipt(event):
            if 'receipt' not in event:
                event['receipt'] = dict(reservation_id=operation['reservation_id'], state='reserved',
                                       spent={key: aggregate for key in METRICS},
                                       held={key: aggregate for key in METRICS}, over_budget=False,
                                       revision=MAX_REVISIONS - 1, idempotency_key=event['key'])
        # Every pending acknowledgment must fit before another observation can
        # replay, including an uncertain intent preceding its final resolution.
        for event in operation['events']:
            reserve_receipt(event)
        if job.get('status') != 'running':
            # Reserve a concise cancellation path before committing admission,
            # not only after a provider token has already been published.
            final = operation['events'][-1] if operation['events'] else None
            if final is None or final['payload']['outcome'] not in ('complete', 'failed', 'unstarted', 'canceled-known'):
                payload = dict(token=None, id='x' * 128, outcome='canceled-known',
                               evidence=[deepcopy(reference)])
                final = dict(key='native-release-' + 'f' * 64, payload=payload,
                             evidence=[deepcopy(reference), deepcopy(reference)])
                operation['events'].append(final)
                reserve_receipt(final)
            continue
        token = job['token']
        observations = [event for event in operation['events'] if event['payload']['token'] == token]
        terminal = (observations[-1] if observations and
                    observations[-1]['payload']['outcome'] in ('complete', 'failed', 'retry') else None)
        if terminal is None:
            pending = set()
            for event in observations:
                payload = event['payload']
                pending.difference_update(payload['resolves'])
                if payload['outcome'] == 'uncertain':
                    pending.add(payload['id'])
            proofs = [deepcopy(reference) for _ in range(127)]
            payload = dict(token=token, id='x' * 128, outcome='complete',
                           usage={key: delta for key in METRICS}, evidence=proofs,
                           measurement_source='\0' * 1024, resolves=sorted(pending))
            terminal = dict(key='native-observation-' + 'f' * 64, payload=payload,
                            evidence=proofs + [deepcopy(reference)])
            operation['events'].append(terminal)
        reserve_receipt(terminal)
        # A concise failure/finalization remains available even if the caller
        # cannot provide an output artifact. Larger annotations may be refused.
        job['history'].append(dict(event='failed', time=1.7976931348623157e308,
                                   reason='accounted', token=token))
        job['status'] = 'failed'
        job.pop('token', None)
        job.pop('stale', None)
    return result


def _native_proof(ledger, references, kinds):
    """Recognize explicit native proof, not a generic operation's name."""
    references = _evidence(references)
    if not references:
        return None
    reference = references[-1]
    encoded = _read_source_bytes(ledger.root / reference['path'], MAX_BYTES)
    _need(hashlib.sha256(encoded).hexdigest() == reference['sha256'],
          'BINDING', 'Retained accounting evidence changed.')
    try:
        proof = decode_json(encoded)
    except (RigError, ValueError, RecursionError):
        return None  # Generic evidence is allowed to contain non-JSON bytes.
    return proof if (isinstance(proof, dict) and isinstance(proof.get('kind'), str)
                     and proof['kind'] in kinds) else None


def _native_usage(ledger, row, attempt):
    """Identify a token-bound native observation, including its actual deltas."""
    proof = _native_proof(ledger, attempt['evidence'], {'native-budget-usage'})
    if (proof is None or proof.get('operation_id') != row['operation_id'] or
            attempt['idempotency_key'] != 'native-observation-' +
                object_hash([proof.get('operation_id'), proof.get('id')]) or
            proof.get('usage') != attempt['usage'] or proof.get('outcome') != attempt['outcome'] or
            proof.get('evidence') != attempt['evidence'][:-1]):
        return None
    return proof


def _require_generic_reconciliation(ledger, row):
    """Only explicit generic accounting may reconcile generic uncertainty.

    Native resolutions name native observation IDs. They cannot implicitly
    clear unknown usage that has no counterpart in that journal, even if an
    earlier native client incorrectly changed the ledger's row state.
    """
    unresolved = False
    for attempt in row['attempts']:
        if _native_usage(ledger, row, attempt) is None:
            unresolved = attempt['outcome'] == 'uncertain'
    _need(not unresolved, 'UNCERTAIN',
          'Reconcile direct ledger uncertainty explicitly before known native usage or another claim.')


def _require_native_reconciliation(ledger, row):
    """A generic terminal settlement cannot erase token-bound unknown usage."""
    pending = set()
    for attempt in row['attempts']:
        proof = _native_usage(ledger, row, attempt)
        if proof is None:
            continue
        token = proof.get('token')
        pending.difference_update((token, identity) for identity in proof.get('resolves', []))
        if proof['outcome'] == 'uncertain':
            pending.add((token, proof['id']))
    _need(not pending, 'UNCERTAIN',
          'Resolve native uncertainty with its original token before terminal generic accounting.')


def _native_finalized(state, row, token):
    return any(event['action'] == 'native-finalize' and
               event['payload']['reservation_id'] == row['id'] and
               event['payload']['operation_id'] == row['operation_id'] and
               event['payload']['token'] == token for event in state['events'])


def _unfinished_native(state, row):
    publications = [event['payload'] for event in state['events'] if
                    event['action'] == 'native-claim' and event['payload']['reservation_id'] == row['id']]
    return bool(publications and not _native_finalized(state, row, publications[-1]['token']))


def _require_native_finalization(state, row):
    _need(not _unfinished_native(state, row),
          'UNCERTAIN', 'Finalize the published native token before canceling its reservation.')


def _native_remainder_known(ledger, row, token):
    observations = [proof for attempt in row['attempts']
                    if (proof := _native_usage(ledger, row, attempt)) is not None]
    if not observations:
        return False
    observations = [proof for proof in observations if proof.get('token') == token]
    pending = set()
    for proof in observations:
        pending.difference_update(proof.get('resolves', []))
        if proof['outcome'] == 'uncertain':
            pending.add(proof['id'])
    return bool(observations) and not pending and observations[-1]['outcome'] == 'retry'


def _native_pending_usage(ledger, row, incoming=None):
    """Keep native observation identities authoritative across generic APIs."""
    pending = {}
    for proof in [*(_native_usage(ledger, row, attempt) for attempt in row['attempts']), incoming]:
        if proof is None:
            continue
        token = proof.get('token')
        unresolved = pending.setdefault(token, set())
        unresolved.difference_update(proof.get('resolves', []))
        if proof['outcome'] == 'uncertain':
            unresolved.add(proof['id'])
    return any(pending.values())


def _read_native_intent(ledger, reference, identifier):
    encoded = _read_source_bytes(ledger.root / reference['path'], MAX_BYTES)
    _need(hashlib.sha256(encoded).hexdigest() == reference['sha256'],
          'BINDING', 'Retained native admission evidence changed.')
    intent = decode_json(encoded)
    fields = {'kind', 'run_id', 'job', 'fixture', 'operation_id', 'input_hashes', 'policy'}
    _need(isinstance(intent, dict) and set(intent) in (fields, fields | {'publication_protocol', 'handoff_hashes'}) and
          intent['kind'] == 'native-budget-intent' and isinstance(intent['policy'], dict),
          'BINDING', 'Retained native admission cannot be authenticated.')
    handoff = intent.get('handoff_hashes')
    if 'publication_protocol' in intent:
        _need(type(intent['publication_protocol']) is int and intent['publication_protocol'] == 1,
              'BINDING', 'Invalid native publication protocol.')
        _handoff_hashes(handoff)
    expected = _native_operation_id(intent['run_id'], intent['job'], intent['input_hashes'],
                                    intent['fixture'], intent['policy'], handoff)
    _need(intent['operation_id'] == identifier == expected,
          'BINDING', 'Retained native intent differs from its admitted operation.')
    return intent


def _native_admission(ledger, event):
    if event['action'] != 'reserve':
        return None
    payload = event['payload']
    identifier = payload.get('operation_id')
    if not isinstance(identifier, str) or not re.fullmatch(r'native-[0-9a-f]{64}', identifier):
        return None
    proof = _native_proof(ledger, payload.get('evidence'), {'native-budget-intent'})
    if (proof is None or proof.get('operation_id') != identifier or
            event['idempotency_key'] != identifier + ':reserve'):
        return None
    return _read_native_intent(ledger, payload['evidence'][-1], identifier)


def _native_admissions(ledger):
    """Read authenticated native intents from all retained reserve events."""
    _need(isinstance(ledger, BudgetLedger), 'BINDING', 'Enrollment replay requires its retained ledger.')
    for event in ledger.snapshot()['state']['events']:
        intent = _native_admission(ledger, event)
        if intent is not None:
            yield intent


def _require_no_native_admission(ledger, root, data):
    """An older journal cannot erase admitted work, even after release."""
    location = object_hash(str(Path(root).resolve()))
    for intent in _native_admissions(ledger):
        _need(intent['run_id'] != data['run_id'] and
              intent['policy'].get('run_location_sha256') != location,
              'BINDING', 'Native admission already exists; restore the original claim journal instead of reenrolling.')


def reject_legacy_budget_rollback(root, data, *, enrollment=None, ledger=None):
    """An earlier unbudgeted snapshot cannot erase enrollment retained by its owner."""
    _need(enrollment is None or isinstance(enrollment, dict), 'BINDING',
          'Enrollment preflight requires an exact owner/ledger association.')
    root = Path(root).resolve()
    location = object_hash(str(root))
    for owner in (root, *root.parents):
        path = owner / NATIVE_OWNER_FILE
        if not path.exists() and not path.is_symlink():
            continue
        stored = _read_native_owner(path)
        _need(stored['owner_location_sha256'] == object_hash(str(owner)),
              'BINDING', 'Native accounting owner was relocated without reconciliation.')
        matches = [entry for entry in stored['enrollments'] if entry['run_id'] == data['run_id'] or
                   entry['run_location_sha256'] == location]
        if matches:
            _need(enrollment is not None, 'BINDING',
                  'A previously enrolled run cannot fall back to unbudgeted ownership.')
            expected = _native_enrollment(root, data)
            _need(len(matches) == 1 and {key: value for key, value in matches[0].items()
                                        if key != 'policy_sha256'} == expected, 'BINDING',
                  'Interrupted enrollment identity differs from its retained original.')
            _need('policy_sha256' in matches[0], 'BINDING',
                  'Interrupted enrollment lacks its retained policy digest.')
            _need(enrollment == {key: stored[key] for key in (
                'schema_version', 'kind', 'owner_location_sha256', 'ledger_path', 'genesis')},
                'BINDING', 'Interrupted enrollment belongs to a different owner or ledger.')
            _need(isinstance(ledger, BudgetLedger) and
                  _native_owner_identity(owner, ledger.root, stored['genesis']) == enrollment,
                  'BINDING', 'Interrupted enrollment requires its original ledger authority.')
            _require_no_native_admission(ledger, root, data)
        _need(enrollment is not None or not (owner / 'project.json').is_file(), 'BINDING',
              'Acquisitions in a budgeted production project must enroll before native admission.')


def _project_enrollment_runs(owner, state, association, ledger):
    """Validate fresh pending enrollment through the existing production owner.

    Only this read-only enrollment preflight permits an unbudgeted binding.
    Normal production validation continues to require the complete association.
    """
    import hatch
    import production_workflow as workflow
    _need(len(state['generation_bindings']) <= MAX_RESERVATIONS, 'BINDING',
          'Project enrollment inventory exceeds its bound.')
    runs = []
    for identifier, binding in state['generation_bindings'].items():
        bound = workflow.safe_path(owner, binding['run_path'])
        data = hatch.manifest(bound)
        subset = dict(state, generation_bindings={identifier: binding})
        if 'director_budget' not in data:
            _require_unrestored_budget(data)
            hatch._require_current_authority(data)
            reject_legacy_budget_rollback(bound, data, enrollment=association, ledger=ledger)
            _need(not any(job.get('attempts', 0) or job.get('token') or
                          'budget_operation' in job or
                          any(entry.get('event') == 'claim' for entry in job['history'])
                          for job in data['jobs']), 'BINDING',
                  'Only never-claimed native bindings may await project budget enrollment.')
            subset.pop('director_budget', None)
        # Reuse exact binding, request, coverage and fixture validation. The
        # temporary subset never changes the authoritative production state.
        runs.extend(workflow._bound_runs(owner, subset))
    _need(len({str(item[2]) for item in runs}) == len(runs), 'BINDING',
          'One acquisition cannot consume multiple production bindings.')
    return runs


def require_production_budget(root, state, run, data):
    """Validate the existing production association; caller keeps native ownership."""
    root, run = Path(root).resolve(), Path(run).resolve()
    if 'director_budget' not in data:
        _need('director_budget' not in state, 'BINDING',
              'Every acquisition in this budgeted project must opt into its ledger.')
        return
    _require_unrestored_budget(state)
    budget = NativeBudget(run, data)
    _need(budget.owner == root, 'BINDING', 'Production acquisition must use its project budget owner.')
    expected = _native_owner_binding(root, budget.ledger.root, budget.config['genesis'])
    _need(state.get('director_budget') == expected, 'BINDING',
          'Production workflow and acquisition must reference the same authoritative ledger.')


def bind_native_budget(root, ledger_root, *, owner_root=None, provider, estimate,
                       retry_allowance, evidence, unknown_price=False, unknown_price_cap=None):
    """Opt a fresh Hatch run into one existing project ledger; never submit work."""
    import hatch
    root = Path(root).resolve()
    owner = root if owner_root is None else Path(owner_root).resolve()
    ledger_root = Path(ledger_root).absolute()
    project = next((parent for parent in root.parents if (parent / 'project.json').is_file()), None)
    _need(project is None or owner == project, 'BINDING',
          'A production acquisition must opt in under its containing project owner.')
    _need(root.is_relative_to(owner) and ledger_root.is_relative_to(owner),
          'PATH', 'Native run and ledger must belong to the declared owner directory.')
    _need(ledger_root != root and ledger_root.resolve() == ledger_root,
          'PATH', 'Use a separate canonical ledger directory.')
    ledger = BudgetLedger(ledger_root); snapshot = ledger.snapshot()
    _id(provider); estimate = _quantities(estimate, unknown=True); evidence = _evidence(evidence)
    _need(len(evidence) < 128 and type(unknown_price) is bool, 'INPUT', 'Bounded explicit native policy required.')
    if unknown_price_cap is not None:
        unknown_price_cap = str(_number(unknown_price_cap, 'amount'))
    _need(_number(estimate['provider_calls'], 'provider_calls') >= 1,
          'ESTIMATE', 'Every native attempt must reserve at least one provider call.')
    _verify_evidence(ledger.root, evidence)
    _need(provider in snapshot['state']['provider_limits'], 'PROVIDER', 'Provider has no explicit budget policy.')
    _need(type(retry_allowance) is int and 0 <= retry_allowance <= 16,
          'INPUT', 'Retries must be bounded from zero to sixteen.')
    config = dict(schema_version=1, owner_depth=len(root.relative_to(owner).parts),
                  ledger_path=ledger_root.relative_to(owner).as_posix(),
                  genesis=next(ledger_root.glob('00000000-*.json')).stem[9:],
                  provider=provider, estimate=deepcopy(estimate), retry_allowance=retry_allowance,
                  evidence=deepcopy(evidence), unknown_price=unknown_price, unknown_price_cap=unknown_price_cap)
    from contextlib import ExitStack
    with hatch.ownership(owner), ExitStack() as locks:
        if root != owner:
            locks.enter_context(hatch.ownership(root))
        data = hatch.manifest(root)
        _require_unrestored_budget(data)
        config.update(run_id=data['run_id'], fixture=data['fixture_mode'],
                      request_sha256=hatch.input_digest(root / 'pet_request.json'),
                      run_location_sha256=object_hash(str(root)))
        existing = data.get('director_budget')
        if existing is not None:
            _need(existing == config, 'BINDING', 'Native accounting policy cannot be replaced.')
        else:
            _need(not any(entry.get('event') == 'claim' for job in data['jobs'] for entry in job['history']),
                  'BINDING', 'Opt in before the first native claim; existing expenditure cannot be invented.')
        association = _native_owner_identity(owner, ledger_root, config['genesis'])
        if existing is None:
            reject_legacy_budget_rollback(root, data, enrollment=association, ledger=ledger)
        project_state = None
        if (owner / 'project.json').is_file():
            import production_workflow as workflow
            project_state = workflow._state(owner)
            _require_unrestored_budget(project_state)
            _need(project_state.get('director_budget', association) == association, 'BINDING',
                  'Production project already belongs to a different ledger.')
            _need(len(project_state['generation_bindings']) <= MAX_RESERVATIONS, 'BINDING',
                  'Project enrollment inventory exceeds its bound.')
            # Freeze fresh sibling journals through publication: a direct native
            # claim uses the same run lock even before the project opts in.
            siblings = {workflow.safe_path(owner, binding['run_path'])
                        for binding in project_state['generation_bindings'].values()}
            for bound in sorted(siblings - {owner, root}):
                locks.enter_context(hatch.ownership(bound))
            for _, _, bound, bound_data in _project_enrollment_runs(owner, project_state, association, ledger):
                if 'director_budget' not in bound_data:
                    continue  # Admission remains blocked until every binding enrolls.
                # Existing associations are checked before any configuration is published.
                previous = NativeBudget(bound, bound_data)
                _need(previous.owner == owner and previous.ledger.root == ledger_root,
                      'BINDING', 'Existing project acquisition uses a different ledger.')
        enrollment = _native_enrollment(root, data, policy=config)
        _need(hatch.input_digest(root / 'pet_request.json') == config['request_sha256'], 'BINDING',
              'Captured enrollment request changed before owner publication.')
        _native_owner_binding(owner, ledger_root, config['genesis'], create=True,
                              enrollment=enrollment)
        if project_state is not None and 'director_budget' not in project_state:
            project_state['director_budget'] = association
            workflow._save(owner, project_state)
        if existing is not None:
            return deepcopy(config)
        data['director_budget'] = config
        hatch.save(root, data)
    return deepcopy(config)


class NativeBudget:
    """Accounting hooks called only while the existing Hatch owner holds its lock.

    The native manifest journals accounting intents; the ledger alone owns
    reservations and balances. This object never claims, fails or ingests a job.
    One bounded operation per job is supported; a completed operation cannot be
    silently reopened by repair. Start a separately identified run for new work.
    """

    @classmethod
    def for_run(cls, root, data, *, recovery=False, claim_handoff=None):
        _need(type(recovery) is bool, 'INPUT', 'Recovery mode must be explicit.')
        if 'director_budget' not in data:
            reject_legacy_budget_rollback(root, data)
            return None
        return cls(root, data, recovery=recovery, claim_handoff=claim_handoff)

    def __init__(self, root, data, *, recovery=False, claim_handoff=None):
        _need(type(recovery) is bool, 'INPUT', 'Recovery mode must be explicit.')
        self.recovery = recovery
        self.claim_handoff = None if claim_handoff is None else _handoff_hashes(claim_handoff)
        self.root, self.data = Path(root).resolve(), data
        _require_unrestored_budget(data)
        self.config = data['director_budget']
        config = self.config
        _need(isinstance(config, dict) and set(config) == {
            'schema_version', 'owner_depth', 'ledger_path', 'genesis', 'provider', 'estimate',
            'retry_allowance', 'evidence', 'unknown_price', 'unknown_price_cap',
            'run_id', 'fixture', 'request_sha256', 'run_location_sha256'},
            'BINDING', 'Malformed native accounting policy.')
        import hatch
        if not recovery:
            hatch._require_current_authority(data)
        _need(config['run_location_sha256'] == object_hash(str(self.root)), 'BINDING',
              'Relocated opted-in runs cannot authorize work; preserve the original owner location.')
        _need(config['run_id'] == data['run_id'] and config['fixture'] is data['fixture_mode'] and
              (recovery or config['request_sha256'] == hatch.input_digest(self.root / 'pet_request.json')),
              'BINDING', 'Native accounting identity or original request changed.')
        depth = config['owner_depth']
        _need(config['schema_version'] == 1 and type(depth) is int and 0 <= depth < len(self.root.parents),
              'PATH', 'Invalid native accounting owner.')
        owner = self.root if depth == 0 else self.root.parents[depth - 1]
        self.owner = owner
        project_state = owner / 'production-workflow.json'
        if project_state.is_file():
            _require_unrestored_budget(hatch.input_json(project_state))
        from pathlib import PurePosixPath
        relative = config['ledger_path']
        _need(isinstance(relative, str) and relative and '\\' not in relative and ':' not in relative,
              'PATH', 'Ledger path must be portable.')
        parts = PurePosixPath(relative)
        _need(not parts.is_absolute() and '..' not in parts.parts and parts.as_posix() == relative and relative != '.',
              'PATH', 'Ledger path escapes its owner.')
        ledger_root = owner.joinpath(*parts.parts)
        self.ledger = BudgetLedger(ledger_root)
        self.ledger.snapshot()
        _need(next(ledger_root.glob('00000000-*.json')).stem[9:] == config['genesis'],
              'BINDING', 'Native ledger identity changed.')
        _verify_evidence(ledger_root, config['evidence'])
        _native_owner_binding(owner, ledger_root, config['genesis'],
                              enrollment=_native_enrollment(self.root, data, recovery=recovery))
        for intent in _native_admissions(self.ledger):
            if (intent['run_id'] != data['run_id'] and
                    intent['policy'].get('run_location_sha256') != config['run_location_sha256']):
                continue
            retained_job = next((job for job in data['jobs'] if job['id'] == intent['job']), None)
            _need(retained_job is not None and
                  retained_job.get('budget_operation', {}).get('operation_id') == intent['operation_id'],
                  'BINDING', 'Native journal lost a retained admission; recover its original operation journal.')
        for job in data['jobs']:
            operation = job.get('budget_operation')
            if operation is None:
                continue
            _need(isinstance(operation.get('input_hashes'), dict) and
                  operation['input_hashes'].get('pet_request.json') == config['request_sha256'],
                  'BINDING', 'Native operation differs from its original request enrollment.')
            expected = _native_operation_id(data['run_id'], job['id'], operation['input_hashes'],
                                             data['fixture_mode'], config, operation.get('handoff_hashes'))
            request = operation['reservation_request']
            _need(operation['operation_id'] == expected and request['operation_id'] == expected and
                  request['idempotency_key'] == expected + ':reserve' and
                  all(request[key] == config[key] for key in
                      ('provider', 'estimate', 'retry_allowance', 'unknown_price', 'unknown_price_cap')) and
                  request['evidence'][:-1] == config['evidence'] and
                  len(request['evidence']) == len(config['evidence']) + 1,
                  'BINDING', 'Native operation or accounting policy was rebound.')
        for job in data['jobs']:
            self._check_claim_bindings(job)

    def _check_claim_bindings(self, job):
        """Authenticate retained claim identities before any accounting replay."""
        _need(any(item is job for item in self.data['jobs']), 'BINDING',
              'Native accounting requires this owner job.')
        claims = [entry for entry in job['history'] if entry.get('event') == 'claim']
        operation = job.get('budget_operation')
        if operation is None:
            _need(not claims, 'BINDING', 'A budgeted native claim lost its original operation.')
            return None
        attempts = operation['attempts']
        _need(isinstance(attempts, list) and len(attempts) <= 128 and
              all(isinstance(token, str) and token for token in attempts) and
              len(set(attempts)) == len(attempts), 'BINDING',
              'Native attempt identities must be unique and bounded.')
        claimed = {}
        for claim in claims:
            token = claim.get('token')
            _need(isinstance(token, str) and token in attempts and token not in claimed and
                  claim.get('input_hashes') == operation['input_hashes'],
                  'BINDING', 'Native claim differs from its original admitted input identity.')
            claimed[token] = claim
        ledger_events = self.ledger.snapshot()['state']['events']
        admission = next((event for event in ledger_events if event['action'] == 'reserve' and
                          event['idempotency_key'] == operation['reservation_request']['idempotency_key']), None)
        reservation_id = operation.get('reservation_id')
        if admission is not None:
            retained_id = admission['receipt']['reservation_id']
            _need(reservation_id in (None, retained_id), 'BINDING', 'Native reservation identity changed.')
            reservation_id = retained_id  # A lost acknowledgement is recoverable; no new admission is made.
        publications = [event['payload'] for event in ledger_events if event['action'] == 'native-claim' and
                        event['payload']['operation_id'] == operation['operation_id']]
        for publication in publications:
            token = publication['token']
            _need(token in claimed and publication['run_id'] == self.data['run_id'] and
                  publication['run_location_sha256'] == self.config['run_location_sha256'] and
                  publication['job'] == job['id'] and
                  publication['input_hashes'] == operation['input_hashes'] and
                  publication['reservation_id'] == reservation_id and
                  publication['attempt'] == attempts.index(token) + 1,
                  'BINDING', 'Native journal lost its published claim; recover the original claim journal.')
        retained_events = {}
        for event in operation['events']:
            payload = event['payload']
            release = payload.get('token') is None
            fields = {'token', 'id', 'outcome', 'evidence'}
            if 'nonexecution_attestation' in payload:
                _need(release and payload['outcome'] == 'canceled-known' and
                      'handoff_hashes' not in operation and isinstance(payload['nonexecution_attestation'], dict),
                      'BINDING', 'Unexpected legacy nonexecution attestation.')
                witness = payload['nonexecution_attestation']
                transitions = [entry for entry in job['history'] if entry.get('event') in ('claim', 'failed')]
                _need(attempts and [entry['token'] for entry in claims] == attempts and
                      witness.get('latest_token') == attempts[-1] and
                      witness.get('claim_history_sha256') == object_hash(transitions),
                      'BINDING', 'Native claim history differs from its retained nonexecution attestation.')
                fields.add('nonexecution_attestation')
            _need(set(payload) == (fields if release else fields | {'usage', 'measurement_source', 'resolves'}),
                  'BINDING', 'Native observation fields differ from the retained protocol.')
            kind = 'native-budget-release' if release else 'native-budget-usage'
            prefix = 'native-release-' if release else 'native-observation-'
            proof = _native_proof(self.ledger, event['evidence'], {kind})
            expected = {'kind': kind, 'fixture': self.data['fixture_mode'],
                        'operation_id': operation['operation_id'], **payload}
            _need(proof == expected and payload.get('evidence') == event['evidence'][:-1] and
                  event['key'] == prefix + object_hash([operation['operation_id'], payload.get('id')]) and
                  event['key'] not in retained_events,
                  'BINDING', 'Native observation differs from its complete retained proof.')
            retained_events[event['key']] = event
        transitions = [entry for entry in job['history'] if entry.get('event') in ('claim', 'failed')]
        for closure in ledger_events:
            final = closure['payload']
            if closure['action'] != 'native-finalize' or final['operation_id'] != operation['operation_id']:
                continue
            index = next((index for index, entry in enumerate(transitions) if
                          entry['event'] == 'failed' and entry.get('token') == final['token']), None)
            observation = retained_events.get(final['observation_key'])
            _need(final['reservation_id'] == reservation_id and final['token'] in claimed and
                  index is not None and object_hash(transitions[:index + 1]) == final['owner_history_sha256'] and
                  observation is not None and observation['payload']['token'] == final['token'] and
                  observation['payload']['outcome'] == 'retry' and observation['evidence'] == final['evidence'],
                  'BINDING', 'Native journal lost or changed its retained owner finalization.')
        for ledger_event in ledger_events:
            payload = ledger_event['payload']
            if (ledger_event['action'] not in ('settle', 'release') or
                    payload['reservation_id'] != reservation_id):
                continue
            proof = _native_proof(self.ledger, payload['evidence'],
                                  {'native-budget-usage', 'native-budget-release'})
            kind = 'native-budget-usage' if ledger_event['action'] == 'settle' else 'native-budget-release'
            prefix = 'native-observation-' if ledger_event['action'] == 'settle' else 'native-release-'
            if (proof is None or proof.get('kind') != kind or
                    ledger_event['idempotency_key'] != prefix + object_hash([proof.get('operation_id'), proof.get('id')])):
                continue  # Direct ledger accounting remains authoritative without a native journal entry.
            _need(proof.get('operation_id') == operation['operation_id'], 'BINDING',
                  'Native accounting proof belongs to a different operation.')
            native_event = retained_events.get(ledger_event['idempotency_key'])
            _need(native_event is not None, 'BINDING',
                  'Native journal lost retained accounting; recover its original observation journal.')
            expected = dict(reservation_id=reservation_id,
                            outcome=native_event['payload']['outcome'],
                            evidence=native_event['evidence'])
            if ledger_event['action'] == 'settle':
                expected['usage'] = native_event['payload']['usage']
            _need(payload == expected, 'BINDING', 'Native observation differs from retained ledger accounting.')
        for event in operation['events']:
            token = event['payload']['token']
            if token is None:
                _need(event['payload']['outcome'] in ('unstarted', 'canceled-known'),
                      'BINDING', 'Only explicit nonexecution releases omit a native token.')
            else:
                _need(isinstance(token, str) and token in claimed, 'BINDING',
                      'Native usage lacks its original published claim.')
        if job['status'] == 'running':
            _need(job.get('token') in claimed and
                  job.get('input_hashes') == operation['input_hashes'],
                  'BINDING', 'Active native job differs from its original admitted claim.')
        return operation

    def _published_claims(self, operation):
        return [event['payload'] for event in self.ledger.snapshot()['state']['events']
                if event['action'] == 'native-claim' and
                event['payload']['operation_id'] == operation['operation_id']]

    def _ledger_retry(self, action):
        """Retry only transient local CAS/lock contention with the same intent."""
        for attempt in range(8):
            try:
                return action()
            except RigError as exc:
                if exc.report['code'] not in ('RIG_BUSY', 'DIRECTOR_BUDGET_STALE') or attempt == 7:
                    raise
                time.sleep(.01 * (attempt + 1))

    def _publication_capacity(self, state, operation, token):
        row = state['reservations'].get(operation['reservation_id'])
        _need(row is not None and row['operation_id'] == operation['operation_id'] and
              row['state'] == 'reserved', 'BINDING', 'Publication requires its current reservation.')
        _require_generic_reconciliation(self.ledger, row)
        estimate = operation['reservation_request']['estimate']
        bound = {**estimate, 'amount': self.config['unknown_price_cap'] if
                 estimate['amount'] is None else estimate['amount']}
        _need(all(Decimal(row['held'][key]) >= _number(bound[key], key) for key in METRICS),
              'EXCEEDED', 'Remaining reservation cannot cover publication.')
        for provider in (None, self.config['provider']):
            spent, held = _totals(state, provider)
            limits = state['limits'] if provider is None else state['provider_limits'][provider]
            _need(all(spent[key] + held[key] <= Decimal(limits[key]) for key in METRICS),
                  'EXCEEDED', 'Actual overrun blocks native publication.')
        _need(operation['attempts'].index(token) < self.config['retry_allowance'] + 1,
              'EXCEEDED', 'Native retry allowance exhausted before publication.')

    @_exact
    def preflight_publication(self, job, token):
        """Keep a known budget refusal pending; final publication rechecks it."""
        operation = self._check_claim_bindings(job)
        _need(not self.recovery and operation is not None and token in operation['attempts'],
              'BINDING', 'Publication preflight requires its admitted native token.')
        def check():
            with job_lock(self.ledger.root):
                self._publication_capacity(self.ledger.snapshot()['state'], operation, token)
        self._ledger_retry(check)

    @_exact
    def publish_claim(self, job, token):
        """Retain publication authority before returning a native handoff.

        The native journal is already durable. A failure here leaves that claim
        running and requires explicit reconciliation; it never returns a token.
        This event measures no provider usage and owns no acquisition state.
        """
        _need(not self.recovery, 'STATE', 'Recovery cannot publish native claims.')
        operation = self._check_claim_bindings(job)
        _need(operation is not None and job['status'] == 'running' and job.get('token') == token,
              'BINDING', 'Publication requires the exact durable native claim.')
        payload = dict(reservation_id=operation['reservation_id'],
            operation_id=operation['operation_id'], run_id=self.data['run_id'],
            run_location_sha256=self.config['run_location_sha256'], job=job['id'], token=token,
            attempt=operation['attempts'].index(token) + 1,
            input_hashes=deepcopy(operation['input_hashes']),
            evidence=deepcopy(operation['reservation_request']['evidence']))
        def mutate(state):
            self._publication_capacity(state, operation, token)
            previous = [event['payload'] for event in state['events'] if
                        event['action'] == 'native-claim' and
                        event['payload']['operation_id'] == operation['operation_id']]
            _need(len(previous) + 1 == payload['attempt'], 'BINDING',
                  'Native publication history is missing or out of order.')
            return {key: payload[key] for key in ('reservation_id', 'operation_id', 'token', 'attempt')}
        key = 'native-claim-' + object_hash([operation['operation_id'], token])
        return self._ledger_retry(lambda: self.ledger._change(self.ledger.snapshot()['sha256'], key,
                                   'native-claim', payload, mutate))

    def _finalize_claim(self, operation, token, latest, owner_history_sha256):
        """Retain closure after the original owner finalization is durable.

        A measured retry alone does not end an active handoff: another measured
        delta may already be durable in its journal. The original failed owner
        must be saved first. Lost closure acknowledgments replay idempotently.
        """
        payload = dict(reservation_id=operation['reservation_id'],
                       operation_id=operation['operation_id'], token=token,
                       observation_key=latest['key'], owner_history_sha256=owner_history_sha256,
                       evidence=deepcopy(latest['evidence']))
        _need(latest['payload']['outcome'] == 'retry', 'STATE',
              'Only a measured retry needs an additional native accounting closure.')
        def mutate(state):
            row = state['reservations'].get(operation['reservation_id'])
            _need(row is not None and row['operation_id'] == operation['operation_id'],
                  'BINDING', 'Native finalization requires its original reservation.')
            publications = [event['payload'] for event in state['events'] if
                            event['action'] == 'native-claim' and
                            event['payload']['reservation_id'] == row['id']]
            _need(not publications or publications[-1]['token'] == token,
                  'STATE', 'Only the current native token can close its accounting.')
            _require_generic_reconciliation(self.ledger, row)
            native = [attempt for attempt in row['attempts'] if
                      (proof := _native_usage(self.ledger, row, attempt)) is not None and proof.get('token') == token]
            _need(_native_remainder_known(self.ledger, row, token) and
                  row['state'] == 'reserved' and native and
                  native[-1]['idempotency_key'] == latest['key'],
                  'UNCERTAIN', 'Current native uncertainty prevents finalization of its original owner.')
            return {field: payload[field] for field in
                    ('reservation_id', 'operation_id', 'token', 'observation_key', 'owner_history_sha256')}
        key = 'native-finalize-' + object_hash([operation['operation_id'], token])
        return self._ledger_retry(lambda: self.ledger._change(self.ledger.snapshot()['sha256'], key,
                                   'native-finalize', payload, mutate))

    def retain_finalization(self, job):
        """Authenticate a durable failed owner before closing its retry token."""
        if job['status'] != 'failed' or 'token' in job:
            return
        operation = self._check_claim_bindings(job)
        if operation is None:
            return
        publications = self._published_claims(operation)
        if not publications:
            return  # Historical ownership uses explicit all-attempt attestation.
        token = publications[-1]['token']
        transitions = [entry for entry in job['history'] if entry.get('event') in ('claim', 'failed')]
        if (len(transitions) < 2 or [(entry['event'], entry.get('token')) for entry in transitions[-2:]] !=
                [('claim', token), ('failed', token)]):
            return
        events = [event for event in operation['events'] if event['payload']['token'] == token]
        if not events or events[-1]['payload']['outcome'] != 'retry':
            return  # Native terminal settlement already closes the row.
        self._require_current_journal()
        self._finalize_claim(operation, token, events[-1], object_hash(transitions))

    def _save(self):
        import hatch
        hatch.save(self.root, self.data)

    def _proof(self, value):
        with tempfile.TemporaryDirectory(prefix='.native-budget-', dir=self.ledger.root.parent) as temporary:
            path = Path(temporary) / 'intent.json'
            path.write_text(json.dumps(value, sort_keys=True, allow_nan=False), encoding='utf-8')
            return self.ledger.retain_evidence(path)

    def _reserve(self, operation):
        if self.recovery:
            # Recovery may acknowledge an already committed admission but
            # cannot call the ledger reserve mutation or manufacture capacity.
            request = operation['reservation_request']
            snapshot = self.ledger.snapshot()
            events = [event for event in snapshot['state']['events']
                      if event['idempotency_key'] == request['idempotency_key']]
            _need(len(events) == 1 and events[0]['action'] == 'reserve',
                  'BINDING', 'Recovery requires an already committed native reservation.')
            payload, bound = _reservation_payload(**{key: request[key] for key in
                ('operation_id', 'provider', 'estimate', 'retry_allowance',
                 'unknown_price', 'unknown_price_cap', 'evidence')})
            receipt = events[0]['receipt']
            row = snapshot['state']['reservations'].get(receipt.get('reservation_id'))
            _need(events[0]['payload'] == payload and isinstance(row, dict) and
                  row['id'] == receipt['reservation_id'] and
                  row['operation_id'] == operation['operation_id'] and
                  row['provider'] == self.config['provider'] and
                  row['reserved'] == bound and row['unknown_price'] == payload['unknown_price'] and
                  receipt.get('reserved') == bound and receipt.get('state') == 'reserved',
                  'BINDING', 'Recovery reservation differs from its committed admission.')
            return deepcopy(receipt)
        options = deepcopy(operation['reservation_request'])
        return self._ledger_retry(lambda: self.ledger.reserve(**options,
                                  expected_revision=self.ledger.snapshot()['sha256']))

    def _require_current_journal(self):
        import hatch
        current = hatch.manifest(self.root, recovery=True)
        _need(object_hash(current) == object_hash(self.data), 'BINDING',
              'Native finalization requires the current on-disk owner journal.')

    def _legacy_release_witness(self, job, row):
        """Bind the owner's all-attempt attestation to verifiable retained history."""
        self._require_current_journal()
        operation = self._check_claim_bindings(job)
        _need(operation is not None and operation['reservation_id'] == row['id'] and
              operation['operation_id'] == row['operation_id'] and 'handoff_hashes' not in operation,
              'BINDING', 'Legacy cancellation differs from its admitted reservation.')
        tokens = operation['attempts']
        transitions = [entry for entry in job['history'] if entry.get('event') in ('claim', 'failed')]
        expected = [(event, token) for token in tokens for event in ('claim', 'failed')]
        _need(tokens and job['status'] == 'failed' and 'token' not in job and
              type(job.get('attempts')) is int and job['attempts'] == len(tokens) and
              [(entry['event'], entry.get('token')) for entry in transitions] == expected,
              'BINDING', 'Legacy cancellation requires ordered original claims and finalized failures.')
        _require_generic_reconciliation(self.ledger, row)
        _need(not _native_pending_usage(self.ledger, row), 'UNCERTAIN',
              'Resolve every native uncertainty before canceling historical work.')
        latest = {}
        for attempt in row['attempts']:
            proof = _native_usage(self.ledger, row, attempt)
            if proof is not None:
                latest[proof['token']] = attempt
        _need(set(latest) == set(tokens) and all(item['outcome'] == 'retry' for item in latest.values()),
              'UNCERTAIN', 'Every original claim needs measured retry usage before cancellation.')
        return dict(scope='all-outstanding-attempts', run_id=self.data['run_id'],
                    run_location_sha256=self.config['run_location_sha256'], job=job['id'],
                    reservation_id=row['id'], latest_token=tokens[-1],
                    latest_observation_key=latest[tokens[-1]]['idempotency_key'],
                    claim_history_sha256=object_hash(transitions))

    def _apply(self, job, event):
        operation = job['budget_operation']
        payload = event['payload']
        def matching_release():
            snapshot = self.ledger.snapshot()['state']
            if any(item['idempotency_key'] == event['key'] for item in snapshot['events']):
                return None  # The ledger's normal idempotency check owns this key.
            row = snapshot['reservations'].get(operation['reservation_id'])
            if row is None or row['state'] != 'released':
                return None
            expected = dict(reservation_id=operation['reservation_id'], outcome=payload['outcome'],
                            evidence=payload['evidence'])
            matches = [item for item in snapshot['events'] if item['action'] == 'release' and
                       item['payload'] == expected]
            _need(row['operation_id'] == operation['operation_id'] and len(matches) == 1 and
                  matches[0]['receipt'].get('reservation_id') == row['id'] and
                  matches[0]['receipt'].get('state') == 'released' and
                  matches[0]['receipt'].get('spent') == row['spent'], 'BINDING',
                  'A different terminal cancellation cannot acknowledge this native intent.')
            return deepcopy(matches[0]['receipt'])
        options = dict(outcome=payload['outcome'], evidence=event['evidence'],
                       idempotency_key=event['key'], expected_revision=self.ledger.snapshot()['sha256'])
        if 'nonexecution_attestation' in payload:
            def check_legacy(row):
                _need(self._legacy_release_witness(job, row) == payload['nonexecution_attestation'],
                      'BINDING', 'Historical cancellation differs from its retained attestation.')
            receipt = self.ledger._release(operation['reservation_id'], check_legacy=check_legacy, **options)
        elif payload['outcome'] in ('unstarted', 'canceled-known'):
            receipt = matching_release()
            if receipt is None:
                try:
                    receipt = self.ledger.release(operation['reservation_id'], **options)
                except RigError as exc:
                    if exc.report['code'] not in ('DIRECTOR_BUDGET_STALE', 'DIRECTOR_BUDGET_STATE', 'DIRECTOR_BUDGET_UNCERTAIN'):
                        raise
                    receipt = matching_release()
                    if receipt is None:
                        raise
        else:
            receipt = self.ledger.settle(operation['reservation_id'], usage=payload['usage'], **options)
        if event.get('receipt') != receipt:
            event['receipt'] = receipt
            self._save()
        return receipt

    def reconcile(self):
        """Replay durable accounting only. Never infer that a provider did not run."""
        # Validate the complete journal before replaying even an earlier valid
        # operation. An invalid identity cannot trigger incidental accounting.
        for job in self.data['jobs']:
            operation = self._check_claim_bindings(job)
            if operation and any('nonexecution_attestation' in event['payload'] for event in operation['events']):
                self._require_current_journal()
        for job in self.data['jobs']:
            operation = job.get('budget_operation')
            if operation is None:
                continue
            snapshot = self.ledger.snapshot()
            key = operation['reservation_request']['idempotency_key']
            if not any(event['idempotency_key'] == key for event in snapshot['state']['events']):
                _need(not operation.get('reservation_id') and not operation['events'],
                      'BINDING', 'Native accounting refers to an absent reservation.')
                continue  # Admission never committed; only an explicit claim may retry it.
            receipt = self._reserve(operation)
            _need(operation.get('reservation_id', receipt['reservation_id']) == receipt['reservation_id'],
                  'BINDING', 'Native reservation identity changed.')
            if operation.get('reservation_id') != receipt['reservation_id']:
                operation['reservation_id'] = receipt['reservation_id']
                self._save()
            for event in operation['events']:
                self._apply(job, event)
            self.retain_finalization(job)

    @_exact
    def before_claim(self, job, input_hashes):
        """Persist an intent and reserve before Hatch exposes its existing token."""
        _need(not self.recovery, 'STATE', 'Recovery cannot authorize new native work.')
        _need(any(item is job for item in self.data['jobs']), 'BINDING', 'Native admission requires this owner job.')
        _need(isinstance(input_hashes, dict) and input_hashes.get('pet_request.json') == self.config['request_sha256'],
              'BINDING', 'Native admission inputs differ from original enrollment.')
        if (self.owner / 'project.json').is_file():
            import production_workflow as workflow
            state = workflow._state(self.owner)
            require_production_budget(self.owner, state, self.root, self.data)
            workflow._bound_runs(self.owner, state)
        import uuid
        import hatch
        operation = job.get('budget_operation')
        handoff = self.claim_handoff if self.claim_handoff is not None else hatch.claim_handoff_identity(self.root, job)
        _need(handoff == hatch.claim_handoff_identity(self.root, job), 'BINDING',
              'Captured native handoff changed before admission.')
        _need(operation is None or operation['input_hashes'] == input_hashes, 'BINDING',
              'Budgeted inputs changed before reservation admission.')
        if operation is not None:
            intent = _read_native_intent(self.ledger, operation['reservation_request']['evidence'][-1],
                                        operation['operation_id'])
            _need(intent.get('publication_protocol') == 1, 'BINDING',
                  'Historical admission has unknown publication status; reconcile without issuing more work.')
            _need(operation.get('handoff_hashes') == handoff == intent['handoff_hashes'],
                  'BINDING', 'Budgeted handoff settings changed; preserve the original pending intent.')
        self.reconcile()
        if operation is None:
            identifier = _native_operation_id(self.data['run_id'], job['id'], input_hashes,
                                               self.data['fixture_mode'], self.config, handoff)
            request = {key: deepcopy(self.config[key]) for key in
                       ('provider', 'estimate', 'retry_allowance', 'unknown_price', 'unknown_price_cap')}
            request.update(operation_id=identifier, idempotency_key=identifier + ':reserve',
                           evidence=deepcopy(self.config['evidence']))
            operation = dict(operation_id=identifier, input_hashes=deepcopy(input_hashes), handoff_hashes=deepcopy(handoff),
                             reservation_request=request, attempts=[], events=[])
            request['evidence'].append(self._proof({
                'kind': 'native-budget-intent', 'run_id': self.data['run_id'], 'job': job['id'],
                'fixture': self.data['fixture_mode'], 'operation_id': identifier,
                'input_hashes': input_hashes, 'policy': self.config,
                'publication_protocol': 1, 'handoff_hashes': handoff}))
            job['budget_operation'] = operation
            self._save()
        receipt = self._reserve(operation)
        if operation.get('reservation_id') != receipt['reservation_id']:
            _need('reservation_id' not in operation, 'BINDING', 'Native reservation identity changed.')
            operation['reservation_id'] = receipt['reservation_id']
            self._save()
        _need(operation['input_hashes'] == input_hashes, 'BINDING',
              'Budgeted inputs changed; reconcile this operation before preparing new work.')
        row = self.ledger.snapshot()['state']['reservations'][operation['reservation_id']]
        _need(row['state'] == 'reserved', 'UNCERTAIN', 'Only reserved known work may issue a native attempt.')
        _require_generic_reconciliation(self.ledger, row)
        attempts = operation['attempts']
        claimed = {entry.get('token') for entry in job['history'] if entry.get('event') == 'claim'}
        _need(claimed == {entry['token'] for entry in self._published_claims(operation)},
              'BINDING', 'Prior claims lack publication authority; reconcile without issuing more work.')
        pending_token = attempts[-1] if attempts and attempts[-1] not in claimed else None
        if attempts and pending_token is None:
            events = [event for event in operation['events'] if event['payload']['token'] == attempts[-1]]
            _need(events and events[-1]['payload']['outcome'] == 'retry',
                  'STATE', 'The previous native attempt needs an evidenced retry outcome.')
        _need(pending_token is not None or len(attempts) < self.config['retry_allowance'] + 1,
              'EXCEEDED', 'Native retry allowance exhausted.')
        estimate = operation['reservation_request']['estimate']
        amount = self.config['unknown_price_cap'] if estimate['amount'] is None else estimate['amount']
        next_bound = {**estimate, 'amount': amount}
        _need(all(Decimal(row['held'][key]) >= _number(next_bound[key], key) for key in METRICS),
              'EXCEEDED', 'Remaining reservation cannot cover another bounded attempt.')
        state = self.ledger.snapshot()['state']
        for provider in (None, self.config['provider']):
            spent, held = _totals(state, provider)
            limits = state['limits'] if provider is None else state['provider_limits'][provider]
            _need(all(spent[key] + held[key] <= Decimal(limits[key]) for key in METRICS),
                  'EXCEEDED', 'Actual overrun blocks further native admission.')
        if pending_token is not None:
            return pending_token  # No native claim was published; reuse its exact intent.
        token = uuid.uuid4().hex
        attempts.append(token)
        self._save()
        return token

    def record(self, job, token, observation):
        """Retain actual deltas before native finalization; replay never charges twice.

        Evidence must already be retained in this ledger. Known reconciliation
        explicitly names every earlier uncertain observation for the same token.
        """
        operation = self._check_claim_bindings(job)
        _need(operation is not None and isinstance(token, str) and
              token in operation['attempts'] and
              any(entry.get('event') == 'claim' and entry.get('token') == token
                  for entry in job['history']), 'BINDING',
              'Usage must belong to this job and its original published native claim.')
        # Earlier durable usage and the reservation association are authoritative
        # only after every retained claim has been authenticated.
        self.reconcile()
        _need(isinstance(observation, dict) and set(observation) == {
            'id', 'outcome', 'usage', 'evidence', 'measurement_source', 'resolves'},
            'INPUT', 'Native outcomes require one complete actual-usage observation.')
        _id(observation['id'])
        _need(observation['outcome'] in ('complete', 'failed', 'retry', 'uncertain'),
              'INPUT', 'Unsupported native budget outcome.')
        _need(isinstance(observation['measurement_source'], str) and
              0 < len(observation['measurement_source'].strip()) and
              len(observation['measurement_source']) <= 1024,
              'EVIDENCE', 'Describe the actual usage measurement source.')
        _need(isinstance(observation['resolves'], list) and
              all(isinstance(value, str) for value in observation['resolves']) and
              len(set(observation['resolves'])) == len(observation['resolves']),
              'INPUT', 'Uncertain observation identities must be explicit and unique.')
        amounts = _quantities(observation['usage'], unknown=True)
        proof = _evidence(observation['evidence'])
        _need(0 < len(proof) < 128, 'EVIDENCE', 'Actual usage requires one to 127 retained evidence references.')
        _verify_evidence(self.ledger.root, proof)
        _need(observation['outcome'] == 'uncertain' or all(value is not None for value in amounts.values()),
              'USAGE', 'Unknown usage retains the native claim and reservation.')
        payload = dict(token=token, **deepcopy(observation))
        payload['usage'] = amounts
        key = 'native-observation-' + object_hash([operation['operation_id'], observation['id']])
        existing = next((event for event in operation['events'] if event['key'] == key), None)
        if existing is not None:
            _need(existing['payload'] == payload, 'IDEMPOTENCY', 'Native observation identity was reused.')
            self._apply(job, existing)
            return deepcopy(existing['receipt'])
        _need(job['status'] == 'running' and job.get('token') == token, 'STATE',
              'New usage observations require the original active native claim.')
        state = self.ledger.snapshot()['state']
        row = state['reservations'][operation['reservation_id']]
        _need(not _native_finalized(state, row, token), 'STATE',
              'Finalized native accounting permits exact replay, not another observation.')
        _need(row['state'] in ('reserved', 'uncertain'), 'STATE',
              'Final accounting requires exact outcome replay, not another observation.')
        if observation['outcome'] != 'uncertain':
            _require_generic_reconciliation(self.ledger, row)
        pending = set()
        for event in operation['events']:
            item = event['payload']
            if item['token'] == token:
                pending.difference_update(item['resolves'])
                if item['outcome'] == 'uncertain':
                    pending.add(item['id'])
        _need(set(observation['resolves']) <= pending, 'INPUT', 'Unknown uncertainty reconciliation identity.')
        _need(observation['outcome'] == 'uncertain' or set(observation['resolves']) == pending,
              'UNCERTAIN', 'Resolve every uncertain observation before releasing or retrying native work.')
        retained = proof + [self._proof({
            'kind': 'native-budget-usage', 'fixture': self.data['fixture_mode'],
            'operation_id': operation['operation_id'], **payload})]
        event = dict(key=key, payload=payload, evidence=retained)
        operation['events'].append(event)
        self._save()
        self._apply(job, event)
        return deepcopy(event['receipt'])

    def release(self, job, *, idempotency_key, outcome, evidence):
        """Release only an evidenced nonrunning remainder, retaining all spend."""
        _id(idempotency_key)
        _need(outcome in ('unstarted', 'canceled-known'), 'INPUT', 'Explicit safe cancellation outcome required.')
        _need(job['status'] != 'running', 'UNCERTAIN', 'Active native ownership cannot be released.')
        operation = job.get('budget_operation')
        _need(operation is not None, 'MISSING', 'Native operation has no reservation intent.')
        if 'handoff_hashes' not in operation:
            self._require_current_journal()
        self.reconcile()
        claimed = {entry.get('token') for entry in job['history'] if entry.get('event') == 'claim'}
        _need(outcome != 'unstarted' or not claimed.intersection(operation['attempts']),
              'STATE', 'A published native claim cannot be canceled as unstarted.')
        proof = _evidence(evidence)
        _need(0 < len(proof) < 128, 'EVIDENCE', 'Cancellation requires retained nonexecution evidence.')
        _verify_evidence(self.ledger.root, proof)
        payload = dict(token=None, id=idempotency_key, outcome=outcome, evidence=proof)
        key = 'native-release-' + object_hash([operation['operation_id'], idempotency_key])
        existing = next((event for event in operation['events'] if event['key'] == key), None)
        if existing is None:
            # Do not retain a deterministically invalid cancellation intent:
            # replaying it would prevent later explicit measured recovery.
            def check_legacy(row):
                payload['nonexecution_attestation'] = self._legacy_release_witness(job, row)
            with job_lock(self.ledger.root):
                self.ledger._release_row(self.ledger.snapshot()['state'], operation['reservation_id'],
                                         outcome, check_legacy=check_legacy)
            retained = proof + [self._proof({
                'kind': 'native-budget-release', 'fixture': self.data['fixture_mode'],
                'operation_id': operation['operation_id'], **payload})]
            existing = dict(key=key, payload=payload, evidence=retained)
            operation['events'].append(existing)
            self._save()
        elif 'nonexecution_attestation' in existing['payload']:
            payload['nonexecution_attestation'] = existing['payload']['nonexecution_attestation']
        _need(existing['payload'] == payload, 'IDEMPOTENCY', 'Cancellation identity was reused.')
        return self._apply(job, existing)

    def authorize_finalization(self, job, token, observation):
        """An old accounting receipt never authorizes a newer native transition."""
        self.reconcile()
        _need(job['status'] == 'running' and job.get('token') == token,
              'STATE', 'Finalization requires the original active native token.')
        operation = job['budget_operation']
        events = [event for event in operation['events'] if event['payload']['token'] == token]
        _need(bool(events), 'STATE', 'Native finalization requires current accounting evidence.')
        latest = events[-1]
        _need(latest['payload']['id'] == observation['id'] and
              latest['payload']['outcome'] == observation['outcome'] and
              observation['outcome'] in ('retry', 'complete', 'failed'),
              'STATE', 'This accounting receipt is not the current native outcome.')
        pending = set()
        for event in events:
            payload = event['payload']
            pending.difference_update(payload['resolves'])
            if payload['outcome'] == 'uncertain':
                pending.add(payload['id'])
        _need(not pending, 'UNCERTAIN',
              'Resolve every native observation before finalizing its owner.')
        # This is a read-only authorization. If saving the owner's finalized
        # state fails, its still-running token may report a later observation.
        row = self.ledger.snapshot()['state']['reservations'][operation['reservation_id']]
        expected = 'reserved' if observation['outcome'] == 'retry' else 'settled'
        _need(row['state'] == expected and row['attempts'] and
              row['attempts'][-1]['idempotency_key'] == latest['key'],
              'UNCERTAIN', 'Current uncertainty or later ledger usage prevents native finalization.')

    def status(self):
        """Report estimates separately from measured deltas and unresolved usage."""
        snapshot = self.ledger.snapshot()
        return {'ledger_revision': snapshot['sha256'], 'provider': self.config['provider'],
                'limits': deepcopy(snapshot['state']['limits']),
                'reservations': deepcopy(snapshot['state']['reservations']),
                'measurement_note': 'spent sums known deltas only; inspect attempts for unavailable measurements.'}
