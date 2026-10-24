#!/usr/bin/env python3
"""Resumable local workflow. Production generation is delegated to installed imagegen."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

from PIL import Image
from profile_spec import load_profile
from asset_contracts import versions, resolve_ref, check_image, hash_object, REVIEW_DIMENSIONS, generation_projection, extraction_projection

SCRIPTS = Path(__file__).resolve().parent
SCHEMA = 3
RETRIES = 3


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def ownership(root, *, resolve_leaf=True):
    """A kernel lock, released after a crash; an old lock file is harmless."""
    root = Path(root)
    # New destinations must not redirect their lock through a racing leaf link.
    root = root.resolve() if resolve_leaf else root.parent.resolve() / root.name
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.parent / ('.' + root.name + '.hatch.lock')
    with lock.open('a+b') as handle:
        # Never write an already locked byte on Windows.
        if lock.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('run is owned by another controller') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / str(relative).replace('\\', '/')).resolve()
    if not path.is_relative_to(root):
        raise ValueError('path escapes run: ' + str(relative))
    return path


def execute(name, *args):
    result = subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)],
                            capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise ValueError(name + ': ' + result.stdout[-4000:] + result.stderr[-4000:])
    return result.stdout


def manifest(root):
    result = read(root / 'imagegen-jobs.json')
    if result.get('schema_version') != SCHEMA or not isinstance(result.get('jobs'), list):
        raise ValueError('unsupported run schema; use hatch.py migrate --run-dir PATH for schema 2, or recover into a separate run')
    profile = load_profile(root / 'profile.json')
    request = read(root / 'pet_request.json')
    from state_selection import prepared_selection
    prepared_selection(root)
    if 'style_sha256' in result and request.get('resolved_style') is None:
        raise ValueError('manifest style hash requires the saved resolved style snapshot; restore the intact pet_request.json')
    if request.get('resolved_style') is not None:
        from style_registry import validate_snapshot
        snapshot = validate_snapshot(request['resolved_style'])
        if request.get('style_contract') != snapshot['contract']:
            raise ValueError('saved style contract differs from resolved style snapshot')
        if result.get('style_sha256') != snapshot['sha256']:
            raise ValueError('manifest style hash differs from resolved style snapshot')
    original_references = {str(resolve_ref(root, ref['copied_path'], result.get('original_root'))) for ref in request.get('references', [])}
    specs = {s['id']: s for s in profile['states']}
    jobs = result['jobs']
    if type(result.get('fixture_mode')) is not bool or not isinstance(result.get('run_id'), str):
        raise ValueError('malformed run identity or fixture label')
    if any(not isinstance(j, dict) for j in jobs) or len(jobs) != len(specs) + 1 or {j.get('id') for j in jobs} != {'base', *specs}:
        raise ValueError('generation manifest must contain exactly one job for base and every profile state')
    for job in jobs:
        name = job['id']
        if job.get('status') not in ('pending', 'running', 'failed', 'generated'):
            raise ValueError('unsupported generation status for ' + name)
        prompt = 'prompts/base-pet.md' if name == 'base' else f'prompts/rows/{name}.md'
        if job.get('output_path') != f'decoded/{name}.png' or job.get('prompt_file') != prompt:
            raise ValueError('malformed generation paths for ' + name)
        images = job.get('input_images')
        if not isinstance(images, list) or any(not isinstance(image, dict) or not isinstance(image.get('path'), str) for image in images):
            raise ValueError('malformed identity references for ' + name)
        attached = {str(inside(root, image['path'])) for image in images}
        if not original_references <= attached:
            raise ValueError('missing original identity reference for ' + name)
        for ref in request.get('references', []):
            target = str(resolve_ref(root, ref['copied_path'], result.get('original_root')))
            recorded = next(image for image in images if str(inside(root, image['path'])) == target)
            if ref.get('role') and recorded.get('role') != ref['role']:
                raise ValueError('changed required reference role for ' + name)
        expected_dependencies = [] if name == 'base' else ['base']
        if job.get('depends_on') != expected_dependencies:
            raise ValueError('generation dependencies differ from profile for ' + name)
        if name != 'base':
            required = {'references/canonical-base.png', f'references/layout-guides/{name}.png'}
            if not required <= {image['path'] for image in images}:
                raise ValueError('missing identity or layout references for ' + name)
        if job['status'] == 'running' and not job.get('token'):
            raise ValueError('running generation has no active token: ' + name)
        if job['status'] == 'generated':
            origins = [key for key in ('generation_receipt', 'derivation_receipt', 'import_receipt') if key in job]
            if len(origins) != 1 or not isinstance(job.get('input_hashes'), dict) or not job.get('output_hash'):
                raise ValueError('generated job needs exactly one origin receipt and input/output hashes: ' + name)
            origin = source_origin(job)
            if 'source_origin' in job and job['source_origin'] != origin:
                raise ValueError('source origin label differs from its receipt: ' + name)
            receipt_fixture = (origin == 'fixture') if origin in ('fixture', 'imagegen') else job.get('import_receipt', {}).get('fixture')
            if origin != 'derived' and (type(receipt_fixture) is not bool or receipt_fixture != result['fixture_mode']):
                raise ValueError('source receipt fixture label differs from the run: ' + name)
            if job.get('derivation_receipt'):
                source = job['derivation_receipt'].get('source')
                if not any(d['source'] == source for d in specs[name]['allowed_derivations']):
                    raise ValueError('active derivation is no longer permitted by the profile: ' + name)
    return result


def source_origin(job):
    """Describe the origin receipt; an editable display label is not evidence."""
    origins = [key for key in ('generation_receipt', 'derivation_receipt', 'import_receipt') if key in job]
    if len(origins) > 1:
        raise ValueError('source has conflicting origin receipts: ' + job['id'])
    if not origins:
        return 'unrecorded'
    origin = origins[0]
    if not isinstance(job[origin], dict):
        raise ValueError('source origin receipt must be an object: ' + job['id'])
    if origin == 'generation_receipt':
        skill = job[origin].get('generation_skill')
        if skill not in ('$fixture', '$imagegen'):
            raise ValueError('unsupported generation origin receipt: ' + job['id'])
        return 'fixture' if skill == '$fixture' else 'imagegen'
    return 'import' if origin == 'import_receipt' else 'derived'


def save(root, data):
    atomic(root / 'imagegen-jobs.json', data)


def event(data, name, **details):
    data.setdefault('history', []).append({'time': time.time(), 'event': name, **details})


def job_for(data, name):
    for job in data['jobs']:
        if job['id'] == name:
            return job
    raise ValueError('unknown job ' + str(name))


def inputs(root, job):
    paths = ['pet_request.json']
    if not job.get('import_receipt'):
        paths.append(job['prompt_file'])
    discovery = read(root / 'pet_request.json').get('brand_discovery_path')
    if discovery:
        paths.append(discovery)
    if job.get('retry_prompt_file') and not job.get('import_receipt'):
        paths.append(job['retry_prompt_file'])
    paths += [x['path'] for x in job['input_images']]
    if job.get('derived_from'):
        paths.append(f"decoded/{job['derived_from']}.png")
    hashes = {p: digest(inside(root, p)) for p in paths}
    hashes['@generation-profile'] = object_hash(generation_projection(load_profile(root / 'profile.json'), job['id']))
    return hashes


def revise_profile(root, data, path):
    if any(j['status'] == 'running' for j in data['jobs']):
        raise ValueError('reconcile running claims before a profile revision')
    before, after = load_profile(root / 'profile.json'), load_profile(path)
    if [s['id'] for s in before['states']] != [s['id'] for s in after['states']]:
        raise ValueError('state-set/order changes require a separate prepared run; preserve this run history')
    generation = [j['id'] for j in data['jobs'] if generation_projection(before, j['id']) != generation_projection(after, j['id'])]
    for job in data['jobs']:
        if job.get('derivation_receipt') and not any(d['source'] == job['derivation_receipt']['source'] for s in after['states'] if s['id'] == job['id'] for d in s['allowed_derivations']):
            if job['id'] not in generation:
                generation.append(job['id'])
    extraction = [s['id'] for s in before['states'] if extraction_projection(before, s['id']) != extraction_projection(after, s['id'])]
    from prepare_pet_run import regenerate_prompts
    invalidate(data, 'profile revision: generation=' + ','.join(generation) + '; extraction=' + ','.join(extraction) + '; playback/review/package metadata checked')
    atomic(root / 'profile.json', after)
    if generation:
        old_prompts = {p: p.read_bytes() for p in (root / 'prompts').rglob('*.md')}
        regenerate_prompts(root, read(root / 'pet_request.json'), after, read(root / 'extraction-settings.json'), include_base='base' in generation)
        for p, content in old_prompts.items():
            name = 'base' if p.name == 'base-pet.md' else p.stem
            if name not in generation:
                p.write_bytes(content)
    for job in data['jobs']:
        if job['id'] in generation:
            job['status'] = 'pending'
            job['history'].append({'event': 'profile-generation-changed', 'time': time.time()})
    event(data, 'profile-revised', previous=before, current=after, generation=generation, extraction=extraction)
    save(root, data)
    return {'generation_stale': generation, 'extraction_stale': extraction, 'review_required': True}


def invalidate(data, reason):
    old = data.get('gates', {})
    if old:
        event(data, 'invalidated-gates', reason=reason, previous=old)
    # Installation history is factual even when future artifacts change.
    if old.get('installed'):
        data.setdefault('installations', []).append(old['installed'])
    data['gates'] = {}


def descendants(data, name):
    found = {name}
    while True:
        more = {j['id'] for j in data['jobs'] if (set(j.get('depends_on', [])) | {j.get('derived_from')}) & found}
        if more <= found:
            return found
        found |= more


def reconcile(root, data):
    """Invalidate stale work without ever launching or duplicating generation."""
    stale = set()
    for job in data['jobs']:
        if job['status'] not in ('generated', 'running'):
            continue
        try:
            valid = inputs(root, job) == job['input_hashes']
            if job['status'] == 'generated':
                valid = valid and digest(inside(root, job['output_path'])) == job['output_hash']
                if job['id'] == 'base':
                    valid = valid and digest(root / 'references/canonical-base.png') == job['canonical_hash']
                    valid = valid and read(root / 'extraction-settings.json')['canonical_hash'] == job['canonical_hash']
        except (OSError, KeyError, ValueError):
            valid = False
        if not valid:
            stale |= descendants(data, job['id'])
    changed = False
    for job in data['jobs']:
        if job['id'] not in stale:
            continue
        if job['status'] == 'running':
            # A call can still be in flight. Keep its token until explicitly failed.
            if not job.get('stale'):
                job['stale'] = True
                try:
                    current = inputs(root, job)
                    reasons = [key for key in set(current) | set(job['input_hashes']) if current.get(key) != job['input_hashes'].get(key)]
                except (ValueError, OSError):
                    reasons = ['missing required input']
                job['stale_reason'] = reasons or ['required dependency became stale']
                job['history'].append({'event': 'inputs-changed-during-generation', 'time': time.time(), 'changed_inputs': job['stale_reason']})
                changed = True
        elif job['status'] == 'generated':
            job['status'] = 'pending'
            previous = job.get('input_hashes', {})
            try:
                current = inputs(root, job)
                reasons = [key for key in set(previous) | set(current) if previous.get(key) != current.get(key)]
            except (OSError, ValueError):
                reasons = ['missing required input']
            job['history'].append({'event': 'stale-input-or-output', 'time': time.time(),
                                   'changed_inputs': reasons or ['source output or active derivation changed'],
                                   'previous_hash': job.get('output_hash')})
            changed = True
    if changed:
        invalidate(data, 'changed generation inputs or outputs')
        save(root, data)
    return changed


def _relocate(value, stage, root):
    if isinstance(value, dict):
        return {k: _relocate(v, stage, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_relocate(v, stage, root) for v in value]
    if isinstance(value, str) and (value == str(stage) or value.startswith(str(stage) + os.sep)):
        return str(root) + value[len(str(stage)):]
    return value


def prepare(root, args):
    root = Path(root).resolve()
    exists = root.exists() and any(root.iterdir())
    if exists and not args.replace_inputs:
        raise ValueError('run exists; use resume, or --replace-inputs for a new revision with a preserved previous directory')
    previous_id = read(root / 'imagegen-jobs.json').get('run_id') if exists and (root / 'imagegen-jobs.json').is_file() else None
    if exists and (root / 'imagegen-jobs.json').is_file():
        if any(j['status'] == 'running' for j in manifest(root)['jobs']):
            raise ValueError('reconcile running handoffs with ingest or fail before replacing inputs')
    stage = Path(tempfile.mkdtemp(prefix='.' + root.name + '-prepare-', dir=root.parent))
    transaction = root.parent / ('.' + root.name + '.prepare.json')
    try:
        cmd = ['--output-dir', stage, '--pet-name', args.pet_name, '--pet-notes', args.pet_notes]
        for flag in ('profile', 'state_selection', 'constraints', 'identity', 'reference_roles', 'brand_discovery_file', 'brand_name', 'brand_brief',
                     'chroma_key', 'style_preset', 'style_notes', 'description', 'pet_id', 'display_name', 'background'):
            value = getattr(args, flag, None)
            if value:
                cmd.extend(['--' + flag.replace('_', '-'), value])
        for flag in ('reference', 'avoid', 'require', 'brand_source', 'style_modifier'):
            for value in getattr(args, flag, []):
                cmd.extend(['--' + flag.replace('_', '-'), value])
        execute('prepare_pet_run.py', *cmd)
        request = _relocate(read(stage / 'pet_request.json'), stage, root)
        data = _relocate(read(stage / 'imagegen-jobs.json'), stage, root)
        data.update(schema_version=SCHEMA, run_id=uuid.uuid4().hex, run_dir=str(root), gates={},
                    history=[], fixture_mode=args.fixture_mode, previous_run_id=previous_id,
                    versions=versions(), limits={'attempts_per_job': 3, 'concurrency': 1, 'generation_calls': None, 'estimated_units_per_call': None, 'unit_budget': None})
        data['style_sha256'] = request['resolved_style']['sha256']
        for job in data['jobs']:
            job.update(attempts=0, history=[])
        atomic(stage / 'pet_request.json', request)
        atomic(stage / 'imagegen-jobs.json', data)
        backup = root.with_name(root.name + '.previous-' + uuid.uuid4().hex)
        previous_manifest_hash = digest(root / 'imagegen-jobs.json') if (root / 'imagegen-jobs.json').is_file() else None
        atomic(transaction, {'stage': str(stage), 'backup': str(backup), 'run_id': data['run_id'],
                             'previous_manifest_hash': previous_manifest_hash})
        if root.exists():
            os.replace(root, backup)
        os.replace(stage, root)
        transaction.unlink()
        return {'run_id': data['run_id'], 'run': str(root), 'previous': str(backup) if backup.exists() else None,
                'style_id': request['resolved_style']['resolved_id'], 'style_sha256': data['style_sha256'],
                'warnings': request.get('style_warnings', [])}
    finally:
        if stage.exists() and not transaction.exists():
            shutil.rmtree(stage)


def _journal_sibling(path, destination, prefix):
    destination = Path(destination).resolve()
    p = Path(path).resolve()
    if p.parent != destination.parent or not p.name.startswith(prefix) or p == destination:
        raise ValueError('invalid transaction journal path')
    return p


def recover_prepare(root):
    root = Path(root).resolve()
    path = root.parent / ('.' + root.name + '.prepare.json')
    if not path.exists():
        return
    record = read(path)
    stage = _journal_sibling(record['stage'], root, '.' + root.name + '-prepare-')
    backup = _journal_sibling(record['backup'], root, root.name + '.previous-')
    if stage.exists() and manifest(stage).get('run_id') != record['run_id']:
        raise ValueError('preparation staging identity differs from journal; all files preserved')
    if not root.exists():
        if stage.exists():
            os.replace(stage, root)
        elif backup.exists():
            os.replace(backup, root)
        else:
            raise ValueError('preparation recovery files missing')
    elif stage.exists():
        previous_hash = digest(root / 'imagegen-jobs.json') if (root / 'imagegen-jobs.json').is_file() else None
        if backup.exists() or previous_hash != record.get('previous_manifest_hash'):
            raise ValueError('preparation recovery inputs changed; all versions preserved')
        os.replace(root, backup)
        os.replace(stage, root)
    path.unlink()
    if (root / 'imagegen-jobs.json').is_file():
        data = manifest(root)
        event(data, 'prepare-recovered', preserved_previous=str(backup) if backup.exists() else None)
        save(root, data)


def claim(root, data, name):
    reconcile(root, data)
    job = job_for(data, name)
    if job['status'] == 'running':
        raise ValueError('generation is already running; reconcile its existing token with ingest or fail')
    if job['status'] == 'generated':
        raise ValueError('already generated; explicitly repair before replacing')
    limits = data.get('limits', {})
    if job.get('attempts', 0) >= limits.get('attempts_per_job', RETRIES):
        raise ValueError('retry bound reached; inspect failure history and explicitly repair')
    if sum(j['status'] == 'running' for j in data['jobs']) >= limits.get('concurrency', 1):
        raise ValueError('generation concurrency bound reached; reconcile an existing claim first')
    total = sum(1 for j in data['jobs'] for e in j['history'] if e['event'] == 'claim')
    if limits.get('generation_calls') is not None and total >= limits['generation_calls']:
        raise ValueError('generation call budget exhausted; recorded attempts include failed or uncertain calls')
    if limits.get('unit_budget') is not None and (total + 1) * limits['estimated_units_per_call'] > limits['unit_budget']:
        raise ValueError('user-estimated generation unit budget exhausted; no prices inferred')
    for dependency in job['depends_on']:
        if job_for(data, dependency)['status'] != 'generated':
            raise ValueError('dependency is not generated: ' + dependency)
    if name != 'base' and not base_review_current(root, data):
        raise ValueError('inspect canonical base/calibration and record base-review before generating rows')
    if name != 'base' and job.get('prompt_settings_hash') != digest(root / 'extraction-settings.json'):
        raise ValueError('calibration changed outside configure; regenerate prompts with configure before claiming rows')
    for key in ('derived_from', 'derivation_receipt', 'import_receipt'):
        job.pop(key, None)
    hashes = inputs(root, job)
    job.pop('stale', None)
    job.update(status='running', token=uuid.uuid4().hex, input_hashes=hashes,
               started_at=time.time(), attempts=job.get('attempts', 0) + 1)
    selected_prompt = job.get('retry_prompt_file', job['prompt_file']) if job['attempts'] > 1 else job['prompt_file']
    prompt_bytes = inside(root, selected_prompt).read_bytes()
    request = read(root / 'pet_request.json')
    job['history'].append({'event': 'claim', 'time': time.time(), 'token': job['token'], 'input_hashes': hashes,
        'prompt_snapshot': {'path': selected_prompt, 'sha256': hashlib.sha256(prompt_bytes).hexdigest(),
                            'utf8': prompt_bytes.decode('utf-8')},
        'input_images': json.loads(json.dumps(job['input_images'])),
        'identity': request.get('identity', {}), 'resolved_style': request.get('resolved_style')})
    save(root, data)
    return {'job': name, 'token': job['token'], 'generation_skill': '$imagegen', 'attempt': job['attempts'],
            'prompt': str(root / selected_prompt),
            'retry_prompt': str(root / job.get('retry_prompt_file', job['prompt_file'])),
            'input_images': [{**x, 'path': str(inside(root, x['path']))} for x in job['input_images']],
            'receipt_template': {'generation_skill': '$fixture' if data['fixture_mode'] else '$imagegen',
                                 'input_hashes': hashes, 'job': name, 'token': job['token']},
            'previous_failures': [e for e in job['history'] if e.get('event') in ('failed', 'repair')],
            'identity': read(root / 'pet_request.json').get('identity', {}),
            'resolved_style': request.get('resolved_style'),
            'style_warnings': style_warnings(root),
            'tool_arguments': {'transparent_background': read(root / 'extraction-settings.json').get('background') != 'chroma'} if name != 'base' else {'transparent_background': read(root / 'pet_request.json')['extraction']['background'] != 'chroma'},
            'attachment_rules': 'View local inputs before editing. Attach every input using referenced_image_paths when all are local; otherwise num_last_images_to_include (max 5). Never use both. A brand-new text-only base omits both.',
            'output_rules': 'Follow installed imagegen skill, including image output; return selected source and QA note. Do not mutate manifests.'}


def select_base(root, data, source_origin=None):
    """Calibrate from the actual selected image, never a text-only fallback guess."""
    from prepare_pet_run import CHROMA_KEY_CANDIDATES, parse_hex_color, regenerate_prompts
    from extract_strip_frames import mask_background, pixels
    path = root / 'references/canonical-base.png'
    request = read(root / 'pet_request.json')
    with Image.open(path) as opened:
        image = opened.convert('RGBA')
    key = tuple(request['chroma_key']['rgb'])
    threshold = request.get('extraction', {}).get('key_threshold', 48)
    # Automatic alpha selection is conservative; incidental partial alpha is not a cutout.
    cleared, mask = mask_background(image, key, threshold, request.get('extraction', {}).get('background', 'auto'))
    is_alpha = mask['mode'] == 'alpha'
    sampled = cleared.copy()
    sampled.thumbnail((256, 256))
    foreground = [p for p in pixels(sampled) if p[3] > 16]
    near = lambda pixel, color: sum((pixel[i] - color[i]) ** 2 for i in range(3)) <= threshold ** 2
    candidates = []
    for preference, (name, color) in enumerate(CHROMA_KEY_CANDIDATES):
        rgb = parse_hex_color(color)
        candidates.append((sum(near(p, rgb) for p in foreground), preference, name, color, rgb))
    selected = min(candidates)
    # Explicit keys remain explicit; their collisions are reported, not silently overridden.
    if request['chroma_key'].get('selection') == 'manual' or is_alpha:
        chosen_key = request['chroma_key']
    else:
        chosen_key = {'hex': selected[3], 'rgb': list(selected[4]), 'name': selected[2], 'selection': 'canonical-base'}
    collisions = sum(near(p, chosen_key['rgb']) for p in foreground)
    warnings = list(mask.get('warnings', []))
    if not is_alpha:
        warnings.append('Key-colored foreground connected to the background cannot be distinguished reliably; inspect the canonical reference.')
    if collisions:
        warnings.append(f'{collisions} sampled foreground pixels are near the selected key; prefer alpha or choose another key before rows.')
    settings = {**request.get('extraction', {}), 'schema_version': 1,
                'background': 'alpha' if is_alpha else 'chroma', 'key_threshold': threshold,
                'holes': request.get('extraction', {}).get('holes', 'preserve'),
                'edge_cleanup': request.get('extraction', {}).get('edge_cleanup', True),
                'chroma_key': chosen_key, 'canonical_hash': digest(path),
                'key_collision_pixels': collisions, 'sampled_foreground': len(foreground),
                'candidates': [{'name': c[2], 'hex': c[3], 'collision_pixels': c[0]} for c in candidates],
                'warnings': warnings}
    if source_origin is not None and request.get('resolved_style') is not None:
        settings['canonical_source_origin'] = source_origin
    from style_registry import compatibility_warnings
    settings['style_warnings'] = compatibility_warnings(request.get('resolved_style'), load_profile(root / 'profile.json'),
                                                       settings['background'], settings.get('resampling'))
    atomic(root / 'extraction-settings.json', settings)
    if data.get('base_review'):
        event(data, 'preserved-base-review', previous=data['base_review'])
    data.pop('base_review', None)
    regenerate_prompts(root, request, load_profile(root / 'profile.json'), settings)
    for job in data['jobs']:
        if job['id'] != 'base':
            job['prompt_settings_hash'] = digest(root / 'extraction-settings.json')
    invalidate(data, 'canonical base selected and extraction settings calibrated')
    return settings


def base_review_current(root, data):
    value = data.get('base_review', {})
    try:
        return (value.get('canonical_hash') == digest(root / 'references/canonical-base.png') and
                value.get('settings_hash') == digest(root / 'extraction-settings.json'))
    except OSError:
        return False


def record_base_review(root, data, note, accept_color_warning=False):
    reconcile(root, data)
    if job_for(data, 'base')['status'] != 'generated' or not note.strip():
        raise ValueError('current canonical base and a visual inspection note are required')
    settings = read(root / 'extraction-settings.json')
    if settings['background'] == 'chroma' and settings.get('key_collision_pixels', 0) and not accept_color_warning:
        raise ValueError('key collides with sampled foreground; configure another key/alpha or explicitly --accept-color-warning with review notes')
    if data.get('base_review'):
        event(data, 'preserved-base-review', previous=data['base_review'])
    data['base_review'] = {'canonical_hash': digest(root / 'references/canonical-base.png'),
                           'settings_hash': digest(root / 'extraction-settings.json'), 'time': time.time(),
                           'note': note, 'accepted_color_warning': accept_color_warning}
    save(root, data)
    return {'base_reviewed': True, 'calibration': settings}


def ingest(root, data, name, source, token, note, used_inputs, fixture=False):
    root = Path(root).resolve()
    job = job_for(data, name)
    if job['status'] != 'running' or token != job.get('token'):
        raise ValueError('generation token is not active')
    if job.get('stale') or inputs(root, job) != job['input_hashes']:
        raise ValueError('generation inputs changed; record failure before retrying')
    if fixture != bool(data.get('fixture_mode')):
        raise ValueError('fixture artifacts require an explicitly fixture-only run; production must use imagegen')
    receipt = read(used_inputs)
    receipt = receipt.get('receipt_template', receipt)
    if receipt.get('generation_skill') != ('$fixture' if fixture else '$imagegen'):
        raise ValueError('generation receipt must identify installed imagegen (or fixture-only source)')
    if receipt.get('input_hashes') != job['input_hashes'] or receipt.get('token') != token or receipt.get('job') != name:
        raise ValueError('receipt must attest the active job/token and every current prompt and grounding input hash')
    if not note.strip():
        raise ValueError('QA note required')
    source = Path(source).resolve()
    managed = [root / folder for folder in ('decoded', 'frames', 'final', 'qa')]
    if any(source == p or source.is_relative_to(p) for p in managed) or source == root / 'references/canonical-base.png':
        raise ValueError('ingest source must be an original outside managed outputs; its source file will be preserved')
    source_hash = digest(source)
    check_image(source)
    with Image.open(source) as image:
        image.verify()
    invalidate(data, 'ingest ' + name)
    save(root, data)  # A crash during file replacement cannot leave approval valid.
    output = inside(root, job['output_path'])
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + '.' + uuid.uuid4().hex + '.incoming')
    with Image.open(source) as image:
        image.convert('RGBA').save(temp, format='PNG')
    os.replace(temp, output)
    if name == 'base':
        shutil.copy2(output, root / 'references/canonical-base.png')
        select_base(root, data, source_origin='fixture' if fixture else 'imagegen')
        job['canonical_hash'] = digest(root / 'references/canonical-base.png')
    for field in ('derived_from', 'derivation_receipt', 'import_receipt'):
        job.pop(field, None)
    job.update(status='generated', output_hash=digest(output), source_hash=source_hash,
               source_path=str(source), qa_note=note, generated_at=time.time(), generation_receipt=receipt,
               source_origin='fixture' if fixture else 'imagegen')
    job['model_call'] = receipt.get('model_call', {'id': None, 'model': None, 'parameters': None,
                           'unavailable_reason': 'The handoff did not expose provider call metadata.'})
    job['history'].append({'event': 'generated', 'time': time.time(), 'source_hash': source_hash,
                           'source_path': str(source), 'output_hash': job['output_hash'],
                           'model_call': job['model_call'], 'generation_receipt': receipt})
    job.pop('token', None)
    save(root, data)
    return {'generated': name, 'fixture': fixture, 'settings': read(root / 'extraction-settings.json') if name == 'base' else None}


def fail(root, data, name, token, reason):
    job = job_for(data, name)
    if job['status'] != 'running' or job.get('token') != token:
        raise ValueError('active token required')
    if not reason.strip():
        raise ValueError('failure reason required')
    job.update(status='failed')
    job['history'].append({'event': 'failed', 'time': time.time(), 'reason': reason, 'token': token})
    job.pop('token', None)
    job.pop('stale', None)
    save(root, data)
    return {'failed': name, 'remaining_attempts': max(0, data.get('limits', {}).get('attempts_per_job', RETRIES) - job['attempts'])}


def repair(root, data, name, reason):
    if not reason.strip():
        raise ValueError('specific repair reason required')
    affected = descendants(data, name)
    if any(j['status'] == 'running' for j in data['jobs'] if j['id'] in affected):
        raise ValueError('reconcile affected running handoffs before repair')
    invalidate(data, 'repair ' + name)
    for job in data['jobs']:
        if job['id'] in affected:
            job['history'].append({'event': 'repair', 'reason': reason, 'time': time.time(),
                                   'previous_hash': job.get('output_hash')})
            job.update(status='pending', attempts=0)
    job = job_for(data, name)
    # Include the repair note in both prompts; workers may choose either on a retry.
    for key in ('prompt_file', 'retry_prompt_file'):
        if key in job:
            p = inside(root, job[key])
            with p.open('a', encoding='utf-8') as out:
                out.write('\nRepair required: ' + reason + '\n')
    save(root, data)
    return {'pending': sorted(affected), 'reason': reason}


def configure(root, data, args):
    if any(j['status'] == 'running' for j in data['jobs']):
        raise ValueError('reconcile running handoffs before changing extraction settings')
    settings = read(root / 'extraction-settings.json')
    previous = dict(settings)
    for key in ('background', 'holes', 'shared_scale', 'key_threshold', 'resampling'):
        value = getattr(args, key, None)
        if value is not None:
            settings[key] = value
    if args.chroma_key:
        from prepare_pet_run import parse_hex_color
        settings['chroma_key'] = {'hex': args.chroma_key.upper(), 'rgb': list(parse_hex_color(args.chroma_key)),
                                  'name': 'user-selected', 'selection': 'manual'}
    if settings.get('shared_scale') is not None and not 0 < settings['shared_scale'] <= 100:
        raise ValueError('shared scale must be >0 and <=100')
    if not 0 <= settings.get('key_threshold', 48) <= 441.7:
        raise ValueError('key threshold must be between 0 and 441.7')
    if 'resampling' in settings:
        from style_registry import RESAMPLING
        if settings['resampling'] not in RESAMPLING:
            raise ValueError('unsupported resampling; use nearest, bilinear, bicubic or lanczos')
    # Re-measure the new key against the selected identity, not against stale scores.
    from extract_strip_frames import mask_background, pixels
    request = read(root / 'pet_request.json')
    with Image.open(root / 'references/canonical-base.png') as opened:
        original, mask = mask_background(opened, tuple(request['chroma_key']['rgb']),
                                        request.get('extraction', {}).get('key_threshold', 48),
                                        request.get('extraction', {}).get('background', 'auto'))
    original.thumbnail((256, 256))
    foreground = [p for p in pixels(original) if p[3] > 16]
    chosen = settings['chroma_key']['rgb']
    threshold = settings['key_threshold']
    settings['key_collision_pixels'] = sum(sum((p[i] - chosen[i]) ** 2 for i in range(3)) <= threshold ** 2 for p in foreground)
    settings['sampled_foreground'] = len(foreground)
    settings['warnings'] = list(mask.get('warnings', []))
    if settings['key_collision_pixels']:
        settings['warnings'].append('Selected key overlaps sampled canonical colors; inspect before row generation.')
    from style_registry import compatibility_warnings
    settings['style_warnings'] = compatibility_warnings(request.get('resolved_style'), load_profile(root / 'profile.json'),
                                                       settings['background'], settings.get('resampling'))
    from prepare_pet_run import regenerate_prompts
    changed = [key for key in ('background', 'holes', 'shared_scale', 'key_threshold', 'chroma_key', 'resampling') if settings.get(key) != previous.get(key)]
    if not changed:
        return {'settings': settings, 'pending': [], 'changed': []}
    generation_change = bool(set(changed) & {'background', 'chroma_key'})
    invalidate(data, 'generation background changed' if generation_change else 'deterministic extraction calibration changed: ' + ', '.join(changed))
    save(root, data)
    atomic(root / 'extraction-settings.json', settings)
    event(data, 'extraction-configured', previous=previous, current=settings,
          previous_base_review=data.get('base_review'))
    data.pop('base_review', None)
    if generation_change:
        regenerate_prompts(root, read(root / 'pet_request.json'), load_profile(root / 'profile.json'), settings)
    pending = []
    for job in data['jobs']:
        if job['id'] != 'base':
            if generation_change and not job.get('import_receipt'):
                job['status'] = 'pending'
                pending.append(job['id'])
            job['prompt_settings_hash'] = digest(root / 'extraction-settings.json')
            job['history'].append({'event': 'settings-changed', 'time': time.time(),
                                   'changed': changed, 'stage': 'generation' if generation_change else 'extraction'})
    save(root, data)
    return {'settings': settings, 'pending': pending, 'changed': changed, 'model_call_required': bool(pending)}


def derive(root, data, name, reason, approved):
    if not approved or not reason.strip():
        raise ValueError('explicit mirror approval and a visual identity/asymmetry decision note are required')
    reconcile(root, data)
    job = job_for(data, name)
    if job['status'] not in ('pending', 'failed'):
        raise ValueError('repair or reconcile target before deriving')
    if not base_review_current(root, data):
        raise ValueError('current base-review required before derivation')
    if read(root / 'pet_request.json').get('identity', {}).get('forbid_mirror'):
        raise ValueError('structured identity explicitly forbids mirroring')
    spec = next(s for s in load_profile(root / 'profile.json')['states'] if s['id'] == name)
    choices = spec.get('allowed_derivations', [])
    if len(choices) != 1:
        raise ValueError('profile must allow exactly one explicit mirror source for this state')
    rule = choices[0]
    source_job = job_for(data, rule['source'])
    if source_job['status'] != 'generated':
        raise ValueError('mirror source must be current and generated')
    from derive_running_left_from_running_right import mirror_strip_preserving_frame_order
    job['derived_from'] = rule['source']
    hashes = inputs(root, job)
    invalidate(data, 'explicitly approved derivation ' + name)
    save(root, data)
    with Image.open(inside(root, source_job['output_path'])) as source:
        mirrored = mirror_strip_preserving_frame_order(source, spec['frames'])
    output = inside(root, job['output_path'])
    temporary = output.with_suffix('.derived.tmp')
    mirrored.save(temporary, format='PNG')
    os.replace(temporary, output)
    receipt = {'kind': 'horizontal-mirror', 'source': rule['source'], 'source_hash': source_job['output_hash'],
               'approved': True, 'note': reason, 'time': time.time(), 'preserves_order': True}
    for field in ('generation_receipt', 'import_receipt', 'source_hash', 'source_path', 'qa_note', 'generated_at', 'model_call'):
        job.pop(field, None)
    job.update(status='generated', input_hashes=hashes, output_hash=digest(output),
               derived_from=rule['source'], derivation_receipt=receipt, source_origin='derived')
    job['history'].append({'event': 'derived', 'output_hash': job['output_hash'], **receipt})
    save(root, data)
    return receipt


def artifact_hashes(root):
    files = ['pet_request.json', 'profile.json', 'extraction-settings.json', 'frames/frames-manifest.json',
             'qa/contact-sheet.png', 'qa/review.json', 'state-selection.json']
    for folder in ('references', 'prompts', 'decoded', 'frames', 'final', 'qa/previews', 'annotations'):
        files += [p.relative_to(root).as_posix() for p in sorted((root / folder).rglob('*'))
                  if p.is_file() and not p.name.endswith(('.tmp', '.lock'))]
    result = {p: digest(inside(root, p)) for p in sorted(set(files)) if inside(root, p).is_file()}
    data = manifest(root)
    keys = ('id', 'status', 'input_hashes', 'input_images', 'output_hash', 'generation_receipt', 'derivation_receipt', 'import_receipt', 'source_origin', 'canonical_hash')
    result['@generation'] = object_hash([{k: j[k] for k in keys if k in j} for j in data['jobs']])
    result['@base-review'] = object_hash(data.get('base_review'))
    result['@pipeline'] = object_hash(versions())
    return result


@contextmanager
def operation(root, data, kind):
    old = data.get('operation')
    if old and old.get('status') == 'running':
        event(data, 'interrupted-operation', operation=old)
    record = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'running', 'started_at': time.time()}
    data['operation'] = record
    save(root, data)
    try:
        yield record
    except BaseException as exc:
        record.update(status='failed', ended_at=time.time(), error=str(exc))
        event(data, 'operation-failed', operation=dict(record))
        save(root, data)
        raise
    else:
        record.update(status='complete', ended_at=time.time())
        event(data, 'operation-complete', operation=dict(record))
        save(root, data)


def validate(root, data):
    reconcile(root, data)
    if any(j['status'] != 'generated' for j in data['jobs']):
        raise ValueError('all generation handoffs must be complete and current')
    invalidate(data, 'deterministic validation started')
    with operation(root, data, 'validate'):
        profile = root / 'profile.json'
        # Preserve provenance for unchanged rows; extract only rows with invalid/missing evidence.
        from inspect_frames import load_manifest, validate_provenance
        from profile_spec import indexed_frames
        p = load_profile(profile)
        try:
            rows = load_manifest(root / 'frames')
        except (ValueError, OSError, TypeError, KeyError):
            rows = {}
        if set(rows) - {s['id'] for s in p['states']}:
            rows = {}
        changed = []
        for spec in p['states']:
            try:
                files = indexed_frames(root / 'frames', spec['id'], spec['frames'])
                errors = validate_provenance(root / 'frames', spec['id'], files, rows.get(spec['id'], {}), p)
            except (ValueError, OSError, KeyError, TypeError):
                errors = ['missing evidence']
            if errors:
                changed.append(spec['id'])
        if changed:
            execute('extract_strip_frames.py', '--decoded-dir', root / 'decoded', '--output-dir', root / 'frames',
                    '--profile', profile, '--states', ','.join(changed))
        execute('inspect_frames.py', '--frames-root', root / 'frames', '--json-out', root / 'qa/review.json', '--profile', profile)
        execute('compose_atlas.py', '--frames-root', root / 'frames', '--output', root / 'final/spritesheet.png',
                '--webp-output', root / 'final/spritesheet.webp', '--profile', profile)
        execute('validate_atlas.py', root / 'final/spritesheet.webp', '--json-out', root / 'final/validation.json', '--profile', profile)
        execute('make_contact_sheet.py', root / 'final/spritesheet.webp', '--output', root / 'qa/contact-sheet.png', '--profile', profile)
        execute('render_animation_previews.py', '--frames-root', root / 'frames', '--output-dir', root / 'qa/previews', '--profile', profile)
        data['gates']['validated'] = {'time': time.time(), 'hashes': artifact_hashes(root)}
    return {'validated': True, 'extracted_states': changed, 'visual_review_required': True}


def require_valid(root, data):
    reconcile(root, data)
    record = data.get('gates', {}).get('validated')
    if not base_review_current(root, data) or not record or record['hashes'] != artifact_hashes(root):
        raise ValueError('missing or stale deterministic validation')
    if not read(root / 'qa/review.json')['ok'] or not read(root / 'final/validation.json')['ok']:
        raise ValueError('validation failed')


def style_warnings(root):
    from style_registry import compatibility_warnings
    request = read(root / 'pet_request.json')
    settings = read(root / 'extraction-settings.json') if (root / 'extraction-settings.json').is_file() else request.get('extraction', {})
    return compatibility_warnings(request.get('resolved_style'), load_profile(root / 'profile.json'),
                                  settings.get('background', 'auto'), settings.get('resampling'))


def style_provenance(root, data):
    request = read(root / 'pet_request.json')
    return {'schema_version': 1, 'resolved_style': request.get('resolved_style'),
            'legacy_style_preset': request.get('style_preset'), 'style_contract': request.get('style_contract'),
            'warnings': style_warnings(root),
            'source_origins': {j['id']: source_origin(j) for j in data['jobs']},
            'interpretation': 'Requested generation guidance, not proof of style quality or imagegen origin. Imported pixels retain their original rendering. Passing visual review is recorded separately.'}


def review_template(root, data):
    require_valid(root, data)
    metrics = read(root / 'qa/review.json')
    style_risks = style_warnings(root)
    warnings = {r['state']: list(dict.fromkeys(r['warnings'] + metrics.get('consistency_metrics', {}).get('warnings', []) + style_risks)) for r in metrics['rows']}
    return {'schema_version': 2, 'reviewer': '', 'fixture': data['fixture_mode'], 'artifact_hashes': artifact_hashes(root),
            'evidence': {'canonical_reference_viewed': False, 'contact_sheet_viewed': False, 'playback_states': []},
            'rows': {s['id']: {'verdict': 'unreviewed', 'notes': '',
                'dimensions': {name: {'verdict': 'unreviewed', 'notes': ''} for name in REVIEW_DIMENSIONS},
                'warning_notes': {w: '' for w in warnings[s['id']]}} for s in load_profile(root / 'profile.json')['states']}}


def review(root, data, receipt):
    require_valid(root, data)
    value = read(receipt)
    states = {s['id'] for s in load_profile(root / 'profile.json')['states']}
    if not isinstance(value.get('reviewer'), str) or not value['reviewer'].strip() or value.get('artifact_hashes') != artifact_hashes(root):
        raise ValueError('review must name reviewer and bind exact current artifact_hashes from review-template')
    if value.get('fixture') is not data['fixture_mode']:
        raise ValueError('review must retain the fixture/production label')
    evidence = value.get('evidence', {})
    if evidence.get('canonical_reference_viewed') is not True or evidence.get('contact_sheet_viewed') is not True or set(evidence.get('playback_states', [])) != states:
        raise ValueError('record canonical reference, contact sheet, and timed playback evidence for every state')
    rows = value.get('rows', {})
    if not isinstance(rows, dict) or set(rows) != states or any(not isinstance(r, dict) or r.get('verdict') not in ('pass', 'fail') or not isinstance(r.get('notes'), str) or not r['notes'].strip() for r in rows.values()):
        raise ValueError('review needs every state with verdict pass/fail and specific notes')
    expected = review_template(root, data)
    for state, row in rows.items():
        dimensions = row.get('dimensions', {})
        if set(dimensions) != set(REVIEW_DIMENSIONS) or any(not isinstance(d, dict) or d.get('verdict') not in ('pass', 'fail') or not isinstance(d.get('notes'), str) or not d['notes'].strip() for d in dimensions.values()):
            raise ValueError('review requires identity, action, direction, loop, extraction and reduced_motion verdicts with evidence notes')
        if row['verdict'] == 'pass' and any(d['verdict'] != 'pass' for d in dimensions.values()):
            raise ValueError('passing row cannot contain a failing review dimension')
        notes = row.get('warning_notes', {})
        if set(notes) != set(expected['rows'][state]['warning_notes']) or any(not isinstance(n, str) or not n.strip() for n in notes.values()):
            raise ValueError('specific review notes required for every current warning')
    value['accepted'] = all(r['verdict'] == 'pass' for r in rows.values())
    value['recorded_at'] = time.time()
    previous_review = root / 'qa/visual-review.json'
    if previous_review.is_file():
        event(data, 'preserved-visual-review', sha256=digest(previous_review),
              receipt_utf8=previous_review.read_bytes().decode('utf-8'))
    if data['gates'].get('reviewed'):
        event(data, 'superseded-review', previous=data['gates']['reviewed'])
    atomic(root / 'qa/visual-review.json', value)
    data['gates']['reviewed'] = {'hash': digest(root / 'qa/visual-review.json'), 'accepted': value['accepted'],
                                'artifact_hashes': artifact_hashes(root)}
    data['gates'].pop('packaged', None)
    save(root, data)
    return {'reviewed': True, 'accepted': value['accepted'], 'fixture': data['fixture_mode']}


def require_review(root, data):
    require_valid(root, data)
    record = data['gates'].get('reviewed')
    if not record or not record['accepted'] or record['artifact_hashes'] != artifact_hashes(root) or not (root / 'qa/visual-review.json').is_file() or record['hash'] != digest(root / 'qa/visual-review.json'):
        raise ValueError('successful current visual review required')


def _destination(root, destination):
    if not destination:
        raise ValueError('--destination is required')
    path = Path(destination).resolve()
    for folder in ('frames', 'references', 'decoded', 'prompts', 'qa', 'final'):
        protected = root / folder
        if path == protected or path.is_relative_to(protected) or protected.is_relative_to(path):
            raise ValueError('destination overlaps run inputs or validation artifacts')
    return path


def export(root, data, destination):
    require_valid(root, data)
    from export_assets import export_assets
    destination = _destination(root, destination)
    if destination.exists():
        raise ValueError('export destination already exists; choose a new path')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with operation(root, data, 'export'):
        stage = Path(tempfile.mkdtemp(prefix='.export-', dir=destination.parent))
        try:
            export_assets(root / 'frames', stage, load_profile(root / 'profile.json'))
            atomic(stage / 'provenance.json', {'run_id': data['run_id'], 'fixture': data['fixture_mode'],
                   'resolved_style': read(root / 'pet_request.json').get('resolved_style'),
                   'style': style_provenance(root, data),
                   'artifact_hashes': artifact_hashes(root), 'generation_jobs': data['jobs'],
                   'deterministic_metrics': read(root / 'qa/review.json'),
                   'visual_review': read(root / 'qa/visual-review.json') if _review_ok(root, data) else None,
                   'app_integration_verified': False})
            from export_assets import write_export_receipt
            write_export_receipt(stage, load_profile(root / 'profile.json'))
            os.replace(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        event(data, 'exported', destination=str(destination), receipt_hash=digest(destination / 'export-receipt.json'))
    return {'export': str(destination), 'accepted': _review_ok(root, data), 'fixture': data['fixture_mode']}


def tree_hashes(root, exclude=()):
    files = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
            raise ValueError('package/archive trees cannot contain links')
        if p.is_file() and p.relative_to(root).as_posix() not in exclude:
            files[p.relative_to(root).as_posix()] = digest(p)
    return files


def package(root, data, destination):
    require_review(root, data)
    destination = _destination(root, destination)
    if destination.exists():
        raise ValueError('package destination already exists; choose a new version path')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with operation(root, data, 'package'):
        stage = Path(tempfile.mkdtemp(prefix='.package-', dir=destination.parent))
        try:
            from export_assets import export_assets
            profile = load_profile(root / 'profile.json')
            export_assets(root / 'frames', stage, profile)
            if profile['export'].get('codex_compatible'):
                request = read(root / 'pet_request.json')
                shutil.copy2(root / 'final/spritesheet.webp', stage / 'spritesheet.webp')
                atomic(stage / 'pet.json', {'id': request['pet_id'], 'displayName': request['display_name'],
                       'description': request['description'], 'spritesheetPath': 'spritesheet.webp'})
            shutil.copy2(root / 'qa/visual-review.json', stage / 'visual-review.json')
            shutil.copy2(root / 'qa/review.json', stage / 'metrics.json')
            atomic(stage / 'style-provenance.json', style_provenance(root, data))
            from export_assets import write_export_receipt
            write_export_receipt(stage, profile)
            atomic(stage / 'receipt.json', {'schema_version': 2, 'run_id': data['run_id'], 'fixture': data['fixture_mode'],
                   'artifacts': artifact_hashes(root), 'files': tree_hashes(stage), 'app_integration_verified': False})
            verify_package(stage)
            os.replace(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        data['gates']['packaged'] = {'path': str(destination), 'receipt_hash': digest(destination / 'receipt.json')}
    return {'package': str(destination), 'installed': False, 'fixture': data['fixture_mode']}


def verify_package(package):
    package = Path(package).resolve()
    receipt = read(package / 'receipt.json')
    if receipt.get('schema_version') != 2 or not isinstance(receipt.get('files'), dict) or not receipt['files']:
        raise ValueError('missing or unsupported package receipt')
    if tree_hashes(package, ('receipt.json',)) != receipt['files']:
        raise ValueError('package file hashes/contents differ from receipt')
    return receipt


def recover_install(destination):
    destination = Path(destination).resolve()
    journal = destination.parent / ('.' + destination.name + '.install.json')
    if not journal.exists():
        return None
    value = read(journal)
    backup = _journal_sibling(value['backup'], destination, destination.name + '.previous-')
    stage = _journal_sibling(value['stage'], destination, '.' + destination.name + '-stage-')
    promoted = ((destination / 'receipt.json').is_file() and
                digest(destination / 'receipt.json') == value['receipt_hash'])
    if not promoted:
        if destination.exists():
            if backup.exists() or tree_hashes(destination) != value.get('previous_hashes'):
                raise ValueError('interrupted installation destination changed; all versions preserved')
        if not stage.is_dir():
            if not destination.exists() and backup.is_dir():
                os.replace(backup, destination)
                journal.unlink()
                return {'recovered': 'rolled-back', 'destination': str(destination)}
            raise ValueError('interrupted installation stage is missing; prior files preserved')
        if digest(stage / 'receipt.json') != value['receipt_hash']:
            raise ValueError('interrupted installation stage changed; all versions preserved')
        verify_package(stage)
        if destination.exists():
            os.replace(destination, backup)
        os.replace(stage, destination)
    verify_package(destination)
    if backup.exists() and tree_hashes(backup) != value.get('previous_hashes'):
        raise ValueError('interrupted installation backup changed; preserve both versions for inspection')
    if stage.exists():
        if digest(stage / 'receipt.json') != value['receipt_hash']:
            raise ValueError('interrupted installation stage changed; preserved')
        verify_package(stage)
        shutil.rmtree(stage)
    journal.unlink()
    return {'destination': str(destination), 'backup': str(backup) if backup.exists() else None,
            'backup_hashes': value.get('previous_hashes'), 'receipt_hash': value['receipt_hash'], 'app_integration_verified': False, 'recovered': 'committed'}


def _existing_install(destination):
    if not destination.exists() or not any(destination.iterdir()):
        return
    if (destination / 'receipt.json').is_file():
        verify_package(destination)
        return
    # Legacy Codex package: only the exact manifest and its sprite are replaceable.
    value = read(destination / 'pet.json') if (destination / 'pet.json').is_file() else {}
    sprite = value.get('spritesheetPath')
    if sprite not in ('spritesheet.webp', 'spritesheet.png') or set(p.name for p in destination.iterdir()) != {'pet.json', sprite}:
        raise ValueError('destination contains unrecognized/unrelated files; choose an isolated package directory')
    with Image.open(destination / sprite) as image:
        image.verify()


def install(root, data, package_path, destination, allow_fixture=False, fail_after_backup=False, fail_after_promote=False):
    require_review(root, data)
    package_path = Path(package_path).resolve()
    record = data['gates'].get('packaged')
    if not record or Path(record['path']) != package_path or digest(package_path / 'receipt.json') != record['receipt_hash']:
        raise ValueError('package not bound to this reviewed run')
    receipt = verify_package(package_path)
    if receipt['artifacts'] != artifact_hashes(root):
        raise ValueError('stale package')
    if receipt['fixture'] and not allow_fixture:
        raise ValueError('fixture package may only be installed with explicit --test-destination')
    profile = load_profile(package_path / 'profile.json')
    if not profile['export'].get('codex_compatible') and not allow_fixture:
        raise ValueError('custom profile is an asset export, not a Codex pet')
    destination = Path(destination).resolve()
    codex_pets = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'pets'
    if allow_fixture and (destination == codex_pets.resolve() or destination.is_relative_to(codex_pets.resolve())):
        raise ValueError('test destination cannot be the installed Codex pets directory')
    if any(destination == p or destination.is_relative_to(p) or p.is_relative_to(destination) for p in (root, package_path)):
        raise ValueError('installation must be outside run and package source')
    destination.parent.mkdir(parents=True, exist_ok=True)
    recovered = recover_install(destination)
    if recovered and recovered.get('receipt_hash') == record['receipt_hash']:
        data['gates']['installed'] = recovered
        data.pop('pending_install', None)
        event(data, 'install-recovered', **recovered)
        save(root, data)
        return recovered
    _existing_install(destination)
    stage = Path(tempfile.mkdtemp(prefix='.' + destination.name + '-stage-', dir=destination.parent))
    backup = destination.with_name(destination.name + '.previous-' + uuid.uuid4().hex)
    journal = destination.parent / ('.' + destination.name + '.install.json')
    promoted = False
    try:
        shutil.copytree(package_path, stage, dirs_exist_ok=True)
        verify_package(stage)
        data['pending_install'] = {'destination': str(destination)}
        save(root, data)
        atomic(journal, {'backup': str(backup), 'stage': str(stage), 'receipt_hash': digest(stage / 'receipt.json'),
                         'previous_hashes': tree_hashes(destination) if destination.exists() else None})
        if destination.exists():
            os.replace(destination, backup)
        if fail_after_backup:
            raise OSError('injected installation failure after backup')
        os.replace(stage, destination)
        promoted = True
        if fail_after_promote:
            raise OSError('injected installation failure after promotion')
        verify_package(destination)
        journal.unlink()
    except BaseException:
        if promoted and destination.exists():
            failed = destination.with_name(destination.name + '.failed-' + uuid.uuid4().hex)
            os.replace(destination, failed)
        if backup.exists():
            os.replace(backup, destination)
        journal.unlink(missing_ok=True)
        data.pop('pending_install', None)
        event(data, 'installation-rolled-back', destination=str(destination))
        save(root, data)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    data['gates']['installed'] = {'destination': str(destination), 'backup': str(backup) if backup.exists() else None,
                                  'backup_hashes': tree_hashes(backup) if backup.exists() else None,
                                  'receipt_hash': digest(destination / 'receipt.json'), 'app_integration_verified': False}
    data.pop('pending_install', None)
    event(data, 'installed', **data['gates']['installed'])
    save(root, data)
    return data['gates']['installed']


def rollback(root, data):
    records = data.get('installations', []) + ([data['gates']['installed']] if data.get('gates', {}).get('installed') else [])
    if not records or not records[-1].get('backup'):
        raise ValueError('no recoverable previous installation recorded')
    record = records[-1]
    destination = Path(record['destination']).resolve()
    backup = _journal_sibling(record['backup'], destination, destination.name + '.previous-')
    with ownership(destination):
        recover_install(destination)
        if not backup.is_dir():
            raise ValueError('previous package is unavailable')
        if record.get('backup_hashes') is None or tree_hashes(backup) != record['backup_hashes']:
            raise ValueError('previous backup is unbound or changed; refusing to restore it')
        if digest(destination / 'receipt.json') != record['receipt_hash']:
            raise ValueError('installation changed since this run; refusing to displace it')
        _existing_install(backup)
        displaced = destination.with_name(destination.name + '.rolled-back-' + uuid.uuid4().hex)
        os.replace(destination, displaced)
        try:
            os.replace(backup, destination)
        except BaseException:
            os.replace(displaced, destination)
            raise
    data['gates'].pop('installed', None)
    event(data, 'rolled-back', destination=str(destination), displaced=str(displaced))
    save(root, data)
    return {'restored': str(destination), 'displaced': str(displaced)}


def cleanup(root, data, mode, apply):
    root = Path(root).resolve()
    require_review(root, data)
    if mode not in ('repair', 'minimal-delivery'):
        raise ValueError('unknown retention mode')
    artifacts = artifact_hashes(root)
    profile = load_profile(root / 'profile.json')
    candidates = [root / 'frames' / s['id'] / f'{i:02d}.png'
                  for s in profile['states'] for i in range(s['frames'])]
    candidates += [root / 'qa/previews' / (s['id'] + '.gif') for s in profile['states']]
    candidates += [root / 'final/spritesheet.png']
    if mode == 'minimal-delivery':
        # Only the run-owned decoded copies; never the original generation tool output.
        candidates += [inside(root, j['output_path']) for j in data['jobs'] if j['id'] != 'base']
    candidates = sorted({p for p in candidates if p.is_file()})
    # Archive a bounded, hash-verified inventory; never sweep arbitrary folders into it.
    names = {p for p in artifacts if not p.startswith(('@', 'frames/', 'qa/previews/'))}
    names.discard('final/spritesheet.png')
    if mode == 'minimal-delivery':
        names -= {j['output_path'] for j in data['jobs'] if j['id'] != 'base'}
    names |= {'imagegen-jobs.json', 'frames/frames-manifest.json', 'qa/visual-review.json'}
    if apply:
        archive = root / ('repair-archive-' + uuid.uuid4().hex + '.zip')
        temporary = archive.with_suffix('.tmp')
        archive_hashes = {p: digest(inside(root, p)) for p in sorted(names)}
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as out:
                for name in archive_hashes:
                    out.write(inside(root, name), name)
                out.writestr('archive-receipt.json', json.dumps({'mode': mode, 'files': archive_hashes, 'fixture': data['fixture_mode']}))
            with zipfile.ZipFile(temporary) as check:
                if check.testzip() or any(hashlib.sha256(check.read(name)).hexdigest() != sha for name, sha in archive_hashes.items()):
                    raise ValueError('repair archive verification failed')
            os.replace(temporary, archive)
            # Recheck each file against the reviewed inventory immediately before removal.
            for p in candidates:
                name = p.relative_to(root).as_posix()
                if p.is_symlink() or not p.resolve().is_relative_to(root) or digest(p) != artifacts[name]:
                    raise ValueError('cleanup candidate changed or escaped run: ' + name)
            invalidate(data, 'cleanup removed rebuildable or explicitly disposable artifacts')
            data['retention'] = {'mode': mode, 'archive': str(archive), 'archive_hash': digest(archive),
                                 'accepted_artifacts': artifacts, 'files': archive_hashes,
                                 'capabilities': 're-extract all rows without generation' if mode == 'repair' else 'identity and receipts retained; deleted row sources require new generation',
                                 'prior_rollout_images_removed': False}
            save(root, data)
            for p in candidates:
                p.unlink()
        finally:
            temporary.unlink(missing_ok=True)
    return {'mode': mode, 'apply': apply, 'remove': [str(p) for p in candidates],
            'archive_files': sorted(names), 'preserved': 'user inputs, canonical reference, prompts, receipts, installed packages and unrelated files',
            'lost_capabilities': [] if mode == 'repair' else ['re-extraction of deleted row sources without regenerating them'],
            'rollout_image_payloads_removed': False}


def _review_ok(root, data):
    try:
        require_review(root, data)
        return True
    except (ValueError, OSError, KeyError, TypeError):
        return False


def status(root, data):
    reconcile(root, data)
    try:
        require_valid(root, data)
        valid = True
    except (ValueError, OSError, KeyError, TypeError):
        valid = False
    reviewed = valid and _review_ok(root, data)
    packaged = False
    if reviewed and data.get('gates', {}).get('packaged'):
        p = data['gates']['packaged']
        try:
            packaged = digest(Path(p['path']) / 'receipt.json') == p['receipt_hash'] and verify_package(Path(p['path']))['artifacts'] == artifact_hashes(root)
        except (ValueError, OSError, KeyError, TypeError):
            pass
    state = 'packaged' if packaged else 'reviewed' if reviewed else 'validated' if valid else 'generated' if all(j['status'] == 'generated' for j in data['jobs']) else 'running' if any(j['status'] == 'running' for j in data['jobs']) else 'failed' if any(j['status'] == 'failed' for j in data['jobs']) else 'pending'
    if data.get('operation', {}).get('status') in ('running', 'failed'):
        state = data['operation']['status']
    installed = False
    installation = data.get('gates', {}).get('installed')
    if packaged and installation:
        try:
            target = Path(installation['destination'])
            installed = digest(target / 'receipt.json') == installation['receipt_hash'] and bool(verify_package(target))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    result = {'run_id': data['run_id'], 'fixture': data['fixture_mode'], 'status': state, 'jobs': data['jobs'],
              'validated': valid, 'reviewed': reviewed, 'packaged': packaged, 'installed': installed,
              'base_reviewed': base_review_current(root, data), 'operation': data.get('operation'),
              'artifact_hashes': artifact_hashes(root), 'app_integration_verified': False,
              'retention': data.get('retention')}
    result['limits'] = data.get('limits', {})
    result['resolved_style'] = read(root / 'pet_request.json').get('resolved_style')
    result['style_warnings'] = style_warnings(root)
    result['claims'] = [{'job': j['id'], 'token': j['token'], 'age_seconds': round(time.time() - j['started_at']),
        'state': 'stale-awaiting-reconciliation' if j.get('stale') else 'active-awaiting-result',
        'stale_reason': j.get('stale_reason'),
        'recovery': 'ingest matching selected output or fail this exact token; do not start another controller'}
        for j in data['jobs'] if j['status'] == 'running']
    atomic(root / 'run-summary.json', result)
    return result


def resume(root, data):
    if data.get('pending_install'):
        destination = Path(data['pending_install']['destination']).resolve()
        with ownership(destination):
            recovered = recover_install(destination)
        if recovered:
            if recovered.get('recovered') == 'committed':
                data.setdefault('gates', {})['installed'] = recovered
            event(data, 'install-recovered', **recovered)
        data.pop('pending_install', None)
        save(root, data)
    if data.get('operation', {}).get('status') == 'running':
        data['operation'].update(status='interrupted', ended_at=time.time())
        event(data, 'operation-interrupted', operation=dict(data['operation']))
        save(root, data)
    result = status(root, data)
    result['next'] = 'Reconcile each running token with ingest or fail. Claim pending/failed jobs within the retry bound. Validate rebuilds missing or stale deterministic artifacts. No generation is launched automatically.'
    return result


def configure_limits(root, data, args):
    import math
    value = dict(data.get('limits', {}))
    for argument, key, maximum in (('max_attempts', 'attempts_per_job', 100), ('concurrency', 'concurrency', 16), ('max_calls', 'generation_calls', 100000)):
        supplied = getattr(args, argument, None)
        if supplied is not None:
            if type(supplied) is not int or not 1 <= supplied <= maximum:
                raise ValueError(argument + f' must be 1..{maximum}')
            value[key] = supplied
    for argument, key in (('estimate_units', 'estimated_units_per_call'), ('unit_budget', 'unit_budget')):
        supplied = getattr(args, argument, None)
        if supplied is not None:
            if not math.isfinite(supplied) or supplied <= 0:
                raise ValueError(argument + ' must be positive and finite')
            value[key] = supplied
    if value.get('unit_budget') is not None and not value.get('estimated_units_per_call'):
        raise ValueError('--unit-budget needs --estimate-units in user-defined units; no price is inferred')
    data['limits'] = value
    event(data, 'limits-configured', limits=value)
    save(root, data)
    return {'limits': value, 'measurements': 'claims count includes failed and unresolved calls; monetary usage is unavailable'}


def annotate(root, data, name, path):
    job = job_for(data, name)
    if name == 'base' or job['status'] != 'generated':
        raise ValueError('annotations require a current state source')
    from extraction_annotations import annotated_source
    from extract_strip_frames import mask_background, load_extraction_config
    value = read(path)
    config = load_extraction_config(root)
    with Image.open(root / job['output_path']) as image:
        masked, _ = mask_background(image, tuple(config['chroma_key']), config['key_threshold'], config['background'], config['holes'], config['edge_cleanup'])
    count = next(s['frames'] for s in load_profile(root / 'profile.json')['states'] if s['id'] == name)
    try:
        annotated_source(masked, count, value, digest(root / job['output_path']))
    except ValueError as exc:
        from extraction_annotations import diagnostic
        diagnostic(masked, count, root / 'qa/extraction-diagnostics', name, exc, value)
        raise
    invalidate(data, 'extraction annotation changed for ' + name)
    atomic(root / 'annotations' / (name + '.json'), value)
    event(data, 'annotation-recorded', state=name, sha256=digest(root / 'annotations' / (name + '.json')))
    save(root, data)
    return {'annotation': name, 'generation_reused': True, 'visual_review_required': True}


def calibrate(root, data, path, apply=False):
    from statistics import median
    from types import SimpleNamespace
    from extraction_annotations import rectangle
    value = read(path)
    if not isinstance(value, list) or not value:
        raise ValueError('calibration needs a list of measured source body rectangles')
    ratios = []
    for measure in value:
        job = job_for(data, measure['state'])
        source = root / job['output_path']
        if measure.get('source_sha256') != digest(source):
            raise ValueError('calibration measurement source hash is stale')
        box = rectangle(measure['body_rect'], check_image(source))
        target = measure['target_height']
        if type(target) not in (int, float) or not 0 < target <= 4096:
            raise ValueError('calibration target height must be 0..4096 pixels')
        ratios.append(target / (box[3] - box[1]))
    scale = median(ratios)
    if max(ratios) / min(ratios) > 1.05:
        raise ValueError('measured body scales disagree by more than 5%; inspect source resolution and measurements')
    if apply:
        configure(root, data, SimpleNamespace(shared_scale=scale, chroma_key=None))
    return {'shared_scale': scale, 'measurements': value, 'applied': apply,
            'note': 'One common pixel multiplier. Configure profile scale_policy shared; viewport clipping is still rejected.'}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'rig':
        from rig import main as rig_main
        raise SystemExit(rig_main(sys.argv[2:]))
    from decomposition_cli import COMMANDS as decomposition_commands, add_arguments as decomposition_arguments
    if len(sys.argv) > 1 and sys.argv[1] == 'states':
        from state_catalog import main as catalog_main
        raise SystemExit(catalog_main(sys.argv[2:]))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['states', 'prepare', 'status', 'claim', 'ingest', 'import-source', 'base-review', 'fail', 'resume', 'repair', 'configure', 'revise-profile', 'derive', 'validate', 'review-template', 'review-ui', 'review', 'export', 'package', 'install', 'rollback', 'cleanup', 'doctor', 'capabilities', 'limits', 'annotate', 'calibrate', 'archive', 'restore', 'migrate', 'styles', 'style', 'style-modifiers', *decomposition_commands])
    p.add_argument('--run-dir', help='Required for lifecycle commands; omit for doctor, capabilities, state discovery and style discovery')
    p.add_argument('--search', default='', help='Style keyword, name or category search')
    p.add_argument('--category', help='Filter styles by stable category ID')
    p.add_argument('--json', action='store_true', help='Machine-readable style discovery with complete metadata')
    p.add_argument('--style-modifier', action='append', default=[], help='Optional thematic modifier; repeat up to four times')
    p.add_argument('--resampling', choices=['nearest', 'bilinear', 'bicubic', 'lanczos'], help='Configure extraction fallback; explicit per-state profile filters take precedence')
    for flag in ('job', 'token', 'source', 'receipt', 'destination', 'package-path', 'out'):
        p.add_argument('--' + flag)
    p.add_argument('--note', default='')
    for flag in ('test-destination', 'fixture-mode', 'fixture', 'replace-inputs', 'apply', 'approve-mirror', 'accept-color-warning'):
        p.add_argument('--' + flag, action='store_true')
    p.add_argument('--mode', choices=['repair', 'minimal-delivery'], default='repair')
    p.add_argument('--pet-name', default='')
    p.add_argument('--pet-notes', default='')
    for flag in ('profile', 'state-selection', 'constraints', 'identity', 'reference-roles', 'brand-discovery-file', 'brand-name', 'brand-brief', 'chroma-key', 'style-preset', 'style-notes', 'description', 'pet-id', 'display-name'):
        p.add_argument('--' + flag)
    p.add_argument('--background', choices=['auto', 'alpha', 'chroma', 'solid', 'keep'])
    p.add_argument('--holes', choices=['preserve', 'key'])
    p.add_argument('--shared-scale', type=float)
    p.add_argument('--key-threshold', type=float)
    p.add_argument('--max-attempts', type=int)
    p.add_argument('--concurrency', type=int)
    p.add_argument('--max-calls', type=int)
    p.add_argument('--estimate-units', type=float)
    p.add_argument('--unit-budget', type=float)
    for flag in ('reference', 'avoid', 'require', 'brand-source'):
        p.add_argument('--' + flag, action='append', default=[])
    decomposition_arguments(p)
    args = p.parse_args()
    required = {
        'claim': ('job',), 'ingest': ('job', 'source', 'token', 'receipt'),
        'import-source': ('job', 'receipt'), 'fail': ('job', 'token'), 'repair': ('job',),
        'derive': ('job',), 'annotate': ('job', 'receipt'), 'calibrate': ('receipt',),
        'revise-profile': ('profile',), 'review-ui': ('destination',), 'review': ('receipt',),
        'export': ('destination',), 'package': ('destination',), 'archive': ('destination',),
        'restore': ('source',), 'install': ('package_path', 'destination')}
    for name in required.get(args.command, ()):
        if not getattr(args, name):
            p.error(args.command + ' requires --' + name.replace('_', '-'))
    if args.command in ('styles', 'style', 'style-modifiers'):
        from style_registry import discover, format_discovery
        try:
            result = discover(args.command, args.search, args.category, args.style_preset,
                              load_profile(args.profile) if args.profile else None, args.background or 'auto')
            if args.out:
                atomic(Path(args.out).expanduser().resolve(), result)
            print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else format_discovery(args.command, result))
        except (ValueError, OSError, TypeError) as exc:
            p.error(str(exc))
        return
    if args.command in ('doctor', 'capabilities'):
        from doctor import doctor, capabilities
        result = capabilities() if args.command == 'capabilities' else doctor(args.destination, args.profile)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result.get('ok', True) else 1)
    if not args.run_dir:
        p.error('--run-dir is required for ' + args.command)
    requested_root = Path(args.run_dir).expanduser()
    root = (requested_root.parent.resolve() / requested_root.name
            if args.command == 'restore' else requested_root.resolve())
    try:
        if args.command == 'restore' and (root.exists() or root.is_symlink()):
            raise ValueError('restore destination must not exist; use a new directory')
        if args.out and (Path(args.out).resolve() == root or Path(args.out).resolve().is_relative_to(root)):
            raise ValueError('--out must be outside the run to avoid changing reviewed artifacts')
        with ownership(root, resolve_leaf=args.command != 'restore'):
            # A restore requires a fresh destination; it must not recover or mutate
            # an unrelated prepare transaction before validating its archive.
            if args.command != 'restore':
                recover_prepare(root)
            if args.command in decomposition_commands:
                from decomposition_cli import dispatch
                result = dispatch(root, args)
            elif args.command == 'restore':
                from portable_runs import restore_run
                result = restore_run(args.source, root)
            elif args.command == 'migrate':
                from portable_runs import migrate
                result = migrate(root)
            elif args.command == 'prepare':
                result = prepare(root, args)
            else:
                data = manifest(root)
                if args.command == 'status': result = status(root, data)
                elif args.command == 'resume': result = resume(root, data)
                elif args.command == 'claim': result = claim(root, data, args.job)
                elif args.command == 'ingest': result = ingest(root, data, args.job, args.source, args.token, args.note, args.receipt, args.fixture)
                elif args.command == 'import-source':
                    from source_interface import import_source
                    result = import_source(root, data, args.job, args.receipt, args.note)
                elif args.command == 'base-review': result = record_base_review(root, data, args.note, args.accept_color_warning)
                elif args.command == 'fail': result = fail(root, data, args.job, args.token, args.note)
                elif args.command == 'repair': result = repair(root, data, args.job, args.note)
                elif args.command == 'configure': result = configure(root, data, args)
                elif args.command == 'revise-profile': result = revise_profile(root, data, args.profile)
                elif args.command == 'derive': result = derive(root, data, args.job, args.note, args.approve_mirror)
                elif args.command == 'validate': result = validate(root, data)
                elif args.command == 'review-template': result = review_template(root, data)
                elif args.command == 'review-ui':
                    from review_ui import build_review
                    result = build_review(root, data, args.destination)
                elif args.command == 'archive':
                    from portable_runs import archive_run
                    result = archive_run(root, data, args.destination)
                elif args.command == 'limits': result = configure_limits(root, data, args)
                elif args.command == 'annotate': result = annotate(root, data, args.job, args.receipt)
                elif args.command == 'calibrate': result = calibrate(root, data, args.receipt, args.apply)
                elif args.command == 'review': result = review(root, data, args.receipt)
                elif args.command == 'export': result = export(root, data, args.destination)
                elif args.command == 'package': result = package(root, data, args.destination)
                elif args.command == 'install':
                    if not args.destination or not args.package_path: raise ValueError('install needs --destination and --package-path')
                    with ownership(Path(args.destination).resolve()):
                        result = install(root, data, args.package_path, args.destination, args.test_destination)
                elif args.command == 'rollback': result = rollback(root, data)
                elif args.command == 'cleanup': result = cleanup(root, data, args.mode, args.apply)
            if args.out:
                out = Path(args.out).resolve()
                atomic(out, result)
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError, StopIteration) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    main()
