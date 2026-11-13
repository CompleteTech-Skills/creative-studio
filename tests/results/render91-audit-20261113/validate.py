from pathlib import Path
import ast,datetime,hashlib,json,re,subprocess
out=Path(__file__).resolve().parent;repo=out.parents[2]
data=json.loads((out/'audit.json').read_text());workflow=json.loads((out/'source-workflow.json').read_text())
def gitfile(revision,path):return subprocess.check_output(['git','show',revision+':'+path],cwd=repo)
for item in data['examined_source_inventory']:
 payload=gitfile(data['foundation_revision'],item['path'])
 assert len(payload)==item['bytes'] and hashlib.sha256(payload).hexdigest()==item['sha256'],item['path']
refs=set()
for item in data['field_catalog']+data['required_semantic_traceability']:
 refs.update(item['tests'])
for item in data['protocols'].values():refs.update(item['tests'])
cache={}
for ref in refs:
 file,address=ref.split(':');cls,method=address.split('.')
 revision=data['native_job_equivalent']['revision'] if file.endswith('test_render3d_job_binding.py') else data['foundation_revision']
 if (revision,file) not in cache:cache[revision,file]=ast.parse(gitfile(revision,file))
 tree=cache[revision,file]
 assert any(isinstance(c,ast.ClassDef) and c.name==cls and any(isinstance(m,ast.FunctionDef) and m.name==method for m in c.body) for c in tree.body),ref
for item in workflow['artifacts']:
 payload=(out/item['path']).read_bytes()
 assert len(payload)==item['bytes'] and hashlib.sha256(payload).hexdigest()==item['sha256'],item['path']
# Expand every schema branch, including nullable objects, independently of the catalog.
expected={}
def catalog_fields(node, definitions, document, path='', required=True, nullable=(), seen=()):
 if '$ref' in node:
  name=node['$ref'].rsplit('/',1)[-1]
  if name not in seen:catalog_fields(definitions[name],definitions,document,path,required,nullable,seen+(name,))
  return
 if path:
  key=(document,path)
  entry={'required':required,'nullable_ancestors':list(nullable)}
  if key in expected:assert expected[key]==entry,(key,expected[key],entry)
  expected[key]=entry
 for name,value in node.get('properties',{}).items():
  catalog_fields(value,definitions,document,(path+'.' if path else '')+name,name in node.get('required',()),nullable,seen)
 if isinstance(node.get('items'),dict):catalog_fields(node['items'],definitions,document,path+'[]',True,nullable,seen)
 for union in ('anyOf','oneOf','allOf'):
  variants=node.get(union,[])
  is_nullable=any(v.get('type')=='null' for v in variants)
  for variant in variants:
   conditions=nullable+(path,) if is_nullable and variant.get('type')!='null' and path else nullable
   # The parent field remains nullable; only its descendants depend on this branch.
   if conditions!=nullable:
    branch=dict(variant)
    if '$ref' in branch:branch=definitions[branch['$ref'].rsplit('/',1)[-1]]
    for name,value in branch.get('properties',{}).items():
     catalog_fields(value,definitions,document,path+'.'+name,name in branch.get('required',()),conditions,seen)
    if isinstance(branch.get('items'),dict):catalog_fields(branch['items'],definitions,document,path+'[]',True,conditions,seen)
   else:catalog_fields(variant,definitions,document,path,required,nullable,seen)
for item in data['examined_source_inventory']:
 if item['path'].startswith('profiles/render3d-') and item['path'].endswith('.schema.json'):
  schema=json.loads(gitfile(data['foundation_revision'],item['path']))
  document=schema['$ref'].rsplit('/',1)[-1]
  catalog_fields(schema['$defs'][document],schema['$defs'],document)
actual={(item['document'],item['path']):item for item in data['field_catalog']}
assert len(actual)==len(data['field_catalog']),'duplicate catalog entries'
assert actual.keys()==expected.keys(),{'missing':sorted(expected.keys()-actual.keys()),'extra':sorted(actual.keys()-expected.keys())}
for key,entry in expected.items():
 assert actual[key]['required']==entry['required'],key
 assert actual[key]['nullable_ancestors']==entry['nullable_ancestors'],key
assert workflow['fixture'] and workflow['native_geometry']=={'vertices':4,'triangles':2}
assert set(data['deliverables'])==set(map(str,range(1,8)))
assert len({item['id'] for item in data['required_semantic_traceability']})==len(data['required_semantic_traceability'])
for doc in ('references/render3d-foundation-audit.md','references/render3d-contracts.md'):
 for link in re.findall(r'\]\(([^)]+)\)',(repo/doc).read_text()):
  if '://' not in link and not link.startswith('#'):assert ((repo/doc).parent/link.split('#')[0]).exists(),(doc,link)
receipt={'kind':'render91-audit-consistency-check','recorded_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'foundation_revision':data['foundation_revision'],'job_binding_revision':data['native_job_equivalent']['revision'],'source_inventory_hashes_checked':len(data['examined_source_inventory']),'actual_test_addresses_resolved':len(refs),'retained_source_artifacts_checked':len(workflow['artifacts']),'field_paths':len(data['field_catalog']),'schema_paths_independently_checked':len(expected),'nullable_branch_descendants_checked':sum(bool(v['nullable_ancestors']) for v in expected.values()),'conditional_required_semantics_checked':True,'semantic_requirements':len(data['required_semantic_traceability']),'all_seven_deliverables_present':True,'relative_document_links_resolved':True,'limitations':['Documentation/evidence consistency only, not a new independent code review or renderer acceptance.']}
(out/'audit-validation.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
