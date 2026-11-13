"""Retain actual GLB/glTF/native export receipts from an existing exact library asset."""
import argparse
import hashlib
from pathlib import Path

from asset_library import AssetLibrary
from asset_library_contracts import canonical_bytes
from asset_library_export import ExportPipeline
from rig_errors import decode_json, object_hash


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library',required=True,type=Path)
    parser.add_argument('--reference-file',required=True,type=Path,help='JSON containing one exact AssetRef.')
    parser.add_argument('--destination',required=True,type=Path,help='New evidence directory.')
    parser.add_argument('--blender',type=Path)
    parser.add_argument('--trusted-native',action='store_true')
    parser.add_argument('--allow-recorded-native-losses',action='store_true',
        help='Explicitly approve the selected version import receipt native loss inventory for this demonstration.')
    args=parser.parse_args()
    library=AssetLibrary(args.library)
    reference=decode_json(args.reference_file.read_bytes())
    manifest=library.resolve(reference)
    args.destination.mkdir(mode=0o700)
    pipeline=ExportPipeline(library,blender=args.blender)
    reports={}
    for format in ('glb','gltf'):
        reports[format]=pipeline.export(reference,args.destination/format,format=format)
    if args.trusted_native:
        report=decode_json(library.read_file(reference,'evidence/import.json'))
        approved=report.get('native_derivative_losses',[]) if args.allow_recorded_native_losses else []
        reports['blend']=pipeline.export(reference,args.destination/'blend',format='blend',
            trusted_native=True,allowed_losses=approved)
    acceptance=dict(schema_version=1,kind='asset-export-demonstration',reference=reference,
        source_identity=manifest['identity'],source_fixture=manifest['provenance'].get('method')=='fixture',
        module_sha256=hashlib.sha256(Path(__file__).with_name('asset_library_export.py').read_bytes()).hexdigest(),
        receipts={format:dict(path=format+'/export.json',sha256=object_hash(report)) for format,report in reports.items()},
        actual_validations={format:report['output_validation']['ok'] for format,report in reports.items()},
        production_approved=False,limitations=['This demonstrates the selected exact asset only; it is not two-scene reuse, package restoration or artistic approval.'])
    (args.destination/'acceptance.json').write_bytes(canonical_bytes(acceptance))
    print(canonical_bytes(acceptance).decode())


if __name__=='__main__':main()
