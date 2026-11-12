from pathlib import Path
import argparse,hashlib,json,os,subprocess,tempfile
parser=argparse.ArgumentParser(description='Verify exact Git bytes under an actual CRLF checkout.')
parser.add_argument('--repository',type=Path,default=Path.cwd())
parser.add_argument('--destination',type=Path,required=True)
arguments=parser.parse_args()
ROOT=arguments.repository.resolve();DEST=arguments.destination.resolve();DEST.mkdir(parents=True,exist_ok=True)
def git(*args,**kwargs):return subprocess.check_output(['git',*args],cwd=ROOT,**kwargs)
head=git('rev-parse','HEAD',text=True).strip();tree=git('rev-parse','HEAD^{tree}',text=True).strip()
entries=[]
for entry in git('ls-files','--stage','-z').decode().split('\0'):
    if not entry:continue
    meta,name=entry.split('\t',1);mode,oid,stage=meta.split();assert stage=='0'
    assert mode in ('100644','100755','120000'),(mode,name)
    entries.append((name,mode,oid))
attrs=git('check-attr','-z','--stdin','text','eol',input=''.join(name+'\0' for name,_,_ in entries).encode()).decode().split('\0')
policy={}
for offset in range(0,len(attrs)-1,3):
    name,key,value=attrs[offset:offset+3];policy.setdefault(name,{})[key]=value
protected=[];converted=[];file_digests={};physics_log_checks=[];source_checks=[]
with tempfile.TemporaryDirectory(prefix='physics167-preserved-crlf-') as directory:
    prefix=directory+'/'
    subprocess.run(['git','-c','core.autocrlf=true','-c','core.eol=crlf','checkout-index','--all','--prefix='+prefix],cwd=ROOT,check=True,capture_output=True)
    checkout=Path(directory)
    for name,mode,oid in entries:
        path=checkout/name
        data=os.readlink(path).encode() if mode=='120000' else path.read_bytes()
        observed=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        digest=hashlib.sha256(data).hexdigest();file_digests[name]=digest
        rules=policy[name]
        bound=rules.get('text')=='unset' or rules.get('eol')=='lf'
        if bound:
            assert observed==oid,(name,oid,observed,rules)
            protected.append(dict(path=name,git_blob=oid,sha256=digest,bytes=len(data),text=rules.get('text'),eol=rules.get('eol')))
        elif observed!=oid:converted.append(name)
    family='tests/results/physics-contracts-20261024'
    for record_name in ['validation.json','decoder-integration/verification.json']:
        manifest=checkout/family/record_name;record=json.loads(manifest.read_text())
        for command in record['commands']:
            target=manifest.parent/command['log'];observed=hashlib.sha256(target.read_bytes()).hexdigest()
            assert observed==command['log_sha256'],(str(target),observed,command['log_sha256'])
            physics_log_checks.append(dict(record=record_name,path=target.relative_to(checkout).as_posix(),sha256=observed))
        if isinstance(record.get('broad_baseline'),dict):
            baseline=record['broad_baseline'];target=manifest.parent/baseline['log']
            observed=hashlib.sha256(target.read_bytes()).hexdigest();assert observed==baseline['log_sha256']
            physics_log_checks.append(dict(record=record_name,path=target.relative_to(checkout).as_posix(),sha256=observed))
        if 'retained_api_probe_script' in record:
            target=manifest.parent/record['retained_api_probe_script'];observed=hashlib.sha256(target.read_bytes()).hexdigest()
            assert observed==record['api_probe_script_sha256'];physics_log_checks.append(dict(record=record_name,path=target.relative_to(checkout).as_posix(),sha256=observed))
        for name,expected in record.get('source_hashes',{}).items():
            assert file_digests[name]==expected,(name,file_digests[name],expected)
            source_checks.append(dict(path=name,sha256=expected,record=record_name))
    physics_files=[item for item in protected if item['path'].startswith(family+'/')]
    assert len(physics_files)>=16
    frozen_names=['scripts/physics_contracts.py','scripts/rig_errors.py','tests/test_physics_contracts.py','profiles/codex-v1.json','profiles/profile.schema.json','assets/playback.js','state-library/catalog.json']
    for name in frozen_names:assert any(item['path']==name for item in protected),name
    regression='tests/results/physics-contracts-20261024/decoder-integration/contracts.log'
    assert file_digests[regression]=='66d868d02ad7723fdcac74f8b6d9599962d5d9a56402048a28f0fda9d7eeec91'
    report=dict(kind='actual-crlf-checkout-preservation',source_commit=head,source_tree=tree,
        shared_attributes_commit='91d14b8339eb16a6285969d9bc772b11bdfec3ec',core_autocrlf=True,core_eol='crlf',
        tracked_files_checked=len(entries),protected_files_verified=len(protected),physics_evidence_files_verified=len(physics_files),
        matched_physics_receipt_digests=physics_log_checks,matched_current_artifact_source_digests=source_checks,
        regression_file=dict(path=regression,sha256=file_digests[regression],byte_identical=True),
        unprotected_text_files_intentionally_converted=converted,protected_files=protected,
        source_worktree_modified=False,head_modified=False,solver_execution=None)
    (DEST/'crlf-verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({key:report[key] for key in ['source_commit','tracked_files_checked','protected_files_verified','physics_evidence_files_verified','regression_file']}))
