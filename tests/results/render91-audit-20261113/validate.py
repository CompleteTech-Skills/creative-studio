"""Check render91 audit evidence consistency.

The receipt is printed to stdout by default. Use --output to create a new file.
"""
from pathlib import Path
import argparse,ast,datetime,hashlib,json,re,subprocess,sys
out=Path(__file__).resolve().parent;repo=out.parents[2]
def check(condition, detail):
 if not condition:
  raise AssertionError(detail)
def gitfile(revision,path):return subprocess.check_output(['git','show',revision+':'+path],cwd=repo)
def optional_gitfile(revision,path):
 try:return subprocess.check_output(['git','show',revision+':'+path],cwd=repo,stderr=subprocess.DEVNULL)
 except (subprocess.CalledProcessError,OSError):return None
def job_binding_source(native):
 """Retained exact bytes; cross-checked against git only when that revision is present."""
 retained=native['retained_test_source']
 payload=(out/retained['path']).read_bytes()
 check(len(payload)==retained['bytes'] and hashlib.sha256(payload).hexdigest()==retained['sha256'], (retained['path']))
 committed=optional_gitfile(native['revision'],retained['source_path'])
 if committed is not None:check(committed==payload, ((native['revision'],retained['source_path'])))
 return payload,committed is not None
def emit(receipt,output=None):
 text = json.dumps(receipt, indent=2) + '\n'
 if output is None:
  sys.stdout.write(text)
  return
 with Path(output).open('x', encoding='utf-8') as target:
  target.write(text)
def main(argv=None):
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--output', help='write the receipt to a new file instead of stdout')
 args = parser.parse_args(argv)
 data=json.loads((out/'audit.json').read_text());workflow=json.loads((out/'source-workflow.json').read_text())
 for item in data['examined_source_inventory']:
  payload=gitfile(data['foundation_revision'],item['path'])
  check(len(payload)==item['bytes'] and hashlib.sha256(payload).hexdigest()==item['sha256'], (item['path']))
 redaction=data['foundation_review']['report_redaction']
 payload=(out/data['foundation_review']['report']).read_bytes()
 check(len(payload)==redaction['bytes'] and hashlib.sha256(payload).hexdigest()==redaction['sha256'], (data['foundation_review']['report']))
 refs=set()
 for item in data['field_catalog']+data['required_semantic_traceability']:
  refs.update(item['tests'])
 for item in data['protocols'].values():refs.update(item['tests'])
 native=data['native_job_equivalent'];job_payload,job_cross_checked=job_binding_source(native)
 cache={}
 for ref in refs:
  file,address=ref.split(':');cls,method=address.split('.')
  if file==native['retained_test_source']['source_path']:key=(native['revision'],file);source=lambda:job_payload
  else:key=(data['foundation_revision'],file);source=lambda:gitfile(data['foundation_revision'],file)
  if key not in cache:cache[key]=ast.parse(source())
  tree=cache[key]
  check(any(isinstance(c,ast.ClassDef) and c.name==cls and any(isinstance(m,ast.FunctionDef) and m.name==method for m in c.body) for c in tree.body), (ref))
 for item in workflow['artifacts']:
  payload=(out/item['path']).read_bytes()
  check(len(payload)==item['bytes'] and hashlib.sha256(payload).hexdigest()==item['sha256'], (item['path']))
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
   if key in expected:check(expected[key]==entry, ((key,expected[key],entry)))
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
 check(len(actual)==len(data['field_catalog']), ('duplicate catalog entries'))
 check(actual.keys()==expected.keys(), ({'missing':sorted(expected.keys()-actual.keys()),'extra':sorted(actual.keys()-expected.keys())}))
 for key,entry in expected.items():
  check(actual[key]['required']==entry['required'], (key))
  check(actual[key]['nullable_ancestors']==entry['nullable_ancestors'], (key))
 check(workflow['fixture'] and workflow['native_geometry']=={'vertices':4,'triangles':2}, ("audit consistency check failed"))
 check(set(data['deliverables'])==set(map(str,range(1,8))), ("audit consistency check failed"))
 check(len({item['id'] for item in data['required_semantic_traceability']})==len(data['required_semantic_traceability']), ("audit consistency check failed"))
 for doc in ('references/render3d-foundation-audit.md','references/render3d-contracts.md'):
  for link in re.findall(r'\]\(([^)]+)\)',(repo/doc).read_text()):
   if '://' not in link and not link.startswith('#'):check(((repo/doc).parent/link.split('#')[0]).exists(), ((doc,link)))
 receipt = {
  'kind': 'render91-audit-consistency-check',
  'recorded_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
  'foundation_revision': data['foundation_revision'],
  'job_binding_revision': native['revision'],
  'job_binding_source': 'retained-copy-sha256-verified',
  'job_binding_revision_cross_checked': job_cross_checked,
  'source_inventory_hashes_checked': len(data['examined_source_inventory']),
  'actual_test_addresses_resolved': len(refs),
  'retained_source_artifacts_checked': len(workflow['artifacts']),
  'field_paths': len(data['field_catalog']),
  'schema_paths_independently_checked': len(expected),
  'nullable_branch_descendants_checked': sum(bool(v['nullable_ancestors']) for v in expected.values()),
  'conditional_required_semantics_checked': True,
  'semantic_requirements': len(data['required_semantic_traceability']),
  'all_seven_deliverables_present': True,
  'relative_document_links_resolved': True,
  'limitations': [
   'Deliverable presence checks keys 1 through 7. Semantic coverage checks unique traceability IDs and resolves cited test addresses.',
   'This receipt covers documentation and evidence consistency. Independent review and renderer acceptance have separate gates.',
  ],
 }
 try:
  emit(receipt, args.output)
 except FileExistsError:
  parser.error(f'--output requires a new path: {args.output}')
 return 0
if __name__=='__main__':raise SystemExit(main())
