"""Actual GLB import/version/package export and atomic scene attachment adapters."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

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
