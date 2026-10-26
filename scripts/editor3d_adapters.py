"""Small executable editor admission adapters; no server or scene controller.

Native owners keep payload meaning. Mathematical adapters lazily reuse rig math.
"""
from __future__ import annotations
from fractions import Fraction
import hashlib
import hmac
import math
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

from editor3d_contracts import (MAX_DOCUMENT_BYTES, WRITE_OPERATIONS, _definitions,
                                validate_contract, _error)
from rig_errors import decode_json, require, safe_path


def frame_time(frame, fps):
    """Exact seconds from a nonnegative frame boundary and reduced frame rate."""
    if (isinstance(frame, bool) or not isinstance(frame, (int, float))
            or isinstance(frame, float) and (not math.isfinite(frame) or not frame.is_integer())
            or not 0 <= frame <= 2_000_000_000):
        _error('FRAME', 'Frame boundary must be an integer in the supported range.')
    frame = int(frame)
    from jsonschema import Draft202012Validator
    if not Draft202012Validator(_definitions()['FrameRate']).is_valid(fps):
        _error('RATE', 'Use a bounded positive rational frame rate.')
    numerator, denominator = int(fps['numerator']), int(fps['denominator'])
    if math.gcd(numerator, denominator) != 1:
        _error('RATE', 'Frame rate must be reduced.')
    return Fraction(frame * denominator, numerator)


def transform_to_blender(transform):
    """Object transform basis conjugation; camera-local conventions are separate."""
    from jsonschema import Draft202012Validator
    if not Draft202012Validator(_definitions()['Transform']).is_valid(transform):
        _error('TRANSFORM', 'Use the native rig transform contract.')
    if abs(math.hypot(*transform['rotation']) - 1) > 1e-6:
        _error('QUATERNION', 'Rotation must be a unit XYZW quaternion.')
    from rig_math import BLENDER_FROM_CANONICAL as basis, transform_matrix
    matrix = transform_matrix(transform)
    return (basis @ matrix @ basis.T).tolist()


def euler_to_quaternion(euler):
    """Fixed-axis extrinsic XYZ radians: Rz @ Ry @ Rx, normalized XYZW."""
    from rig_math import axis_angle, qmul, quaternion, vector
    x, y, z = vector(euler)
    return quaternion(qmul(axis_angle([0, 0, 1], float(z)),
        qmul(axis_angle([0, 1, 0], float(y)), axis_angle([1, 0, 0], float(x))))).tolist()


def quaternion_to_euler(value):
    """Return extrinsic XYZ plus gimbal warning; quaternion stays authoritative."""
    from rig_math import qmatrix, quaternion
    matrix = qmatrix(quaternion(value, strict=True))
    pitch = math.asin(max(-1., min(1., -float(matrix[2, 0]))))
    locked = abs(math.cos(pitch)) < 1e-7
    if locked:
        roll = math.atan2(-float(matrix[1, 2]), float(matrix[1, 1]))
        yaw = 0.
    else:
        roll = math.atan2(float(matrix[2, 1]), float(matrix[2, 2]))
        yaw = math.atan2(float(matrix[1, 0]), float(matrix[0, 0]))
    return {'euler': [roll, pitch, yaw], 'gimbal_lock': locked,
            'convention': 'extrinsic-XYZ-radians'}


def verify_native_reference(reference, root):
    """Read a bounded regular JSON manifest and invoke its current native owner.

This verifies a manifest contract, not its transitive resources or a whole scene.
An unmerged owner is unavailable, never silently parsed as a different format.
"""
    from jsonschema import Draft202012Validator
    if not Draft202012Validator({'$defs': _definitions(), '$ref': '#/$defs/NativeReference'}).is_valid(reference):
        _error('REFERENCE', 'Invalid native resource reference.')
    owner = reference['owner']
    if owner not in ('rig', 'model3d', 'production2d'):
        _error('UNAVAILABLE', 'Native owner is not integrated in this checkout: ' + owner)
    declared = reference['file']
    if declared['media_type'] != 'application/json' or declared['bytes'] > MAX_DOCUMENT_BYTES:
        _error('REFERENCE', 'Native manifests must be bounded JSON documents.')
    path = safe_path(root, declared['path'], exists=True)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        observed = os.fstat(stream.fileno())
        if not stat.S_ISREG(observed.st_mode) or observed.st_size != declared['bytes']:
            _error('BYTES', 'Native manifest byte count differs.')
        payload = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(payload) != declared['bytes'] or hashlib.sha256(payload).hexdigest() != declared['sha256']:
        _error('HASH', 'Native manifest bytes changed.')
    value = decode_json(payload)
    if not isinstance(value, dict):
        _error('KIND', 'Native manifest must be a JSON object.')
    kind = reference['native_kind']
    if owner == 'rig':
        from rig_contracts import KINDS, validate_contract as native_validate
        if kind not in KINDS.values():
            _error('KIND', 'Unknown rig native kind.')
        native_validate(value, kind)
    elif owner == 'model3d':
        from model3d_contracts import validate_contract as native_validate
        if value.get('kind') != kind:
            _error('KIND', 'Native Model3D kind differs.')
        native_validate(value)
    else:
        from production_contracts import validate_structure
        if kind not in ('project', 'scene', 'shot', 'cast', 'clip'):
            _error('KIND', 'Unknown production2d native kind.')
        validate_structure(value, kind)
    native_id = value.get('id', value.get('project_id'))
    if native_id != reference['id']:
        _error('IDENTITY', 'Native ID differs from the bound identity.')
    native_version = value.get('version', str(value.get('schema_version')) + '.0.0')
    if native_version != reference['version']:
        _error('VERSION', 'Native version differs from the exact reference.')
    return {'status': 'valid-native-contract', 'owner': owner, 'id': native_id,
            'sha256': declared['sha256'], 'transitive_resources_verified': False,
            'review_status': 'unreviewed'}


def validate_api_request(request, *, authorization, token, origin, server_origin,
                         content_type='application/json', content_length=None,
                         current_revision=None, current_project_sha256=None):
    """Authenticate and admit raw JSON request bytes against a saved snapshot.

The actual service must read no more than Content-Length, reject duplicate HTTP
headers, serialize writes with the native lock, then recheck this snapshot under
that lock. This preflight performs no operation and returns no token or secrets.
"""
    if not isinstance(token, str) or len(token) < 24:
        _error('TOKEN', 'Server token must contain at least 24 characters.')
    try:
        authorized = isinstance(authorization, str) and hmac.compare_digest(
            authorization.encode('utf-8'), ('Bearer ' + token).encode('utf-8'))
    except UnicodeError:
        authorized = False
    if not authorized:
        _error('AUTH', 'A valid local bearer token is required.')
    try:
        server = urlsplit(server_origin)
    except (ValueError, TypeError):
        _error('ORIGIN', 'Invalid loopback origin.')
    if (server.scheme != 'http' or server.hostname not in ('127.0.0.1', 'localhost')
            or server.username or server.password or server.path or server.query or server.fragment):
        _error('ORIGIN', 'Configure an exact HTTP loopback origin.')
    try:
        if server.port is None or not 1 <= server.port <= 65535:
            _error('ORIGIN', 'The loopback origin requires an explicit port.')
    except ValueError:
        _error('ORIGIN', 'Invalid loopback port.')
    if origin is not None and origin != server_origin:
        _error('ORIGIN', 'Cross-origin requests are refused.')
    if not isinstance(content_type, str) or content_type.lower().split(';', 1)[0].strip() != 'application/json':
        _error('CONTENT_TYPE', 'Use application/json.')
    if isinstance(request, str):
        request = request.encode('utf-8')
    if not isinstance(request, bytes):
        _error('BODY', 'Admission requires the original UTF-8 JSON bytes.')
    if (isinstance(content_length, bool) or not isinstance(content_length, int)
            or not 0 < content_length <= MAX_DOCUMENT_BYTES or len(request) != content_length):
        _error('BODY', 'Require an exact Content-Length of at most 4 MiB.')
    value = decode_json(request)
    validate_contract(value)
    if value['kind'] != 'editor3d-request':
        _error('KIND', 'Expected an editor request.')
    if value['operation'] in WRITE_OPERATIONS and value['operation'] != 'projects.create':
        if current_revision is None or current_project_sha256 is None:
            _error('SNAPSHOT', 'A current saved snapshot is required for a mutation.')
        if value['expected_revision'] != current_revision or value['expected_project_sha256'] != current_project_sha256:
            _error('STALE', 'Reload the saved revision and reapply the command.')
    return {'status': 'admitted', 'request': value, 'executed': False}
