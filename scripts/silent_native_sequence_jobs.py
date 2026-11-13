"""Persistent native sequence jobs using canonical render verification/export.

Native request, result, backend and snapshot closure remain canonical. The outer
immutable delivery adds job ownership without changing native sequence receipts.
"""
from __future__ import annotations
import copy
import hashlib
import inspect
import re
import os
from pathlib import Path
import shutil
import stat
import time
from contextlib import contextmanager

from atomic_publish import pinned_directory,private_stage,_rename_no_replace
import silent_jobs as jobs
import silent_sequence as sequence
import silent_sequence_jobs as controls


def _boundary():
    if not sequence.publication_supported():raise ValueError('secure native sequence jobs unavailable on this platform')
    sequence._capability_factory()
    if 'destination_dir_fd' not in inspect.signature(sequence.export_sequence).parameters:
        raise ValueError('canonical native sequence descriptor destination is unavailable')
    sequence._decoder()


def _implementation():
    return {**controls._implementation(),'native_controller':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _admit(request,result,budget):
    controls._budget(budget)
    settings=request.get('settings') if isinstance(request,dict) else None
    resolution=settings.get('resolution') if isinstance(settings,dict) else None
    if (not isinstance(resolution,list) or len(resolution)!=2 or
            any(type(n) is not int or not 1<=n<=4096 for n in resolution)):
        raise ValueError('bounded native resolution required')
    images=result.get('images') if isinstance(result,dict) else None
    if not isinstance(images,list) or not 1<=len(images)<=sequence.media.MAX_FRAMES:
        raise ValueError('bounded complete native frame inventory required')
    total=0
    for image in images:
        record=image.get('file') if isinstance(image,dict) else None
        count=record.get('bytes') if isinstance(record,dict) else None
        if type(count) is not int or not 1<=count<=sequence.MAX_FRAME_BYTES:raise ValueError('bounded native frame bytes required')
        total+=count
        if total>sequence.MAX_TOTAL_BYTES:raise ValueError('native frames exceed total-byte budget')
    if 5*total+2*jobs.MAX_PLAN_BYTES>budget['max_work_bytes']:raise ValueError('native sequence exceeds peak work-byte budget')
    if 128*1024**2+resolution[0]*resolution[1]*48>budget['max_memory_bytes']:
        raise ValueError('native sequence exceeds decoded-memory budget')
    return 5*total+2*jobs.MAX_PLAN_BYTES


def create_native(request,result,render_root,job_root,destination,*,snapshot_root,expected_backend,budget=None):
    _boundary();budget=copy.deepcopy(budget or controls.DEFAULT_BUDGET)
    request=copy.deepcopy(request);result=copy.deepcopy(result);expected_backend=copy.deepcopy(expected_backend)
    peak=_admit(request,result,budget)
    render_root=Path(render_root).absolute();snapshot_root=Path(snapshot_root).absolute()
    # Pin/admit ordinary paths before the canonical owner opens their closure.
    with pinned_directory(render_root),pinned_directory(snapshot_root):pass
    source,proof=sequence.native_source(request,result,render_root,snapshot_root=snapshot_root,expected_backend=expected_backend)
    if len(source['frames'])/sequence.media.fps_fraction(source['fps'])>3600:raise ValueError('native job duration exceeds one hour')
    job_root=sequence.media._destination(job_root);destination=sequence.media._destination(destination)
    plan={'schema_version':1,'kind':'silent-native-sequence-job','source':source,'proof':proof,
        'render_root':str(render_root),'snapshot_root':str(snapshot_root),'destination':str(destination),
        'budget':budget,'implementation':_implementation()}
    plan['id']=sequence.media._digest(plan);jobs._bytes(plan,jobs.MAX_PLAN_BYTES)
    state={'schema_version':1,'job':plan['id'],'phase':'queued','runs':0,'progress':{},'error':None,'receipt_sha256':None}
    with pinned_directory(destination.parent) as fd:
        if shutil.disk_usage(Path('/proc/self/fd')/str(fd)).free<peak:raise ValueError('insufficient free native delivery storage')
    with pinned_directory(job_root.parent) as parent:
        if shutil.disk_usage(Path('/proc/self/fd')/str(parent)).free<peak:raise ValueError('insufficient free native job storage')
        with private_stage(parent,prefix='.native-sequence-job-') as (_,wrapper):
            os.mkdir('payload',mode=0o700,dir_fd=wrapper)
            fd=os.open('payload',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=wrapper)
            try:
                jobs._put_json(fd,'plan.json',plan,maximum=jobs.MAX_PLAN_BYTES);jobs._put_json(fd,'state.json',state);jobs._write(fd,'lock',b'')
                _rename_no_replace('payload',job_root.name,source_dir_fd=wrapper,destination_dir_fd=parent);os.fsync(parent)
            finally:os.close(fd)
    return {'plan':plan,'state':state}


def _plan(fd):
    info=os.fstat(fd)
    if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:raise ValueError('native job must be private and owned')
    value=jobs._json(fd,'plan.json',jobs.MAX_PLAN_BYTES)
    fields={'schema_version','kind','id','source','proof','render_root','snapshot_root','destination','budget','implementation'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int or
            value['schema_version']!=1 or value['kind']!='silent-native-sequence-job'):
        raise ValueError('invalid native sequence job plan')
    if value['id']!=sequence.media._digest({k:v for k,v in value.items() if k!='id'}):raise ValueError('native job identity changed')
    controls._source(value['source'])
    if value['source']['origin']!='blender3d':raise ValueError('native job requires actual canonical render provenance')
    proof=value['proof']
    if not isinstance(proof,dict) or set(proof)!={'request','result','expected_backend','verification'}:raise ValueError('native source closure required')
    _admit(proof['request'],proof['result'],value['budget'])
    for key in ('render_root','snapshot_root','destination'):
        if not isinstance(value[key],str) or not 0<len(value[key])<=4096 or not Path(value[key]).is_absolute():raise ValueError('absolute native locations required')
    return value


@contextmanager
def _locked(root):
    import fcntl
    with pinned_directory(root) as fd:
        plan=_plan(fd);lock=os.open('lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):raise ValueError('unsafe native job lock')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:raise ValueError('native sequence job already owned') from exc
            yield fd,plan
        finally:os.close(lock)


def _live_source(plan):
    proof=plan['proof']
    source,current=sequence.native_source(proof['request'],proof['result'],plan['render_root'],
        snapshot_root=plan['snapshot_root'],expected_backend=proof['expected_backend'])
    if source!=plan['source'] or current!=proof:raise ValueError('native source or snapshot closure changed')


def _workspace(fd):
    with os.scandir(Path('/proc/self/fd')/str(fd)) as entries:
        for count,entry in enumerate(entries,1):
            if count>32:raise ValueError('native job workspace exceeds entry budget')
            info=entry.stat(follow_symlinks=False)
            if entry.name in {'plan.json','state.json','lock','cancel'}:
                if not stat.S_ISREG(info.st_mode):raise ValueError('unsafe native checkpoint entry')
            elif entry.name in {'native-sequence','.native-sequence-cache'}:
                if not stat.S_ISDIR(info.st_mode):raise ValueError('unsafe native sequence cache')
            elif re.fullmatch(r'\.sequence-publish-[a-z0-9_]{8}',entry.name):
                # The canonical publisher deliberately retains empty pinned
                # wrappers. Never remove a replaceable outer basename.
                child=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    owned=os.fstat(child)
                    if owned.st_uid!=os.geteuid() or stat.S_IMODE(owned.st_mode)&0o077:raise ValueError('unowned native staging wrapper')
                    with os.scandir(Path('/proc/self/fd')/str(child)) as contents:
                        if next(contents,None) is not None:raise ValueError('unowned native staging content')
                finally:os.close(child)
            else:raise ValueError('unowned partial artifact in native job workspace')


def _cache(fd,plan,checkpoint):
    proof=plan['proof']
    with sequence._capability(fd) as authority:
        cache=authority.path/'native-sequence'
        if not jobs._exists(fd,'native-sequence'):
            sequence.export_native_sequence(proof['request'],proof['result'],plan['render_root'],'native-sequence',
                snapshot_root=plan['snapshot_root'],expected_backend=proof['expected_backend'],
                destination_dir_fd=fd,stage_name='.native-sequence-cache',cancel=checkpoint)
        report=sequence.verify_sequence(cache)
        if report['source']!=plan['source'] or report['provenance']!=proof:raise ValueError('native cache identity changed')
        return report


def _delivery(plan,state):
    with pinned_directory(plan['destination']) as fd,sequence._capability(fd) as authority:
        if set(os.listdir(authority.path))!={'sequence','job.json'}:raise ValueError('invalid native delivery inventory')
        raw=jobs._read(fd,'job.json',jobs.MAX_PLAN_BYTES)
        digest=hashlib.sha256(raw).hexdigest()
        if state['receipt_sha256'] is not None and state['receipt_sha256']!=digest:raise ValueError('native delivery receipt changed')
        envelope=jobs._json(fd,'job.json',jobs.MAX_PLAN_BYTES)
        if not isinstance(envelope,dict) or set(envelope)!={'schema_version','kind','job','plan_sha256','sequence_receipt_sha256','source_identity_sha256','visual_review'}:
            raise ValueError('invalid native delivery ownership envelope')
        if (type(envelope['schema_version']) is not int or envelope['schema_version']!=1 or envelope['kind']!='silent-native-sequence-delivery' or
                envelope['job']!=plan['id'] or envelope['plan_sha256']!=sequence.media._digest(plan) or
                envelope['source_identity_sha256']!=plan['source']['identity_sha256'] or envelope['visual_review']!='not-performed'):
            raise ValueError('native delivery belongs to another job')
        report=sequence.verify_sequence(authority.path/'sequence',expected_receipt_sha256=envelope['sequence_receipt_sha256'])
        if report['source']!=plan['source'] or report['provenance']!=plan['proof']:raise ValueError('native delivery source closure changed')
        return digest,report


def _publish(fd,plan,state):
    destination=sequence.media._destination(plan['destination'])
    with pinned_directory(destination.parent) as parent,private_stage(parent,prefix='.native-sequence-delivery-') as (_,wrapper):
        os.mkdir('payload',mode=0o700,dir_fd=wrapper)
        payload=os.open('payload',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=wrapper)
        try:
            os.mkdir('sequence',mode=0o700,dir_fd=payload)
            output=os.open('sequence',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=payload)
            cache=None
            try:
                cache=os.open('native-sequence',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                lock=os.open('lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=cache)
                try:
                    if not stat.S_ISREG(os.fstat(lock).st_mode):raise ValueError('unsafe canonical sequence lock')
                finally:os.close(lock)
                jobs._write(output,'lock',b'')
                for name in ('plan.json','sequence.json'):jobs._write(output,name,jobs._read(cache,name,jobs.MAX_PLAN_BYTES))
                os.mkdir('frames',mode=0o700,dir_fd=output)
                frames=os.open('frames',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=output)
                try:
                    for frame in plan['source']['frames']:
                        jobs._copy_artifact(cache,frame['path'],frames,Path(frame['path']).name,sequence.MAX_FRAME_BYTES,
                            {'sha256':frame['sha256'],'bytes':frame['bytes']})
                finally:os.close(frames)
                with sequence._capability(output) as authority:
                    report=sequence.verify_sequence(authority.path)
                    if report['source']!=plan['source'] or report['provenance']!=plan['proof']:raise ValueError('native publication cache changed')
                reference=hashlib.sha256(jobs._read(output,'sequence.json',jobs.MAX_PLAN_BYTES)).hexdigest()
            finally:
                os.close(output)
                if cache is not None:os.close(cache)
            envelope={'schema_version':1,'kind':'silent-native-sequence-delivery','job':plan['id'],
                'plan_sha256':sequence.media._digest(plan),'sequence_receipt_sha256':reference,
                'source_identity_sha256':plan['source']['identity_sha256'],'visual_review':'not-performed'}
            raw=jobs._bytes(envelope,jobs.MAX_PLAN_BYTES);jobs._write(payload,'job.json',raw);os.fsync(payload)
            _rename_no_replace('payload',destination.name,source_dir_fd=wrapper,destination_dir_fd=parent);os.fsync(parent)
            return hashlib.sha256(raw).hexdigest()
        finally:os.close(payload)


def run(job_root,*,resume=False,retry=False,progress=None):
    if resume and retry:raise ValueError('select resume or retry, not both')
    with _locked(job_root) as (fd,plan):
        state=controls._state(fd,plan)
        if state['phase']=='completed':
            try:_delivery(plan,state)
            except (ValueError,OSError) as exc:controls._save(fd,state,'stale',error=str(exc)[:4096])
            return {'plan':plan,'state':state}
        if state['phase'] in controls.ACTIVE:controls._save(fd,state,'interrupted',error='Lost native controller ownership')
        allowed={'interrupted'} if resume else ({'failed','cancelled','blocked'} if retry else {'queued'})
        if state['phase'] not in allowed:raise ValueError('explicit native resume/retry required')
        if plan['implementation']!=_implementation():raise ValueError('stale native controller/decoder implementation')
        if state['runs']>=plan['budget']['max_runs']:raise ValueError('native job run quota exhausted')
        if retry and jobs._exists(fd,'cancel'):jobs._unlink(fd,'cancel')
        state['runs']+=1;controls._save(fd,state,'validating',error=None);started=time.monotonic();checks=0
        def checkpoint(phase='preparing'):
            nonlocal checks
            if time.monotonic()-started>plan['budget']['wall_seconds']:raise ValueError('native sequence wall-time budget exceeded')
            checks+=1
            stage={'validating':'canonical-native-verify','finalizing':'native-publication'}.get(phase,'canonical-native-export')
            controls._save(fd,state,phase,progress={'stage':stage,'checkpoint':checks})
            if progress is not None:progress(copy.deepcopy(state))
            return jobs._exists(fd,'cancel')
        try:
            _boundary()
            if checkpoint('validating'):raise ValueError('native sequence cancelled')
            _workspace(fd)
            _live_source(plan)
            if Path(plan['destination']).exists():
                controls._save(fd,state,'verifying');digest,report=_delivery(plan,state)
            else:
                report=_cache(fd,plan,checkpoint)
                if checkpoint('finalizing'):raise ValueError('native sequence cancelled before publication')
                digest=_publish(fd,plan,state)
            controls._save(fd,state,'completed',receipt_sha256=digest,error=None,
                progress={'stage':'completed','frames':len(report['source']['frames']),'elapsed_seconds':time.monotonic()-started})
        except (ValueError,OSError) as exc:
            controls._save(fd,state,'cancelled' if jobs._exists(fd,'cancel') else 'failed',error=str(exc)[:4096])
        return {'plan':plan,'state':state}


def status(job_root):
    import fcntl
    with pinned_directory(job_root) as fd:
        plan=_plan(fd);owned=False
        lock=os.open('lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):raise ValueError('unsafe native job lock')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:owned=True
            # Read the mutable checkpoint after the ownership decision. An
            # owner may have completed between opening the directory and flock.
            state=controls._state(fd,plan)
            if not owned and state['phase'] in controls.ACTIVE:controls._save(fd,state,'interrupted',error='Lost native controller ownership')
            if not owned and state['phase']=='completed':
                try:_delivery(plan,state)
                except (ValueError,OSError) as exc:controls._save(fd,state,'stale',error=str(exc)[:4096])
        finally:os.close(lock)
    return {'plan':plan,'state':state,'owned':owned}


def cancel(job_root):
    with pinned_directory(job_root) as fd:
        plan=_plan(fd);state=controls._state(fd,plan)
        if state['phase']=='completed':return {'job':plan['id'],'cancellation_requested':False}
        if jobs._exists(fd,'cancel'):jobs._read(fd,'cancel',16)
        else:jobs._write(fd,'cancel',b'cancel\n')
    return {'job':plan['id'],'cancellation_requested':True}
