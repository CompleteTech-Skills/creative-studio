from pathlib import Path
from copy import deepcopy
import tempfile,json,hashlib
from unittest.mock import patch
from scene3d_example import build_example
from scene3d_graph import SceneGraph
from scene3d_contracts import canonical_bytes
from rig_errors import RigError
out=[]
with tempfile.TemporaryDirectory() as tmp:
 b=build_example(Path(tmp)/'p');s=deepcopy(b['documents']['example-scene.json']);ref=b['project']['animations'][0];s['nodes'][1]['animation_bindings']=[dict(target='mesh',clip=ref,start_frame=0,end_frame=24,speed=1.,loop=False)];raw={ref['path']:canonical_bytes(b['documents'][ref['path']])}
 control=SceneGraph(s,animation_payloads=raw);control.duplicate('mesh','copy');size=sum(map(len,control._animation_payloads.values()))
 for name,limit,expected in [('files',1,False),('bytes',size-1,False),('exact-bytes',size,True)]:
  g=SceneGraph(s,animation_payloads=raw);before=g.digest
  with patch('scene3d_graph.'+('MAX_FILES' if name=='files' else 'MAX_TOTAL_BYTES'),limit):
   try:g.duplicate('mesh','copy');accepted=True
   except RigError:accepted=False
  assert accepted==expected
  if not accepted:assert g.digest==before and g._animation_payloads==raw and not g.derived_documents()
  out.append(dict(case=name,limit=limit,accepted=accepted,unchanged_on_rejection=accepted or g.digest==before))
print(json.dumps(out,indent=2))
