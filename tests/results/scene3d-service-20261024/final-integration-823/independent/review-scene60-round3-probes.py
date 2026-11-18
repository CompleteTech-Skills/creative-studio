from pathlib import Path
from copy import deepcopy
import tempfile,json,time,hashlib
from scene3d_example import build_example,node
from scene3d_graph import SceneGraph
from scene3d_prefabs import PrefabLibrary
from scene3d_contracts import canonical_bytes,validate_document
from scene3d_snapshot import snapshot_project
r=[]
def run(name,fn):
 try:r.append(dict(case=name,result=fn()))
 except Exception as e:r.append(dict(case=name,exception=type(e).__name__,error=str(e)))
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp);b=build_example(root/'source');scene=b['documents']['example-scene.json'];prefab=b['documents']['example-prefab.json']
 def child_conflict():
  l=PrefabLibrary();p=deepcopy(prefab);r1=l.register(p);p['version']='2.0.0';p['nodes']=p['nodes'][:1];r2=l.register(p);g=SceneGraph(scene);l.instantiate(g,r1,'instance',overrides=dict(parameters={},materials={},nodes=[],children=[node('optional',parent='prefab-mesh'),node('grandchild',parent='optional')]));conflicts=l.upgrade(g,'instance',r2);print('conflicts',conflicts,file=__import__('sys').stderr);return l.upgrade(g,'instance',r2,resolutions={'child:optional':'source'})
 run('upgrade-removed-optional-subtree',child_conflict)
 def tampered_context():
  s=deepcopy(scene);ref=b['project']['animations'][0];s['nodes'][1]['animation_bindings']=[dict(target='mesh',clip=ref,start_frame=0,end_frame=24,speed=1.,loop=False)];clips={p:deepcopy(d) for p,d in b['documents'].items() if d['kind']=='scene3d-animation'};clip=clips[ref['path']];clip['tracks'][0]['keys'][0]['value']=[99,99,99];g=SceneGraph(s,external_references={},animation_documents=clips,animation_payloads={ref['path']:canonical_bytes(b['documents'][ref['path']])});d=g.duplicate('mesh','copy')['derived_documents'];out=snapshot_project(root/'source',root/'tampered',g,scene_path='example-scene.json');return dict(published=True,derived_start=next(iter(d.values()))['tracks'][0]['keys'][0]['value'],claimed_source=ref['sha256'],actual_context_sha256=hashlib.sha256(canonical_bytes(clip)).hexdigest())
 run('mismatched-animation-context',tampered_context)
 def repeated_dag():
  docs={};reference=None
  for i in range(30):
   clip=deepcopy(b['documents']['example-animation.json']);clip['id']='dag-'+str(i);clip['bindings']=[] if reference is None else [dict(target='mesh',clip=deepcopy(reference),start_frame=0,end_frame=24,speed=1.,loop=False) for _ in range(2)];clip['dependencies']=[] if reference is None else [reference];payload=canonical_bytes(clip);path='dag/'+str(i)+'.json';docs[path]=clip;reference=dict(kind=clip['kind'],id=clip['id'],version=clip['version'],path=path,sha256=hashlib.sha256(payload).hexdigest())
  s=deepcopy(scene);s['nodes'][1]['animation_bindings']=[dict(target='mesh',clip=reference,start_frame=0,end_frame=24,speed=1.,loop=False)];g=SceneGraph(s,animation_documents=docs,animation_payloads={p:canonical_bytes(d) for p,d in docs.items()});original=g._map_clip;count=0
  def counted(*args,**kwargs):
   nonlocal count
   count+=1;return original(*args,**kwargs)
  g._map_clip=counted;start=time.monotonic();out=g.duplicate('mesh','copy');return dict(unique_source_clips=len(docs),map_calls=count,unique_derived_clips=len(out['derived_documents']),seconds=time.monotonic()-start)
 run('duplicate-shared-clip-DAG',repeated_dag)
print(json.dumps(r,indent=2))
