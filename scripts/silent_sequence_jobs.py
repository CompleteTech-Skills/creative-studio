"""Persistent orchestration over canonical byte-preserving image-sequence export.

This adapter does not render, convert image precision, or encode previews.
Each job retains its canonical source, decoder identities and export cache.
"""
from __future__ import annotations
import copy
from contextlib import contextmanager
import hashlib
import importlib.metadata
import os
from pathlib import Path
import stat
import time

from atomic_publish import pinned_directory, private_stage, _rename_no_replace
import silent_jobs as jobs
import silent_sequence as sequence

ACTIVE={'validating','preparing','verifying','finalizing'}
PHASES=ACTIVE|{'queued','completed','interrupted','failed','cancelled','stale','blocked'}
DEFAULT_BUDGET={'wall_seconds':1200,'max_work_bytes':8*1024**3,'max_memory_bytes':1024**3,'max_runs':8}
BUDGET=sequence.media._obj({'wall_seconds':sequence.media._int(1,3600),
    'max_work_bytes':sequence.media._int(1024**2,16*1024**3),
    'max_memory_bytes':sequence.media._int(1024**2,2*1024**3),'max_runs':sequence.media._int(1,8)})


def _implementation():
    identities={name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for name,module in [('sequence',sequence),('jobs',jobs),('controller',__import__(__name__)),
                            ('path_boundary',__import__('rig_errors')),
                            ('publication',__import__('atomic_publish')),
                            ('decoder',__import__('render3d_verify'))]}
    for package in ('numpy','Pillow','OpenEXR'):
        try:identities['package:'+package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:identities['package:'+package]='unavailable'
    return identities


def _plan(fd):
    info=os.fstat(fd)
    if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('sequence job must be private and owned by this user')
    plan=jobs._json(fd,'plan.json',sequence.MAX_JSON_BYTES)
    fields={'schema_version','kind','id','source','source_root','destination','provenance','budget','implementation'}
    if (not isinstance(plan,dict) or set(plan)!=fields or type(plan['schema_version']) is not int or
            plan['schema_version']!=1 or plan['kind']!='silent-sequence-job'):
        raise ValueError('invalid sequence job plan')
    sequence._check_source(plan['source'])
    sequence.media._structural(plan['budget'],BUDGET,'sequence job budget')
    if plan['id']!=sequence.media._digest({k:v for k,v in plan.items() if k!='id'}):
        raise ValueError('sequence job identity changed')
    if plan['source']['origin']!='imported':raise ValueError('native jobs require canonical render closure integration')
    if not isinstance(plan['provenance'],dict) or not plan['provenance']:raise ValueError('source provenance required')
    for key in ('source_root','destination'):
        if not isinstance(plan[key],str) or not Path(plan[key]).is_absolute():raise ValueError('absolute job locations required')
    return plan


def _state(fd,plan):
    value=jobs._json(fd,'state.json')
    if (not isinstance(value,dict) or set(value)!={'schema_version','job','phase','runs','progress','error','receipt_sha256'} or
            type(value['schema_version']) is not int or value['schema_version']!=1 or value['job']!=plan['id'] or
            not isinstance(value['phase'],str) or value['phase'] not in PHASES or
            type(value['runs']) is not int or not 0<=value['runs']<=8 or not isinstance(value['progress'],dict)):
        raise ValueError('invalid sequence job checkpoint')
    if value['error'] is not None and (not isinstance(value['error'],str) or len(value['error'])>4096):raise ValueError('invalid sequence job error')
    if value['receipt_sha256'] is not None:
        sequence.media._structural(value['receipt_sha256'],sequence.SHA,'sequence receipt hash')
    return value


def _save(fd,state,phase,**changes):
    state.update(changes,phase=phase);jobs._put_json(fd,'state.json',state,replace=True)


@contextmanager
def _locked(root):
    import fcntl
    with pinned_directory(root) as fd:
        plan=_plan(fd)
        lock=os.open('lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):raise ValueError('unsafe sequence job lock')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:raise ValueError('sequence job already owned') from exc
            yield fd,plan
        finally:os.close(lock)


def create(source,source_root,job_root,destination,*,provenance,budget=None):
    if not sequence.publication_supported():raise ValueError('secure sequence staging unavailable on this platform')
    sequence._capability_factory()
    source=sequence._check_source(copy.deepcopy(source));budget=copy.deepcopy(budget or DEFAULT_BUDGET)
    sequence.media._structural(budget,BUDGET,'sequence job budget')
    if source['origin']!='imported':raise ValueError('native jobs require canonical render closure integration')
    if not isinstance(provenance,dict) or not provenance:raise ValueError('explicit source provenance required')
    if len(source['frames'])/sequence.media.fps_fraction(source['fps'])>3600:
        raise ValueError('sequence job duration exceeds one hour')
    if 128*1024**2+source['resolution'][0]*source['resolution'][1]*48>budget['max_memory_bytes']:
        raise ValueError('sequence exceeds decoded-memory admission budget')
    if 3*sum(f['bytes'] for f in source['frames'])+sequence.MAX_JSON_BYTES>budget['max_work_bytes']:
        raise ValueError('sequence exceeds work-byte budget')
    source_root=Path(source_root).absolute()
    with pinned_directory(source_root):pass
    job_root=sequence.media._destination(job_root);destination=sequence.media._destination(destination)
    plan={'schema_version':1,'kind':'silent-sequence-job','source':source,'source_root':str(source_root),
        'destination':str(destination),'provenance':copy.deepcopy(provenance),'budget':budget,'implementation':_implementation()}
    plan['id']=sequence.media._digest(plan);jobs._bytes(plan,sequence.MAX_JSON_BYTES)
    state={'schema_version':1,'job':plan['id'],'phase':'queued','runs':0,'progress':{},'error':None,'receipt_sha256':None}
    with pinned_directory(job_root.parent) as parent:
        with private_stage(parent,prefix='.sequence-job-') as (_,wrapper):
            os.mkdir('payload',mode=0o700,dir_fd=wrapper)
            fd=os.open('payload',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=wrapper)
            try:
                jobs._put_json(fd,'plan.json',plan,maximum=sequence.MAX_JSON_BYTES)
                jobs._put_json(fd,'state.json',state);jobs._write(fd,'lock',b'')
                _rename_no_replace('payload',job_root.name,source_dir_fd=wrapper,destination_dir_fd=parent);os.fsync(parent)
            finally:os.close(fd)
    return {'plan':plan,'state':state}


def _provenance(plan):return {'job':plan['id'],'source':plan['provenance']}


def _cache_reuse(plan):
    stage=Path(plan['destination']).parent/('.sequence-job-'+plan['id'][:32])
    try:
        with pinned_directory(stage) as fd,sequence._capability(fd) as authority:
            if sequence._read_json(authority.path,'plan.json')!={'source':plan['source'],'provenance':_provenance(plan)}:
                raise ValueError('sequence cache belongs to another job')
            sequence._inventory(authority.path,plan['source'],staging=True)
            count=0
            for frame in plan['source']['frames']:
                if not jobs._exists(fd,frame['path']):continue
                data=jobs._read(fd,frame['path'],sequence.MAX_FRAME_BYTES)
                if len(data)!=frame['bytes'] or hashlib.sha256(data).hexdigest()!=frame['sha256']:
                    raise ValueError('sequence cache bytes changed')
                count+=1
            return count
    except FileNotFoundError:
        # Only an absent cache is fresh. A partial cache whose plan or frame
        # directory disappeared remains a malformed cache, not reusable input.
        if stage.exists():raise ValueError('incomplete sequence cache')
        return 0


def _verify(plan,state):
    # The shared publication authority recognizes only live registered
    # capabilities. Keep the canonical verifier and receipt hash on one inode.
    with pinned_directory(plan['destination']) as fd,sequence._capability(fd) as authority:
        report=sequence.verify_sequence(authority.path,expected_receipt_sha256=state['receipt_sha256'])
        if report['source']!=plan['source'] or report['provenance']!=_provenance(plan):
            raise ValueError('sequence destination belongs to another job')
        digest=hashlib.sha256(jobs._read(fd,'sequence.json',sequence.MAX_JSON_BYTES)).hexdigest()
    return report,digest


def status(job_root):
    # An active owner must not have its checkpoint rewritten by a status reader.
    import fcntl
    with pinned_directory(job_root) as fd:
        plan=_plan(fd);state=_state(fd,plan);owned=False
        lock=os.open('lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):raise ValueError('unsafe sequence job lock')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:owned=True
            if not owned and state['phase'] in ACTIVE:_save(fd,state,'interrupted',error='Lost controller ownership; explicit resume required')
            if not owned and state['phase']=='completed':
                try:_verify(plan,state)
                except (ValueError,OSError) as exc:_save(fd,state,'stale',error=str(exc)[:4096])
        finally:os.close(lock)
    return {'plan':plan,'state':state,'owned':owned}


def cancel(job_root):
    with pinned_directory(job_root) as fd:
        plan=_plan(fd);state=_state(fd,plan)
        if state['phase']=='completed':return {'job':plan['id'],'cancellation_requested':False}
        if not jobs._exists(fd,'cancel'):jobs._write(fd,'cancel',b'cancel\n')
        else:jobs._read(fd,'cancel',16)
    return {'job':plan['id'],'cancellation_requested':True}


def run(job_root,*,resume=False,retry=False,progress=None):
    if resume and retry:raise ValueError('select resume or retry, not both')
    with _locked(job_root) as (fd,plan):
        state=_state(fd,plan)
        if state['phase']=='completed':
            try:_verify(plan,state)
            except (ValueError,OSError) as exc:_save(fd,state,'stale',error=str(exc)[:4096])
            return {'plan':plan,'state':state}
        if state['phase'] in ACTIVE:_save(fd,state,'interrupted',error='Lost controller ownership')
        allowed={'interrupted'} if resume else ({'failed','cancelled','blocked'} if retry else {'queued'})
        if state['phase'] not in allowed:raise ValueError('explicit compatible resume/retry required')
        if plan['implementation']!=_implementation():raise ValueError('stale sequence controller/decoder implementation')
        if state['runs']>=plan['budget']['max_runs']:raise ValueError('sequence job run quota exhausted')
        if retry and jobs._exists(fd,'cancel'):jobs._unlink(fd,'cancel')
        state['runs']+=1;_save(fd,state,'validating',error=None);started=time.monotonic();checks=0
        def checkpoint():
            nonlocal checks
            if time.monotonic()-started>plan['budget']['wall_seconds']:raise ValueError('sequence job wall-time budget exceeded')
            checks+=1
            _save(fd,state,'preparing',progress={'stage':'canonical-sequence-copy','checkpoint':checks})
            if progress is not None:progress(copy.deepcopy(state))
            return jobs._exists(fd,'cancel')
        try:
            if checkpoint():raise ValueError('sequence job cancelled')
            reused=0
            if Path(plan['destination']).exists():
                report,digest=_verify(plan,state)
                reused=len(plan['source']['frames'])
            else:
                reused=_cache_reuse(plan)
                sequence.export_sequence(plan['source'],plan['source_root'],plan['destination'],
                    provenance=_provenance(plan),stage_name='.sequence-job-'+plan['id'][:32],cancel=checkpoint)
                _save(fd,state,'verifying');report,digest=_verify(plan,state)
            _save(fd,state,'completed',receipt_sha256=digest,error=None,
                progress={'stage':'completed','frames':len(report['source']['frames']),
                          'cached_reused':reused,'cached_created':len(plan['source']['frames'])-reused,
                          'elapsed_seconds':time.monotonic()-started})
            return {'plan':plan,'state':state}
        except (ValueError,OSError) as exc:
            _save(fd,state,'cancelled' if jobs._exists(fd,'cancel') else 'failed',error=str(exc)[:4096])
            return {'plan':plan,'state':state}


def run_batch(job_roots,*,retry_failed=False,progress=None):
    if not isinstance(job_roots,list) or not 1<=len(job_roots)<=jobs.MAX_BATCH:raise ValueError('bounded independent job list required')
    roots=[str(Path(p).absolute()) for p in job_roots]
    if len(set(roots))!=len(roots):raise ValueError('duplicate batch job')
    inventories=[status(root) for root in roots]
    if len({v['plan']['destination'] for v in inventories})!=len(roots):raise ValueError('duplicate batch destination')
    results=[]
    for root,value in zip(roots,inventories):
        phase=value['state']['phase']
        if phase=='completed':results.append(value);continue
        try:results.append(run(root,resume=phase=='interrupted',retry=retry_failed and phase in {'failed','cancelled','blocked'},progress=progress))
        except (ValueError,OSError) as exc:results.append({'job_root':root,'phase':'blocked','error':str(exc)[:4096]})
    return {'schema_version':1,'kind':'silent-sequence-batch','concurrency':1,'jobs':results}
