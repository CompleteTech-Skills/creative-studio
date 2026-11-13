"""Stable public Python facade for Hatch 3D v1 (optional dependencies required)."""
from rig_errors import RigError, VERSION as __version__
from rig_models import Geometry, Mesh, RigAsset, SkinWeights, TextureImage, load_rig
from rig_import import load_character_model
from rig_geometry import analyze_geometry
from rig_templates import load_rig_template, generate_skeleton
from rig_corrections import apply_rig_corrections
from rig_skinning import generate_skin_weights, evaluate_deformation
from rig_skeleton import evaluate_fk
from rig_ik import solve_ik
from rig_animation import evaluate_animation, blend_poses, crossfade, bake_clip
from rig_retarget import retarget_animation
from rig_validation import validate_rig, test_motion_ranges
from rig_render import render_rig_preview
from rig_gltf import export_gltf
from rig_blender import export_blender, render_blender_preview
from rig_package import package_rig, restore_rig
from rig_jobs import create_run, save_rig
from rig_facial import create_morph_target, edit_morph_target, expression_catalog, apply_expressions, gaze_at

from rig_grips import create_grip_pose, validate_grip_pose, solve_grip_pose
