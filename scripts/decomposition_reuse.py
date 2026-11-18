"""Reuse observed decomposition evidence in the existing forward-generation pipeline.

Preparation imports an observed canonical image; it does not manufacture a rig,
hidden anatomy, new poses, animation frames, or provider-call receipts. Required
component references and their provenance travel with ordinary Hatch Pet jobs.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from asset_contracts import hash_object, reference_roles, sha, text, validate_identity
from decomposition_package import _copy_inventory, _inventory, _validated
from profile_spec import load_profile

OPERATIONS = {
    'animate': {'kind': 'new-artwork', 'purpose': 'Animate the imported character using its observed anatomy and visual identity.'},
    'wave': {'kind': 'new-artwork', 'purpose': 'Make a small wave with an appropriate observed appendage; do not invent an arm or hand for a character that lacks one.'},
    'walk': {'kind': 'new-artwork', 'purpose': 'Create a walking or locomotion cycle appropriate to the character\'s observed anatomy.'},
    'pose': {'kind': 'new-artwork', 'purpose': 'Create the requested new pose while preserving all unchanged character traits.'},
    'expression': {'kind': 'new-artwork', 'purpose': 'Change the requested facial expression while preserving all unchanged character traits.'},
    'replace': {'kind': 'new-artwork', 'purpose': 'Replace only the selected component with the requested new artwork.'},
    'preserve-except': {'kind': 'new-artwork', 'purpose': 'Change only the selected components as explicitly described; preserve every other observed trait.'},
    'recolor': {'kind': 'layer-edit', 'purpose': 'Recolor only the selected component pixels as requested; preserve their alpha, geometry, and placement.'},
    'hide': {'kind': 'layer-edit', 'purpose': 'Hide only the selected components; do not fill pixels concealed behind them.'},
    'transform': {'kind': 'layer-edit', 'purpose': 'Adjust the selected layers\' position, rotation, or scale only as requested; do not invent concealed pixels.'},
    'reorder': {'kind': 'layer-edit', 'purpose': 'Change only the selected components\' front-to-back order as requested.'},
}
_TARGETED = {'replace', 'preserve-except', 'recolor', 'hide', 'transform', 'reorder'}
_MARKINGS = {'marking', 'markings', 'pattern', 'patterns', 'decoration', 'decorations', 'visual-detail', 'visual-details', 'detail', 'details'}
_PROPS = {'accessory', 'accessories', 'clothing', 'clothes', 'held-item', 'held-items', 'prop', 'props', 'equipment'}


def _unique(items):
    return list(dict.fromkeys(items))


def derive_identity(manifest, profile=None):
    """Describe measured visible traits; explicit identity annotations take priority.

    This prepopulation is metadata, not a semantic acceptance verdict. No colors
    are guessed when the analysis did not provide a measured palette, and no
    materials or hidden body parts are inferred from a component label.
    """
    extensions = manifest.get('extensions', {})
    provided = extensions.get('identity', {})
    if not isinstance(provided, dict):
        raise ValueError('decomposition extensions.identity must be a structured identity object')
    identity = copy.deepcopy(provided)
    identity.setdefault('schema_version', 1)
    for key in ('markings', 'materials', 'approved_props', 'asymmetries', 'unresolved_conflicts'):
        identity.setdefault(key, [])
    for key in ('proportions', 'palette', 'directions'):
        identity.setdefault(key, {})
    # Validate shape before extending so malformed annotations fail clearly.
    if not isinstance(identity['directions'], dict):
        raise ValueError('decomposition identity.directions must be an object')
    direction_profile = profile or {'states': [{'id': state} for state in identity['directions']]}
    identity = validate_identity(identity, direction_profile)
    palette = extensions.get('palette', manifest.get('palette', {}))
    if not isinstance(palette, dict):
        raise ValueError('decomposition palette must map names to #RRGGBB colors')
    for name, color in palette.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(color, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', color):
            raise ValueError('decomposition palette must map names to #RRGGBB colors')
        identity['palette'].setdefault(name, color.upper())
    components = [component for component in manifest.get('components', [])
                  if component.get('review', {}).get('status') != 'rejected']
    width, height = manifest['canvas']['width'], manifest['canvas']['height']
    for component in components:
        name, identifier = component['name'], component['id']
        left, top, right, bottom = component['bbox']
        identity['proportions'].setdefault(identifier + '_width_to_canvas', (right - left) / width)
        identity['proportions'].setdefault(identifier + '_height_to_canvas', (bottom - top) / height)
        category = component['category'].strip().lower().replace('_', '-')
        category_family = category.split('/', 1)[0]
        observed = component.get('provenance', {}).get('kind') != 'inferred'
        if observed and category_family in _MARKINGS:
            identity['markings'].append('Preserve observed ' + name + ' and its source-space placement.')
        if observed and category_family in _PROPS:
            identity['approved_props'].append(name)
        if component.get('side') in ('left', 'right'):
            identity['asymmetries'].append('Preserve the anatomical ' + component['side'] + ' designation and observed placement of ' + name + '.')
    for key in ('markings', 'approved_props', 'asymmetries'):
        identity[key] = _unique(identity[key])
    # A single view cannot establish that unobserved opposing sides are identical.
    # Only an explicit identity annotation can permit future mirror approval.
    identity['forbid_mirror'] = provided.get('forbid_mirror', True)
    return validate_identity(identity, direction_profile)


def operation_contract(operation, component_ids=(), note=''):
    name = operation or 'animate'
    if name not in OPERATIONS:
        raise ValueError('unsupported decomposition operation; choose ' + ', '.join(OPERATIONS))
    identifiers = list(component_ids)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError('decomposition component selections must not repeat')
    if name in _TARGETED and not identifiers:
        raise ValueError(name + ' requires at least one explicit --component target')
    if name in _TARGETED | {'pose', 'expression'}:
        text(note, name + ' operation note describing the requested change')
    return {'schema_version': 1, 'operation': name, 'kind': OPERATIONS[name]['kind'],
            'targets': identifiers, 'note': note, 'purpose': OPERATIONS[name]['purpose'],
            'requires_new_artwork': OPERATIONS[name]['kind'] == 'new-artwork',
            'hidden_geometry_available': False,
            'execution': 'Forward jobs prepared; no layer edit, new artwork, or animation has been produced.'}


def _reuse_profile(manifest, operation):
    """A generic asset contract; Codex's exact nine states require an explicit profile."""
    canvas = manifest['canvas']
    width, height = canvas['width'], canvas['height']
    shrink = min(1.0, 384 / max(width, height))
    cell_width, cell_height = max(16, round(width * shrink)), max(16, round(height * shrink))
    if operation['operation'] == 'animate':
        specs = [('new-state', 'Create the requested new character performance using the observed anatomy and source identity. This state has no existing animation frames.', 4, 'gesture')]
    elif operation['operation'] in ('wave', 'walk'):
        specs = [(operation['operation'], operation['purpose'], 6 if operation['operation'] == 'wave' else 8,
                  'gesture' if operation['operation'] == 'wave' else 'directional')]
    else:
        specs = [(operation['operation'], operation['purpose'], 1, 'static')]
    return load_profile({
        'schema_version': 2, 'id': 'decomposition-reuse-' + operation['operation'] + '-v1',
        'layout': {'columns': max(item[2] for item in specs), 'cell_width': cell_width, 'cell_height': cell_height},
        'states': [{'id': name, 'purpose': purpose, 'frames': count,
                    'durations': [140 if count > 1 else 1000] * count, 'motion': motion,
                    'alignment': {'resampling': 'nearest' if canvas['pixel_art'] else 'lanczos'},
                    'generation': {'instructions': purpose + (' Requested change: ' + operation['note'] if operation['note'] else '')},
                    'allowed_derivations': []} for name, purpose, count, motion in specs],
        'generation': {'instructions': 'Preserve the imported character\'s source artwork style, species or design, proportions, visible features, colors, clothing, accessories, and anatomical side labels.',
                       'requirements': ['Use the original identity reference and all selected component references.',
                                        'Newly exposed anatomy and new animation frames require actual new artwork and visual review; the static decomposition does not supply them.']},
        'validation': {'require_provenance': True, 'allow_detached_details': True},
        'export': {'formats': ['frames', 'atlas', 'gif', 'webp', 'browser'], 'static_state': specs[0][0],
                   'static_frame': 0, 'codex_compatible': False}, 'events': {}})


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def prepare_from_decomposition(root, destination, args=None, component_ids=(), operation=None, operation_note=''):
    """Create a complete normal run, import its base, and leave real review/calls pending."""
    import hatch
    from prepare_pet_run import regenerate_prompts
    from source_interface import import_source

    root = Path(root).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    args = args or argparse.Namespace()
    manifest, validation, accepted = _validated(root, bool(getattr(args, 'require_approved', False)))
    if manifest.get('review', {}).get('status') == 'rejected':
        raise ValueError('repair rejected components before reusing semantic decomposition metadata')
    if destination.exists() or destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('decomposition reuse requires a new run outside its source tree')
    selected_ids = list(component_ids)
    selected = []
    by_id = {component['id']: component for component in manifest['components']}
    for identifier in selected_ids:
        if identifier not in by_id:
            raise ValueError('unknown decomposition reference component: ' + str(identifier))
        component = by_id[identifier]
        if component['review']['status'] == 'rejected':
            raise ValueError('repair rejected component before reusing it: ' + identifier)
        selected.append(component)
    intent = operation_contract(operation, selected_ids, operation_note)
    profile_arg = getattr(args, 'profile', None)
    profile = load_profile(profile_arg) if profile_arg else _reuse_profile(manifest, intent)
    identity = validate_identity(_read(root / manifest['identity']['path']), profile)
    if getattr(args, 'identity', None):
        # Explicit supplied fields override measured prepopulation. Normal strict
        # identity validation and constraint conflict detection still apply.
        identity.update(_read(Path(args.identity).expanduser()))
        identity = validate_identity(identity, profile)
    fixture = manifest['source']['fixture']
    if getattr(args, 'fixture_mode', False) and not fixture:
        raise ValueError('forward reuse cannot change the decomposition source fixture label')
    original_references = list(getattr(args, 'reference', []))
    user_roles = reference_roles(_read(args.reference_roles) if getattr(args, 'reference_roles', None) else None, len(original_references))
    source_role = {'front': 'front', 'back': 'back', 'left': 'side', 'right': 'side'}.get(manifest['character']['orientation'], 'identity')
    source_note = 'Observed foreground of imported character; source SHA-256 ' + manifest['source']['sha256'] + '. Orientation: ' + manifest['character']['orientation'] + '. Semantic review: ' + manifest['review']['status'] + '.'
    references = [str(root / manifest['foreground']['path'])] + [str(root / component['asset']['path']) for component in selected] + original_references
    roles = [{'role': source_role, 'note': source_note}]
    roles += [{'role': 'edit-target' if intent['operation'] in _TARGETED else 'detail',
               'note': 'Component ' + component['id'] + ' (' + component['name'] + '), source-space bbox ' + json.dumps(component['bbox']) + '; ' + component['provenance']['kind'] + '; review ' + component['review']['status'] + '. Preserve declared anatomical side ' + component['side'] + '.'} for component in selected]
    roles += user_roles
    requirements = list(getattr(args, 'require', []))
    requirements.append('Preserve the source character\'s identity, proportions, facial features, palette, markings, clothing, accessories, and anatomical asymmetries except explicitly requested component changes.')
    requirements.append('Decomposition source and selected components are required provenance-tracked references; semantic status is ' + manifest['review']['status'] + '.')
    if intent['operation'] in _TARGETED:
        requirements.append('Only component IDs ' + ', '.join(selected_ids) + ' are approved change targets. Requested change: ' + intent['note'])
    elif intent['note']:
        requirements.append('Requested character operation: ' + intent['purpose'] + ' ' + intent['note'])
    requirements.append('Do not infer that static extracted layers contain all hidden anatomy or any unproduced animation frames. New artwork must use the installed imagegen workflow and receive fresh visual review.')
    inventory = _inventory(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with hatch.ownership(destination):
        if destination.exists():
            raise ValueError('forward run destination appeared during preparation; no existing data was replaced')
        workspace = Path(tempfile.mkdtemp(prefix='.decomposition-reuse-', dir=destination.parent))
        prepared = workspace / 'run'
        try:
            inputs = workspace / 'inputs'
            inputs.mkdir()
            hatch.atomic(inputs / 'identity.json', identity)
            hatch.atomic(inputs / 'profile.json', profile)
            hatch.atomic(inputs / 'roles.json', roles)
            canonical_descriptor = {'schema_version': 1, 'kind': 'import', 'layout': 'image',
                                    'files': [str(root / manifest['foreground']['path'])],
                                    'origin': 'Faithful observed character foreground imported from decomposition ' + manifest['character']['id'] + '; ' + manifest['source']['origin'] + '; original source SHA-256 ' + manifest['source']['sha256'],
                                    'fixture': fixture}
            hatch.atomic(inputs / 'base-source.json', canonical_descriptor)
            supplied = vars(args).copy()
            supplied.update(replace_inputs=False, fixture_mode=fixture, profile=str(inputs / 'profile.json'),
                            identity=str(inputs / 'identity.json'), reference_roles=str(inputs / 'roles.json'),
                            pet_name=getattr(args, 'pet_name', '') or manifest['character']['name'],
                            pet_id=getattr(args, 'pet_id', None) or manifest['character']['id'],
                            pet_notes=getattr(args, 'pet_notes', '') or ('Imported character ' + manifest['character']['name'] + '. Observed semantic components: ' + '; '.join(component['name'] + ' [' + component['category'] + ']' for component in manifest['components']) + '.'),
                            reference=references, require=requirements,
                            avoid=list(getattr(args, 'avoid', [])), brand_source=list(getattr(args, 'brand_source', [])),
                            background='alpha', style_preset=getattr(args, 'style_preset', None) or ('pixel' if manifest['canvas']['pixel_art'] else 'auto'),
                            style_notes=getattr(args, 'style_notes', None) or 'Preserve the source artwork style and original visual identity without redesign.')
            forward_args = argparse.Namespace(**supplied)
            hatch.prepare(prepared, forward_args)
            with hatch._journal_admission(prepared):
                snapshot = prepared / 'references' / 'decomposition'
                snapshot.mkdir()
                _copy_inventory(root, snapshot, inventory)
                snapshot_manifest, _, _ = _validated(snapshot)
                if snapshot_manifest != manifest:
                    raise ValueError('source decomposition changed during forward preparation')
                reference_receipts = [{'component_id': component['id'], 'asset_path': component['asset']['path'],
                                       'asset_sha256': component['asset']['sha256'], 'mask_sha256': component['mask']['sha256'],
                                       'source_sha256': component['provenance']['source_sha256'],
                                       'provenance': component['provenance'], 'review': component['review'],
                                       'bbox': component['bbox'], 'transform': component['transform'],
                                       'role': roles[index + 1]['role'], 'run_reference_index': index + 1}
                                      for index, component in enumerate(selected)]
                origin = {'schema_version': 1, 'kind': 'character-decomposition-reuse',
                          'character_id': manifest['character']['id'], 'source_sha256': manifest['source']['sha256'],
                          'decomposition_sha256': inventory['decomposition.json'], 'identity_sha256': inventory['identity.json'],
                          'snapshot_path': 'references/decomposition', 'snapshot_inventory_sha256': hash_object(inventory),
                          'snapshot_files': inventory, 'source_revision': manifest['revision'],
                          'semantic_status': manifest['review']['status'], 'accepted': accepted,
                          'fixture': fixture, 'source_origin': manifest['source']['origin'],
                          'selected_components': reference_receipts, 'operation': intent,
                          'canonical_foreground_sha256': manifest['foreground']['sha256'],
                          'validation': {'faithful_fidelity_passed': validation['faithful_fidelity_passed']},
                          'animation_ready': False, 'imagegen_output': False}
                request = hatch.read(prepared / 'pet_request.json')
                request['decomposition'] = origin
                for index, component in enumerate(selected, start=1):
                    request['references'][index]['decomposition_component'] = reference_receipts[index - 1]
                hatch.atomic(prepared / 'pet_request.json', request)
                regenerate_prompts(prepared, request, profile, request['extraction'], include_base=True)
                data = hatch.manifest(prepared)
                import_source(prepared, data, 'base', inputs / 'base-source.json',
                              'Imported hash-verified observed foreground without a model call. Source decomposition semantic status: ' + manifest['review']['status'] + '. Canonical visual inspection is still required.')
                base = hatch.job_for(data, 'base')
                base['import_receipt']['decomposition'] = origin
                base['input_hashes'] = hatch.inputs(prepared, base)
                hatch.event(data, 'decomposition-reused', decomposition_sha256=origin['decomposition_sha256'],
                            source_sha256=origin['source_sha256'], selected_components=selected_ids,
                            operation=intent['operation'], semantic_status=origin['semantic_status'])
                hatch._save_admitted(prepared, data)
                if any(getattr(args, flag, None) is not None for flag in ('max_attempts', 'concurrency', 'max_calls', 'estimate_units', 'unit_budget')):
                    hatch.configure_limits(prepared, data, args)
                # Normal generator metadata contains run_dir; move the fully prepared
                # transaction only after its normal schema and origin checks pass.
                for name in ('pet_request.json', 'imagegen-jobs.json'):
                    relocated = hatch._relocate(hatch.read(prepared / name), prepared, destination)
                    if name == 'imagegen-jobs.json':
                        hatch._save_admitted(prepared, relocated)
                    else:
                        hatch.atomic(prepared / name, relocated)
                data = hatch.manifest(prepared)
                # Relocating the run root changes metadata only when a historical path
                # points at staging; compute its current base dependency hashes again.
                base = hatch.job_for(data, 'base')
                base['input_hashes'] = hatch.inputs(prepared, base)
                hatch._save_admitted(prepared, data)
                if _inventory(root) != inventory:
                    raise ValueError('source decomposition changed during forward preparation')
                os.replace(prepared, destination)
        finally:
            shutil.rmtree(workspace)
    return {'prepared': str(destination), 'run_id': data['run_id'], 'base_source_origin': 'import',
            'source_sha256': manifest['source']['sha256'], 'selected_components': selected_ids,
            'semantic_status': manifest['review']['status'], 'fixture': fixture,
            'operation': intent, 'profile_id': profile['id'],
            'codex_conversion_requested': profile['export'].get('codex_compatible', False),
            'base_review_required': True, 'new_artwork_produced': False,
            'next': 'Inspect the imported canonical reference, record base-review, then claim the intended state through the existing imagegen workflow.'}
