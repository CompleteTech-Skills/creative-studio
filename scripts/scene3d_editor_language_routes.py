"""Fixed authenticated EditorServer routes over the canonical language adapter.

Pass ``language_routes()`` to EditorServer's trusted ``routes`` argument. The
server applies its Host, Origin, bearer-token and bounded-body checks first.
The language owner modules remain ordinary dependencies, loaded on demand.
"""
from rig_errors import require


def _request(body, keys):
    require(isinstance(body, dict) and set(body) == keys,
            'SCENE3D_LANGUAGE_REQUEST', 'Unexpected or missing language request fields.')


def _plan(server, body):
    from scene3d_editor_language import plan_repository_description
    _request(body, {'text', 'environment', 'expected_revision', 'expected_sha256'})
    require(type(body['expected_revision']) is int and body['expected_revision'] >= 0
            and isinstance(body['expected_sha256'], str) and len(body['expected_sha256']) == 64,
            'SCENE3D_LANGUAGE_REQUEST', 'Use the exact saved editor revision and hash.')
    current = server.repository.snapshot()
    require((current['revision'], current['sha256']) ==
            (body['expected_revision'], body['expected_sha256']),
            'SCENE3D_LANGUAGE_STALE', 'The saved scene changed before language planning.')
    result = plan_repository_description(server.repository, body['text'], environment=body['environment'])
    require((result['expected_revision'], result['expected_sha256']) ==
            (body['expected_revision'], body['expected_sha256']),
            'SCENE3D_LANGUAGE_STALE', 'The saved scene changed during language planning.')
    return result


def _apply(server, body):
    from scene3d_editor_language import apply_repository_plan
    _request(body, {'plan', 'transaction_id'})
    # The repository validates transaction identity and performs the atomic
    # expected-revision/hash check under its canonical lock.
    return apply_repository_plan(server.repository, body['plan'], body['transaction_id'])


def language_routes():
    """Return a new fixed allowlist; untrusted requests never select callables."""
    return {'language/plan': _plan, 'language/apply': _apply}
