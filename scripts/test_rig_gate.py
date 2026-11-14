#!/usr/bin/env python3
"""Run one named release gate; inherited failures stay visible."""
import argparse
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
GROUPS={
 'schema-math':['test_rig_math.py', 'test_rig_contracts.py', 'test_rig_compatibility.py', 'test_rig_camera3d_contracts.py', 'test_rig_scene3d_math.py', 'test_rig_physics_timing_cache.py', 'test_rig_landmarks.py', 'test_rig_editor3d_contracts.py', 'test_render3d_contracts.py', 'test_render3d_independent.py', 'test_animation3d_contracts.py', 'test_animation3d_independent.py', 'test_animation3d_closure.py', 'test_animation3d_owner_adapters.py', 'test_animation3d_admission_regressions.py', 'test_animation3d_clips.py', 'test_animation3d_evaluator.py', 'test_animation3d_edit.py', 'test_animation3d_poses.py', 'test_animation3d_diagnostics.py', 'test_animation3d_extrapolation.py', 'test_animation3d_saved_workflow.py', 'test_animation3d_clip_edits.py', 'test_animation3d_standalone_admission.py', 'test_rig_director_capabilities.py', 'test_rig_camera3d_optics.py'],
 'skinning':['test_rig_skinning.py', 'test_rig_weight_input_boundaries.py', 'test_rig_load_limits.py'],
 'animation-ik':['test_rig_controls.py', 'test_rig_animation.py', 'test_rig_specializations.py', 'test_rig_morph_authoring.py'],
 'interoperability':['test_rig_interop.py', 'test_rig_import_transforms.py', 'test_rig_material_validation.py', 'test_rig_texture_integrity.py', 'test_rig_jpeg.py', 'test_rig_scene3d_service.py'],
 'operations-archive':['test_rig_correction_validation.py', 'test_rig_workflow.py', 'test_rig_http.py', 'test_rig_render.py'],
}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('gate',choices=['compatibility',*GROUPS]);args=parser.parse_args()
    sys.path.insert(0,str(ROOT/'tests'));sys.path.insert(0,str(ROOT/'scripts'))
    files=sorted(p.name for p in (ROOT/'tests').glob('test_*.py') if not p.name.startswith(('test_rig_', 'test_model3d_', 'test_render3d_', 'test_animation3d_'))) if args.gate=='compatibility' else GROUPS[args.gate]
    suite=unittest.TestSuite()
    for filename in files:suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=filename))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
