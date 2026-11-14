"""Actual GLB import/version/package export and atomic scene attachment adapters."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from asset_library import AssetLibrary
from rig_errors import RigError
from rig_gltf import GltfBuilder, read_gltf
from rig_contracts import DEFAULT_LIMITS
from scene3d_cli_assets import add_parsers, execute
from scene3d_contracts import load_project
from scene3d_example import build_example
from scene3d_graph import SceneGraph


class NativeAssetCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library_root = self.root / 'library'
        self.library = AssetLibrary(self.library_root, create=True)
        self.license = self.root / 'license.txt'
        self.license.write_text('CC0-1.0. Procedural engineering geometry fixture.\n')
        builder = GltfBuilder()
        builder.json['asset']['extras'] = {'hatchSource': {'fixture': True}}
        positions = builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3', bounds=True)
        indices = builder.accessor([0, 1, 2], 'SCALAR', 5123)
        builder.json['meshes'] = [dict(primitives=[dict(attributes={'POSITION': positions}, indices=indices)])]
        builder.json['nodes'] = [dict(name='Source parent', children=[1, 2]),
                                 dict(name='First', mesh=0, translation=[1, 0, 0]),
                                 dict(name='Second', mesh=0, translation=[3, 0, 0])]
        builder.json['scenes'] = [dict(nodes=[0])]
        self.source = self.root / 'source.glb'
        builder.write(self.source)
        self.original = self.source.read_bytes()
        self.parser = argparse.ArgumentParser()
        add_parsers(self.parser.add_subparsers(dest='action', required=True))

    def command(self, arguments):
        return execute(self.parser.parse_args(arguments))

    def import_model(self):
        return self.command(['import', '--library', str(self.library_root), '--source', str(self.source),
            '--asset-id', 'native-model', '--attribution', 'Fixture author', '--license-file', str(self.license)])

    def write_reference(self, reference):
        path = self.root / 'reference.json'
        path.write_text(json.dumps(reference))
        return path

    def test_real_versioned_geometry_and_exact_package_export_preserve_sources(self):
        imported = self.import_model()
        self.assertTrue(imported['independent_validator']['ok'])
        self.assertEqual(imported['geometry']['vertices'], 6)
        self.assertEqual(imported['geometry']['triangles'], 2)
        self.assertTrue(imported['original_bytes_preserved'])
        self.assertTrue(imported['scene_instances_unchanged'])
        reference_path = self.write_reference(imported['reference'])
        replacement = self.command(['version', '--library', str(self.library_root), '--source', str(self.source),
            '--reference-file', str(reference_path), '--version', '1.0.1', '--attribution', 'Fixture author',
            '--license-file', str(self.license)])
        self.assertEqual(replacement['previous_reference'], imported['reference'])
        self.assertEqual(replacement['reference']['version'], '1.0.1')
        self.library.resolve(imported['reference'])
        self.library.resolve(replacement['reference'])
        output = self.root / 'exported'
        exported = self.command(['export', '--library', str(self.library_root), '--reference-file', str(reference_path),
                                  '--destination', str(output)])
        self.assertEqual(exported['reference'], imported['reference'])
        self.assertEqual((output / 'imports/native-model/1.0.0/native/source/model.glb').read_bytes(), self.original)
        normalized = read_gltf(output / 'imports/native-model/1.0.0/normalized.glb', DEFAULT_LIMITS)
        self.assertGreater(len(normalized.json['meshes']), 0)
        before = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in output.rglob('*') if p.is_file()}
        with self.assertRaises(RigError):
            self.command(['export', '--library', str(self.library_root), '--reference-file', str(reference_path),
                          '--destination', str(output)])
        self.assertEqual(before, {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in output.rglob('*') if p.is_file()})
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_exact_registered_version_attaches_and_reopens_real_scene_geometry(self):
        imported = self.import_model()
        reference_path = self.write_reference(imported['reference'])
        project = self.root / 'project'
        bundle = build_example(project)
        graph = SceneGraph.from_bundle(bundle, 'example-scene.json', source_root=project)
        arguments = ['attach', '--library', str(self.library_root), '--reference-file', str(reference_path),
            '--project', str(project), '--scene', 'example-scene.json', '--identifier', 'new-model',
            '--expected-revision', '0', '--expected-sha256', graph.digest]
        library_before = (self.library_root / 'library.sqlite3').read_bytes()
        output = self.root / 'attached'
        dry = self.command(arguments + ['--destination', str(output), '--dry-run'])
        self.assertTrue(dry['dry_run'])
        self.assertFalse(output.exists())
        result = self.command(arguments + ['--destination', str(output)])
        reopened = load_project(output)
        scene = reopened['documents'][result['active_scene']['path']]
        self.assertIn('new-model', {n['id'] for n in scene['nodes']})
        imported_nodes = [n for n in scene['nodes'] if n.get('asset') == imported['scene_asset_reference']]
        self.assertEqual(len(imported_nodes), 2)
        self.assertEqual((output / 'imports/native-model/1.0.0/native/source/model.glb').read_bytes(), self.original)
        self.assertEqual(load_project(project)['inventory'], bundle['inventory'])
        self.assertEqual((self.library_root / 'library.sqlite3').read_bytes(), library_before)
        with self.assertRaises(RigError):
            self.command([value if value != graph.digest else '0' * 64 for value in arguments] +
                         ['--destination', str(self.root / 'stale')])
        self.assertFalse((self.root / 'stale').exists())

    def test_concurrent_valid_selected_scene_edit_rejects_both_dry_run_and_publication(self):
        from scene3d_contracts import canonical_bytes
        import scene3d_import
        import scene3d_snapshot
        imported = self.import_model()
        reference = self.write_reference(imported['reference'])
        actual_resolve = scene3d_import.resolve_import
        library_before = (self.library_root / 'library.sqlite3').read_bytes()
        for dry_run in (True, False):
            project = self.root / ('race-dry' if dry_run else 'race-actual')
            bundle = build_example(project)
            original = SceneGraph.from_bundle(bundle, 'example-scene.json', source_root=project)
            def resolve_then_edit(library, selected):
                resolved = actual_resolve(library, selected)
                current = load_project(project)
                graph = SceneGraph.from_bundle(current, 'example-scene.json', source_root=project)
                graph.rename('mesh', 'CONCURRENT USER EDIT', expected_revision=0)
                documents = current['documents']
                documents['example-scene.json'] = graph.document()
                def rebind(value, hashes):
                    if isinstance(value, dict):
                        if value.get('path') in hashes and 'sha256' in value:
                            value['sha256'] = hashes[value['path']]
                        for child in value.values():
                            rebind(child, hashes)
                    elif isinstance(value, list):
                        for child in value:
                            rebind(child, hashes)
                for _ in range(8):
                    hashes = {path: hashlib.sha256(canonical_bytes(document)).hexdigest()
                              for path, document in documents.items()}
                    for document in documents.values():
                        rebind(document, hashes)
                for path, document in documents.items():
                    (project / path).write_bytes(canonical_bytes(document))
                load_project(project)  # The concurrent replacement is a valid actual closure.
                return resolved
            output = self.root / ('rejected-dry' if dry_run else 'rejected-actual')
            arguments = ['attach', '--library', str(self.library_root), '--reference-file', str(reference),
                '--project', str(project), '--scene', 'example-scene.json', '--identifier', 'attached-asset',
                '--expected-revision', '0', '--expected-sha256', original.digest, '--destination', str(output)]
            if dry_run:
                arguments.append('--dry-run')
            with patch.object(scene3d_import, 'resolve_import', side_effect=resolve_then_edit), \
                    patch.object(scene3d_snapshot, 'read_source_bytes', wraps=scene3d_snapshot.read_source_bytes) as copier:
                with self.assertRaises(RigError) as error:
                    self.command(arguments)
                self.assertEqual(error.exception.report['code'], 'SCENE3D_SOURCE_CHANGED')
                copier.assert_not_called()
            self.assertFalse(output.exists())
            remaining = load_project(project)['documents']['example-scene.json']
            self.assertEqual(remaining['revision'], 1)
            self.assertEqual(next(node['name'] for node in remaining['nodes'] if node['id'] == 'mesh'),
                             'CONCURRENT USER EDIT')
        self.assertEqual((self.library_root / 'library.sqlite3').read_bytes(), library_before)

    def test_nonfinite_scale_fails_before_library_creation_and_does_not_change_source(self):
        destination = self.root / 'not-created'
        with self.assertRaises(RigError) as error:
            self.command(['import', '--library', str(destination), '--create-library', '--source', str(self.source),
                '--asset-id', 'model', '--attribution', 'Fixture author', '--license-file', str(self.license),
                '--unit-scale', '1e400'])
        self.assertEqual(error.exception.report['code'], 'SCENE3D_ASSET_UNITS')
        self.assertFalse(destination.exists())
        self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
