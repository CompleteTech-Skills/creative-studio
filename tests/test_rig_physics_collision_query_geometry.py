"""Independent slab oracle versus actual triangulated native query boxes."""
import json
import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1','Native backend opt-in required')
class QueryGeometryTests(unittest.TestCase):
    def test_actual_native_box_surface_matches_independent_ray_slab_oracle(self):
        source=(Path(__file__).resolve().parents[1]/'scripts'/'physics_collision_native.py').read_text()
        program="import math,json,sys,tempfile\nfrom pathlib import Path\nscratch_context=tempfile.TemporaryDirectory();scratch=scratch_context.name\nimport pybullet as p\nfrom itertools import product\nrecords=[];maxerr=0\nfor size in [[.001,.001,.001],[10,.2,10],[1000,1000,1000]]:\n for rotation in [[0,0,0],[.3,.7,.4]]:\n  c=p.connect(p.DIRECT);q=p.getQuaternionFromEuler(rotation);pos=[.2,-.1,.3];shape=collision_build_shape(p,c,collision_query_collider(dict(shape=dict(type='box',size=size),transform=dict(translation=[0,0,0],rotation=[0,0,0,1]))),Path(scratch));n=p.createMultiBody(baseMass=0,baseCollisionShapeIndex=shape,basePosition=pos,baseOrientation=q,physicsClientId=c);p.changeDynamics(n,-1,collisionMargin=0,physicsClientId=c)\n  for axis in range(3):\n   for signs in product([-1,0,1],repeat=2):\n    a=[0.,0.,0.];b=[0.,0.,0.];a[axis]=size[axis]*2;b[axis]=-size[axis]*2\n    for j,sgn in zip([j for j in range(3) if j!=axis],signs):a[j]=b[j]=sgn*size[j]/2*(1-1e-6)\n    mat=p.getMatrixFromQuaternion(q)\n    def tx(v):return [pos[i]+sum(mat[3*i+j]*v[j] for j in range(3)) for i in range(3)]\n    aw=tx(a);bw=tx(b)\n    for start,end,expected in [(aw,bw,.375),(bw,aw,.375),(tx([0,0,0]),aw,.25)]:\n     hit=p.rayTest(start,end,physicsClientId=c)[0]\n     if hit[0]<0:raise AssertionError(('miss',size,rotation,axis,signs,start,end))\n     err=abs(hit[2]-expected)*size[axis]*4;maxerr=max(maxerr,err)\n     records.append([size,rotation,axis,signs,err])\n  p.disconnect(c)\nprint(json.dumps({'count':len(records),'max_position_error':maxerr}))\n"
        result=subprocess.run([os.environ['HATCH_PYBULLET_PYTHON'],'-I','-c',source+'\n'+program],capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(report['count'],486)
        self.assertLessEqual(report['max_position_error'],1e-8)
