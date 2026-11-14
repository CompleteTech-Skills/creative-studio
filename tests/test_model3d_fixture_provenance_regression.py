"""Actual normalized GLB provenance across native import, rig and export."""
import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from asset_library_import import SourceSnapshot, normalize_gltf
from model3d_interchange import export_mesh
from model3d_mesh_import import import_mesh, import_canonical
from model3d_mesh_asset import build_asset
from model3d_primitives import primitive
from rig_errors import RigError
from rig_gltf import export_geometry
from rig_import import load_character_model
from rig_jobs import create_run
from test_model3d_mesh_asset import record, provenance, source


class FixtureProvenanceRegression(unittest.TestCase):
    def test_normalized_fixture_import_rig_export_and_spoof(self):
        with tempfile.TemporaryDirectory() as tmp:
            # This fixture owns the temporary directory and explicitly admits
            # its canonical location before creating any source files.
            root = Path(tmp).resolve(strict=True)
            original = root / 'original.glb'
            export_geometry(primitive('box').geometry(), original)
            before = original.read_bytes()
            snapshot = SourceSnapshot(original, root / 'snapshot')
            normalized, _, _ = normalize_gltf(snapshot, root / 'normalized.glb', fixture=True)
            normalized_bytes = normalized.read_bytes()
            self.assertTrue(load_character_model(normalized).report['fixture'])
            rig = create_run(normalized, root / 'rig')
            self.assertTrue(rig.manifest['source']['fixture'])
            self.assertEqual(rig.manifest['source']['origin'], 'procedural-fixture')
            geometry_record = record(root, 'normalized.glb', 'model/gltf-binary')
            claim = provenance(); claim.update(fixture=False, origin='imported')
            src = source(geometry_record); src.update(provenance=claim, evidence_class='observed')
            with patch.object(Path, 'home', return_value=root), self.assertRaises(RigError):
                build_asset(root, geometry_record, asset_id='probe', version='1.0.0',
                            category='prop', sources=[src], provenance=claim)
            with import_mesh(normalized) as captured:
                self.assertTrue(captured.model.report['fixture'])
                for fmt in ('glb', 'gltf'):
                    dest = root / fmt
                    export_mesh(captured.model.geometry, dest, format=fmt, source_snapshot=captured)
                    self.assertTrue(load_character_model(dest / ('model.' + fmt)).report['fixture'])
                    self.assertEqual((dest / 'source/normalized.glb').read_bytes(), normalized_bytes)
                captured.model.report['fixture'] = False
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'spoof', source_snapshot=captured)
                self.assertFalse((root / 'spoof').exists())
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(normalized.read_bytes(), normalized_bytes)

    def test_entry_binding_and_mutation_during_export(self):
        import rig_import
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            export_geometry(primitive('plane').geometry(), root / 'plain.glb')
            export_geometry(primitive('box').geometry(), root / 'marked.gltf', provenance={'fixture': True})
            doc = json.loads((root / 'marked.gltf').read_text())
            doc['buffers'].append({'byteLength': (root / 'plain.glb').stat().st_size, 'uri': 'plain.glb'})
            (root / 'marked.gltf').write_text(json.dumps(doc))
            originals = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
            with import_mesh(root / 'marked.gltf') as captured:
                captured.model.report['fixture'] = False
                captured.provenance['source_entry'] = 'plain.glb'
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'spoof', source_snapshot=captured)
                captured.model.report['fixture'] = True
                captured.provenance['source_entry'] = 'marked.gltf'
                real_load = rig_import.load_character_model
                def mutate(*args, **kwargs):
                    result = real_load(*args, **kwargs)
                    captured.provenance['source_entry'] = 'plain.glb'
                    captured.model.report['fixture'] = False
                    return result
                with patch.object(rig_import, 'load_character_model', side_effect=mutate):
                    with self.assertRaises(RigError):
                        export_mesh(captured.model.geometry, root / 'race', source_snapshot=captured)
                captured.provenance['source_entry'] = 'marked.gltf'
                captured.model.report['fixture'] = True
                export_mesh(captured.model.geometry, root / 'good', source_snapshot=captured)
                for name, data in originals.items():
                    self.assertEqual((root / 'good/source' / name).read_bytes(), data)
                self.assertTrue(load_character_model(root / 'good/model.glb').report['fixture'])
                self.assertTrue(create_run(root / 'good/model.glb', root / 'rig').manifest['source']['fixture'])

    def test_demo_outputs_reimport_as_fixtures(self):
        from demonstrate_model3d_mesh import demonstrate
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            report = demonstrate(root / 'demo')
            self.assertTrue(report['fixture'])
            self.assertEqual(len(report['records']), 18)
            for record in report['records']:
                path = root / 'demo' / (record['primitive'] + '-' + record['format']) / ('model.' + record['format'])
                if record['format'] != 'obj':
                    self.assertTrue(load_character_model(path).report['fixture'])
                else:
                    from asset_library import AssetLibrary
                    from rig_jobs import CanonicalArtifact
                    library = AssetLibrary(root / 'demo/registry')
                    rig = create_run(CanonicalArtifact(library, record['canonical_ref'], entry='model.obj'), root / ('check-' + record['primitive']))
                    self.assertTrue(rig.manifest['source']['fixture'])
                    self.assertEqual((rig.root / rig.manifest['source']['path']).read_bytes(), path.read_bytes())
            self.assertTrue(create_run(root / 'demo/box-glb/model.glb', root / 'rig').manifest['source']['fixture'])
            self.assertEqual(len(list((root / 'demo').glob('*-obj/model.obj'))), 6)

    def test_canonical_fixture_library_export_and_native_rig(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Admit the owned temporary root before passing it to the library.
            root = Path(tmp).resolve(strict=True)
            from test_model3d_mesh_import import MeshImportTests
            case = MeshImportTests(); case.root = root
            library, manifest, ref = case.canonical()
            originals = {item['path']: library.read_file(ref, item['path']) for item in manifest['files']}
            self.assertTrue(manifest['provenance']['fixture'])
            with import_canonical(library, ref) as captured:
                self.assertIsNot(captured.model.report.get('fixture'), True)
                for fmt in ('glb', 'gltf'):
                    dest = root / fmt
                    export_mesh(captured.model.geometry, dest, format=fmt, source_snapshot=captured)
                    self.assertTrue(load_character_model(dest / ('model.' + fmt)).report['fixture'])
                    for name, data in originals.items():
                        self.assertEqual((dest / 'source' / name).read_bytes(), data)
                rig = create_run(root / 'glb/model.glb', root / 'rig')
                self.assertTrue(rig.manifest['source']['fixture'])
                self.assertEqual(rig.manifest['source']['origin'], 'procedural-fixture')
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'obj', format='obj', source_snapshot=captured)
                captured.provenance['manifest']['provenance']['fixture'] = False
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'tampered', source_snapshot=captured)
                self.assertFalse((root / 'tampered').exists())

    def test_registered_canonical_builder_rejects_conflicting_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            from test_model3d_mesh_import import MeshImportTests
            case = MeshImportTests(); case.root = root
            library, manifest, ref = case.canonical()
            for item in manifest['files']:
                path = root / item['path']; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(library.read_file(ref, item['path']))
            original = (root / manifest['files'][0]['path']).read_bytes()
            (root / 'canonical.json').write_text(json.dumps(dict(canonical_ref=ref, manifest=manifest)))
            geometry = record(root, manifest['files'][0]['path'], 'model/obj')
            evidence = record(root, 'canonical.json', 'application/json')
            claim = provenance(); claim.update(fixture=False, origin='imported')
            src = source(geometry); src.update(provenance=claim, evidence_class='observed')
            with patch.object(Path, 'home', return_value=root), self.assertRaisesRegex(RigError, 'fixture'):
                build_asset(root, geometry, asset_id='probe', version='1.0.0', category='prop',
                            sources=[src], provenance=claim, canonical_evidence=evidence)
            with patch.object(Path, 'home', return_value=root), self.assertRaisesRegex(RigError, 'fixture'):
                build_asset(root, geometry, asset_id='probe', version='1.0.0', category='prop',
                            sources=[src], provenance=provenance(), canonical_evidence=evidence)
            self.assertEqual((root / geometry['path']).read_bytes(), original)

    def test_registered_marked_glb_normalization_retains_fixture(self):
        from asset_library_contracts import seal
        from asset_library_dependencies import DependencyGraph, normalize_static_units, locator
        from test_model3d_mesh_import import MeshImportTests
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); case = MeshImportTests(); case.root = root
            library, manifest, ref, _, name = case.normalized_canonical()
            def rename(value):
                if isinstance(value, dict): return {k: rename(v) for k, v in value.items()}
                if isinstance(value, list): return [rename(v) for v in value]
                return 'runtime/marked.glb' if value == name else value
            revised = rename(deepcopy(manifest)); revised['version'] = '2.0.0'
            revised['provenance'].update(fixture=False, method='import')
            files = {}
            for item in revised['files']:
                path = root / 'input' / item['path']; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(library.read_file(ref, name if item['path'] == 'runtime/marked.glb' else item['path']))
                files[item['path']] = path
            before = files['runtime/marked.glb'].read_bytes()
            ref = library.register(seal(revised), files)
            result = normalize_static_units(DependencyGraph({'local': library}), locator('local', ref),
                                            'runtime/marked.glb', version='3.0.0')
            derived = result['asset']['reference']
            self.assertTrue(library.resolve(derived)['provenance']['fixture'])
            self.assertEqual(library.read_file(derived, 'runtime/marked.glb'), before)
            output = root / 'reopened.glb'
            output.write_bytes(library.read_file(derived, result['native_path']))
            self.assertTrue(load_character_model(output).report['fixture'])
            with import_canonical(library, derived) as captured:
                self.assertTrue(captured.model.report['fixture'])
            rig = create_run(output, root / 'converted-rig')
            self.assertTrue(rig.manifest['source']['fixture'])

    def test_claimed_canonical_manifest_cannot_taint_plain_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(strict=True)
            from test_model3d_mesh_import import MeshImportTests
            case = MeshImportTests(); case.root = root
            _, manifest, ref = case.canonical()
            export_geometry(primitive('box').geometry(), root / 'plain.glb')
            with import_mesh(root / 'plain.glb') as captured:
                captured.provenance.update(manifest=manifest, canonical_ref=ref)
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'spoof', source_snapshot=captured)
                self.assertFalse((root / 'spoof').exists())

    def test_unmarked_source_cannot_be_spoofed_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_geometry(primitive('box').geometry(), root / 'plain.glb')
            with import_mesh(root / 'plain.glb') as captured:
                captured.model.report['fixture'] = True
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'spoof', source_snapshot=captured)
                self.assertFalse((root / 'spoof').exists())

    def test_fixture_obj_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_geometry(primitive('box').geometry(), root / 'fixture.glb', provenance={'fixture': True})
            with import_mesh(root / 'fixture.glb') as captured:
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'obj', format='obj', source_snapshot=captured)
                self.assertFalse((root / 'obj').exists())
