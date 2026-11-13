# Native laboratory performance adapter

`director_scene.build_performance(asset_root, destination, blender=...)` extends
the immutable procedural assets from `director_assets.build_assets` into a
bounded five-second performance. The source directory is copied through bounded
regular-file reads, and original GLB hashes remain bound to their identities.
The destination must be new; successful publication uses the shared exclusive
rename primitive.

Aster and Moss use the existing native humanoid rigs, skinning, quaternion
math, pose clips, forward kinematics and inverse kinematics. Their head and
arm channels are independently authored. Aster reaches a tabletop key and
lifts it; Moss turns and gestures toward the console. The reach is rejected
if its measured native IK residual exceeds one centimeter. This does not claim
a general-purpose grasp, collision solver or human-approved acting.

The key stays at its initial tabletop transform through frame71, then follows
Aster's native `hand.right` world transform from frame72 onward. Ownership is
explicitly absent before pickup and assigned to Aster afterward. No ownership
handoff between characters is claimed. The key transform is sampled from the
actual rig, not inferred from dialogue or metadata.

The canonical Scene3D base hierarchy supplies actor placement, two perspective
cameras and two lights. Native rig evaluation supplies skeletal animation and
skinned vertices. These authorities remain separate: the adapter does not add
a competing general scene graph or animation engine. Camera cuts select the
wide view before frame60 and the investigation view afterward. Both robots
must fit the selected camera frustum at the verification frames.

Every actor has one full five-second native clip and two exact 2.5-second
segments. The Scene3D shot bindings use the segments in their own local time:
`*-establish` and `*-discovery`. This prevents a second shot from replaying the
first half of the motion. Frame120 is the terminal boundary and is not a
rendered frame.

The fixed Blender worker imports actual animated GLBs, preserves native
armatures and actions, adds the real laboratory and prop meshes, authors the
prop action, cameras and lights, and saves an editable `.blend`. A separate
Blender process reopens it with automatic script execution disabled. Its
samples must include both robots, every native mesh, both cameras and lights,
and the prop. All sampled vertices are compared with the native skinned
geometry in canonical meters; prop samples and selected cameras must match
independently. Numerical consistency does not establish artistic approval.

`performance.json` retains hashes, measured comparisons and explicit unreviewed
status. The invocation logs and jobs retain the exact original temporary paths
as historical execution evidence; they are not portable runtime references.
The Scene3D project adapter supplies relative, hash-locked runtime files for
rendering. This operation does not render frames or encode a film; the shared
Render3D owner performs those later steps.
