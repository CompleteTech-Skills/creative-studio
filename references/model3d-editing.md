# Model3D semantic editing

`model3d_semantic` provides copy-based component editing over the existing native
mesh authority. Import GLB/glTF/OBJ through the owning native snapshot path, then
author supported part labels, hierarchy, local affine matrices, pivots and
attachment points. Imported triangle meshes start with unknown semantics; a
2D mask or bounding box does not prove anatomical segmentation. Native source
bytes and original hierarchy remain retained even when editing an explicit
world-space mesh view. When #45 is installed, `from_assembly_ir` reuses its
validated actual local meshes, transforms, anchors and named hierarchy directly.

The Python operations are `select`, `group_parts`, `transform_part`, `reparent_part`,
`set_visibility`, `label_part`, `duplicate_part`, `replace_part`, `split_part`,
`combine_parts`, `change_material`, `define_pose` and `apply_pose`. They return
new revisions and leave earlier objects/files intact. Splits require complete
authored face partitions; combinations retain source ancestry and transport
compatible attributes/morphs/materials. Duplicate internal attachment targets are
remapped within the new subtree. Reparenting preserves world placement unless
explicitly disabled. A rigid pose must move actual represented geometry; it
does not imply skinning, IK, solved contacts or functioning animation.

`map_decomposition` checks an existing source-bound decomposition through its
native loader and captures the exact original, annotation, identity, component
and mask bytes. An explicit component correspondence retains its 2D pivots,
attachments and uncertainty; native depth and 3D placement remain authored.
`identity_contract` reuses the existing identity validator, binds exact original
metadata and measures only supplied part, proportion, color and mirror rules.
Reports list unmeasured rendered/perceptual/source requirements. Strict rules
reject violating edits, without inventing an absolute identity score.
`bind_style` freezes the existing registry snapshot plus actual adaptation notes.
`map_states` maps existing catalog definitions to real rigid poses or unavailable;
it does not alter the fixed nine-state Codex contract or claim completed clips.

`register_derivative` binds an exact artifact hash to selected component and
optional style/identity inputs. Changed dependencies become stale while unrelated
work remains current. Immutable snapshots are saved only into new destinations.
The loader verifies a bounded no-follow capture of the declared metadata/native
geometry/evidence closure before parsing. Unknown schemas, malformed records,
source replacement races, path aliases, broken hashes and stale dependency claims
fail. Geometry stays in the native content-addressed format.

```bash
python scripts/model3d_semantic_cli.py import --source input.glb --destination output/revision1
python scripts/model3d_semantic_cli.py inspect --source output/revision1
python scripts/model3d_semantic_cli.py edit --source output/revision1 --operation transform --spec move.json --destination output/revision2
python scripts/model3d_semantic_cli.py export --source output/revision2 --destination output/hierarchical-export --format glb
```

The standalone command installs no providers and uses the repository's optional
native geometry dependencies. It returns structured JSON/nonzero failures.
Expected argument failures preserve the prior revision. Source replacement
explicitly selects an existing material slot; known fixture geometry promotes
the resulting fixture classification and preserves original classifications.
Imported skins/animation are refused by this mesh-only path and remain in the
native rig workflow. Central dispatcher routes are registered by the integration
owner after review; direct commands above are the executable source interfaces.

Actual GLB/glTF exports contain structural `part:<id>` nodes, separately named
native mesh children, semantic extras, PBR/UV/image resources and an immutable
native companion. Independent reimport checks inventories, numeric geometry,
materials, vertex attributes, original images and float32 face preservation.
Hidden geometry remains in the companion and is explicitly omitted from the
visible export. Arbitrary source constraints and authoring modifiers are not
claimed to survive ordinary glTF.

Limits include 4096 parts, cumulative depth 128, native ceilings of 1,000,000
vertices/2,000,000 triangles, 256 MiB total evidence, 32 MiB per evidence record,
and a conservative 12× edit working-set admission within the native 2 GiB memory
budget. Native bundle allocation is checked before array decoding. Matrix and
integer precision, vertex/texture resources and file/JSON bounds are validated.
This is cooperative local editing, not an arbitrary Python sandbox or a claim
of machine power-loss durability.

Advanced vertex/UV/material editing, save/reopen/edit/reopen, actual shared-model
multi-angle identity renders and human review use the existing Blender/#49
authority. They are not inferred from API/unit success. Full #47 acceptance also
requires final #45/#46/#49 integration, combined regressions, independent final
review and ordinary merge. Detailed APIs: [semantic editing](model3d/semantic-editing.md).
