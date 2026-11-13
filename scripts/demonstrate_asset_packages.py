#!/usr/bin/env python3
"""Retained exact-byte asset package engineering demonstration; no art approval."""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

from asset_library import AssetLibrary
from asset_library_contracts import canonical_bytes, seal
from asset_library_dependencies import DependencyGraph, locator
from asset_library_packages import (INDEX, create_package, restore_package, inspect_package,
    create_archive, restore_package_archive, create_transport, restore_transport)


def demonstrate(destination):
    destination = Path(destination).absolute()
    destination.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    sample = root / 'examples/asset-library-foundation'
    library = AssetLibrary(destination / 'original-library', create=True)
    manifest = json.loads((sample / 'manifest.json').read_text())
    sources = {f['path']: sample / f['path'] for f in manifest['files']}
    first = library.register(manifest, sources)
    revised = deepcopy(manifest); revised['version'] = '2.0.0'
    revised['metadata']['description'] = 'Same real triangle bytes, revised immutable metadata.'
    second = library.register(seal(revised), sources)
    graph = DependencyGraph({'local': library})
    package = destination / 'package'
    created = create_package(graph, [], package, package_id='exact-package-fixture',
        scopes={'scene-one': [locator('local', first)], 'scene-two': [locator('local', second)]},
        policy=dict(require_sources=True, require_license_evidence=True, intended_use='test'))
    restored = restore_package(package, destination / 'relocated' / 'from-directory')
    archive = destination / 'package.zip'; create_archive(package, archive)
    archive_restored = restore_package_archive(archive, destination / 'relocated' / 'from-zip')
    parts = destination / 'parts'; create_transport(package, parts, part_size=128)
    parts_restored = restore_transport(parts, destination / 'relocated' / 'from-parts')
    negative = destination / 'missing-payload-copy'; shutil.copytree(package, negative)
    blob = negative / 'blobs' / manifest['files'][0]['sha256']; exact = blob.read_bytes(); blob.unlink()
    missing = inspect_package(negative)
    assert missing['state'] == 'incomplete'
    blob.write_bytes(b'X' * len(exact)); substituted = inspect_package(negative)
    assert substituted['state'] == 'incomplete'
    blob.write_bytes(exact); repaired = inspect_package(negative)
    assert repaired['verified']
    for result in [restored, archive_restored, parts_restored]:
        actual = AssetLibrary(result['library'])
        assert actual.resolve(first) == manifest
        assert actual.resolve(second) == seal(revised)
        for record in manifest['files']:
            assert actual.read_file(first, record['path']) == sources[record['path']].read_bytes()
    artifacts = {}
    for path in sorted(destination.rglob('*')):
        if path.is_file() and not path.name.endswith('.hatch.lock'):
            payload = path.read_bytes()
            artifacts[path.relative_to(destination).as_posix()] = dict(bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest())
    receipt = dict(schema_version=1, fixture=True, package=created,
        references=[first, second], directory_restore=restored, archive_restore=archive_restored,
        multipart_restore=parts_restored, missing_payload=missing, substituted_payload=substituted,
        exact_repair=repaired, artifacts=artifacts, production_art_acceptance=False,
        limitation='Actual OBJ/license byte preservation; native skin/texture/motion tests are separate.')
    (destination / 'acceptance.json').write_bytes(canonical_bytes(receipt))
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    result = demonstrate(args.destination)
    print(json.dumps(dict(verified=True, package_sha256=result['package']['package_sha256'],
                         receipt=str(Path(args.destination) / 'acceptance.json')), sort_keys=True))
