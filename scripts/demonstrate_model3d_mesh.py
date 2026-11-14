"""Execute the Model3D mesh foundation using labeled synthetic geometry.

Writes genuine GLB/glTF/registry-bound OBJ meshes and a hash-bound acceptance report. This is
numerical/import evidence, never a visual or production-art approval.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

import numpy as np
from atomic_publish import _rename_no_replace
from model3d_coordinates import conversion_matrix, transform_editable
from model3d_interchange import export_mesh
from model3d_mesh_import import import_mesh
from model3d_primitives import primitive
from model3d_topology import topology_report
from rig_errors import RigError, require
from rig_import import load_character_model
from rig_jobs import create_run, CanonicalArtifact
from asset_library import AssetLibrary, file_record
from asset_library_contracts import load_manifest, seal


def demonstrate(destination):
    destination = Path(destination).absolute()
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    require(not destination.exists() and not destination.is_symlink(), 'MODEL3D_EXPORT_EXISTS',
            'Demonstration destination must be new.')
    stage = Path(tempfile.mkdtemp(prefix='.model3d-demo-', dir=parent))
    try:
        records = []
        library = AssetLibrary(stage / 'registry', create=True)
        template = load_manifest((Path(__file__).resolve().parents[1] / 'examples/asset-library-foundation/manifest.json').read_bytes())
        for kind in ('box', 'plane', 'sphere', 'cylinder', 'cone', 'torus'):
            original = primitive(kind, name=kind)
            transform = conversion_matrix(source_basis='right-handed-z-up', unit_scale=.01,
                                          pivot=[.25, -.5, 1], translation=[1, 2, 3])
            editable = transform_editable(original, transform)
            geometry = editable.geometry()
            for fmt in ('glb', 'gltf', 'obj'):
                root = stage / (kind + '-' + fmt)
                manifest = export_mesh(geometry, root, format=fmt, editable=editable, fixture=fmt != 'obj')
                reference = None
                if fmt == 'obj':
                    from copy import deepcopy
                    canonical = deepcopy(template)
                    canonical['identity'].update(id=kind, slug=kind, name=kind)
                    paths = [root / 'model.obj', root / 'materials.mtl',
                             Path(__file__).resolve().parents[1] / 'examples/asset-library-foundation/source/license.txt']
                    files = [file_record(paths[0], 'model.obj', 'geometry'),
                             file_record(paths[1], 'materials.mtl', 'source'),
                             file_record(paths[2], 'license.txt', 'license', False)]
                    canonical['files'] = files
                    canonical['provenance']['sources'] = [files[0]]
                    canonical['provenance']['license_evidence'] = [files[2]]
                    canonical['publication']['inventory'] = [f['path'] for f in files]
                    reference = library.register(seal(canonical), {f['path']: p for f, p in zip(files, paths)})
                    rig = create_run(CanonicalArtifact(library, reference, entry='model.obj'), stage / (kind + '-rig'))
                    require(rig.manifest['source']['fixture'] and rig.manifest['source']['sha256'] == files[0]['sha256'],
                            'MODEL3D_DEMO', 'Registry fixture identity or source digest lost.')
                    loaded = rig.geometry
                else:
                    loaded = load_character_model(root / ('model.' + fmt)).geometry
                require(loaded.triangle_count == geometry.triangle_count, 'MODEL3D_DEMO',
                        'Decoded triangle count differs.')
                require(np.allclose(loaded.meshes[0].positions[loaded.meshes[0].triangles],
                                    geometry.meshes[0].positions[geometry.meshes[0].triangles],
                                    rtol=1e-6, atol=1e-7), 'MODEL3D_DEMO', 'Decoded coordinates differ.')
                require(loaded.meshes[0].name == kind, 'MODEL3D_DEMO', 'Semantic name changed.')
                records.append(dict(primitive=kind, format=fmt, manifest=manifest,
                                    topology=topology_report(geometry.meshes[0]), canonical_ref=reference))
        source = stage / 'box-glb' / 'model.glb'
        original_bytes = source.read_bytes()
        with import_mesh(source) as captured:
            imported = export_mesh(captured.model.geometry, stage / 'imported-box',
                                   source_snapshot=captured)
        require(source.read_bytes() == original_bytes, 'MODEL3D_DEMO', 'Original source changed.')
        require((stage / 'imported-box' / 'source' / 'model.glb').read_bytes() == original_bytes,
                'MODEL3D_DEMO', 'Preserved source differs.')
        files = {}
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                payload = path.read_bytes()
                files[path.relative_to(stage).as_posix()] = dict(bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest())
        report = dict(schema_version=1, fixture=True, approval='not-reviewed',
                      authority='native rig Mesh/Geometry/import/export with explicit Model3D adapters',
                      records=records, imported_source=imported, files=files,
                      limitations=['No provider generation, reconstruction or artistic approval.',
                                   'OBJ fixture identity requires its canonical registry reference.',
                                   'No Blender, rendering or hosted CI execution inferred.'])
        (stage / 'acceptance.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        _rename_no_replace(stage, destination)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        report = demonstrate(args.destination)
        print(json.dumps(dict(ok=True, exports=len(report['records']), files=len(report['files']),
                              fixture=True, destination=str(Path(args.destination).absolute()))))
        return 0
    except (RigError, OSError) as exc:
        print(json.dumps(dict(ok=False, error=exc.report if isinstance(exc, RigError) else str(exc))))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
