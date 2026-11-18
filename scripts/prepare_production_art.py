#!/usr/bin/env python3
"""Prepare auditable imagegen acquisition runs for the two-character pilot.

This script prepares claims; it never calls a model or fabricates receipts.
Use the installed imagegen skill with every claimed prompt and attachment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import hatch
from profile_spec import load_profile


CHARACTERS = {
    'milo': {
        'name': 'Milo',
        'description': 'a small warm amber bear with rounded ears, cream muzzle and belly, dark brown eyes, short soft limbs and a coral neckerchief whose knot stays on the viewer-right in the front view',
        'palette': {'fur': '#C88B47', 'cream': '#F3DCB4', 'scarf': '#D96654', 'eyes': '#392B26'},
        'markings': ['cream muzzle and belly', 'small dark brown nose'],
        'asymmetries': ['coral scarf knot on viewer-right in the front view'],
        'props': ['coral neckerchief'],
    },
    'luma': {
        'name': 'Luma',
        'description': 'a slender teal rabbit with long upright ears and cream inner ears, cream muzzle and belly, dark brown eyes, soft paws and an ochre waistcoat with two small buttons',
        'palette': {'fur': '#548F8B', 'cream': '#E5DDC2', 'waistcoat': '#C69540', 'eyes': '#302D2B'},
        'markings': ['cream inner ears and muzzle', 'two small waistcoat buttons'],
        'asymmetries': [],
        'props': ['ochre waistcoat'],
    },
}

STYLE = ('Polished storybook cut-paper animation artwork, softly painted fur texture, '
         'clean readable silhouettes, rounded anatomy, warm light from upper left. '
         'True transparent RGBA background. No floor, shadow outside the character, scenery, text, labels, grid lines, watermark or unrelated props.')

PARTS = ('Create ONE animation construction sheet in an exact 4-column by 4-row grid of equally sized invisible cells. '
    'Every separate item is centered with generous transparent margins within its own cell; nothing crosses a cell boundary. '
    'Front view only, consistent scale and palette. These are reusable drawings, not sixteen complete characters. '
    'Row 1 left to right: torso with costume, no head or arms or legs; whole head including ears and muzzle and nose but NO eyes, eyebrows or mouth; '
    'viewer-left upper arm ending at wrist, no hand; viewer-right upper arm ending at wrist, no hand. '
    'Row 2: viewer-left open paw; viewer-right open paw; viewer-left entire short leg with foot; viewer-right entire short leg with foot. '
    'Row 3: a pair of open eyes only; a pair of closed blinking eyes only; a pair of raised friendly eyebrows only; a pair of neutral eyebrows only. '
    'Row 4: closed neutral smiling mouth only; open A speaking mouth only; E speaking mouth only; round O speaking mouth only. '
    'Do not include any eye socket, background patch or duplicate muzzle around facial drawings. '
    'Torso and limbs have full rounded overlapping joint ends for a cutout rig. Keep the torso roughly 220px high and head roughly 210px wide within a cell, '
    'except the rabbit head with ears can be tall. All four mouth shapes share the same center and scale, all eye pairs share the same spacing. '
    'Preserve identity, costume details and upper-left lighting from the attached canonical four-view reference. '
    'The second attachment is the required overall layout safety guide: keep all artwork inside the square; use the explicit 4x4 arrangement described here, and do not copy visible guide marks.')


def _require_unused(path: Path):
    # exists() alone misses dangling symlinks. Never resolve a leaf before this
    # check: that would silently turn a linked destination into a different run.
    if path.exists() or path.is_symlink():
        raise ValueError('acquisition path already exists; choose unused paths: ' + str(path))


def _write_new_input(path: Path, value: dict, created: list):
    # Exclusive creation also refuses a file/link created after the preflight.
    with path.open('x', encoding='utf-8') as output:
        created.append((path, os.fstat(output.fileno())))
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def prepare(root: Path, character: str):
    root = Path(root)
    _require_unused(root)
    root = root.resolve()
    profile_path = root.parent / (character + '-profile.json')
    identity_path = root.parent / (character + '-identity.json')
    transaction_path = root.parent / ('.' + root.name + '.prepare.json')
    paths = (root, profile_path, identity_path, transaction_path)
    if len(set(paths)) != len(paths):
        raise ValueError('acquisition destination and input paths must be distinct')
    for path in paths:
        _require_unused(path)
    spec = CHARACTERS[character]
    profile = load_profile({
        'schema_version': 1, 'id': character + '-construction-v3',
        'layout': {'columns': 1, 'cell_width': 1024, 'cell_height': 1024},
        'states': [{'id': 'parts', 'frames': 1, 'durations': [1000], 'purpose': 'One layered construction sheet for manual hash-bound part extraction',
                    'motion': 'static', 'generation': {'instructions': PARTS}}],
        'generation': {'instructions': 'Create the requested original character design and construction artwork. ' + STYLE},
        'validation': {'require_provenance': True, 'allow_detached_details': True},
        'export': {'formats': ['frames'], 'static_state': 'parts', 'static_frame': 0},
    })
    identity = {'schema_version': 1, 'markings': spec['markings'],
        'palette': spec['palette'], 'materials': ['softly painted fur', 'matte woven costume'],
        'proportions': {'head_to_body_height': 0.6}, 'approved_props': spec['props'],
        'asymmetries': spec['asymmetries'], 'directions': {'parts': 'front'},
        'forbid_mirror': True, 'unresolved_conflicts': []}
    args = argparse.Namespace(replace_inputs=False, fixture_mode=False, profile=str(profile_path),
        identity=str(identity_path), pet_name=spec['name'],
        pet_notes=spec['description'] + '. Base image: a design turnaround with four full-body views arranged left to right: front, three-quarter, side, back. Consistent relative scale. No text.',
        reference=[], avoid=['text', 'background', 'unrelated props'],
        require=['original non-fixture production artwork', 'retain asymmetry and costume', 'true transparency'],
        brand_source=[], background='alpha', style_preset='painterly', style_notes=STYLE)
    root.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        _write_new_input(profile_path, profile, created)
        _write_new_input(identity_path, identity, created)
        # A destination or recovery journal may have appeared while reserving
        # the inputs. Keep it untouched and let the caller choose another run.
        _require_unused(root)
        _require_unused(transaction_path)
        result = hatch.prepare(root, args)
        with hatch._journal_admission(root):
            data = hatch.manifest(root)
            data['limits'].update(attempts_per_job=1, concurrency=1, generation_calls=2)
            hatch._save_admitted(root, data)
        return result
    except BaseException:
        # Only remove inputs exclusively created here, and leave any replacement
        # alone. hatch.prepare owns recovery of its separate run transaction.
        for path, owned in reversed(created):
            try:
                if os.path.samestat(path.lstat(), owned):
                    path.unlink()
            except FileNotFoundError:
                pass
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--character', choices=CHARACTERS, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.destination, args.character), indent=2))
