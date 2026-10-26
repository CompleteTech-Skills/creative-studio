# Editor contract fixtures

These self-authored engineering fixtures exercise the #99 editor binding contract.
They are not an editable scene, browser demo or accepted production artwork.

`project.json`, `scene.json`, `node.json`, and `asset.json` form an exact editor
binding closure. The asset pins actual bytes from the retained Model3D robotic
manifest. The scene pins an intentionally unavailable Scene3D owner; the absent
`unavailable/scene.json` must never be generated or relabeled as a native scene.
`animation.json` is a standalone root example not bound in that base project.
The other roots illustrate intent/revision/session/derived/service records;
`response.json` explicitly records unavailable execution.

Run `python scripts/editor3d_contracts.py --validate FILE` for one root. Run
`test_rig_editor3d_contracts` for relational closure and negative controls. Structural
admission and exact JSON hashes cannot establish native resource truth, actual
persistence, rendering, browser behavior, source licensing or visual approval.
See `docs/scene-editor/architecture.md` for authority and operation boundaries.
