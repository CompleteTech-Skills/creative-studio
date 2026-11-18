"""Explicit imported-art interface. This module never calls an image model."""
from pathlib import Path
from PIL import Image
from asset_contracts import check_image, sha, strict_keys, text, MAX_SOURCE_PIXELS


def import_source(root, data, name, descriptor_path, note):
    import hatch
    root = Path(root).resolve()
    hatch.reconcile(root, data)
    job = hatch.job_for(data, name)
    if job['status'] == 'running':
        raise ValueError('reconcile the active generation token before importing; no claim is replaced')
    if job['status'] == 'generated':
        raise ValueError('repair the existing source before importing its replacement')
    descriptor_path = Path(descriptor_path).resolve()
    spec = hatch.read(descriptor_path)
    strict_keys(spec, {'schema_version', 'kind', 'layout', 'files', 'origin', 'fixture'}, 'source descriptor')
    if type(spec.get('schema_version')) is not int or spec['schema_version'] != 1 or spec.get('kind') != 'import':
        raise ValueError('source descriptor requires schema_version 1 and kind import')
    text(spec.get('origin'), 'source origin/attribution')
    text(note, 'import inspection note')
    if spec.get('fixture', False) is not data['fixture_mode']:
        raise ValueError('source fixture label must match the run; never import fixtures as production artwork')
    layout = spec.get('layout')
    if layout not in ('image', 'strip', 'frames'):
        raise ValueError('unsupported source layout; use image (base), strip, or ordered frames')
    files = spec.get('files')
    if not isinstance(files, list) or not files or any(not isinstance(p, str) for p in files):
        raise ValueError('source files must be a nonempty ordered path list')
    profile = hatch.load_profile(root / 'profile.json')
    count = 1 if name == 'base' else next(s['frames'] for s in profile['states'] if s['id'] == name)
    if (name == 'base' and layout != 'image') or (name != 'base' and layout == 'image'):
        raise ValueError('base requires image layout; states require strip or frames layout')
    if len(files) != (count if layout == 'frames' else 1):
        raise ValueError('source file count does not match layout and profile frame count')
    if name != 'base' and not hatch.base_review_current(root, data):
        raise ValueError('inspect and record base-review before importing state artwork')
    paths = [(descriptor_path.parent / p).resolve() for p in files]
    for path in paths:
        if path == root or path.is_relative_to(root):
            raise ValueError('import originals must be outside the run; internal outputs are not original sources')
    sizes = [check_image(p) for p in paths]
    if layout == 'frames' and len(set(sizes)) != 1:
        raise ValueError('imported frames must share one viewport size; no per-pose resizing is performed')
    if layout == 'frames' and sizes[0][0] * count * sizes[0][1] > MAX_SOURCE_PIXELS:
        raise ValueError('combined frame source exceeds source pixel bound')
    if layout == 'frames':
        image = Image.new('RGBA', (sizes[0][0] * count, sizes[0][1]))
        for index, path in enumerate(paths):
            with Image.open(path) as frame:
                image.paste(frame.convert('RGBA'), (index * sizes[0][0], 0))
    else:
        with Image.open(paths[0]) as frame:
            image = frame.convert('RGBA')
    receipt = {'schema_version': 1, 'kind': 'import', 'layout': layout, 'origin': spec['origin'],
               'fixture': data['fixture_mode'], 'descriptor_sha256': sha(descriptor_path), 'note': note,
               'files': [{'origin_path': str(p), 'sha256': sha(p)} for p in paths]}
    hatch.invalidate(data, 'explicit artwork import ' + name)
    hatch._save_admitted(root, data)
    target = root / job['output_path']
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.import.tmp')
    image.save(temporary, format='PNG')
    hatch.os.replace(temporary, target)
    if name == 'base':
        hatch.shutil.copy2(target, root / 'references/canonical-base.png')
        hatch.select_base(root, data, source_origin='import')
        job['canonical_hash'] = sha(root / 'references/canonical-base.png')
    for key in ('generation_receipt', 'derivation_receipt', 'derived_from', 'source_path', 'source_hash', 'model_call', 'generated_at', 'qa_note'):
        job.pop(key, None)
    job.update(import_receipt=receipt, source_origin='import')
    job.update(status='generated', input_hashes=hatch.inputs(root, job), output_hash=sha(target))
    job['history'].append({'event': 'imported', 'time': hatch.time.time(), 'receipt': receipt,
                           'output_hash': job['output_hash']})
    hatch._save_admitted(root, data)
    return {'imported': name, 'origin': receipt['origin'], 'fixture': data['fixture_mode'],
            'imagegen_output': False, 'visual_review_required': True}
