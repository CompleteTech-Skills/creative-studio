"""Actual filesystem/CLI checks for bounded exact-byte LookDev delivery."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev import Project, INDEX, load_closure, capture_closure
from lookdev_contracts import canonical_bytes
from rig_errors import RigError
from test_lookdev_contracts import material, reference, texture, image_bytes


class PortableClosureTests(unittest.TestCase):
    def test_exact_owner_reader_closure(self):
        raw=canonical_bytes(material());ref=reference(material(),'material.json')
        observed=[]
        def read(path, maximum):
            observed.append((path,maximum));return raw
        closure=capture_closure(read,[dict(ref,kind='material')])
        self.assertEqual(closure.references,[ref])
        self.assertEqual(closure.sources,{'material.json':raw})
        self.assertEqual(observed[0][0],'material.json')

    def test_owner_reader_cannot_bypass_type_or_size_bounds(self):
        ref=reference(material(),'material.json')
        for reader in (lambda path,maximum:'text', lambda path,maximum:b'x'*(maximum+1)):
            with self.assertRaises(RigError):capture_closure(reader,[ref])
