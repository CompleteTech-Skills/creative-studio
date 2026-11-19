"""Read-only observation of directory admission in a normal offline production claim.

Use PYTHONPATH=scripts:tests from the detached source. No directory substitution
or generated provider work. Existing fixture helpers prepare two local runs.
"""
from contextlib import ExitStack
import os
from pathlib import Path
from unittest.mock import patch

import native_journal
import test_director_budget_native as fixture


def observe_production_claim():
    case = fixture.NativeBudgetTests('test_rate_limit_through_production_owner_preserves_measured_spend')
    case.setUp()
    try:
        project, runs, ledger, evidence = case.project_enrollment_fixture('normal')
        selected = runs[0]
        for run in runs:
            fixture.bind_native_budget(
                run, ledger.root, owner_root=project, provider='imagegen',
                estimate=fixture.quantities(provider_calls=1, amount=2),
                retry_allowance=0, evidence=evidence)
        original = selected.stat()
        records = []

        def observe(function, label):
            def wrapped(path, *args, **kwargs):
                if Path(path).is_relative_to(selected) or path == project:
                    identity = (original.st_dev, original.st_ino)
                    witness = any(
                        Path(item.path) == selected and item.identity == identity
                        for item in native_journal._ADMISSIONS.get())
                    descriptors = 0
                    for entry in Path('/proc/self/fd').iterdir():
                        try:
                            descriptors += int(os.path.samestat(os.fstat(int(entry.name)), original))
                        except OSError:
                            continue
                    records.append((label, witness, descriptors))
                return function(path, *args, **kwargs)
            return wrapped

        stages = [
            (fixture.hatch, 'manifest', 'manifest read'),
            (fixture.hatch, 'append_input_text', 'prompt stage'),
            (fixture.hatch, 'claim', 'claim entry'),
            (fixture.hatch, '_native_budget', 'inside Hatch'),
            (fixture.workflow, '_save', 'production journal save')]
        with ExitStack() as patches:
            for module, name, label in stages:
                patches.enter_context(patch.object(module, name, observe(getattr(module, name), label)))
            handoff = fixture.workflow.claim_generation(project, 'first', 'base')
        print('ordinary synthetic production handoff:', bool(handoff.get('token')))
        for record in dict.fromkeys(records):
            print(record)
        assert handoff.get('token')
        assert any(label == 'inside Hatch' and witness for label, witness, _ in records)
        assert any(label == 'claim entry' and (witness or descriptors) for label, witness, descriptors in records), (
            'The original production run directory has no retained witness or open descriptor on entry to Hatch.claim.')
    finally:
        case.doCleanups()


if __name__ == '__main__':
    observe_production_claim()
