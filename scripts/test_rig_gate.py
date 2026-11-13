#!/usr/bin/env python3
"""Run one named release gate; inherited failures stay visible."""
import argparse
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
GROUPS={
 'schema-math':['test_rig_director_capabilities.py','test_rig_math.py','test_rig_contracts.py','test_rig_compatibility.py','test_rig_camera3d_contracts.py','test_rig_camera3d_optics.py','test_rig_scene3d_math.py','test_rig_physics_timing_cache.py','test_rig_landmarks.py','test_rig_editor3d_contracts.py','test_render3d_contracts.py','test_render3d_independent.py'],
 'skinning':['test_rig_skinning.py','test_rig_weight_input_boundaries.py'],
 'animation-ik':['test_rig_controls.py','test_rig_animation.py','test_rig_specializations.py','test_rig_morph_authoring.py'],
