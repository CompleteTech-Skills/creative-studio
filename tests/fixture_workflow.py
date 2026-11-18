"""Deterministic gate-test fixtures; these shapes are never production artwork."""
import argparse
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hatch
from profile_spec import load_profile


def small_profile():
    return load_profile({
        'schema_version': 1, 'id': 'fixture-v1',
        'layout': {'columns': 3, 'cell_width': 96, 'cell_height': 96},
        'states': [
            {'id': state, 'purpose': purpose, 'frames': 3, 'durations': [40, 70, 90],
             'motion': motion}
            for state, purpose, motion in [('rest', 'Quiet idle', 'quiet'), ('jump', 'Rise and land', 'vertical')]
        ],
        'export': {'formats': ['frames', 'atlas', 'gif', 'webp', 'browser'], 'static_state': 'rest', 'static_frame': 1},
        'events': {'ready': 'rest', 'success': 'jump'}})


def fixture_strip(spec, size):
    width, height = size
    image = Image.new('RGBA', (spec['frames'] * width, height))
    draw = ImageDraw.Draw(image)
    for index in range(spec['frames']):
        x = index * width + width // 2
        y = height // 3 - (index % 3) * 7 if spec['motion'] == 'vertical' else height // 3
        draw.rounded_rectangle((x - 18, y, x + 18, y + 44), radius=8, fill=(180, 60, 40, 255))
        draw.rectangle((x + 17, y + 18, x + 22 + index * 3, y + 23), fill=(180, 60, 40, 255))
        draw.rectangle((x - 8, y + 10, x - 4, y + 15), fill=(15, 20, 30, 255))
    return image


def ingest_fixture(root, data, name, source):
    handoff = hatch.claim(root, data, name)
    receipt = root.parent.resolve() / ('fixture-receipt-' + name + '.json')
    hatch.atomic(receipt, handoff['receipt_template'])
    return hatch.ingest(root, data, name, source, handoff['token'],
                        'Synthetic fixture for deterministic pipeline tests only.', receipt, True)


def build_run(root, profile=None, validate=True):
    root = Path(root)
    profile = load_profile(profile or small_profile())
    profile_path = root.parent / 'fixture-profile.json'
    hatch.atomic(profile_path, profile)
    args = argparse.Namespace(replace_inputs=False, fixture_mode=True, profile=str(profile_path),
                              pet_name='Fixture', pet_notes='Deterministic test fixture',
                              reference=[], avoid=['glitter'], require=['red body'], brand_source=[],
                              style_preset='clay', style_notes='simple rounded surfaces')
    hatch.prepare(root, args)
    sources = root.parent / 'fixture-sources'
    sources.mkdir(exist_ok=True)
    base_spec = {'frames': 1, 'motion': 'quiet'}
    base = sources / 'base.png'
    fixture_strip(base_spec, (96, 96)).save(base)
    data = hatch.manifest(root)
    ingest_fixture(root, data, 'base', base)
    hatch.record_base_review(root, data, 'Fixture calibration checkpoint; no production imagery.', True)
    for spec in profile['states']:
        source = sources / (spec['id'] + '.png')
        fixture_strip(spec, (profile['layout']['cell_width'], profile['layout']['cell_height'])).save(source)
        ingest_fixture(root, data, spec['id'], source)
    if validate:
        hatch.validate(root, data)
    return data


def record_fixture_review(root, data, verdict='pass'):
    receipt = hatch.review_template(root, data)
    receipt['reviewer'] = 'deterministic fixture gate test (not a production visual reviewer)'
    receipt['evidence'] = {'canonical_reference_viewed': True, 'contact_sheet_viewed': True,
                           'playback_states': list(receipt['rows'])}
    for row in receipt['rows'].values():
        row.update(verdict=verdict, notes='Fixture receipt exercises the gate only; makes no production artwork claim.')
        for dimension in row['dimensions'].values():
            dimension.update(verdict=verdict, notes='Synthetic gate exercise only; not a visual or semantic certification.')
        row['warning_notes'] = {key: 'Synthetic warning handling exercise; no production judgment.' for key in row['warning_notes']}
    path = root.parent / 'fixture-visual-review.json'
    hatch.atomic(path, receipt)
    return hatch.review(root, data, path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--profile')
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run = output / 'run'
    data = build_run(run, args.profile)
    record_fixture_review(run, data)
    hatch.export(run, data, output / 'export')
    hatch.package(run, data, output / 'package')
    hatch.atomic(output / 'fixture-report.json', hatch.status(run, data))
    print(json.dumps({'fixture': True, 'run': str(run), 'preview': str(output / 'export/preview.html'),
                      'production_artwork': False, 'installed': False}, indent=2))
