"""Exact native Scene3D source fixture for real deformable solver checks."""
from copy import deepcopy
import math
from physics_backend_example import build_inputs
from physics_backends import canonical,digest


def build_softbody_inputs(*,frames=100,stiffness=.2,damping=.2,pins=False):
    world,payloads=build_inputs(frames=frames)
    from physics_backends import parse
    points=[(.5,0,0),(-.5,0,0),(0,.5,0),(0,-.5,0),(0,0,.5),(0,0,-.5)]
    triangles=[(0,2,4),(4,2,1),(1,2,5),(5,2,0),(4,3,0),(1,3,4),(5,3,1),(0,3,5)]
    for _ in range(2):
        edges={};faces=[]
        def middle(a,b):
            edge=tuple(sorted((a,b)))
            if edge not in edges:
                p=[points[a][i]+points[b][i] for i in range(3)];length=math.sqrt(sum(x*x for x in p))
                edges[edge]=len(points);points.append(tuple(.5*x/length for x in p))
            return edges[edge]
        for a,b,c in triangles:
            ab,bc,ca=middle(a,b),middle(b,c),middle(c,a)
            faces.extend([(a,ab,ca),(ab,b,bc),(ca,bc,c),(ab,bc,ca)])
        triangles=faces
    obj='# Procedural soft-body engineering fixture; source geometry, not cached animation.\n'
    obj+=''.join('v %.12g %.12g %.12g\n'%p for p in points)
    obj+=''.join('f %d %d %d\n'%tuple(i+1 for i in face) for face in triangles)
    payloads['ball.obj']=obj.encode()
    asset=parse(payloads['ball-asset.json']);asset['content']['sha256']=asset['source']['sha256']=digest(payloads['ball.obj'])
    asset['meshes'][0]['sha256']=digest(payloads['ball.obj'])
    asset['resources'].update(vertices=len(points),triangles=len(triangles),bytes=len(payloads['ball.obj']))
    payloads['ball-asset.json']=canonical(asset)
    scene=parse(payloads['physics-scene.json'])
    for ref in scene['dependencies']:
        if ref['id']=='ball-asset':ref['sha256']=digest(payloads['ball-asset.json'])
    scene['nodes'][0]['asset']['sha256']=digest(payloads['ball-asset.json'])
    payloads['physics-scene.json']=canonical(scene)
    project=parse(payloads['project.json'])
    for ref in project['assets']+project['dependency_lock']:
        if ref['id']=='ball-asset':ref['sha256']=digest(payloads['ball-asset.json'])
        if ref['id']=='physics-scene':ref['sha256']=digest(payloads['physics-scene.json'])
    project['scenes'][0]['sha256']=digest(payloads['physics-scene.json'])
    payloads['project.json']=canonical(project)
    world['scene']['sha256']=digest(payloads['physics-scene.json'])
    world['assets'][0]['sha256']=digest(payloads['ball.obj'])
    world['bodies']=world['bodies'][1:]
    world['components']=[dict(id='ball',category='soft-body',geometry='ball-asset',material='engineering',parent=None,
        pins=[2] if pins else [],vertex_count=len(points),stiffness=stiffness,damping=damping,mass=1.,colliders=['floor'])]
    world['solver']['required_categories']=['rigid-body','soft-body'];world['solver']['iterations']=1000;world['solver']['fixed_step']['denominator']=24000;world['budgets']['steps']=100000
    world['budgets']['soft_body_vertices']=4096
    return world,payloads
