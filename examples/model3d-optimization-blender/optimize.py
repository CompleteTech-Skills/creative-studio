#!/usr/bin/env python3
"""Run the optional trusted planar Blender profile on one immutable source."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from rig_errors import RigError,require
from rig_source_snapshot import import_snapshot
from model3d_optimization_blender import decimate_blender
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--input',required=True,type=Path);p.add_argument('--destination',required=True,type=Path)
p.add_argument('--blender');p.add_argument('--angle-degrees',type=float,default=1.)
p.add_argument('--triangle-budget',required=True,type=int);p.add_argument('--max-distance',type=float,default=.001)
p.add_argument('--uv-tolerance',type=float,default=1e-4);a=p.parse_args()
try:
    with import_snapshot(a.input,blender=a.blender) as snapshot:
        require(not snapshot.model.skeleton and not snapshot.model.weights and not snapshot.model.clips,'MODEL3D_DECIMATE_PROFILE','This explicit profile is static, unrigged geometry only.')
        report=decimate_blender(snapshot.model.geometry,a.destination,angle_degrees=a.angle_degrees,
            budgets={'triangles':a.triangle_budget},max_distance=a.max_distance,uv_tolerance=a.uv_tolerance,
            recompute_normals=True,source_snapshot=snapshot,executable=a.blender)
    print(json.dumps(report,sort_keys=True))
except RigError as error:
    print(json.dumps(error.report,sort_keys=True));raise SystemExit(1)
except OSError as error:
    print(json.dumps(dict(code='MODEL3D_DECIMATE_IO',message=str(error))));raise SystemExit(1)
