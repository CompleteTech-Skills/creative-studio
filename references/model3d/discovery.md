# Standalone Model3D discovery

From the repository root, run with the same Python environment as the existing
Model3D tools:

```sh
python -B scripts/model3d_discovery_cli.py doctor
python -B scripts/model3d_discovery_cli.py capabilities
python -B scripts/model3d_discovery_cli.py providers
python -B scripts/model3d_discovery_cli.py formats
python -B scripts/model3d_discovery_cli.py limits
python -B scripts/model3d_discovery_cli.py --help
python -B scripts/model3d_discovery_cli.py formats --help
```

Each successful query emits one JSON object to stdout and exits 0. Help exits 0;
argparse usage errors exit 2. `capabilities` is the existing `model3d.capabilities()`
snapshot unchanged. `limits` exposes the same `model3d_contracts.LIMITS` object,
static contract and inspection ceilings that can be further restricted by a
document or backend, not generation or scheduler budgets.

For owner integration, import `doctor()`, `capabilities()`, `providers()`,
`formats()`, `limits()`, or `main(argv=None)` from
`scripts/model3d_discovery_cli.py` with `scripts` on Python's module search path.
The functions return JSON-serializable dictionaries; `main` returns 0 for a
successful query and argparse handles help and usage. The discovery-specific
objects have `schema_version: 1`. Central CLI wiring is deliberately separate.

`doctor` reuses the capability snapshot's module-spec and distribution-metadata
checks. `installed` means a module spec was found, and `version` is distribution
metadata or null; neither proves a successful import or an operational workflow.
`jsonschema` is used for contract validation; Pillow is needed for source image
and geometry inspection; NumPy is needed for geometry inspection. Missing packages
are reported, not installed, and do not change the query's success status.
This is not a general machine health check. No credentials, providers, external
executables, models, or runtime readiness are inspected.

Format evidence comes from `model3d.py` (geometry capability),
`model3d_validation.py` (JSON contracts, declared image encodings and delegated
geometry inspection), and `rig_import.load_character_model` (geometry authority).
Inspection is restricted to valid declared contracts and their referenced payloads.
The contract schemas can name other formats as intent; that does not implement
creation or export. The standalone Model3D CLI has no creation, export, or native
write operation. There is no complete shared format inventory API, so this list is
bounded to the inspector rather than an inventory of adjacent rig or primitive APIs.
Providers reports no integrated Model3D provider registry or execution. These
queries and any future aliases do not implement generation, reconstruction,
scheduling, or review approval. This interface is a bounded discovery feature,
not completion of issue #10 or its child relationship #50.
