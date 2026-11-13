"""Strict byte-backed and filesystem-backed project validation use one engine."""
import json
from pathlib import Path
import tempfile
import unittest

from rig_errors import RigError
from scene3d_contracts import canonical_bytes, load_project, validate_project_files
from scene3d_example import build_example


class ProjectByteValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'source'
        self.bundle = build_example(self.root)
        self.files = {path: (self.root / path).read_bytes() for path in self.bundle['inventory']}

    def test_exact_real_byte_closure_has_identical_inventory_documents_and_budgets(self):
        before = dict(self.files)
        self.assertEqual(validate_project_files(self.files), load_project(self.root))
        self.assertEqual(self.files, before)

    def test_missing_payload_and_modified_bytes_fail_canonical_hash_checks(self):
        missing = dict(self.files)
        missing.pop('triangle.obj')
        with self.assertRaisesRegex(RigError, 'Missing regular dependency'):
            validate_project_files(missing)
        changed = dict(self.files)
        changed['triangle.obj'] += b'\n'
        with self.assertRaisesRegex(RigError, 'hash mismatch'):
            validate_project_files(changed)

    def test_unreferenced_paths_are_checked_for_escapes_and_portable_aliases(self):
        for path in ('../outside', '/absolute', 'A/../B', 'triangle.OBJ'):
            with self.subTest(path=path), self.assertRaises(RigError):
                validate_project_files(dict(self.files, **{path: b'kept'}))
        with self.assertRaises(RigError):
            validate_project_files(dict(self.files, foreign=Path('/etc/passwd')))

    def test_project_node_and_file_budgets_use_same_engine(self):
        for field, value in (('max_nodes', 2), ('max_files', 1), ('max_bytes', 1)):
            files = dict(self.files)
            project = json.loads(files['project.json'])
            project['budgets'][field] = value
            files['project.json'] = canonical_bytes(project)
            with self.subTest(field=field), self.assertRaises(RigError):
                validate_project_files(files)

    def test_filesystem_source_keeps_its_missing_regular_dependency_error(self):
        (self.root / 'triangle.obj').unlink()
        with self.assertRaisesRegex(RigError, 'Missing regular dependency'):
            load_project(self.root)
        self.assertEqual(validate_project_files(self.files), self.bundle)


if __name__ == '__main__':
    unittest.main()
