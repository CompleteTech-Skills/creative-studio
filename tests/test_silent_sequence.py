"""Sequence mechanics fixtures; native integration is an explicit separate gate."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import silent_sequence as sequence

class SequenceContracts(unittest.TestCase):
    def test_schemas_are_standalone_and_strict(self):
        value=sequence.schemas()['source']
        self.assertFalse(value['additionalProperties'])
        self.assertEqual(value['properties']['output']['properties']['format']['enum'],['png','exr'])
        for name,schema in sequence.schemas().items():
            self.assertEqual(schema,json.loads((Path(__file__).resolve().parents[1]/f'profiles/silent-sequence-{name}.schema.json').read_text()))

    def test_preflight_rejects_fake_precision_and_excess_before_decode(self):
        request={'settings':{'resolution':[40000,40000],'output':{'format':'exr','bit_depth':8,
            'alpha':'straight','color_space':'srgb','passes':['combined']}},'fps':{'numerator':24,'denominator':1},
            'frame_range':{'start':0,'end':1}}
        result={'images':[{'frame':0,'pass_name':'combined','file':{'path':'frames/000000.exr','sha256':'a'*64,'bytes':1}}]}
        with self.assertRaises(ValueError):sequence.native_source(request,result,'.',snapshot_root='.',expected_backend={})


    def test_missing_canonical_capability_fails_without_creating_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with mock.patch.object(sequence,'publication_supported',return_value=True), \
                    mock.patch.object(sequence,'_capability_factory',side_effect=ValueError('canonical capability unavailable')):
                with self.assertRaisesRegex(ValueError,'canonical capability unavailable'):
                    sequence.export_sequence({},'.',root/'delivery',provenance={})
            self.assertEqual(list(root.iterdir()),[])

    def test_unsupported_publication_fails_before_owner_decode_or_disk_io(self):
        with mock.patch.object(sequence,'publication_supported',return_value=False):
            with self.assertRaisesRegex(ValueError,'unavailable'):
                sequence.export_native_sequence({}, {}, '.', 'unused',snapshot_root='.',expected_backend={})

    def test_invalid_progress_observer_fails_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with mock.patch.object(sequence,'publication_supported',return_value=True), \
                    mock.patch.object(sequence,'_capability_factory'):
                with self.assertRaisesRegex(ValueError,'progress observer must be callable'):
                    sequence.export_sequence({},'.',root/'output',provenance={},progress=False)
            self.assertEqual(list(root.iterdir()),[])

    def test_invalid_deadlines_are_typed_and_rejected_before_io(self):
        for deadline in (True,'tomorrow',float('nan'),float('inf'),10**400):
            with self.subTest(deadline=type(deadline).__name__), \
                    mock.patch.object(sequence,'publication_supported',return_value=True), \
                    mock.patch.object(sequence,'_capability_factory'), \
                    mock.patch.object(sequence,'pinned_directory') as pin:
                with self.assertRaisesRegex(ValueError,'finite monotonic timestamp'):
                    sequence.export_sequence({},'.','unused',provenance={},deadline=deadline)
                with self.assertRaisesRegex(ValueError,'finite monotonic timestamp'):
                    sequence.verify_sequence('.',deadline=deadline)
                with self.assertRaisesRegex(ValueError,'finite monotonic timestamp'):
                    sequence.native_source({}, {}, '.',snapshot_root='.',expected_backend={},deadline=deadline)
                pin.assert_not_called()


if __name__=='__main__':unittest.main()
