"""Native geometry/skin/animation/texture survive exact package restoration."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from PIL import Image
from asset_library import AssetLibrary, file_record
from asset_library_contracts import seal
from asset_library_dependencies import DependencyGraph, locator
from asset_library_packages import create_package, restore_package, inspect_package, INDEX
from rig_gltf import export_gltf
from rig_import import load_character_model
from rig_interop import compare_imported
from rig_models import TextureImage
from rig_test_helpers import fixture

ROOT = Path(__file__).resolve().parents[1]


class NativePackageTests(unittest.TestCase):
    def test_real_glb_and_external_gltf_restore_reimport_and_missing_resource_controls(self):
        with tempfile.TemporaryDirectory(prefix='native-asset-package-') as temporary:
            root = Path(temporary).resolve()
            rig = fixture(root)
            buffer = io.BytesIO(); Image.new('RGBA', (2, 2), (255, 30, 50, 255)).save(buffer, format='PNG')
            rig.geometry.images = [TextureImage('red.png', 'image/png', buffer.getvalue())]
            rig.geometry.textures = [dict(source=0)]
            rig.geometry.materials[0]['pbrMetallicRoughness'] = {'baseColorTexture': {'index': 0}}
            for mesh in rig.geometry.meshes:
                mesh.attributes['TEXCOORD_0'] = np.zeros((len(mesh.positions), 2))
            for suffix in ('glb', 'gltf'):
                folder = root / suffix; folder.mkdir()
                source = folder / ('animated.' + suffix); export_gltf(rig, source)
                imported = load_character_model(source)
                self.assertTrue(compare_imported(rig, imported)['ok'])
                files = [file_record(path, path.relative_to(folder).as_posix(), 'runtime')
                         for path in imported.source_files]
                license_path = ROOT / 'examples/asset-library-foundation/source/license.txt'
                files += [file_record(license_path, 'source/license.txt', 'license', False)]
                sources = {f['path']: folder / f['path'] for f in files[:-1]}
                sources[files[-1]['path']] = license_path
                doc = json.loads((ROOT / 'examples/asset-library-foundation/manifest.json').read_text())
                doc['identity']['id'] = 'animated-' + suffix; doc['identity']['slug'] = 'animated-' + suffix
                doc['files'] = files; doc['publication']['inventory'] = [f['path'] for f in files]
                doc['provenance']['sources'] = files[:-1]; doc['provenance']['license_evidence'] = [files[-1]]
                doc['compatibility']['formats'] = [suffix]
                library = AssetLibrary(folder / 'library', create=True)
                reference = library.register(seal(doc), sources)
                package = folder / 'package'
                create_package(DependencyGraph({'local': library}), [locator('local', reference)], package,
                               package_id='native-' + suffix)
                result = restore_package(package, folder / 'relocated')
                restored = AssetLibrary(result['library'])
                materialized = folder / 'reimport'; materialized.mkdir()
                for record in files:
                    target = materialized / record['path']; target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(restored.read_file(reference, record['path']))
                actual = load_character_model(materialized / source.name)
                self.assertTrue(compare_imported(rig, actual)['ok'])
                self.assertEqual(set(actual.clips), {'bend', 'neutral'})
                self.assertGreater(len(actual.weights), 0); self.assertGreater(len(actual.geometry.images), 0)
                # Every resource, including external buffers, source and license,
                # independently blocks the same completeness gate when absent.
                for record in files:
                    blob = package / 'blobs' / record['sha256']; content = blob.read_bytes(); blob.unlink()
                    self.assertEqual(inspect_package(package)['state'], 'incomplete')
                    blob.write_bytes(content)
                    self.assertTrue(inspect_package(package)['verified'])
