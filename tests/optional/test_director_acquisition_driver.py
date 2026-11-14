"""Explicit actual raster-claim and image-ingestion integration gate.

Requires the canonical AcquisitionGateway source dependency. No provider or
model is invoked; the raster source is an explicitly labelled fixture.
"""
import argparse
import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image
import hatch
from director_acquisition import NativeAcquisitionGateway
from director_execution_drivers import NativeAcquisitionDriver
from fixture_workflow import small_profile
from render3d_jobs import JobStore
from rig_errors import RigError, object_hash


class AcquisitionExecutionDriverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.native = self.root / 'native'
        profile = self.root / 'profile.json'
        hatch.atomic(profile, small_profile())
        hatch.prepare(self.native, argparse.Namespace(replace_inputs=False, fixture_mode=True,
            profile=str(profile), pet_name='Driver fixture', pet_notes='Synthetic claim integration',
            reference=[], avoid=[], require=[], brand_source=[], style_preset='clay', style_notes='fixture'))
        self.gateway = NativeAcquisitionGateway(self.native, asset_kind='raster')
        self.store = JobStore(self.root / 'jobs')
        self.driver = NativeAcquisitionDriver(self.gateway, self.store)
        data = hatch.manifest(self.native)
        self.binding = dict(job='base', input_sha256=object_hash(hatch.inputs(self.native, hatch.job_for(data, 'base'))))

    def test_denied_admission_does_not_acquire_a_native_token(self):
        before = (self.native / 'imagegen-jobs.json').read_bytes()
        with self.assertRaisesRegex(RigError, 'deferred'):
            self.driver.dispatch(self.binding, admit=lambda db, record: False)
        self.assertEqual(before, (self.native / 'imagegen-jobs.json').read_bytes())

    def test_repeated_dispatch_reuses_native_token_and_cancel_preserves_uncertainty(self):
        first = self.driver.dispatch(self.binding, admit=lambda db, record: True)
        second = self.driver.dispatch(self.binding, admit=lambda db, record: True)
        self.assertEqual(first['claim_id'], second['claim_id'])
        self.assertFalse(first['provider_called'])
        self.assertEqual(hatch.job_for(hatch.manifest(self.native), 'base')['attempts'], 1)
        report = self.driver.cancel(self.binding)
        self.assertEqual(report['state'], 'uncertain')
        self.assertEqual(report['receipt']['claim_id'], first['claim_id'])
        self.assertEqual(self.driver.resume(self.binding)['receipt']['claim_id'], first['claim_id'])

    def test_real_ingested_fixture_is_decoded_and_tampering_rejects_completion(self):
        claimed = self.driver.dispatch(self.binding, admit=lambda db, record: True)
        source = self.root / 'source.png'
        Image.new('RGBA', (96, 96), (35, 80, 160, 255)).save(source)
        self.gateway.ingest('base', claimed['claim_id'], source,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            receipt=claimed['handoff']['receipt_template'], note='Synthetic integration fixture')
        report = self.driver.inspect(self.binding)
        self.assertEqual(report['state'], 'complete')
        self.assertEqual(len(report['artifacts']), 1)
        self.assertTrue(report['artifacts'][0]['fixture'])
        self.assertIsNone(report['actual_usage'])
        image = self.native / report['artifacts'][0]['path']
        image.write_bytes(b'not a decoded output')
        self.assertNotEqual(self.driver.inspect(self.binding)['state'], 'complete')


if __name__ == '__main__':
    unittest.main()
