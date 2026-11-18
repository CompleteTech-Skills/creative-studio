from copy import deepcopy
from pathlib import Path
import hashlib,json,subprocess,sys,tempfile
from scene3d_example import build_example
from scene3d_contracts import all_refs,load_project
from scene3d_graph import SceneGraph
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project
from scene3d_import import import_gltf
from asset_library import AssetLibrary
from rig_errors import RigError
from test_rig_texture_integrity import TextureIntegrityTests,image_bytes
out={}
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp);source=root/'source';build_example(source)
 def floats(v):
  if type(v) is int:return float(v)
  if isinstance(v,list):return [floats(x) for x in v]
  if isinstance(v,dict):return {k:floats(x) for k,x in v.items()}
  return v
 docs={p.name:floats(json.loads(p.read_bytes())) for p in source.glob('*.json')}
 docs['project.json']['metadata']={'schema_version':1.0,'duration_frames':24.0}
 scene=docs['example-scene.json'];clipref=deepcopy(docs['project.json']['animations'][0]);scene['nodes'][1]['animation_bindings']=[dict(target='mesh',clip=clipref,start_frame=0.0,end_frame=24.0,speed=1.0,loop=False)]
 prefab=docs['example-prefab.json'];prefab['parameters']={'offset':1.0};prefab['parameter_bindings']=[dict(parameter='offset',node='prefab-mesh',property='translation',component=0.0)]
 digests={}
 def write(name):
  if name in digests:return digests[name]
  if name in docs:
   for r in all_refs(docs[name]):r['sha256']=write(r['path'])
   (source/name).write_bytes((json.dumps(docs[name],indent=3)+'\r\n').encode())
  digests[name]=hashlib.sha256((source/name).read_bytes()).hexdigest();return digests[name]
 write('project.json');before={p.name:p.read_bytes() for p in source.iterdir()}
 b=load_project(source);g=SceneGraph.from_bundle(b,'example-scene.json',source_root=source);copied=g.duplicate('mesh','copy');l=PrefabLibrary.from_files(source,b);l.instantiate(g,b['project']['prefabs'][0],'instance',overrides=dict(parameters={'offset':3.0},materials={},nodes=[]));l.detach(g,'instance');receipt=snapshot_project(source,root/'api-copy',g,scene_path='example-scene.json');reopen=load_project(root/'api-copy');assert reopen['status']=='hash-and-contract-verified'
 for p,raw in before.items():
  retained='history/project-'+hashlib.sha256(raw).hexdigest()+'.json' if p=='project.json' else p
  assert (root/'api-copy'/retained).read_bytes()==raw
 assert all(type(x) is float for x in reopen['project']['metadata'].values())
 commands=root/'commands.json';commands.write_text(json.dumps(dict(schema_version=1,commands=[dict(operation='duplicate',parameters=dict(identifier='mesh',new_id='cli-copy'))])))
 cli=subprocess.run([sys.executable,'scripts/scene3d_edit.py','--project',str(source),'--scene','example-scene.json','--commands',str(commands),'--destination',str(root/'cli-copy')],capture_output=True,text=True,timeout=30);assert cli.returncode==0,cli.stderr;load_project(root/'cli-copy');assert before=={p.name:p.read_bytes() for p in source.iterdir()}
 out['integral_float_raw_sources']=dict(api_snapshot='verified',cli_exit=cli.returncode,source_files=len(before),source_sha256=digests,raw_bytes_unchanged=True,metadata_floats_preserved=True,derived_documents=len(copied['derived_documents']))
t=TextureIntegrityTests();t.setUp()
try:
 library=AssetLibrary(t.root/'library',create=True);license=t.root/'license.txt';license.write_text('CC0 engineering fixture for independent Scene60 integration review.\n');valid=image_bytes(fmt='JPEG',size=(32,32));cases=[]
 for label,data,mime in [('png',image_bytes(),'image/png'),('jpeg',valid,'image/jpeg')]+[('jpeg-cut-'+str(cut),valid[:-cut]+b'\xff\xd9','image/jpeg') for cut in (5,20,100,300)]:
  for route,suffix,external,datauri in [('glb','.glb',False,False),('external','.gltf',True,False),('data-uri','.gltf',False,True)]:
   path=t.model(data,suffix,mime=mime,external=external,data_uri=datauri);before=path.read_bytes()
   with library._connect() as db:count=db.execute('SELECT COUNT(*) FROM versions').fetchone()[0]
   try:
    imported=import_gltf(path,library,asset_id=label+'-'+route,attribution='Independent review',license_file=license);accepted=True;code=None
    assert library.read_file(imported.library_reference,'imports/'+label+'-'+route+'/1.0.0/native/source/model'+suffix)==before
    assert not imported.report['production_approved'];assert imported.report['fixture']
   except RigError as e:accepted=False;code=e.report['code']
   assert accepted==('cut' not in label),(label,route,code)
   assert path.read_bytes()==before
   with library._connect() as db:after=db.execute('SELECT COUNT(*) FROM versions').fetchone()[0]
   assert after==count+(1 if accepted else 0)
   cases.append(dict(case=label,route=route,accepted=accepted,error_code=code,source_sha256=hashlib.sha256(before).hexdigest(),image_sha256=hashlib.sha256(data).hexdigest()))
 out['texture_service_imports']=cases
finally:t.temp.cleanup()
print(json.dumps(out,indent=2))
