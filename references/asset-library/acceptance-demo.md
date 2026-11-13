# Two-scene library acceptance demonstration

Run in the pinned optional 3D environment with Blender 4.5.3 and the Khronos validator configured:

```sh
PYTHONPATH=scripts python scripts/demonstrate_asset_library_acceptance.py \
  --output NEW_DIRECTORY --blender /trusted/blender
```

This creates procedural engineering fixtures, not production artwork. Nine independent asset identities contain executable character, rig, skeletal animation, textured PBR material, texture, environment, prop, camera and light payloads. The robotic rig visibly articulates. Both scenes pin the same exact character/rig/clip/material/environment versions; instance placement and camera composition differ.

The workflow uses the existing library, dependency, search, scene-binding, export, package and Render3D owners. It saves and independently reopens a Blender character, retains native and GLB artifacts, exports the character, packages the full project closure, restores into an isolated library at another path, and compares decoded geometry/skin/clip/material signatures. Two animation samples in each scene are rendered before and after restoration. Pixel comparisons require exact decoded RGBA equality on the same pinned CPU backend; this is not a claim of cross-platform image identity.

A test package copy loses its required texture blob. Verification must report incomplete and restoration must leave no destination or replacement payload. Wrong bytes remain blocked. Restoring the exact bytes must restore verification and the texture's exact immutable identity.

`acceptance.json` records observed results, exact references, semantic signatures, renderer receipts and negative-test outcomes. `manifest.json` inventories every retained file except itself. Source fixtures, original/restored decoded GLBs, native Blender evidence, complete package/context, and actual rendered PNGs remain inspectable. The initial lightweight fixture-only run does not establish full workflow acceptance; use a successful complete command and its retained evidence. Hosted CI, independent review, artistic approval and the broader parent asset-library acceptance remain separate gates.
