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
 check(isinstance(revision,str) and re.fullmatch('[0-9a-f]{40}',revision), 'Expected an exact Git revision.')
 kind=subprocess.check_output(['git','cat-file','--batch-check=%(objecttype)'],
                              cwd=repo,input=(revision+'\n').encode('ascii')).strip()
 if kind==(revision+' missing').encode('ascii'):
  return None
 check(kind==b'commit', 'Retained test source revision is not a commit.')
 try:
  return gitfile(revision,path)
 except subprocess.CalledProcessError as error:
  raise AssertionError('Present job revision lacks its claimed test source.') from error
def job_binding_source(native):
 """Retained exact bytes; cross-checked against git only when that revision is present."""
 retained=native['retained_test_source']
 payload=(out/retained['path']).read_bytes()
 check(len(payload)==retained['bytes'] and hashlib.sha256(payload).hexdigest()==retained['sha256'], (retained['path']))
 committed=optional_gitfile(native['revision'],retained['source_path'])
 if committed is not None:check(committed==payload, ((native['revision'],retained['source_path'])))
 return payload,committed is not None

def verify_foundation_review(audit):
    review = audit['foundation_review']
    redaction = review['report_redaction']
    payload = (out / review['report']).read_bytes()
    check(len(payload) == redaction['bytes'] and hashlib.sha256(payload).hexdigest() == redaction['sha256'],
          'Redacted foundation review digest or size differs.')
    report = payload.decode('utf-8')
    check(re.findall(r'^Exact reviewed head: `([0-9a-f]{40})`\.$', report, re.MULTILINE)
          == [audit['foundation_revision']], 'Foundation review revision differs.')
    for field, label in (('focused_passed', 'Focused Render3D discovery'),
                         ('schema_math_passed', 'Full combined schema/math gate')):
        counts = re.findall(r'^- ' + label + r': \*\*(\d+) passed\*\*,', report, re.MULTILINE)
        check(counts == [str(review[field])], 'Foundation review count differs for ' + label)
    lines = report.splitlines()
    check(review['newly_spawned_whole_feature'] == 'PASS'
          and '# Fresh independent whole-feature Render3D core review' in lines
          and any(line.startswith('Reviewer: newly spawned `<agent-root>/') for line in lines)
          and 'No actionable finding identified in the reviewed feature.' in lines,
          'Foundation review scope or outcome differs.')
    check(review['schema_template_parity'] == 'PASS'
          and '- Schema generation parity, rig-template generation parity and diff whitespace: passed.' in lines,
          'Foundation review parity result differs.')

def verify_command_arguments(commands):
    python = '<rig-py312-venv>/bin/python'
    script = '<audit-worktree>/scripts/render3d.py'
    work = '<local-coordination-dir>/render91-source-workflow/'
    scene_destination = work + 'scene-snapshot'
    expected = [
        [python, script, 'snapshot-rig', '--source', work + 'original-source/render91-fixture',
         '--destination', work + 'native-snapshot'],
        [python, script, 'verify-snapshot', work + 'relocated-native-snapshot'],
        [python, script, 'snapshot-scene', '--source', work + 'scene-source',
         '--destination', scene_destination],
        [python, script, 'verify-snapshot', scene_destination, '--adapter', 'scene3d'],
    ]
    check([command.get('argv') for command in commands] == expected,
          'Recorded argv differs from the redacted capture and verification commands.')

def verify_snapshot_inventory(manifest_path, manifest, artifacts):
    root = (out / manifest_path).parent
    members = {row['path']: row for row in manifest['files']}
    check(bool(members) and len(members) == len(manifest['files']) and 'snapshot.json' not in members
          and manifest['entrypoint'] in members, 'Invalid snapshot member inventory.')
    check(manifest['total_bytes'] == sum(row['bytes'] for row in members.values()),
          'Snapshot total differs from its member inventory.')
    expected = {manifest_path, *(root.name + '/' + path for path in members)}
    recorded = {path for path in artifacts if path.startswith(root.name + '/')}
    actual = {path.relative_to(out).as_posix() for path in root.rglob('*') if path.is_file()}
    check(expected == recorded == actual, 'Snapshot member paths differ from the retained inventory.')
    for path, member in members.items():
        row = artifacts[root.name + '/' + path]
        check(row['bytes'] == member['bytes'] and row['sha256'] == member['sha256'],
              'Snapshot member size or digest differs from the workflow inventory: ' + path)
    return expected

def verify_recorded_runs(data,workflow):
 baseline=data['baseline']
 baseline_log=(out/baseline['log']).read_bytes()
 check(hashlib.sha256(baseline_log).hexdigest()==baseline['log_sha256'], 'Legacy baseline log digest differs.')
 check(data['foundation_revision'].startswith(baseline['revision']), 'Legacy baseline revision differs.')
 summary=re.findall(rb'^Ran (\d+) tests? in [^\r\n]+\r?$',baseline_log,re.MULTILINE)
 discovery=re.findall(rb'^Discovered (\d+) 2D tests without optional 3D dependencies\r?$',baseline_log,re.MULTILINE)
 check(summary==[str(baseline['passed']).encode('ascii')] and baseline['passed']>0
       and discovery==[str(baseline['legacy_discovered_not_executed']).encode('ascii')]
       and baseline['failed']==baseline['skipped']==0 and baseline_log.splitlines().count(b'OK')==1,
       'Legacy baseline does not record the claimed passing run.')

 expected=data['hosted_evidence']
 payload=(out/expected['file']).read_bytes()
 check(hashlib.sha256(payload).hexdigest()==expected['sha256'], 'Hosted evidence digest differs.')
 hosted=json.loads(payload)
 checks=hosted['statusCheckRollup']
 check(hosted['headRefOid']==expected['source_head'] and hosted['state']==expected['state']=='MERGED'
       and hosted['mergeCommit']==expected['merge_commit'] and len(checks)==expected['passed_checks']
       and bool(checks) and all(c['status']=='COMPLETED' and c['conclusion']=='SUCCESS' for c in checks),
       'Hosted evidence does not record the claimed successful PR checks.')

 pairs=(
  ('native_capture_relocated_verify_equal','native-capture','native-relocated-verify','native-source-snapshot/snapshot.json'),
  ('scene_capture_verify_equal','scene-capture','scene-verify','scene-source-snapshot/snapshot.json'),
 )
 expected_commands=[(name+'.stdout.json',name+'.stderr.txt') for _,first,second,_ in pairs for name in (first,second)]
 commands=workflow['commands']
 check(workflow['source_revision']==data['foundation_revision']
       and data['source_workflow']=='source-workflow.json'
       and [(c['stdout'],c['stderr']) for c in commands]==expected_commands
       and all(type(c['returncode']) is int and c['returncode']==0 for c in commands),
       'Retained source workflow did not record the four successful commands.')
 verify_command_arguments(commands)
 artifacts={item['path']:item for item in workflow['artifacts']}
 expected_paths={name for command in expected_commands for name in command}
 check(len(artifacts)==len(workflow['artifacts'])
       and expected_paths.issubset(artifacts)
       and all(snapshot in artifacts for _,_,_,snapshot in pairs),
       'Retained workflow output is absent or duplicated in the verified artifact inventory.')
 for flag,first,second,snapshot in pairs:
  captured=json.loads((out/(first+'.stdout.json')).read_bytes())
  verified=json.loads((out/(second+'.stdout.json')).read_bytes())
  retained=json.loads((out/snapshot).read_bytes())
  check(workflow[flag] is True and captured==verified==retained,
        'Retained capture and verification receipts differ.')
  expected_paths.update(verify_snapshot_inventory(snapshot,retained,artifacts))
 check(expected_paths==artifacts.keys(), 'Unexpected artifact in recorded source workflow.')
 for path,item in artifacts.items():
  payload=(out/path).read_bytes()
  check(len(payload)==item['bytes'] and hashlib.sha256(payload).hexdigest()==item['sha256'], path)

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
 tree=subprocess.check_output(['git','rev-parse','--verify','--end-of-options',
                               data['foundation_revision']+'^{tree}'],cwd=repo).decode('ascii').strip()
 check(tree==data['foundation_tree'], 'Foundation tree differs from the pinned revision.')
 for item in data['examined_source_inventory']:
  payload=gitfile(data['foundation_revision'],item['path'])
  check(len(payload)==item['bytes'] and hashlib.sha256(payload).hexdigest()==item['sha256'], (item['path']))
 verify_foundation_review(data)
 refs=set()
 for item in data['field_catalog']+data['required_semantic_traceability']:
  refs.update(item['tests'])
 for item in data['protocols'].values():refs.update(item['tests'])
 native=data['native_job_equivalent'];job_payload,job_cross_checked=job_binding_source(native)
 refs.update(native['tests'])
 cache={}
 for ref in refs:
  file,address=ref.split(':');cls,method=address.split('.')
  if file==native['retained_test_source']['source_path']:key=(native['revision'],file);source=lambda:job_payload
  else:key=(data['foundation_revision'],file);source=lambda:gitfile(data['foundation_revision'],file)
  if key not in cache:cache[key]=ast.parse(source())
  tree=cache[key]
  check(any(isinstance(c,ast.ClassDef) and c.name==cls and any(isinstance(m,ast.FunctionDef) and m.name==method for m in c.body) for c in tree.body), (ref))
 verify_recorded_runs(data,workflow)
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
 check(data['field_catalog_semantics']['complete_paths']==len(expected), 'Recorded schema path count differs.')
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
