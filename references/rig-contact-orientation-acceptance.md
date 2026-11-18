# Native contact orientation acceptance

Contact validation retains the existing positional error and planted-drift checks.
A contact with an explicit orientation, or an inherited chain orientation, also
requires final evaluated FK to satisfy the native angular bound and the canonical
solver to report convergence. Explicit contact orientation takes precedence.
Later contacts can move an earlier endpoint; the earlier solver report alone is
therefore insufficient evidence of the final pose. At each distinct time from
the union of all contacts' existing seven samples, validation checks the final
pose against every active required orientation. Each contact's position and
drift continue to use its own seven samples.

The angular threshold is the native solver's fixed `1e-5` radians. The chain's
`tolerance` field remains positional. No new solver or chain field is introduced.

`contact_metrics` adds `max_contact_orientation_error_rad` and
`orientation_samples`. Each contact adds `max_orientation_error_rad`,
`orientation_samples`, `orientation_tolerance_rad`, and `orientation_converged`.
Contacts without an orientation have zero angular samples and null angular
maximum, tolerance, and convergence. Aggregate angular maximum is null when no
goal was measured. `validate_rig` preserves its existing metrics and contacts
check, adds the aggregate angular fields, and rejects unmet required orientation.
For measured orientation goals, the report also records each evaluated clip's
version, angular maximum, sample count, and convergence in scalar metrics under
`contact_orientation/<clip-id>/<clip-content-sha256>/`. The hash covers the
canonical clip object used for evaluation, including programmatic edits. The
report's existing rig hash continues to bind the source manifest.
Position-only legacy acceptance remains unchanged.

The independently authored `tests/test_rig_contact_orientation_validation.py`
uses actual native rigs, clip evaluation, FK, and a separate rotation-matrix
oracle. It covers both solvers, explicit/inherited and signed goals, constrained
failures, final-pose changes, aggregation, convergence, and position-only controls.
It is enrolled in the existing `animation-ik` gate. Preparation alone is not
executed evidence; this work does not establish visual or complete rig acceptance.
