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
        _need(event['action'] in ('reserve','settle','release') and isinstance(event['payload'],dict) and isinstance(event['receipt'],dict), 'CORRUPT', 'Invalid event action.')
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
            for key in METRICS:
                if amounts[key] is not None:
                    spent = Decimal(row['spent'][key]) + Decimal(amounts[key])
                    row['spent'][key] = str(spent)
                    row['held'][key] = str(max(Decimal(row['reserved'][key]) - spent, Decimal(0)))
            row['attempts'].append(dict(usage=amounts,outcome=outcome,evidence=proof,idempotency_key=idempotency_key))
            if outcome in ('complete','failed'):
                row['state']='settled';row['held']={key:'0' for key in METRICS}
            else:row['state']='uncertain' if outcome=='uncertain' else 'reserved'
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
            _need(outcome!='unstarted' or not row['attempts'], 'STATE', 'Started work cannot be released as unstarted.')
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


def _native_owner_binding(owner, ledger_root, genesis, *, create=False):
    """One immutable ledger association; creation requires Hatch owner ownership."""
    import hatch
    expected = _native_owner_identity(owner, ledger_root, genesis)
    path = owner / NATIVE_OWNER_FILE
    _need(not path.is_symlink(), 'PATH', 'Native owner association cannot be a link.')
    if path.exists():
        _need(decode_json(_read_source_bytes(path, MAX_BYTES)) == expected,
              'BINDING', 'Native owner already belongs to a different ledger or location.')
    else:
        _need(create, 'BINDING', 'Native owner ledger association is missing.')
        hatch.atomic(path, expected)
    return expected


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
                      request_sha256=hatch.digest(root / 'pet_request.json'),
                      run_location_sha256=object_hash(str(root)))
        existing = data.get('director_budget')
        if existing is not None:
            _need(existing == config, 'BINDING', 'Native accounting policy cannot be replaced.')
        else:
            _need(not any(entry.get('event') == 'claim' for job in data['jobs'] for entry in job['history']),
                  'BINDING', 'Opt in before the first native claim; existing expenditure cannot be invented.')
        association = _native_owner_identity(owner, ledger_root, config['genesis'])
        project_state = None
        if (owner / 'project.json').is_file():
            import production_workflow as workflow
            project_state = workflow._state(owner)
            _require_unrestored_budget(project_state)
            _need(project_state.get('director_budget', association) == association, 'BINDING',
                  'Production project already belongs to a different ledger.')
            for binding in project_state['generation_bindings'].values():
                bound = workflow.safe_path(owner, binding['run_path'])
                if bound == root:
                    continue
                bound_data = hatch.manifest(bound)
                _need('director_budget' in bound_data, 'BINDING',
                      'Opt existing unclaimed project acquisitions into the same ledger first.')
                # Existing associations are checked before any configuration is published.
                previous = NativeBudget(bound, bound_data)
                _need(previous.owner == owner and previous.ledger.root == ledger_root,
                      'BINDING', 'Existing project acquisition uses a different ledger.')
        _native_owner_binding(owner, ledger_root, config['genesis'], create=True)
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
    def for_run(cls, root, data):
        return cls(root, data) if 'director_budget' in data else None

    def __init__(self, root, data):
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
        _need(config['run_location_sha256'] == object_hash(str(self.root)), 'BINDING',
              'Relocated opted-in runs cannot authorize work; preserve the original owner location.')
        _need(config['run_id'] == data['run_id'] and config['fixture'] is data['fixture_mode'] and
              config['request_sha256'] == hatch.digest(self.root / 'pet_request.json'),
              'BINDING', 'Native accounting identity or original request changed.')
        depth = config['owner_depth']
        _need(config['schema_version'] == 1 and type(depth) is int and 0 <= depth < len(self.root.parents),
              'PATH', 'Invalid native accounting owner.')
        owner = self.root if depth == 0 else self.root.parents[depth - 1]
        self.owner = owner
        project_state = owner / 'production-workflow.json'
        if project_state.is_file():
            _require_unrestored_budget(hatch.read(project_state))
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
        _native_owner_binding(owner, ledger_root, config['genesis'])
        for job in data['jobs']:
            operation = job.get('budget_operation')
            if operation is None:
                continue
            expected = 'native-' + object_hash({
                'run_id': data['run_id'], 'job': job['id'], 'inputs': operation['input_hashes'],
                'fixture': data['fixture_mode'], 'policy': config})
            request = operation['reservation_request']
            _need(operation['operation_id'] == expected and request['operation_id'] == expected and
                  request['idempotency_key'] == expected + ':reserve' and
                  all(request[key] == config[key] for key in
                      ('provider', 'estimate', 'retry_allowance', 'unknown_price', 'unknown_price_cap')) and
                  request['evidence'][:-1] == config['evidence'] and
                  len(request['evidence']) == len(config['evidence']) + 1,
                  'BINDING', 'Native operation or accounting policy was rebound.')

    def _save(self):
        import hatch
        hatch.save(self.root, self.data)

    def _proof(self, value):
        with tempfile.TemporaryDirectory(prefix='.native-budget-', dir=self.ledger.root.parent) as temporary:
            path = Path(temporary) / 'intent.json'
            path.write_text(json.dumps(value, sort_keys=True, allow_nan=False), encoding='utf-8')
            return self.ledger.retain_evidence(path)

    def _reserve(self, operation):
        options = deepcopy(operation['reservation_request'])
        return self.ledger.reserve(**options, expected_revision=self.ledger.snapshot()['sha256'])

    def _apply(self, operation, event):
        payload = event['payload']
        options = dict(outcome=payload['outcome'], evidence=event['evidence'],
                       idempotency_key=event['key'], expected_revision=self.ledger.snapshot()['sha256'])
        if payload['outcome'] in ('unstarted', 'canceled-known'):
            receipt = self.ledger.release(operation['reservation_id'], **options)
        else:
            receipt = self.ledger.settle(operation['reservation_id'], usage=payload['usage'], **options)
        if event.get('receipt') != receipt:
            event['receipt'] = receipt
            self._save()
        return receipt

    def reconcile(self):
        """Replay durable accounting only. Never infer that a provider did not run."""
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
                self._apply(operation, event)

    @_exact
    def before_claim(self, job, input_hashes):
        """Persist an intent and reserve before Hatch exposes its existing token."""
        import uuid
        self.reconcile()
        operation = job.get('budget_operation')
        if operation is None:
            identifier = 'native-' + object_hash({
                'run_id': self.data['run_id'], 'job': job['id'], 'inputs': input_hashes,
                'fixture': self.data['fixture_mode'], 'policy': self.config})
            request = {key: deepcopy(self.config[key]) for key in
                       ('provider', 'estimate', 'retry_allowance', 'unknown_price', 'unknown_price_cap')}
            request.update(operation_id=identifier, idempotency_key=identifier + ':reserve',
                           evidence=deepcopy(self.config['evidence']))
            operation = dict(operation_id=identifier, input_hashes=deepcopy(input_hashes),
                             reservation_request=request, attempts=[], events=[])
            request['evidence'].append(self._proof({
                'kind': 'native-budget-intent', 'run_id': self.data['run_id'], 'job': job['id'],
                'fixture': self.data['fixture_mode'], 'operation_id': identifier,
                'input_hashes': input_hashes, 'policy': self.config}))
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
        attempts = operation['attempts']
        claimed = {entry.get('token') for entry in job['history'] if entry.get('event') == 'claim'}
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
        # Earlier durable usage and the reservation association are authoritative
        # even when their native acknowledgement was interrupted.
        self.reconcile()
        _need(isinstance(observation, dict) and set(observation) == {
            'id', 'outcome', 'usage', 'evidence', 'measurement_source', 'resolves'},
            'INPUT', 'Native outcomes require one complete actual-usage observation.')
        _id(observation['id'])
        _need(observation['outcome'] in ('complete', 'failed', 'retry', 'uncertain'),
              'INPUT', 'Unsupported native budget outcome.')
        _need(isinstance(observation['measurement_source'], str) and
              0 < len(observation['measurement_source'].strip()) <= 1024,
              'EVIDENCE', 'Describe the actual usage measurement source.')
        _need(isinstance(observation['resolves'], list) and
              all(isinstance(value, str) for value in observation['resolves']) and
              len(set(observation['resolves'])) == len(observation['resolves']),
              'INPUT', 'Uncertain observation identities must be explicit and unique.')
        operation = job.get('budget_operation')
        _need(operation is not None and token in operation['attempts'], 'BINDING',
              'Usage must belong to this job and its original native token.')
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
            self._apply(operation, existing)
            return deepcopy(existing['receipt'])
        _need(job['status'] == 'running' and job.get('token') == token, 'STATE',
              'New usage observations require the original active native claim.')
        row = self.ledger.snapshot()['state']['reservations'][operation['reservation_id']]
        _need(row['state'] in ('reserved', 'uncertain'), 'STATE',
              'Final accounting requires exact outcome replay, not another observation.')
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
        self._apply(operation, event)
        return deepcopy(event['receipt'])

    def release(self, job, *, idempotency_key, outcome, evidence):
        """Release only an evidenced nonrunning remainder, retaining all spend."""
        _id(idempotency_key)
        _need(outcome in ('unstarted', 'canceled-known'), 'INPUT', 'Explicit safe cancellation outcome required.')
        _need(job['status'] != 'running', 'UNCERTAIN', 'Active native ownership cannot be released.')
        operation = job.get('budget_operation')
        _need(operation is not None, 'MISSING', 'Native operation has no reservation intent.')
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
            row = self.ledger.snapshot()['state']['reservations'][operation['reservation_id']]
            _need(row['state'] == 'reserved', 'UNCERTAIN', 'Only a known reserved remainder can be released.')
            retained = proof + [self._proof({
                'kind': 'native-budget-release', 'fixture': self.data['fixture_mode'],
                'operation_id': operation['operation_id'], **payload})]
            existing = dict(key=key, payload=payload, evidence=retained)
            operation['events'].append(existing)
            self._save()
        _need(existing['payload'] == payload, 'IDEMPOTENCY', 'Cancellation identity was reused.')
        return self._apply(operation, existing)

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
        row = self.ledger.snapshot()['state']['reservations'][operation['reservation_id']]
        expected = 'reserved' if observation['outcome'] == 'retry' else 'settled'
        _need(not pending and row['state'] == expected and row['attempts'] and
              row['attempts'][-1]['idempotency_key'] == latest['key'],
              'UNCERTAIN', 'Current uncertainty or later ledger usage prevents native finalization.')

    def status(self):
        """Report estimates separately from measured deltas and unresolved usage."""
        snapshot = self.ledger.snapshot()
        return {'ledger_revision': snapshot['sha256'], 'provider': self.config['provider'],
                'limits': deepcopy(snapshot['state']['limits']),
                'reservations': deepcopy(snapshot['state']['reservations']),
                'measurement_note': 'spent sums known deltas only; inspect attempts for unavailable measurements.'}
