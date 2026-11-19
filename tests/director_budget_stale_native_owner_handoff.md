# Native budget current-owner recovery controls

These offline tests use frozen revision `10b4205fd09dd59afcca7309786b8a4c214a3b00`, Python 3.12.14, disposable journals, a local ledger, and synthetic usage. A complete frozen checkout supplies the style catalog required by the existing native fixture.

```sh
PYTHONPATH=scripts:tests <local-work-dir>/env-rig/bin/python -m unittest -v test_director_budget_stale_native_owner
```

The three current-owner checks passed with exit 0. The captured output has SHA-256 `6bb6a091f08f49fea1a9fa5c67653615ef65757756b49378d3a04ba9e5bbd91c`. The tests verify these outcomes.

- Retrying an interrupted pending claim returns the same token and reservation with exactly one native attempt and zero measured usage.
- Recording a later observation preserves and charges an earlier pending observation. Resubmitting the later observation leaves the owner directory byte-identical.
- Recovery across the ledger commit boundary charges two distinct observations exactly once. The run retains its original token and the reservation remains uncertain, with one provider call and 5.75 amount units held. Repeated reconciliation, repeated observation submission and refused release preserve the owner directory byte for byte.

The previous seven-case runs stopped during fixture setup. Those results do not establish stale-owner behavior. This run covers recovery with the current owner; stale-owner refusal remains unverified.

| Preserved local output | SHA-256 |
| --- | --- |
| Original setup error | `50119086c46749915cdfccf6e03fac06cb3af2f5f167323cb6dd30d67925e119` |
| Original baseline setup error | `eb75ed991c64710709bf6c9b099e613712616fb354922e41dd791d55423caaff` |
