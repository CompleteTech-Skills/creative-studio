# Camera3D optics requirement-to-test traceability (#76)

This table maps each task of work package #76 (Camera3D 2/8: projection,
physical optics, focus and camera hierarchy) to the tests that exercise it.
Every test name below is checked to exist by
`tests/test_rig_camera3d_color.py::CameraColorTests::test_traceability_names_existing_tests`,
so a renamed or deleted test fails ordinary discovery instead of leaving a
stale row.

Evidence terms follow the repository vocabulary: a row is *implemented* when
the code exists, *tested* when the listed host tests pass, and *independently
verified* only when the listed native Blender gate has run and produced a
hashed `acceptance.json`. None of these rows is visual or production approval:
the native fixtures are engineering scenes, and every `acceptance.json`
records `production_visual_approval: false`.

## Task 1: view, projection, unprojection, rays, frusta, parent-world transforms

Implementation: `scripts/camera3d_math.py`, `scripts/camera3d_blender.py`
(`camera_world`).

| Requirement | Tests |
| --- | --- |
| Perspective projection matrix and clipping reference | `tests/test_rig_camera3d_contracts.py`: `test_perspective_independent_lens_field_of_view_and_clipping_reference` |
| Orthographic projection, shift and pixel aspect | `tests/test_rig_camera3d_contracts.py`: `test_orthographic_depth_independence_shift_and_pixel_aspect`; `tests/test_rig_camera3d_optics.py`: `test_orthographic_depth_independence` |
| Project/unproject round trip and center rays | `tests/test_rig_camera3d_contracts.py`: `test_roundtrip_unprojection_and_actual_center_rays` |
| Ray-plane and frustum intersection | `tests/test_rig_camera3d_contracts.py`: `test_actual_ray_plane_and_frustum_intersection_reference` |
| Parent order, look-at, Blender camera basis | `tests/test_rig_camera3d_contracts.py`: `test_parent_order_look_at_and_blender_camera_basis_are_distinct_from_object_basis` |
| Scene parent and rig ancestor composition, cycle rejection | `tests/test_rig_camera3d_optics.py`: `test_parent_and_rig_offsets_compose_and_cycle_rejected`, `test_missing_scene_parent_and_invalid_parameters` |

## Task 2: focal length, sensor fit, shift, clips, orthographic scale, FOV

Implementation: `camera3d_math.optical_gate`, `field_of_view`,
`with_field_of_view`, `scene_camera_optics`; `camera3d_blender.optical_plan`.

| Requirement | Tests |
| --- | --- |
| Sensor fit, gate, shift, overscan; singular projection rejected | `tests/test_rig_camera3d_contracts.py`: `test_sensor_fit_gate_shift_overscan_and_singular_projection_rejection` |
| FOV derivation and Blender shift sign/units | `tests/test_rig_camera3d_optics.py`: `test_fov_gate_and_shift_mapping` |
| FOV edits derive focal length without mutating the source | `tests/test_rig_camera3d_optics.py`: `test_fov_edits_derive_focal_without_mutating_source` |
| Scene3D embedded camera keeps vertical FOV and orthographic height | `tests/test_rig_camera3d_optics.py`: `test_native_scene_camera_keeps_vertical_projection` |
| Orthographic scale conversion | `tests/test_rig_camera3d_optics.py`: `test_orthographic_depth_independence` |

## Task 3: physical DOF, focus, aperture, exposure, motion blur, white balance

Implementation: `camera3d_blender.optical_plan` and `apply_camera`;
`scripts/lookdev_color.py` (shared color authority); `scripts/camera3d_color.py`
(camera binding).

| Requirement | Tests |
| --- | --- |
| DOF blade limits, orthographic DOF rejection, exposure/shutter bounds including out-of-range integers | `tests/test_rig_camera3d_optics.py`: `test_missing_scene_parent_and_invalid_parameters`, `test_orthographic_depth_independence` |
| Shared white balance bounds (1800 to 100000 K, tint -500 to 500, exposure -32 to 32 stops); rejected, never clamped | `tests/test_lookdev_color.py`: `test_supported_kelvin_tint_and_exposure_bounds`, `test_native_unsupported_values_reject_instead_of_clamping` |
| Color profile hash covers white balance, exposure and encoding | `tests/test_lookdev_color.py`: `test_authoritative_profile_hash_covers_wb_exposure_and_encoding` |
| Camera takes exposure only from the shared profile | `tests/test_rig_camera3d_color.py`: `test_uses_shared_hash_and_exposure_without_mutating_inputs`, `test_shared_exposure_supports_the_existing_camera_domain` |
| PNG/sRGB and EXR/linear-sRGB output agreement | `tests/test_rig_camera3d_color.py`: `test_matches_native_output_contracts_and_rejects_disagreements` |
| Tampered color or nonrigid pose rejected before native mutation | `tests/test_rig_camera3d_color.py`: `test_rejects_tampered_color_before_touching_the_native_scene`, `test_rejects_nonrigid_pose_before_native_mutation` |
| Synthetic backend failures preserve the previous state or the completed activation | `tests/test_rig_camera3d_color.py`: `test_native_color_rejections_preserve_existing_camera_and_scene`, `test_late_render_clamp_rolls_back_shared_color_and_render`, `test_late_camera_hash_failure_restores_existing_camera`, `test_old_camera_cleanup_failure_keeps_committed_camera_and_color` |
| Silent color/output substitutions, late RNA changes and permitted float rounding | `tests/test_rig_camera3d_color.py`: `test_effective_color_fields_reject_silent_backend_substitutions`, `test_final_readback_detects_late_field_change_and_accepts_float32` |
| Scene-owned camera tags, legacy shared tags and rejection of dependency-graph color changes with rollback | `tests/test_rig_camera3d_color.py`: `test_shared_camera_scene_only_and_explicit_activation`, `test_ambiguous_legacy_shared_tag_is_preserved`, `test_update_drift_restores_profile_camera_and_bindings`, `test_actual_camera_shared_white_balance` |
| Standalone camera exposure or activation clears stale tags and rolls back all affected tags on failure | `tests/test_rig_camera3d_color.py`: `test_standalone_exposure_and_camera_switch_binding_lifecycle`, `test_standalone_invalidation_failure_restores_all_bindings` |
| Foreground comparison rejects repeated transforms and identical incorrect linear radiance | `tests/test_rig_camera3d_color.py`: `test_foreground_probe_rejects_wrong_radiance_or_second_transform` |

**ISO is unsupported.** No ISO or ISO-equivalent sensitivity simulation is
implemented or claimed. Exposure is expressed only in stops through the shared
color profile. Browser-side white balance is also unsupported by this binding.

## Task 4: verification in actual Blender output

| Requirement | Native gate | Enabling flags |
| --- | --- | --- |
| Projection matrices match Camera3D across sensor fits, gate policies, shift, overscan and non-square pixels; DOF, exposure and motion blur change actual renders; state survives save/reopen | `tests/test_rig_camera3d_optics.py`: `test_actual_blender_optical_acceptance` (runs `scripts/verify_camera3d_optics.py`) | `HATCH_CAMERA3D_BLENDER_TEST=1`, `HATCH_CAMERA_EVIDENCE` |
| Neutral, temperature, tint and one-stop PNG foregrounds match independent direct Blender renders. EXRs preserve known foreground radiance and match each other across the frame. Source material and world color, JPEG transitions and reopened bindings are checked | `tests/test_rig_camera3d_color.py`: `test_actual_camera_shared_white_balance` (runs `scripts/verify_camera3d_color.py`) | `HATCH_CAMERA3D_COLOR_TEST=1`, `HATCH_CAMERA_COLOR_EVIDENCE` |

Both native gates also need `HATCH_BLENDER` (pinned Blender 4.5.3) and
`HATCH_CAMERA_TEST_SITE` (a jsonschema installation for Blender's Python 3.11).
Without the explicit flag each gate reports a skip, and a skip is not evidence.

## Recorded commands, observed results and hashes

Evidence is not committed to the repository. Each native gate writes its
rendered files, the reopened `.blend`, `resolved-inputs.json` and an
`acceptance.json` containing the observed pixel differences, projection errors,
Blender version and the SHA-256 of every output file and of the source scripts.
The color report also binds the invoking test, Blender workflow, fixture inputs,
dependency requirements and local helpers used for validation and hashing.

The schema-math gate runs the Camera3D and LookDev color test modules. The
Blender 4.5.3 job in `.github/workflows/rig-3d.yml` enables both native gates
and uploads these directories in the `blender-evidence` artifact:

- `camera-optics-gate/`: the optics gate's `acceptance.json` and outputs.
- `camera-color-gate/`: the white balance gate's `acceptance.json` and outputs.

Cite a specific CI run and artifact when claiming independent verification; a
passing host-only run establishes *tested*, not native behavior.
