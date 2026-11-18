# Native laboratory performance adapter

`director_scene.build_performance(asset_root, destination, blender=...)` extends
the immutable procedural assets from `director_assets.build_assets` into a
bounded five-second performance. The producer directory is copied through bounded regular-file reads into
`assets/`. The copied report inventory, original GLBs, identities and all four
sealed registry versions are checked against the staged bytes. The inventory
includes the producer's empty coordination files. The registry retains private
permissions and reopens at the published destination. The receipt records the
checked staged report digest. Animation is authored only in `actor-rigs/`, a
separate working copy, so the published producer inventory remains valid.
The destination must be new. Publication uses the shared exclusive rename.

Aster and Moss use the existing native humanoid rigs, skinning, quaternion
math, pose clips, forward kinematics and inverse kinematics. Their head and
arm channels are independently authored. Aster reaches a tabletop key and
lifts it. Moss turns and gestures toward the console. Aster starts outside the
workbench and arcs the reaching hand above its edge. The reach is rejected if
its measured native IK residual exceeds one centimeter.

The key rests within the tabletop footprint through frame 72. At frame 72 its
unchanged transform becomes the fixed offset from Aster's native `hand.right`
bone. Aster owns the key from that frame onward. The adapter checks the hand
vertex gap at pickup, tabletop support and carried key clearance. The
attachment provides a deterministic engineering fixture without grasp physics.

The canonical Scene3D base hierarchy supplies actor placement, two perspective
cameras and two lights. Native rig evaluation supplies skeletal animation and
skinned vertices. These authorities remain separate: the adapter does not add
a competing general scene graph or animation engine. Camera cuts select the
wide view before frame 60 and the investigation view afterward. At all 121
authored frame boundaries the adapter checks both robot frusta and conservatively
tests their triangle bounds and the key bounds against the actual lab mesh boxes.

Every actor has one full five-second native clip and two exact 2.5-second
segments. The Scene3D shot bindings use the segments in their own local time:
`*-establish` and `*-discovery`. This prevents a second shot from replaying the
first half of the motion. The shared Scene3D evaluator plays the hash-verified
portable project at global frames 0 through 119. It provides all runtime key
transforms, fixed actor placement, world, lights, and the camera selection.
The Camera3D owner provides the selected world and projection matrices. Native
geometry is sampled at nine boundaries, including both sides of the cut and
pickup, with out-of-order replay across the cut. Frame 120 remains a separate
authoring and key-attachment endpoint.

The fixed Blender worker imports actual animated GLBs, preserves native
armatures and actions, adds the real laboratory and prop meshes, authors the
prop action, cameras and lights, and saves an editable `.blend`. A separate
Blender process reopens it with automatic script execution disabled. Its
samples must include both robots, every native mesh, both cameras and lights,
and the prop. All sampled vertices are compared with the native skinned
geometry in canonical meters. The sampled native reopen compares the key, camera
world matrices, and actual Blender projection matrices against shared playback.
The fixture contains zero LookDev material override pins. The native worker
imports the GLB materials and verifies that their mesh slots survive reopen.
This path evaluates fixture state and does not render or compare images. Visual
review remains unreviewed.

`performance.json` retains hashes, measured comparisons and explicit unreviewed
status. The invocation logs and jobs retain the exact original temporary paths
as historical execution evidence; they are not portable runtime references.
The Scene3D project adapter supplies relative, hash-locked runtime files for
rendering. This operation does not render frames or encode a film; the shared
Render3D owner performs those later steps.
