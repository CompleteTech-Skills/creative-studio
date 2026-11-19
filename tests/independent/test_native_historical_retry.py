"""Confirm that the current Hatch CLI can retry an actual migrated v2 fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

OLD = '3d226b124720b1ec2f7a2e5a5c187de0171ba1e8'
PREVIOUS = 'e7bf696bf4ec030363e2c9aecc269837fdf6f3bd'
current = Path.cwd()


def export(commit, target):
    target.mkdir()
    with tempfile.TemporaryFile() as stream:
        subprocess.run(['git', 'archive', commit], stdout=stream, check=True)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive:
            archive.extractall(target, filter='data')


def cli(repository, run, command, *args):
    argv = [sys.executable, str(repository / 'scripts/hatch.py'), command, '--run-dir', str(run), *args]
    env = dict(os.environ, PYTHONPATH=str(repository / 'scripts'))
    result = subprocess.run(argv, env=env, capture_output=True, text=True)
    print(repository.name, command, result.returncode, flush=True)
    if result.returncode:
        print(result.stderr.strip().splitlines()[-1], flush=True)
        return result.returncode, {}
    return result.returncode, json.loads(result.stdout)


with tempfile.TemporaryDirectory(prefix='hatch-legacy-review-') as temporary:
    home = Path(temporary)
    old, previous = home / 'schema2', home / 'e7bf696b'
    export(OLD, old)
    export(PREVIOUS, previous)
    outcomes = []
    for name, repo in [('before', previous), ('after', current)]:
        run = home / name
        assert cli(old, run, 'prepare', '--fixture-mode', '--pet-name', 'Legacy fixture',
                   '--pet-notes', 'Historical local regression, no provider')[0] == 0
        code, claim = cli(old, run, 'claim', '--job', 'base')
        assert code == 0
        assert cli(old, run, 'fail', '--job', 'base', '--token', claim['token'], '--note', 'Offline failure')[0] == 0
        history = json.loads((run / 'imagegen-jobs.json').read_bytes())['jobs'][0]['history']
        assert any(e['event'] == 'claim' and 'profile.json' in e['input_hashes'] and
                   '@generation-profile' not in e['input_hashes'] for e in history)
        assert cli(repo, run, 'migrate')[0] == 0
        code, next_claim = cli(repo, run, 'claim', '--job', 'base')
        data = json.loads((run / 'imagegen-jobs.json').read_bytes())['jobs'][0]
        print(name, 'durable job status', data['status'], 'attempts', data['attempts'],
              'received new token', bool(next_claim.get('token')), flush=True)
        outcomes.append(code)
    assert outcomes == [0, 0], ('Supported migrated retry failed', outcomes)
