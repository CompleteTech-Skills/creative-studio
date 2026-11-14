"""Portable source closure path admission through the native importer."""
import json
from pathlib import Path
import struct
import tempfile
import unittest

from rig_errors import RigError
from rig_source_snapshot import import_snapshot


class SourceSnapshotAliasTests(unittest.TestCase):
    def test_file_directory_and_directory_spelling_collisions(self):
        positions = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
        for names in (('A', 'a/b'), ('a/b', 'A'),
                      ('Dir/a', 'dir/b'), ('dir/b', 'Dir/a')):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                try:
                    for name, data in zip(names, (positions, b'\0' * 4)):
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                except (FileExistsError, NotADirectoryError):
                    self.skipTest('Host filesystem cannot represent case-colliding fixture')
                document = {'asset': {'version': '2.0'},
                    'buffers': [{'uri': names[0], 'byteLength': 36}, {'uri': names[1], 'byteLength': 4}],
                    'bufferViews': [{'buffer': 0, 'byteLength': 36}],
                    'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3',
                                   'min': [0, 0, 0], 'max': [1, 1, 0]}],
                    'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}]}],
                    'nodes': [{'mesh': 0}], 'scenes': [{'nodes': [0]}], 'scene': 0}
                entry = root / 'model.gltf'
                entry.write_text(json.dumps(document))
                with self.assertRaises(RigError) as caught:
                    import_snapshot(entry)
                self.assertEqual(caught.exception.report['code'], 'RIG_PATH')

    def test_distinct_files_in_same_directory_are_preserved(self):
        # The existing real import fixtures exercise successful snapshots; this
        # case also checks that ordinary repeated directory prefixes are legal.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'dir').mkdir()
            (root / 'dir/a').write_bytes(struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0))
            (root / 'dir/b').write_bytes(b'\0' * 4)
            doc = {'asset': {'version': '2.0'}, 'buffers': [{'uri': 'dir/a', 'byteLength': 36}, {'uri': 'dir/b', 'byteLength': 4}],
                   'bufferViews': [{'buffer': 0, 'byteLength': 36}],
                   'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3', 'min': [0, 0, 0], 'max': [1, 1, 0]}],
                   'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}]}], 'nodes': [{'mesh': 0}], 'scenes': [{'nodes': [0]}], 'scene': 0}
            (root / 'model.gltf').write_text(json.dumps(doc))
            with import_snapshot(root / 'model.gltf') as snapshot:
                self.assertEqual({r['path'] for r in snapshot.source_records}, {'model.gltf', 'dir/a', 'dir/b'})
