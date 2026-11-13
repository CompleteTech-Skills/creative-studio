# Camera versions, editorial timing and transitions

The Camera3D sequence owner samples the existing camera/shot/storyboard
documents without changing production v1 camera or audio behavior. Source
resolution still belongs to `camera3d_contracts.load_registry`; scene evaluation,
motion and image rendering remain with their native owners. The compositor
combines actual rendered pixels supplied by those owners.

## Exact sequence sampling

`CameraSequence.load(root, registry, storyboard_reference)` loads an exact
hash-bound registry closure. `CameraSequence(storyboard, documents)` accepts an
already verified in-memory closure; it does not authenticate arbitrary caller
objects. Both validate editorial rate, durations and transition overlap.

A shot's transition applies to its entrance. The first shot must therefore use
a cut. For previous duration A, incoming duration B and overlap N, the incoming
shot starts at `A-N` and the combined output contains `A+B-N` frames. At overlap
index k, outgoing local frame is `A-N+k`, incoming local frame is k. Display
intervals are half-open; no terminal frame is emitted. Hard cuts switch directly
from outgoing local A-1 to incoming local 0 with no camera interpolation.

`sequence.sample(frame)` returns one or two `FrameSource` records with exact
shot references and local frames. Random seeking gives the same result as
forward playback. Three-shot overlap is rejected because it would require an
additional explicit compositing graph. A middle shot must have enough frames
for both its entrance and the next shot's entrance overlap.

`sequence.composite(frame, provider)` calls
`provider(shot_reference, local_frame)` for each required view. A cut copies the
single supplied image. An overlap composites the two supplied RGBA images.
This callback is an integration boundary, not a mock render backend or proof
that geometry was rendered. The standalone preview below uses retained actual
Blender-rendered fixtures and identifies their exact original bytes.

## Color, alpha and transition semantics

The implemented raster path accepts decoded straight-alpha 8-bit RGBA in sRGB.
It converts RGB to linear sRGB, premultiplies by alpha, performs the transition,
unpremultiplies where alpha is nonzero, and encodes sRGB output. Hidden RGB in
fully transparent pixels does not bleed into a dissolve. HDR/EXR and arbitrary
color spaces require the rendering/color owners; this PNG path does not label
8-bit processing as HDR.

Progress samples temporal centers: `(k+0.5)/N`. A one-frame overlap is the
midpoint, and there is no duplicated endpoint frame. `smoothstep` applies
`t²(3-2t)` to that exact rational progress. The implemented transitions are:

- Dissolve: premultiplied linear interpolation of both actual views.
- Fade: outgoing view to the configured solid color, then solid color to the
  incoming view. The color's sRGB and alpha are respected.
- Wipe: incoming pixels enter from the named left/right/up/down edge; pixel
  centers determine the boundary, avoiding resolution-dependent off-by-one
  interpretation.
- Cut: the sequence selects the new camera view at the exact frame boundary.

Default image admission is at most 1,048,576 pixels per view, configurable to a
maximum of 16,777,216. Inputs must match dimensions and be RGBA. No implicit
rescaling, frame-rate conversion, camera interpolation or audio creation occurs.

## Immutable camera management

The additive `camera3d-catalog` document lists exact camera version references,
an explicit enabled subset and an optional active reference. Disabling the
active camera requires explicitly clearing or replacing active selection.
No arbitrary camera is silently selected. A sequence supplied with a catalog
rejects shots using disabled camera versions.

`update_catalog` returns a new validated version with a strictly increasing
semantic version. It adds/removes exact references, enables/disables cameras,
and changes active selection. Removal fails for references in the supplied
actual project dependency closure. The project owner must supply that closure;
an empty reference list is not an assertion that a whole project was inspected.
Older camera versions remain usable by selecting their original exact
references. No stored source is mutated or deleted by catalog commands.

`revise_camera` creates a newer camera revision, or duplicates it under a new
stable ID. Renaming changes its display metadata; it does not change its ID.
Transforms and optical references are copied. `resolve_camera` creates a
detached shot evaluation from a camera and its resolved optics, including
explicit transform or separately resolved optical overrides. Other shots retain
their original camera and optics. Shared immutable targets stay shared without
shared mutable evaluation state.

`export_document` publishes validated canonical JSON to a new file using the
same exclusive rename authority as archives. The caller chooses a project or
global preset directory. These are trusted output directories: unrelated
processes must not replace their ancestors. Existing files and racing leaf
destinations are preserved. Imported untrusted sources are read through the
asset library's bounded no-follow descriptor traversal, including Windows
reparse-point checks. Export is a serialization operation, not a second asset
database or a replacement for project transactions.

## Presets and spatial storyboard planning

`camera3d_presets` supplies ten optical/transform presets: standard 50 mm,
wide 24 mm, cinematic 35 mm, portrait 85 mm, telephoto 135 mm, shallow-depth
portrait, deep-focus environment, orthographic technical, isometric and
top-down. `native_camera_preset` returns an existing `camera3d-preset` document
plus its exact canonical optical dependency. It supports duplication, validated
custom overrides and import/export through native documents. Resetting means
recreating the unchanged versioned default; stored custom versions remain.

Twelve movement presets emit the Camera3D motion owner's exact parameter names:
slow dolly in/out, horizontal tracking, vertical pedestal, crane reveal,
half/full orbit, character follow, over-the-shoulder tracking, handheld, static
tripod and dolly zoom. Twelve shot presets combine meaningful optics, movement
and subject framing: establishing, two-shot, close-up, medium, full-body,
over-the-shoulder, hero, product showcase, entrance, exit, action tracking and
cinematic reveal. Preset definitions do not themselves render footage.

`plan_storyboard_panel` consumes supplied real scene subject bounds and a
strict panel record. It computes a canonical camera transform, lens/focus,
movement choice, subject target and frame duration. It supports an explicit
camera side or eye-level/high/low/top-down angle, center/left-third/right-third
composition, shot size, lens preference, subject target and validated transition.
Narrative purpose, scene/shot IDs, notes and optional image/framing references
remain in the unchanged source panel. No image is interpreted as measured 3D
geometry. The plan records inferred side/lens assumptions and always requires
actual render review; occlusion and artistic quality cannot be proved from
bounds. Camera placement is intentionally editable before publication.

## Continuity diagnostics

`boundary_continuity` measures evaluated camera height and lens change, subject
world displacement, projected size/visibility/screen side, motion screen
direction, explicit line-of-action crossings, supplied bone/eye gaze direction
and supplied prop state changes. Warnings carry actual numeric measurements.
They remain advisory so intentional unconventional edits can be retained.
Subject positions and gaze must come from their authoritative scene/rig
evaluators. The function does not infer occlusion, anatomy, narrative intent or
visual approval from metadata.

## Reproduce pixel evidence

With the optional native test environment:

```bash
python -m unittest discover -s tests -p test_rig_camera3d_sequence.py -v
python scripts/camera3d_contracts.py --check
python scripts/camera3d_sequence.py \
  --outgoing examples/rig-3d/humanoid/preview/blender/frame-0000.png \
  --incoming examples/rig-3d/quadruped/preview/blender/frame-0000.png \
  --destination NEW_DIRECTORY --duration-frames 8
```

The command creates 24 decoded PNG transition frames, an inspectable contact
sheet and a manifest binding both retained source hashes and every output hash.
The source images are original procedural engineering fixtures rendered by
Blender in the retained rig demonstrations. This command does not claim a new
Blender invocation, continuous camera motion, a silent encoded film, or
production-art approval. The complete camera mission still requires native
renderer/editor integration and the five-shot demonstration in #80–#82.


## Actual corresponding-camera previews

`verify_camera3d_previews.py` uses the Camera3D optical owner's actual Cycles
adapter to render all ten camera presets, twelve shot plans and four explicit
storyboard angle/composition plans. An original procedural cube has declared
canonical world bounds. Each view compares its rendered alpha silhouette bounds
with the canonical projection of those same bounds, within three pixels at
192×128, and records the complete plan, output hash and actual pixel count.
`previews.blend` preserves the actual cameras and geometry for inspection.
The static shot previews exercise authored placement and optics. A second pass
evaluates all twelve movement presets through the motion owner and the target
aim constraint owner, rendering eight consecutive display frames at 8 fps each.
Fixed background depth anchors expose actual tracking/follow/dolly parallax.
The static tripod must retain identical pixels; every other movement must show
a measured raster difference. The host gate creates animated GIF previews from
those actual PNGs. These are short engineering previews, not the #82 film.

```bash
HATCH_CAMERA3D_PREVIEW_TEST=1 HATCH_BLENDER=/path/to/blender \
HATCH_CAMERA_TEST_SITE=/path/to/blender-python-jsonschema \
HATCH_CAMERA_PREVIEW_EVIDENCE=NEW_DIRECTORY \
python -m unittest discover -s tests -p test_rig_camera3d_sequence.py -v
```

The hosted Blender job executes this explicit native gate and retains its PNGs,
plans and measurements. After Blender completes, the host gate composites 24
dissolve/fade/wipe frames from the corresponding establishing and close-up
camera renders and verifies both source hashes against the native report.
These engineering previews establish corresponding
camera output and framing, not production visual approval, motion-preset film
acceptance, multi-camera editor review, or the five-shot demonstration.


`sample_movement_preset` is the production preset sampling entry point. It
passes the exact frame/fps, authored base pose, optics, parameters and evaluated
world target to `camera3d_motion.sample_motion`. Dolly/tracking/pedestal/crane,
orbit/follow and dolly-zoom presets then use the constraint owner's
`look_at_world` to retain the supplied subject. Static tripod and handheld retain
authored orientation. An aimed preset without an actual target fails explicitly.
The returned dependency hash binds the motion sample and aim algorithm/version
and the resulting pose. Presets do not infer a scene target or resolve arbitrary
source references; those remain with the authoritative scene/project owners.
