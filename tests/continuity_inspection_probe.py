"""Retain source-bound local GLB/glTF inspection receipts for procedural fixtures."""
import argparse
import hashlib
import io
import json
from pathlib import Path

from PIL import Image

from test_rig_continuity_inspection import gltf, pointer
from continuity_contracts import canonical_bytes
from continuity_inspection import compare_assets, inspect_asset
from rig_errors import RigError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='new directory for fixture files and receipts')
    root = parser.parse_args().output
    root.mkdir()
    base, _ = gltf(root, 'baseline.glb', skinned=True)
    changed, _ = gltf(root, 'geometry-change.glb', width=1.5, skinned=True)
    recolored, _ = gltf(root, 'texture-change.glb', color=(200, 20, 10), skinned=True)
    external, _ = gltf(root, 'external.gltf', external=True, skinned=True)
    external_texture = root / 'texture.png'
    one, builder = gltf(root, 'capacity-one.glb')
    image = io.BytesIO()
    Image.new('RGBA', (512, 512), (20, 40, 60, 255)).save(image, format='PNG')
    header = dict(mimeType='image/png', bufferView=builder.view(image.getvalue()))
    builder.json['images'] = [header]
    builder.write(one)
    builder.json['images'].append(header)
    two = root / 'capacity-two.glb'
    builder.write(two)

    fixture_paths = sorted(root.iterdir())
    original = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture_paths}
    baseline = dict(source=pointer(root, base))
    measured = inspect_asset(
        root, **baseline,
        landmarks={'bottom': dict(mesh_id='mesh-1-0', vertex_index=0),
                   'top': dict(mesh_id='mesh-1-0', vertex_index=2)},
        distances={'height': ['bottom', 'top']},
    )
    geometry = compare_assets(root, baseline, dict(source=pointer(root, changed)))
    material = compare_assets(root, baseline, dict(source=pointer(root, recolored)))
    external_request = dict(source=pointer(root, external),
                            dependencies=[pointer(root, external_texture, kind='texture')])
    exact = inspect_asset(root, **external_request)
    try:
        inspect_asset(root, external_request['source'])
    except RigError as exc:
        missing = exc.report
    else:
        raise AssertionError('the external closure was accepted without its explicit texture pin')
    assert missing['code'] == 'RIG_MISSING_ASSET'
    assert missing['asset'] == 'texture.png'
    limits = dict(memory_bytes=8 * 1024**2)
    admitted = inspect_asset(root, pointer(root, one), limits=limits)
    try:
        inspect_asset(root, pointer(root, two), limits=limits)
    except RigError as exc:
        rejection = exc.report
    else:
        raise AssertionError('combined image capacity was incorrectly admitted')
    assert rejection['code'] == 'RIG_LIMIT_MEMORY'
    inspection = measured['inspection']
    assert inspection['fixture'] and not inspection['visual_approval']
    assert inspection['measurements']['distances']['height']['distance_m'] == 2
    assert inspection['skins']['mesh-1-0']['weights']['shape'] == [3, 4]
    assert admitted['inspection']['native_image_admission']['images'][0]['width'] == 512
    assert not geometry['geometry_identity'] and geometry['needs_visual_review']
    assert material['geometry_identity'] and not material['material_identity']
    assert original == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture_paths}

    receipts = {
        'inspection': measured,
        'geometry-comparison': geometry,
        'texture-comparison': material,
        'external-closure-inspection': exact,
        'missing-dependency-rejection': missing,
        'capacity-one-inspection': admitted,
        'capacity-two-rejection': rejection,
    }
    receipt_hashes = {}
    for name, value in receipts.items():
        payload = canonical_bytes(value) + b'\n'
        filename = name + '.json'
        (root / filename).write_bytes(payload)
        receipt_hashes[filename] = hashlib.sha256(payload).hexdigest()
    report = dict(
        status='passed-local-fixture-inspection', fixture=True, production_artwork_approved=False,
        sources_unchanged=original, receipt_sha256=receipt_hashes, software=inspection['software'],
        vertices=inspection['analysis']['vertices'], triangles=inspection['analysis']['triangles'],
        authored_skin_joints=len(inspection['skins']['mesh-1-0']['joints']),
        texture_bytes=inspection['textures'][0]['bytes'], combined_image_memory_limit=limits['memory_bytes'],
    )
    (root / 'report.json').write_bytes(canonical_bytes(report) + b'\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
