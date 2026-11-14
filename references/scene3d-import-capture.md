# Captured native import resolver

`scene3d_import_capture.resolve_captured_import(bundle, asset_reference,
source_root, verified_inventory=None)` is a context manager for native consumers.
The bundle must come from the canonical project loader and contain its full
inventory. An explicitly supplied inventory must equal that inventory.

The complete registered closure is admitted into the shared capture's private
tree before any native rig loader runs. The resolver checks the exact asset pin,
registered import report and hierarchy, native manifest bytes, typed skeleton pin
when present, original source identity and component mesh membership. Static
imports resolve through their registered native manifest even when their public
asset has no skeleton capability.

Inside the context, consumers receive `imported` (the canonical ImportedScene),
`rig` (loaded native RigProject), `native_root`, the private project `root`, its
verified `bundle`, and the exact `asset_reference`. Keep the context open through
all native processing. The yielded directories are removed on exit. No caller
source path is returned as a native read location. Do not retain filesystem paths
from a completed context or fall back to mutable latest assets.

This resolver supplies exact input identity and topology. It does not choose a
shot, evaluate animation timing, blend clips and poses, or approve rendered output.
Pose and temporal preview consumers must still use the canonical selected-shot
scene evaluator.

Validation uses real imported static and skinned GLB fixtures, copied project
snapshots, native geometry evaluation, source mutation after admission, and
changed-byte or wrong-reference rejection. Run:

```sh
PYTHONPATH=scripts:tests python -m unittest test_rig_scene3d_import_capture -v
```

Use the repository's configured Khronos validator and acquire the shared native
validation lock in the coordinated development environment.
