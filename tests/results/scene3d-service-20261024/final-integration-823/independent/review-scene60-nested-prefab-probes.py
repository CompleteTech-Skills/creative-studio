from pathlib import Path
from copy import deepcopy
import tempfile,json
from scene3d_example import build_example,node
from scene3d_contracts import load_project
from scene3d_graph import SceneGraph
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project
from rig_errors import RigError
results=[]
with tempfile.TemporaryDirectory() as temp:
 root=Path(temp);source=root/'source';b=build_example(source);base=b['documents']['example-prefab.json']
 for label,prop,value,good in [('unknown','translation',2,False),('type','translation','bad',False),('scale','scale',-1,False),('quaternion','rotation',.5,False),('valid','translation',4,True)]:
  l=PrefabLibrary.from_files(source,b);inner=deepcopy(base);inner['id']='inner-'+label;inner['parameters']={'amount':1.};inner['parameter_bindings']=[dict(parameter='amount',node='prefab-mesh',property=prop,component=3 if prop=='rotation' else 0)];ir=l.register(inner)
  outer=deepcopy(base);outer['id']='outer-'+label;outer['nodes']=[outer['nodes'][0]];outer['dependencies']=[];outer['provenance']['fixture']=False;outer['provenance']['origin']='authored';orr=l.register(outer)
  child=node('extra','prefab_instance','prefab-root');child['prefab']=ir;child['prefab_overrides']=dict(parameters={'unknown' if label=='unknown' else 'amount':value},materials={},nodes=[]);override=dict(parameters={},materials={},nodes=[],children=[child])
  s=deepcopy(b['documents']['example-scene.json']);s['provenance']['fixture']=False;s['provenance']['origin']='authored';g=SceneGraph(s,external_references={});before=g.document();payloads=l.payloads()
  try:l.instantiate(g,orr,'instance',overrides=override);accepted=True;code=None
  except RigError as e:accepted=False;code=e.report['code']
  assert accepted==good,(label,'api',accepted,code)
  if not accepted:assert g.document()==before and l.payloads()==payloads
  else:assert g.document()['provenance']['fixture'];l.expand(g.document())
  raw=deepcopy(b['documents']['example-scene.json']);instance=node('raw-instance','prefab_instance');instance['prefab']=orr;instance['prefab_overrides']=override;raw['nodes'].append(instance);raw['roots'].append(instance['id']);raw['dependencies'].append(orr)
  rawgraph=SceneGraph(raw);added={p:v for p,v in payloads.items() if p not in b['inventory']};dest=root/(label+'-snapshot')
  try:snapshot_project(source,dest,rawgraph,scene_path='example-scene.json',derived_documents=added);published=True;load_project(dest)
  except RigError as e:published=False
  assert published==good,(label,'snapshot',published)
  if not good:assert not dest.exists()
  results.append(dict(case=label,api_accepted=accepted,published=published,structured_rejection=good or code is not None))
 # Each source graph is valid and shallow enough separately; their composition is not.
 l=PrefabLibrary.from_files(source,b);inner=deepcopy(base);inner['id']='deep-inner';inner['roots']=['i0'];inner['nodes']=[node('i'+str(k),parent=None if k==0 else 'i'+str(k-1)) for k in range(70)];inner['dependencies']=[];ir=l.register(inner);outer=deepcopy(base);outer['id']='deep-outer';outer['roots']=['o0'];outer['nodes']=[node('o'+str(k),parent=None if k==0 else 'o'+str(k-1)) for k in range(70)];outer['nodes'][-1]['type']='prefab_instance';outer['nodes'][-1]['prefab']=ir;outer['dependencies']=[ir];g=SceneGraph(b['documents']['example-scene.json']);before=g.document()
 try:orr=l.register(outer);l.instantiate(g,orr,'too-deep');accepted=True
 except RigError:accepted=False
 assert not accepted and g.document()==before
 results.append(dict(case='composed-transform-depth-70-plus-70',accepted=accepted,unchanged=True))
print(json.dumps(results,indent=2))
