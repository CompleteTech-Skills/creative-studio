"""Independent milestone projections from executed owner checks and reviews.

This module does not mutate the project or infer downstream completion. Native
owners must provide their actual completion checks; absent checks stay blocked.
"""
from copy import deepcopy
from datetime import datetime, timezone

from director_contracts import validate_document
from director_schema import MILESTONES
from director_qa import need, reference


def milestone_report(project, profile_reference, *, owner_checks=None, review_status=None):
    """Call trusted, configured owner verifiers; never accept browser verdicts.

    ``owner_checks`` is an in-process map of actual native verification functions,
    installed by the host. It cannot be provided by JSON/HTTP clients. Each
    function returns the canonical v2 milestone fields after executing its own
    domain checks. No generic hash, schema, screenshot or upstream pass fills an
    absent owner function. The complete project semantics are checked afterward.
    """
    owner_checks = owner_checks or {}
    reference(profile_reference)
    need(isinstance(owner_checks, dict) and set(owner_checks) <= set(MILESTONES)
         and all(callable(value) for value in owner_checks.values()),
         'OWNER', 'Milestone checks must be installed native owner functions.')
    reports = []
    for name in MILESTONES:
        row = dict(name=name, schema_version=2, status='blocked', evidence=[], diagnostics=[],
                   authority='automated', scope='Current exact project; independent '+name+' acceptance.',
                   timestamp=datetime.now(timezone.utc).isoformat(), required_inputs=[], artifact_refs=[],
                   validation_results=[], evidence_refs=[], failure_reason=None,
                   remaining_work=['Install and execute the authoritative '+name+' acceptance adapter.'])
        if name in owner_checks:
            supplied = owner_checks[name](deepcopy(project))
            need(isinstance(supplied, dict) and set(supplied) == set(row), 'OWNER', 'Native verifier must return a complete v2 milestone.')
            need(supplied['name'] == name, 'OWNER', 'Native verifier changed milestone identity.')
            row = deepcopy(supplied)
            for field in ('required_inputs', 'artifact_refs', 'evidence_refs', 'evidence'):
                for ref in row[field]: reference(ref)
        # Visual attestations require their native journal adapter. Automated
        # structural checks can never supply a passing visual milestone.
        need(not (name == 'visual_qa' and row['status'] == 'passed' and row['authority'] == 'automated'),
             'REVIEW', 'Automated checks cannot complete visual QA.')
        reports.append(row)
    projected = deepcopy(project)
    need(projected['schema_version'] == 2, 'VERSION', 'Milestone reporting requires the explicit v2 project contract.')
    projected['milestones'] = reports
    projected['status'] = 'complete' if all(row['status'] == 'passed' for row in reports) else 'blocked'
    validate_document(projected)
    return dict(schema_version=1, kind='director-production-status', project_id=project['id'],
                profile=deepcopy(profile_reference),
                project_semantic_version=project['semantic_version'], milestones=reports,
                status='succeeded' if projected['status'] == 'complete' else 'partial',
                review_status=deepcopy(review_status or {}), production_art_approved=False,
                scope='Executed independent native checks only; this projection does not save a new project version.')
