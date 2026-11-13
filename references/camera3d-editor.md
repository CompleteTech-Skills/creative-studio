# Saved camera comparison

The local Editor3D camera workbench compares one or two saved native scene
cameras using actual WebGL scene geometry. Choose cameras and select **Compare
saved cameras**. Each pane has its own render context; the navigation camera and
saved camera documents are unchanged. Safe-area and thirds overlays can be
hidden independently of scene authoring.

The authenticated `camera-workbench/evaluate` route requires the exact saved
revision and closure digest. SceneGraph supplies evaluated camera poses,
including inherited transforms and native constraints. Camera3D supplies the
projection matrices and the explicit native scene optics conversion. Perspective
and orthographic comparisons use the same 256×144 frame size and square pixels.
The adapter rejects scaled or reflected camera poses and stale revisions.

This is a saved base-pose viewport comparison. Animated Camera3D tracks, authored
DOF, path/target editing, optical inspector controls beyond native Scene3D, shot
contact sheets and offline-render fidelity remain separate acceptance work.
Native scalar camera edits use the existing inspector transaction interface.
The browser describes lighting and antialiasing as preview approximations.

Run portable checks with `PYTHONPATH=scripts:tests python -m unittest discover -s
tests -p test_rig_camera3d_editor.py -v`. Set
`HATCH_CAMERA_EDITOR_BROWSER_TEST=1` to execute the actual two-camera WebGL test,
which verifies rendered pixels, exact snapshot binding, navigation preservation
and unchanged saved scene bytes. The dedicated camera-editor CI job enables it.
