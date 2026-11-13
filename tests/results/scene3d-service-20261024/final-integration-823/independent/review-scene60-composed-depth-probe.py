from pathlib import Path
from copy import deepcopy
import tempfile,json
from scene3d_example import build_example,node
from scene3d_graph import SceneGraph
from scene3d_prefabs import PrefabLibrary
from scene3d_contracts import validate_prefab_instances
from rig_errors import RigError
results=[]
with tempfile.TemporaryDirectory() as tmp:
 b=build_example(Path(tmp)/'source');base=b['documents']['example-prefab.json']
 for inner_length,expected in [(63,True),(64,False)]:
  library=PrefabLibrary();inner=deepcopy(base);inner.update(id='inner',nodes=[node('i'+str(k),parent=None if k==0 else 'i'+str(k-1)) for k in range(inner_length)],roots=['i0'],dependencies=[],material_slots=[],sockets=[]);ir=library.register(inner)
  outer=deepcopy(base);outer.update(id='outer',nodes=[node('o'+str(k),parent=None if k==0 else 'o'+str(k-1)) for k in range(63)],roots=['o0'],dependencies=[],material_slots=[],sockets=[]);outer['nodes'][-1].update(type='prefab_instance',prefab=ir);orr=library.register(outer)
  g=SceneGraph(b['documents']['example-scene.json']);before=g.document()
  try:library.instantiate(g,orr,'instance',parent='root');accepted=True
  except RigError as e:accepted=False;error=e.report
  assert accepted==expected,(inner_length,accepted)
  if accepted:
   expanded=library.expand(g.document());evidence=validate_prefab_instances(g.document(),library.resolve);assert evidence['expanded_nodes']==len(expanded['scene']['nodes'])
  else:assert g.document()==before
  results.append(dict(composed_transform_depth=inner_length+65,accepted=accepted,atomic=True))
 # Invalid parameter defaults are rejected before catalog mutation, even if a
 # hypothetical instance override might repair them.
 library=PrefabLibrary();p=deepcopy(base);p['parameters']={'w':.5};p['parameter_bindings']=[dict(parameter='w',node='prefab-root',property='rotation',component=3.0)];before=library.payloads()
 try:library.register(p);accepted=True
 except RigError:accepted=False
 assert not accepted and library.payloads()==before
 results.append(dict(case='invalid-default-registration',accepted=False,catalog_unchanged=True))
print(json.dumps(results,indent=2))
