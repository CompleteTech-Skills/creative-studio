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
        program="import math,json,sys,tempfile\nfrom pathlib import Path\nscratch_context=tempfile.TemporaryDirectory();scratch=scratch_context.name\nimport pybullet as p\nfrom itertools import product\nrecords=[];maxerr=0\nfor size in [[.001,.001,.001],[10,.2,10],[1000,1000,1000],[999.123456789]*3]:\n for rotation in [[0,0,0],[.3,.7,.4]]:\n  c=p.connect(p.DIRECT);q=p.getQuaternionFromEuler(rotation);pos=[.2,-.1,.3];shape=collision_build_query_shapes(p,c,dict(shape=dict(type='box',size=size),transform=dict(translation=[0,0,0],rotation=[0,0,0,1])),Path(scratch))[0];n=p.createMultiBody(baseMass=0,baseCollisionShapeIndex=shape,basePosition=pos,baseOrientation=q,physicsClientId=c);p.changeDynamics(n,-1,collisionMargin=0,physicsClientId=c)\n  for axis in range(3):\n   for signs in product([-1,0,1],repeat=2):\n    a=[0.,0.,0.];b=[0.,0.,0.];a[axis]=size[axis]*2;b[axis]=-size[axis]*2\n    for j,sgn in zip([j for j in range(3) if j!=axis],signs):a[j]=b[j]=sgn*size[j]/2*(1-1e-6)\n    mat=p.getMatrixFromQuaternion(q)\n    def tx(v):return [pos[i]+sum(mat[3*i+j]*v[j] for j in range(3)) for i in range(3)]\n    aw=tx(a);bw=tx(b)\n    for start,end,expected in [(aw,bw,.375),(bw,aw,.375),(tx([0,0,0]),aw,.25)]:\n     hit=p.rayTest(start,end,physicsClientId=c)[0]\n     if hit[0]<0:raise AssertionError(('miss',size,rotation,axis,signs,start,end))\n     err=abs(hit[2]-expected)*size[axis]*4;maxerr=max(maxerr,err)\n     records.append([size,rotation,axis,signs,err])\n  p.disconnect(c)\nprint(json.dumps({'count':len(records),'max_position_error':maxerr}))\n"
        result=subprocess.run([os.environ['HATCH_PYBULLET_PYTHON'],'-I','-c',source+'\n'+program],capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(report['count'],648)
        self.assertLessEqual(report['max_position_error'],1e-8)

    def test_native_capsule_axes_and_compound_frames_match_physical_proxy(self):
        source=(Path(__file__).resolve().parents[1]/'scripts'/'physics_collision_native.py').read_text()
        program = r"""
import pybullet as p
import tempfile
from pathlib import Path
for axis in ['X','Y','Z']:
 for nested in [False,True]:
  for angles in [[0,0,0],[.3,.7,.4]]:
   client=p.connect(p.DIRECT);q=p.getQuaternionFromEuler(angles);center=[.2,.3,-.4]
   shape=dict(type='capsule',radius=.25,height=1,axis=axis)
   if nested:shape=dict(type='compound',children=[dict(shape=shape,transform=dict(translation=[0,0,0],rotation=[0,0,0,1]))])
   collider=dict(shape=shape,transform=dict(translation=center,rotation=q))
   matrix=p.getMatrixFromQuaternion(q);j=['X','Y','Z'].index(axis);direction=[matrix[3*i+j] for i in range(3)]
   start=[center[i]+2*direction[i] for i in range(3)]
   with tempfile.TemporaryDirectory() as directory:
    for query in [False,True]:
     native=collision_build_query_shapes(p,client,collider,Path(directory))[0] if query else collision_build_shape(p,client,collider,Path(directory))
     body=p.createMultiBody(baseMass=0,baseCollisionShapeIndex=native,physicsClientId=client)
     p.changeDynamics(body,-1,collisionMargin=0,physicsClientId=client)
     hit=p.rayTest(start,center,physicsClientId=client)[0]
     assert hit[0]>=0,(axis,nested,angles,query,'miss')
     assert abs(hit[2]-.625)<1e-8,(axis,nested,angles,query,hit)
     p.removeBody(body,physicsClientId=client)
   p.disconnect(client)
"""
        result=subprocess.run([os.environ['HATCH_PYBULLET_PYTHON'],'-I','-c',source+'\n'+program],capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_native_triangle_edge_tolerance_is_rejected_without_fake_miss(self):
        source=(Path(__file__).resolve().parents[1]/'scripts'/'physics_collision_native.py').read_text()
        program = r"""
import pybullet as p
import tempfile
from pathlib import Path
for size,gap in [(1.,1e-5),(1000.,.001)]:
 client=p.connect(p.DIRECT)
 collider=dict(shape=dict(type='box',size=[size]*3),transform=dict(translation=[0,0,0],rotation=[0,0,0,1]))
 with tempfile.TemporaryDirectory() as directory:
  shape=collision_build_query_shapes(p,client,collider,Path(directory))[0]
  body=p.createMultiBody(baseMass=0,baseCollisionShapeIndex=shape,physicsClientId=client)
  p.changeDynamics(body,-1,collisionMargin=0,physicsClientId=client)
  x=size/2+gap
  hit=p.rayTest([x,size,0],[x,-size,0],physicsClientId=client)[0]
  assert hit[0]>=0,('characterized native tolerance changed',size,gap)
  assert not collision_query_box_surface_valid(hit[3],dict(translation=[0,0,0],rotation=[0,0,0,1]),collision_leaves(collider)[0])
 p.disconnect(client)
"""
        result=subprocess.run([os.environ['HATCH_PYBULLET_PYTHON'],'-I','-c',source+'\n'+program],capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
