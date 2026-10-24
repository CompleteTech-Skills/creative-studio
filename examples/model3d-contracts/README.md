# Read-only Model3D example

From the repository root:

```sh
python -m pip install 'Pillow>=10.1' -r requirements-rig.txt
python scripts/model3d.py validate --file examples/model3d-contracts/robotic.json --root .
```

`robotic.json` references the existing source and animated mechanical GLB in
`examples/rig-3d/robotic/`, without duplicating or changing those payloads. Actual
inspection reports 706 vertices, 1,368 triangles, 11 skin bindings and three clips.
The original mesh and exported asset are synthetic engineering fixtures; visual
and license approval remain pending/unknown, not fabricated.

For an independent portable check, copy this manifest and every path in the
verifier's `files` list into a new directory, preserving their relative paths, then
validate using that directory as `--root`. No original absolute paths are stored.
The automated test `test_concurrent_readers_and_fresh_directory_restoration`
executes that procedure, verifies equivalent output and requires failure after a
required model is removed. This is a verified dependency-closure copy, not a new
archive format or a claim that the full asset-library roadmap is complete.

Use `--structural-only` only to inspect the contract without payloads. Its successful
result explicitly does not establish that a model exists, is renderable or has been
approved. See `references/model3d-contracts.md` for API, budgets and limitations.
