# Reconstruction evidence C/D/E/F

Run from the repository root with the optional rig test dependencies:

```sh
python scripts/model3d_reconstruction_demo.py --destination output/new-reconstruction-demo
```

The destination must be new. Source artwork defaults to the retained Milo canonical PNG; override it with `--artwork` for a compatible alpha image. C preserves original bytes and explicitly records its stride-16 alpha sampling, threshold and assumed metric scale. D builds authored contour-with-hole, revolution and finite-base relief fixtures. E forward-renders a known box into calibrated silhouettes, then reconstructs a new voxel mesh from only those observations. F retains incompleteness/redundancy and structured contradiction/unknown-calibration diagnostics.

`acceptance.json` binds every real mesh, original observation, method report and occupancy artifact. Sources and source dimensions are retained exactly. Outputs remain partial and unreviewed. These workflows do not execute providers, infer hidden anatomy or establish full parent #10 acceptance. See [reconstruction APIs](../../references/model3d/reconstruction.md).
