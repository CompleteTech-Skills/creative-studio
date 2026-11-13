"""Register/retrieve the real procedural triangle fixture through the library API."""
import argparse
import hashlib
import json
from pathlib import Path
from asset_library import AssetLibrary
from asset_library_contracts import canonical_bytes, load_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path,
                        help='New private directory; existing destinations are preserved.')
    args = parser.parse_args()
    example = Path(__file__).resolve().parents[1] / 'examples/asset-library-foundation'
    manifest = load_manifest((example / 'manifest.json').read_bytes())
    sources = {f['path']: example / f['path'] for f in manifest['files']}
    library = AssetLibrary(args.destination, create=True)
    reference = library.register(manifest, sources)
    reopened = AssetLibrary(args.destination)
    reopened.resolve(reference)
    actual = {path: hashlib.sha256(reopened.read_file(reference, path)).hexdigest() for path in sources}
    print(json.dumps(dict(schema_version=1, fixture=True, reference=reference,
                          files=actual, payload_verified=True, geometry_validated=False,
                          production_approved=False, source='procedural-engineering-fixture'), indent=2))


if __name__ == '__main__': main()
