# Native five-shot laboratory narrative

`scripts/director_narrative.py` authors a fixed, deterministic interpretation of
“Create a 30-second silent 3D animation of two robots exploring an abandoned
laboratory.” It consumes the existing Aster and Moss native rigs. It does not
call a provider or replace the rig, Scene3D evaluator, renderer, job controller,
or publication service.

The production timeline has 720 frames at exactly 24 fps:

| Shot | Half-open global frames | Action |
| --- | --- | --- |
| entry | 0–120 | Two distinct robots enter the laboratory. |
| exploration | 120–264 | Independent gaze and planted-foot traversal. |
| discovery | 264–432 | Aster locates and picks up the laboratory key. |
| cooperation | 432–576 | Separate object-local grips support one ownership transfer. |
| resolution | 576–720 | Moss raises the key and the laboratory lights respond. |

Root placements use canonical right-handed Y-up, Z-forward meters. Native
skeletal clips remain local, with explicit Scene3D root tracks. Native IK,
gaze, skinning, finger controls, sockets, animation clips and the existing
Animation3D interaction contract provide the authored behavior. The handoff
uses separate object-local grip transforms; coincident palms or an increased
socket-distance tolerance do not demonstrate a valid transfer.

`scripts/director_narrative_project.py:populate_narrative(source_root, stage)`
constructs documents only inside an empty application-owned private build tree.
It preserves the complete original source closure under `source-original/`,
checks unchanged geometry, skeleton and material identities, versions the new
character assets and native motion, and loads the completed closure through the
canonical Scene3D loader. Final publication belongs to the canonical Scene3D
snapshot service and must bind the expected completed source project hash.

The resulting project registers five production shots and four separate proof
shots. Consumers must select the explicit `production_shots` references returned
by the builder rather than concatenating every registered shot. Alternative
handoff views and a same-camera lighting pair are proof inputs, not extra film
segments. Geometry, camera and light controls require actual native render
requests, decoded outputs, source receipts and independent inspection.

The private authoring result is **not an acceptance receipt**. Closure validation
and passing pose tests do not establish full-sequence collision freedom, final
publication safety, rendered readability, artistic approval, an editable
full-sequence Blender project, or a delivered silent movie. Those gates remain
explicitly incomplete until their actual artifacts and current-source checks
exist. The original Director 30 and 31 films and their receipts remain immutable.

Native unit tests use the optional rig environment and the repository's bounded
test launcher. The new narrative tests are isolated from the legacy 2D suite.
