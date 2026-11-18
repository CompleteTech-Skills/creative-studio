from pathlib import Path
from copy import deepcopy
import tempfile,json,hashlib
from scene3d_example import build_example,node
from scene3d_graph import SceneGraph
from scene3d_snapshot import snapshot_project
from scene3d_contracts import canonical_bytes,load_project
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp);b=build_example(root/'source');s=deepcopy(b['documents']['example-scene.json']);c=node('animated-child',parent='mesh');s['nodes'].append(c)
 clip=deepcopy(b['documents']['example-animation.json']);clip['id']='child-animation';clip['tracks'][0]['target']='animated-child';path='child-animation.json';ref=dict(kind=clip['kind'],id=clip['id'],version=clip['version'],path=path,sha256=hashlib.sha256(canonical_bytes(clip)).hexdigest());s['nodes'][1]['animation_bindings']=[dict(target='mesh',clip=ref,start_frame=0,end_frame=24,speed=1.,loop=False)]
 first=snapshot_project(root/'source',root/'validated',SceneGraph(s),scene_path='example-scene.json',derived_documents={path:clip});bundle=load_project(root/'validated');g=SceneGraph.from_bundle(bundle,first['active_scene']['path'],source_root=root/'validated');result=g.duplicate('mesh','copy');child=result['mapping']['animated-child'];before=g.digest;out={}
 try:out['delete_result']=g.delete(child);out['delete_changed_scene']=g.digest!=before
 except Exception as e:out['delete_error']=str(e)
 try:snapshot_project(root/'validated',root/'reopened',g,scene_path=first['active_scene']['path']);out['snapshot']='published'
 except Exception as e:out['snapshot_error']=str(e)
 print(json.dumps(out,indent=2))
