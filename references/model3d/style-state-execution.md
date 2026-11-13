# Executed style aspects and catalog rigid poses

`model3d_semantic_style.apply_style_materials` resolves the real frozen style
registry, then changes actual core PBR parameters on selected mesh-bearing parts.
Supported selections are `clay` (metallic0/roughness0.92), `3d-toy` (0/0.3) and
`plush` (0/1). These are explicit rough ceramic, painted plastic and matte cloth
approximations. They preserve geometry, colors, opacity, emission, texture bytes
and UVs; they do not generate topology, hair/strands, learned maps or arbitrary
renderer shaders. Unsupported styles and extension-driven materials fail instead
of recording an unexecuted adaptation.

The operation appends revised material slots, preserving original source slots
and opaque extras. Part evidence records source slot/hash, before/after factors
and registry snapshot. The exact registry contract and supported adaptations stay
in the immutable semantic companion. Unselected component appearance/resources
remain unchanged; normal dependency invalidation and strict identity rules apply.
The resulting GLB/glTF contains real changed PBR values, verified by reimport.
Actual lit appearance and human review remain separate authoring gates.

`apply_catalog_state(asset, state_id)` loads the existing catalog, checks its exact
snapshot hash against the stored mapping, then evaluates the represented saved
rigid pose through `apply_pose`. Missing, unavailable and stale state mappings
fail explicitly. Actual native mesh positions change where that pose moves a
part. This is execution of a rigid pose, not fabricated skeletal animation or
coverage of every catalog state. Catalog semantics and the fixed Codex profile
remain unchanged.

```bash
python scripts/model3d_semantic_style.py style --source revision1 --style clay --part body --destination revision2
python scripts/model3d_semantic_style.py state --source revision2 --state waving --destination revision3
python scripts/demonstrate_model3d_style_state.py --source examples/model3d-semantic/robot/native --destination output/new-style-state-proof
```

The source snapshot must already contain the relevant saved pose and explicit
catalog mapping. Each command saves a new immutable destination and returns
structured failure diagnostics. Tests execute actual native GLB export/reimport
to prove selected PBR changes and unchanged source geometry/colors, actual
catalog-to-pose displacement, unavailable/stale rejection, unsupported shaders
and preservation of opaque material extras. No rendered or artistic approval is
inferred from those measurements.
