from pathlib import Path
from unittest.mock import patch
import copy,hashlib,json,sys,tempfile
sys.path.insert(0,'scripts')
import physics_contracts as physics
from rig_errors import RigError
records=[];initial_limit=sys.get_int_max_str_digits()
with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'world.json'
    for negative in (False,True):
        digits=('-' if negative else '')+'9'*5000
        for name,text in [('root',digits),('array','['+digits+']'),('nested','{"value":'+digits+'}')]:
            payload=text.encode();path.write_bytes(payload);before=hashlib.sha256(payload).hexdigest()
            try:physics.load_contract(path)
            except physics.PhysicsError as exc:
                assert exc.report['code']=='PHYSICS_JSON';physics.validate_contract(exc.report,'PhysicsDiagnostic')
                records.append(dict(case=('negative-' if negative else 'positive-')+name,code=exc.report['code'],input_unchanged=hashlib.sha256(path.read_bytes()).hexdigest()==before))
            else:raise AssertionError('Oversized integer accepted')
    world=physics.load_contract('examples/physics-3d/contracts-world.json')
    world['scene']['path']='assets/雪é🐣.json'
    text=json.dumps(world,ensure_ascii=False);payload=text.encode('utf-8');path.write_bytes(payload)
    with patch('rig_errors.MAX_JSON_BYTES',len(payload)):
        assert physics.load_contract(path)==world
        assert physics.validate_contract(physics.decode_json(text))==world
        assert physics.validate_contract(physics.decode_json(payload))==world
    records.append(dict(case='unicode-exact-byte-limit',bytes=len(payload),characters=len(text),accepted=True,input_unchanged=path.read_bytes()==payload))
    with patch('rig_errors.MAX_JSON_BYTES',len(payload)-1):
        for kind,data in [('text',text),('bytes',payload)]:
            try:physics.decode_json(data)
            except RigError as exc:
                assert exc.report['code']=='RIG_LIMIT_JSON';records.append(dict(case='unicode-over-limit-'+kind,code=exc.report['code']))
            else:raise AssertionError('Over-byte-limit document accepted')
        try:physics.load_contract(path)
        except physics.PhysicsError as exc:
            assert exc.report['code']=='PHYSICS_JSON';physics.validate_contract(exc.report,'PhysicsDiagnostic')
            records.append(dict(case='unicode-over-limit-domain-file',code=exc.report['code'],input_unchanged=path.read_bytes()==payload))
        else:raise AssertionError('Over-byte-limit world accepted')
    for malformed in [b'"\xff"',b'{"x":1,"x":2}',b'{"x":NaN}']:
        path.write_bytes(malformed)
        try:physics.load_contract(path)
        except physics.PhysicsError as exc:
            physics.validate_contract(exc.report,'PhysicsDiagnostic');records.append(dict(case='malformed-file',payload_hex=malformed.hex(),code=exc.report['code'],input_unchanged=path.read_bytes()==malformed))
        else:raise AssertionError('Malformed file accepted')
    path.write_bytes(payload)
    with patch('physics_contracts.decode_json',side_effect=ValueError('programming sentinel')):
        try:physics.load_contract(path)
        except ValueError as exc:assert type(exc) is ValueError;records.append(dict(case='programming-error-not-concealed',type=type(exc).__name__))
        else:raise AssertionError('Programming error swallowed')
assert initial_limit==sys.get_int_max_str_digits()
assert all(record.get('input_unchanged',True) for record in records)
print(json.dumps(dict(probes=records,interpreter_digit_limit_before=initial_limit,interpreter_digit_limit_after=sys.get_int_max_str_digits(),actual_solver_execution=None),indent=2))
