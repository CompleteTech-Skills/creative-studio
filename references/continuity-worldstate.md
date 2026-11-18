# Persistent structural world state (issue 117)

Temporal documents explicitly select schema version `1.1.0` to retain rich world
state in the existing `IdentityRegistry` temporal contents, heads, events and
snapshots. Version `1.0.0` keeps its original closed shape and simple replay;
unknown versions and mixed-version event streams reject. No second registry,
physics store, acquisition controller or approval store is introduced.

The timeline's `world_state` contains canonical meter-based mapped locations,
persistent entities and scoped facts. Scene entries declare `location_id` and
`incoming_world_state`. These incoming declarations must match replay at that
scene's earliest story boundary. They cannot reset a costume, door, light or
character's knowledge merely because a different scene file was loaded.
Native Scene3D still owns project closures, node assets, actual hierarchy/world
transforms, visibility and exact scene/shot version bindings. Persistent assets
must agree with mapped native node assets or their exact mesh content. Character
revision pins resolve through the existing canonical identity registry.

Each persistent entity distinguishes canonical asset identity, world entity ID,
unique shot instance ID, native node ID and declared render object IDs. Render IDs
are retained mappings, not evidence that a renderer ran. Two independent entities
may use the same canonical mesh. Duplicate persistent, instance and render IDs,
unknown parents, parent cycles and unmapped locations reject atomically.

`physical_presence`, native `render_visible`, and `visibility_reason` are distinct.
An out-of-frame or occluded character can remain physically present with its
native render visibility enabled. Explicit entry/exit events change presence;
occlusion/reveal changes camera-observation state. `world-relocate` records an
exact before/after canonical transform, mapped locations, authored mechanism and
cause. Story time jumps use the existing exact temporal mapping rather than
reinterpretation of pixel coordinates or implicit teleportation.

Costume selections bind exact canonical costume records, compatible character
revision pins, clothing, materials, textures, layer visibility, attachments,
combination evidence, appearance/wear, optional physics evidence and reference
renders. Required canonical accessories and material overrides cannot vanish
from a selection. Accessory states retain equipped/removed/damaged records.
These are structural authored records; retained references confer no new visual
approval, cloth simulation or geometric compatibility certification.

Interactive entity fields retain bounded door, light, device, damage, weather,
time-of-day, atmosphere and lighting state. Camera-specific artistic treatment
remains with its native camera/look owners. No door deformation, light rendering,
weather solver or image approval is inferred from a stored state transition.

Facts carry an explicit project/world/scene/character/trait/shot/time-bounded scope,
category, certainty, value, causes and evidence. Knowledge names a character.
Unknown and unresolved values remain null; inferred assertions require retained
evidence. Causes must identify the current or earlier applied event; event
prerequisites must already have applied. Temporary facts have an explicit
half-open lifetime. Snapshots retain the full authored fact history and separately
list `active_fact_ids` for the queried exact time and scene/shot context.

Changes use strict `world-entity`, `world-fact` and `world-relocate` operations with
before-state preconditions. Reasons restrict which fields may change. Entity
identity and instance inventories remain immutable. Native source references,
revision CAS, event ordering, branch conflict detection, snapshot caches and
recovery continue through the existing temporal owner. Evaluator fingerprints
include the world-state implementation. Changed cached results require the
existing explicit cache-discard recovery; old evidence is never rewritten.

Run `python scripts/continuity_contracts.py --generate-schemas profiles --check`
and `python -m unittest discover -s tests -p 'test_rig_continuity_worldstate.py' -v`
in the optional rig environment. The retained synthetic trace is produced by
`python tests/continuity_worldstate_probe.py NEW_DIRECTORY`. Its real native
Scene3D closure and exact snapshots test structural persistence. They establish
no production artwork, full motion/contact/physics acceptance or visual approval.
Exclusive prop ownership/handoff transactions remain issue 118. Diff, last-known
and full entity-history query extensions are tracked under issue 116 and must
consume this same replay authority.
