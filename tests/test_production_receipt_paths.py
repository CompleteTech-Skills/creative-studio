"""Owned receipt path regressions using local synthetic acquisition only."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import hatch
import production_workflow as workflow
import test_production_workflow as fixture


class ProductionReceiptPathTests(unittest.TestCase):
    setUp = fixture.ProductionWorkflowTests.setUp
    acquisition = fixture.ProductionWorkflowTests.acquisition
    import_spec = fixture.ProductionWorkflowTests.import_spec

    def test_owned_temporary_alias_preserves_native_receipt_and_adoption_history(self):
        run, _ = self.acquisition()
        handoff = workflow.claim_generation(self.root, 'a', 'base')
        source = self.home / 'synthetic-source.png'
        shutil.copyfile(self.root / 'art/a-body.png', source)
        temporary_directory = workflow.tempfile.TemporaryDirectory
        observed = []

        @contextmanager
        def aliased_temporary_directory(*args, **kwargs):
            with temporary_directory(*args, **kwargs) as temporary:
                if kwargs.get('prefix') != 'hatch-production-receipt-':
                    yield temporary
                    return
                alias = self.home.resolve() / 'owned-receipt-alias'
                alias.symlink_to(Path(temporary).resolve(), target_is_directory=True)
                self.assertTrue(alias.is_symlink())
                observed.append(alias)
                try:
                    yield str(alias)
                finally:
                    alias.unlink()

        with patch.object(workflow.tempfile, 'TemporaryDirectory', aliased_temporary_directory):
            result = workflow.ingest_generation(
                self.root, 'a', 'base', source, handoff['token'],
                'Synthetic owned temporary alias; no provider execution.',
                handoff['receipt_template'])

        self.assertEqual(len(observed), 1)
        self.assertFalse(result['provider_called'])
        item = hatch.job_for(hatch.manifest(run), 'base')
        self.assertEqual(item['status'], 'generated')
        self.assertEqual(item['generation_receipt']['production_context'], handoff['production_context'])
        self.assertIsNone(item['model_call']['id'])
        self.assertIsNone(item['model_call']['model'])
        self.assertTrue(item['model_call']['unavailable_reason'])

        spec = {key: value for key, value in self.import_spec().items()
                if key not in ('source', 'origin')}
        adoption = workflow.adopt_generated_component(
            self.root, 'a', 'base', 'a', '1.0.0', 'body', spec, '1.0.1')
        provenance = adoption['image']['provenance']
        snapshot = hatch.read(self.root / provenance['acquisition']['receipt_path'])
        self.assertEqual(snapshot['job']['history'], hatch.job_for(hatch.manifest(run), 'base')['history'])
        self.assertEqual(snapshot['job']['generation_receipt']['production_context'], handoff['production_context'])
        self.assertEqual(provenance['kind'], 'fixture')
        self.assertTrue(adoption['review_required'])
        self.assertFalse(adoption['provider_called'])
