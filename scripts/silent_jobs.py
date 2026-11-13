"""Bounded persistent jobs over the canonical silent-media encoder.

Jobs snapshot exact PNG masters, never append partial movies, and publish one
immutable delivery directory. This is not a renderer or a render scheduler.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from atomic_publish import (_rename_no_replace, private_stage, pinned_directory,
                            private_stage_supported)
import silent_video as media

MAX_PLAN_BYTES=32*1024**2
MAX_STATE_BYTES=1024**2
MAX_RECEIPT_BYTES=64*1024**2
MAX_TOOL_BYTES=256*1024**2
MAX_BATCH=16
ACTIVE={'validating','preparing','encoding','verifying','finalizing'}
TERMINAL={'completed','failed','interrupted','cancelled','blocked','stale'}
STATES=ACTIVE|TERMINAL|{'queued'}
DEFAULT_BUDGET={'wall_seconds':1200,'max_work_bytes':8*1024**3,
                'max_memory_bytes':1024**3,'max_attempts':3,'max_runs':8}
BUDGET=media._obj({'wall_seconds':media._int(1,3600),'max_work_bytes':media._int(1024**2,32*1024**3),
                  'max_memory_bytes':media._int(1024**2,2*1024**3),
                  'max_attempts':media._int(1,3),'max_runs':media._int(1,8)})


def publication_supported():
    return private_stage_supported()


def _worker_boundary():
    try:
        from rig_errors import directory_capability
    except ImportError as exc:
        raise ValueError('canonical scoped descriptor capability is unavailable') from exc
    if 'destination_dir_fd' not in inspect.signature(media.encode).parameters:
        raise ValueError('canonical silent encoder descriptor destination API is unavailable')
    return directory_capability


def worker_capability():
    if not publication_supported():return {'status':'unavailable','reason':'secure job staging requires Linux procfs/dirfd support'}
    try:_worker_boundary()
    except ValueError as exc:return {'status':'blocked','reason':str(exc)}
    return {'status':'available','concurrency':1,'threads':2,'gpu':False}


def _bytes(value,maximum):
    try:data=(json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    except (TypeError,ValueError,RecursionError) as exc:raise ValueError('invalid finite job JSON') from exc
    if len(data)>maximum:raise ValueError('job JSON exceeds byte budget')
    return data


@contextmanager
def _parent(fd,relative):
    if (not isinstance(relative,str) or not 0<len(relative)<=1024 or relative.startswith('/') or
            '\\' in relative or ':' in relative or '\0' in relative or
            any(p in ('','.','..') for p in relative.split('/'))):raise ValueError('unsafe job path')
    current=os.dup(fd)
    try:
        parts=relative.split('/')
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
            os.close(current);current=child
        yield current,parts[-1]
    finally:os.close(current)


def _read(fd,relative,maximum):
    with _parent(fd,relative) as (parent,name):
        descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(descriptor,'rb') as stream:
            size=os.fstat(stream.fileno())
            if not stat.S_ISREG(size.st_mode) or not 0<size.st_size<=maximum:
                raise ValueError('job record must be a bounded regular file')
            data=stream.read(maximum+1)
            if len(data)!=size.st_size:raise ValueError('job record changed while reading')
            return data


def _json(fd,name,maximum=MAX_STATE_BYTES):
    def unique(pairs):
        out={}
        for key,value in pairs:
            if key in out:raise ValueError('duplicate job JSON key')
            out[key]=value
        return out
    try:return json.loads(_read(fd,name,maximum),object_pairs_hook=unique,
        parse_constant=lambda v:(_ for _ in ()).throw(ValueError('nonfinite job JSON')))
    except (UnicodeError,RecursionError,json.JSONDecodeError) as exc:raise ValueError('invalid job JSON') from exc


def _exists(fd,name):
    try:
        with _parent(fd,name) as (parent,base):os.stat(base,dir_fd=parent,follow_symlinks=False)
        return True
    except FileNotFoundError:return False


def _write(fd,name,data,*,replace=False):
    with _parent(fd,name) as (parent,base):
        temp='.job-write-'+os.urandom(12).hex()
        descriptor=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            if replace:os.replace(temp,base,src_dir_fd=parent,dst_dir_fd=parent)
            else:_rename_no_replace(temp,base,source_dir_fd=parent,destination_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:os.unlink(temp,dir_fd=parent)
            except FileNotFoundError:pass


def _put_json(fd,name,value,*,replace=False,maximum=MAX_STATE_BYTES):
    _write(fd,name,_bytes(value,maximum),replace=replace)


def _unlink(fd,name):
    with _parent(fd,name) as (parent,base):
        # Pin and validate the same parent used by unlink, including cancellation.
        descriptor=os.open(base,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):raise ValueError('unsafe job marker')
            os.unlink(base,dir_fd=parent)
        finally:os.close(descriptor)


def _sha_path(path,maximum):
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=maximum:raise ValueError('unsafe executable/artifact bytes')
        digest=hashlib.sha256();size=0
        for block in iter(lambda:stream.read(1024**2),b''):
            size+=len(block)
            if size>maximum:raise ValueError('executable/artifact grew beyond budget')
            digest.update(block)
        if size!=info.st_size:raise ValueError('executable/artifact changed during read')
        return {'sha256':digest.hexdigest(),'bytes':size}


def _reference(fd,name,maximum):
    with _parent(fd,name) as (parent,base):
        descriptor=os.open(base,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(descriptor,'rb') as stream:
            observed=os.fstat(stream.fileno())
            if not stat.S_ISREG(observed.st_mode) or not 0<observed.st_size<=maximum:raise ValueError('artifact must be a bounded regular file')
            digest=hashlib.sha256();size=0
            for block in iter(lambda:stream.read(1024**2),b''):
                size+=len(block)
                if size>maximum:raise ValueError('artifact grew beyond byte budget')
                digest.update(block)
            if size!=observed.st_size:raise ValueError('artifact changed while hashing')
            return {'sha256':digest.hexdigest(),'bytes':size}


def _copy_artifact(fd,name,target_fd,target_name,maximum,expected):
    with _parent(fd,name) as (parent,base):
        source=os.open(base,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(source,'rb') as stream:
            observed=os.fstat(stream.fileno())
            if not stat.S_ISREG(observed.st_mode) or not 0<observed.st_size<=maximum:raise ValueError('unsafe private artifact')
            output=os.open(target_name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=target_fd)
            digest=hashlib.sha256();size=0
            with os.fdopen(output,'wb') as destination:
                for block in iter(lambda:stream.read(1024**2),b''):
                    size+=len(block)
                    if size>maximum:raise ValueError('artifact copy exceeds byte budget')
                    digest.update(block);destination.write(block)
                destination.flush();os.fsync(destination.fileno())
            if size!=observed.st_size or {'sha256':digest.hexdigest(),'bytes':size}!=expected:
                raise ValueError('verified private artifact changed before publication')


def _tools(ffmpeg=None,ffprobe=None):
    tools={}
    for name,override in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]:
        path=media._binary(name,override)
        before=_sha_path(path,MAX_TOOL_BYTES)
        version=media._run([path,'-version'],{'timeout_seconds':10,'max_output_bytes':1024**2,'max_log_bytes':1024**2})
        if _sha_path(path,MAX_TOOL_BYTES)!=before:raise ValueError('media executable changed during capability measurement')
        tools[name]={'path':path,**before,'version_stdout':version['stdout']}
    return tools


def _implementation():
    modules={'controller':Path(__file__),'encoder':Path(media.__file__)}
    import atomic_publish
    modules['publication']=Path(atomic_publish.__file__)
    import rig_errors
    modules['path_boundary']=Path(rig_errors.__file__)
    try:
        import silent_profiles
        modules['profiles']=Path(silent_profiles.__file__)
    except ImportError:pass
    return {k:_sha_path(v,MAX_TOOL_BYTES)['sha256'] for k,v in modules.items()}


def _estimate(request):
    source=sum(f['bytes'] for f in request['source']['frames']);limits=request['limits']
    # Cache + encoder's private snapshot + retained attempts + verification and
    # final payload copies. This is admission, not a claim of OS-level isolation.
    return {'source_bytes':source,'peak_work_bytes':3*source+5*limits['max_output_bytes']+8*limits['max_log_bytes']+MAX_RECEIPT_BYTES,
            'decoded_memory_estimate':128*1024**2+request['source']['width']*request['source']['height']*48,
            'threads':request.get('encoding',{}).get('threads',1),'gpu_bytes':0}


def _plan(fd):
    directory=os.fstat(fd)
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid!=os.geteuid() or stat.S_IMODE(directory.st_mode)&0o077:
        raise ValueError('job root must be private and owned by the effective user')
    plan=_json(fd,'plan.json',MAX_PLAN_BYTES)
    if not isinstance(plan,dict) or set(plan)!={'schema_version','kind','id','request','source_root','destination','context','tools','implementation','budget','estimate'}:
        raise ValueError('invalid job plan fields')
    if plan['schema_version']!=1 or plan['kind']!='silent-video-job-plan':raise ValueError('unsupported job plan')
    media.validate_request(plan['request']);media._structural(plan['budget'],BUDGET,'job budget')
    value={k:v for k,v in plan.items() if k!='id'}
    if plan['id']!=media._digest(value) or plan['estimate']!=_estimate(plan['request']):raise ValueError('job plan identity changed')
    for key in ('source_root','destination'):
        if not isinstance(plan[key],str) or not 0<len(plan[key])<=4096 or not Path(plan[key]).is_absolute():raise ValueError('job locations must be explicit absolute paths')
    if not isinstance(plan['context'],dict):raise ValueError('job source/timeline context must be an object')
    tool=media._obj({'path':{'type':'string','minLength':1,'maxLength':4096},'sha256':media.SHA,'bytes':media._int(1,MAX_TOOL_BYTES),'version_stdout':{'type':'string','minLength':1,'maxLength':1024**2}})
    media._structural(plan['tools'],media._obj({'ffmpeg':tool,'ffprobe':tool}),'job tools')
    media._structural(plan['implementation'],media._obj({k:media.SHA for k in ('controller','encoder','publication','profiles','path_boundary')}),'job implementation')
    return plan


def _state(fd,plan):
    state=_json(fd,'state.json')
    required={'schema_version','kind','job','phase','attempts','runs','cached_created','cached_reused','progress','error','receipt','artifact','elapsed_seconds'}
    if not isinstance(state,dict) or set(state)!=required or state.get('schema_version')!=1 or state.get('kind')!='silent-video-job-state' or state.get('job')!=plan['id']:
        raise ValueError('invalid job checkpoint identity')
    if not isinstance(state['phase'],str) or state['phase'] not in STATES:raise ValueError('invalid job checkpoint phase')
    for key,maximum in [('attempts',3),('runs',8),('cached_created',media.MAX_FRAMES),('cached_reused',media.MAX_FRAMES)]:
        if type(state[key]) is not int or not 0<=state[key]<=maximum:raise ValueError('job checkpoint counters out of bounds')
    if not isinstance(state['progress'],dict) or state['error'] is not None and (not isinstance(state['error'],str) or len(state['error'])>4096):
        raise ValueError('invalid job checkpoint diagnostic')
    if state['receipt'] is not None and not isinstance(state['receipt'],dict) or state['artifact'] is not None and not isinstance(state['artifact'],dict):
        raise ValueError('invalid job artifact checkpoint')
    exact_sha={'type':'string','pattern':'^[0-9a-f]{64}$'}
    receipt_schema=media._obj({'sha256':exact_sha,'bytes':media._int(1,MAX_RECEIPT_BYTES)})
    artifact_schema=media._obj({'sha256':exact_sha,'bytes':media._int(1,plan['request']['limits']['max_output_bytes']),
                               'path':{'const':'video'+media.PROFILES[plan['request']['profile']]['extension']}})
    if state['receipt'] is not None:media._structural(state['receipt'],receipt_schema,'job receipt checkpoint')
    if state['artifact'] is not None:media._structural(state['artifact'],artifact_schema,'job artifact checkpoint')
    if state['phase']=='completed' and (state['receipt'] is None or state['artifact'] is None):
        raise ValueError('completed job checkpoint requires immutable artifact and receipt references')
    if type(state['elapsed_seconds']) not in (int,float) or not 0<=state['elapsed_seconds']<=86400:raise ValueError('invalid job elapsed time')
    _bytes(state,MAX_STATE_BYTES)
    return state


def _save(fd,state,phase=None,**values):
    if phase is not None:state['phase']=phase
    state.update(values);_put_json(fd,'state.json',state,replace=True)


@contextmanager
def _locked(root,*,blocking=False):
    with pinned_directory(root) as fd:
        import fcntl
        plan=_plan(fd)  # Do not modify an unrelated folder by creating lock files.
        lock=os.open('lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):raise ValueError('unsafe job lock')
            try:fcntl.flock(lock,fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:raise ValueError('job is already owned') from exc
            yield fd,lock,plan
        finally:os.close(lock)


def create(request,source_root,job_root,destination,*,context=None,budget=None,ffmpeg=None,ffprobe=None):
    if not publication_supported():raise ValueError('secure job staging is unavailable on this platform')
    request=copy.deepcopy(media.validate_request(request));budget=copy.deepcopy(budget or DEFAULT_BUDGET)
    media._structural(budget,BUDGET,'job budget')
    if len(request['source']['frames'])/media.fps_fraction(request['source']['fps'])>3600:
        raise ValueError('job duration exceeds one-hour limit')
    estimate=_estimate(request)
    if (estimate['threads']>2 or estimate['peak_work_bytes']>budget['max_work_bytes'] or
            estimate['decoded_memory_estimate']>budget['max_memory_bytes']):
        raise ValueError('job exceeds declared work/thread/memory budget')
    job_root=media._destination(job_root);destination=media._destination(destination)
    context=copy.deepcopy(context or {});_bytes(context,MAX_PLAN_BYTES)
    if not isinstance(context,dict):raise ValueError('explicit source context must be an object')
    source_root=Path(source_root).absolute()
    if source_root.is_symlink() or not source_root.is_dir():raise ValueError('source root must be an existing directory')
    tools=_tools(ffmpeg,ffprobe)
    capabilities=media.capabilities(ffmpeg=tools['ffmpeg']['path'],ffprobe=tools['ffprobe']['path'])
    if capabilities.get('status')!='available' or capabilities['profiles'][request['profile']]['status']!='supported':
        raise ValueError('requested encoder/muxer profile unavailable; no fallback')
    plan={'schema_version':1,'kind':'silent-video-job-plan','request':request,'source_root':str(source_root),
          'destination':str(destination),'context':context,'tools':tools,'implementation':_implementation(),
          'budget':budget,'estimate':estimate}
    plan['id']=media._digest(plan);_bytes(plan,MAX_PLAN_BYTES)
    state={'schema_version':1,'kind':'silent-video-job-state','job':plan['id'],'phase':'queued',
           'attempts':0,'runs':0,'cached_created':0,'cached_reused':0,'progress':{},'error':None,
           'receipt':None,'artifact':None,'elapsed_seconds':0}
    with pinned_directory(job_root.parent) as parent_fd:
        parent=Path('/proc/self/fd')/str(parent_fd)
        if shutil.disk_usage(parent).free<estimate['peak_work_bytes']:raise ValueError('insufficient free job storage')
        with private_stage(parent_fd,prefix='.silent-job-create-') as (_,wrapper_fd):
            payload_fd=None
            try:
                os.mkdir('payload',mode=0o700,dir_fd=wrapper_fd)
                payload_fd=os.open('payload',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=wrapper_fd)
                for name in ('cache','attempts'):os.mkdir(name,mode=0o700,dir_fd=payload_fd)
                _write(payload_fd,'lock',b'');_put_json(payload_fd,'plan.json',plan,maximum=MAX_PLAN_BYTES);_put_json(payload_fd,'state.json',state)
                _rename_no_replace('payload',job_root.name,source_dir_fd=wrapper_fd,destination_dir_fd=parent_fd);os.fsync(parent_fd)
            finally:
                if payload_fd is not None:os.close(payload_fd)
    return {'plan':plan,'state':state,'owned':False}


def _quick_delivery(plan,state):
    with pinned_directory(plan['destination']) as delivery_fd:
        raw=_read(delivery_fd,'receipt.json',MAX_RECEIPT_BYTES)
        if state['receipt']!={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}:
            raise ValueError('completed delivery receipt changed')
        envelope=_json(delivery_fd,'receipt.json',MAX_RECEIPT_BYTES)
        name='video'+media.PROFILES[plan['request']['profile']]['extension']
        if (set(os.listdir(Path('/proc/self/fd')/str(delivery_fd)))!={name,'receipt.json'} or envelope.get('job')!=plan['id'] or
                envelope.get('plan_sha256')!=media._digest(plan) or envelope.get('artifact')!=state['artifact'] or
                state['artifact'].get('path')!=name):raise ValueError('completed delivery identity changed')
        reference=_reference(delivery_fd,name,plan['request']['limits']['max_output_bytes'])
        if reference!={k:state['artifact'][k] for k in ('sha256','bytes')}:raise ValueError('completed delivery artifact changed')


def status(job_root):
    with pinned_directory(job_root) as fd:
        import fcntl
        plan=_plan(fd);state=_state(fd,plan)
        lock=os.open('lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        owned=False
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):raise ValueError('unsafe job lock')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:owned=True
            if not owned and state['phase'] in ACTIVE:_save(fd,state,'interrupted',error='Controller/worker no longer owns checkpoint; explicit resume required')
            if state['phase']=='completed':
                try:_quick_delivery(plan,state)
                except (ValueError,OSError) as exc:
                    if not owned:_save(fd,state,'stale',error=str(exc)[:4096])
                    else:state={**state,'phase':'stale','error':str(exc)[:4096]}
        finally:os.close(lock)
        return {'plan':plan,'state':state,'owned':owned,'cancellation_requested':_exists(fd,'cancel')}


def cancel(job_root):
    with pinned_directory(job_root) as fd:
        plan=_plan(fd);state=_state(fd,plan)
        if state['phase']=='completed':return {'job':plan['id'],'cancellation_requested':False,'phase':'completed'}
        try:
            marker=os.open('cancel',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=fd)
            with os.fdopen(marker,'wb') as stream:stream.write(b'cancel\n');stream.flush();os.fsync(stream.fileno())
        except FileExistsError:_read(fd,'cancel',16)
        os.fsync(fd)
        return {'job':plan['id'],'cancellation_requested':True}


def _workspace_shape(fd):
    if not set(os.listdir(Path('/proc/self/fd')/str(fd)))<={'plan.json','state.json','lock','cache','attempts','input','cancel'}:
        raise ValueError('unowned/partial artifact in job root; retained for inspection')
    attempts_fd=os.open('attempts',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try:
        import re
        for name in os.listdir(Path('/proc/self/fd')/str(attempts_fd)):
            info=os.stat(name,dir_fd=attempts_fd,follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and re.fullmatch(r'000[1-3]',name):continue
            if stat.S_ISREG(info.st_mode) and re.fullmatch(r'(worker-[0-9]{4}\.log|cancel-run-[0-9]{4}\.txt)',name):continue
            raise ValueError('unowned attempt artifact; retained for inspection')
    finally:os.close(attempts_fd)


def _work_bytes(fd):
    total=0;count=0;root=Path('/proc/self/fd')/str(fd)
    for folder,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            path=Path(folder)/name;info=path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError('unsafe entry in job workspace')
            total+=info.st_size if stat.S_ISREG(info.st_mode) else 0;count+=1
            if count>media.MAX_FRAMES*5+128:raise ValueError('job workspace file count exceeds budget')
    return total


def _check_implementation(plan):
    if _implementation()!=plan['implementation']:raise ValueError('stale controller/encoder implementation; create a new job')
    for tool in plan['tools'].values():
        if _sha_path(tool['path'],MAX_TOOL_BYTES)!={k:tool[k] for k in ('sha256','bytes')}:
            raise ValueError('stale media executable identity; create a new job')


def _cache(fd,plan,state):
    source=plan['request']['source'];root=Path('/proc/self/fd')/str(fd);created=reused=0
    cache_fd=os.open('cache',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try:
        for frame in source['frames']:
            if _exists(fd,'cancel'):raise ValueError('job cancelled during frame preparation')
            name=f'{frame["index"]:06d}.png'
            if _exists(cache_fd,name):
                data=_read(cache_fd,name,media.MAX_FRAME_BYTES);reused+=1
            else:
                data=media._read_frame(plan['source_root'],frame['path']);created+=1
            if len(data)!=frame['bytes'] or hashlib.sha256(data).hexdigest()!=frame['sha256']:
                raise ValueError('stale source/cache frame bytes')
            image=media._image(data,(source['width'],source['height']));image.close()
            if not _exists(cache_fd,name):_write(cache_fd,name,data)
            _save(fd,state,'preparing',cached_created=created,cached_reused=reused,progress={'phase':'preparing','frame':frame['index']+1,'total_frames':len(source['frames'])})
        actual=set(os.listdir(Path('/proc/self/fd')/str(cache_fd)));expected={f'{f["index"]:06d}.png' for f in source['frames']}
        if actual!=expected:raise ValueError('unexpected frame-cache inventory')
    finally:os.close(cache_fd)
    # Keep the original source manifest unchanged. A private adapter root offers
    # its exact original relative names, without pretending this is a rerender.
    return root/'input'


def _input_view(fd,plan):
    # Caller-controlled paths are admitted by media.validate_request. Duplicate
    # aliases and traversal already fail there; build a private captured tree.
    root=Path('/proc/self/fd')/str(fd)
    if not _exists(fd,'input'):os.mkdir('input',mode=0o700,dir_fd=fd)
    input_fd=os.open('input',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try:
        for frame in plan['request']['source']['frames']:
            parts=frame['path'].split('/');current=os.dup(input_fd)
            try:
                for part in parts[:-1]:
                    try:os.mkdir(part,mode=0o700,dir_fd=current)
                    except FileExistsError:pass
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current);os.close(current);current=child
                name=parts[-1]
                data=_read(fd,f'cache/{frame["index"]:06d}.png',media.MAX_FRAME_BYTES)
                if not _exists(current,name):_write(current,name,data)
                elif _read(current,name,media.MAX_FRAME_BYTES)!=data:raise ValueError('private input mapping differs from verified cache')
            finally:os.close(current)
    finally:os.close(input_fd)
    return root/'input'


def _artifact_receipt(fd,relative,plan,input_root):
    path=Path('/proc/self/fd')/str(fd)/relative
    return media.verify_export(path,plan['request'],root=input_root,
        ffmpeg=plan['tools']['ffmpeg']['path'],ffprobe=plan['tools']['ffprobe']['path'],cancel=lambda:_exists(fd,'cancel'))


def _publish(fd,plan,state,artifact_relative,receipt):
    destination=Path(plan['destination']);media._destination(destination)
    if _work_bytes(fd)+receipt['artifact_bytes']+MAX_RECEIPT_BYTES>plan['budget']['max_work_bytes']:
        raise ValueError('insufficient work-byte budget for immutable final payload')
    with pinned_directory(destination.parent) as parent_fd:
        parent=Path('/proc/self/fd')/str(parent_fd)
        if shutil.disk_usage(parent).free<receipt['artifact_bytes']+MAX_RECEIPT_BYTES:raise ValueError('insufficient free delivery storage')
        with private_stage(parent_fd,prefix='.silent-job-publish-') as (_,wrapper_fd):
            payload_fd=None
            try:
                os.mkdir('payload',mode=0o700,dir_fd=wrapper_fd)
                payload_fd=os.open('payload',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=wrapper_fd)
                artifact_name='video'+media.PROFILES[plan['request']['profile']]['extension']
                _copy_artifact(fd,artifact_relative,payload_fd,artifact_name,plan['request']['limits']['max_output_bytes'],
                    {'sha256':receipt['artifact_sha256'],'bytes':receipt['artifact_bytes']})
                envelope={'schema_version':1,'kind':'silent-job-delivery','job':plan['id'],'plan_sha256':media._digest(plan),
                    'request':plan['request'],'context':plan['context'],'tools':plan['tools'],'media':receipt,
                    'artifact':{'path':artifact_name,'sha256':receipt['artifact_sha256'],'bytes':receipt['artifact_bytes']},
                    'visual_review':'not-performed'}
                raw=_bytes(envelope,MAX_RECEIPT_BYTES);_write(payload_fd,'receipt.json',raw)
                # No caller callbacks run while private payload is copied/verified
                # and renamed from this independently pinned private parent.
                _rename_no_replace('payload',destination.name,source_dir_fd=wrapper_fd,destination_dir_fd=parent_fd);os.fsync(parent_fd)
                _save(fd,state,'completed',receipt={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)},
                      artifact=envelope['artifact'],error=None,progress={'phase':'completed','frame':len(plan['request']['source']['frames'])})
                return envelope
            finally:
                if payload_fd is not None:os.close(payload_fd)


def _recover_delivery(fd,plan,state,input_root):
    destination=Path(plan['destination'])
    if not destination.exists():return False
    with pinned_directory(destination) as delivery_fd, _worker_boundary()(delivery_fd) as delivery_cap:
        envelope=_json(delivery_fd,'receipt.json',MAX_RECEIPT_BYTES)
        if (envelope.get('job')!=plan['id'] or envelope.get('plan_sha256')!=media._digest(plan) or
                envelope.get('request')!=plan['request'] or envelope.get('context')!=plan['context'] or envelope.get('tools')!=plan['tools']):
            raise ValueError('preexisting destination has another job owner')
        name='video'+media.PROFILES[plan['request']['profile']]['extension']
        if set(os.listdir(Path('/proc/self/fd')/str(delivery_fd)))!={name,'receipt.json'} or envelope.get('artifact',{}).get('path')!=name:
            raise ValueError('invalid preexisting delivery inventory')
        receipt=media.verify_export(delivery_cap.path/name,plan['request'],root=input_root,
            ffmpeg=plan['tools']['ffmpeg']['path'],ffprobe=plan['tools']['ffprobe']['path'],cancel=lambda:_exists(fd,'cancel'))
        if receipt['artifact_sha256']!=envelope['artifact']['sha256'] or receipt['artifact_bytes']!=envelope['artifact']['bytes']:
            raise ValueError('preexisting delivery artifact changed')
        for key in ('artifact_sha256','artifact_bytes','request_sha256','source_manifest_sha256','video_stream_count','audio_stream_count','other_stream_count','frame_count','fps','profile','duration'):
            if envelope.get('media',{}).get(key)!=receipt.get(key):raise ValueError('preexisting media receipt differs from actual measured artifact')
        raw=_read(delivery_fd,'receipt.json',MAX_RECEIPT_BYTES)
        _save(fd,state,'completed',receipt={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)},artifact=envelope['artifact'],error=None,
            progress={'phase':'completed','frame':len(plan['request']['source']['frames'])})
    return True


def _worker(fd,life_fd,lock_fd,fresh):
    if (not publication_supported() or any(type(v) is not int or v<0 for v in (fd,life_fd,lock_fd)) or
            os.getpgrp()!=os.getpid() or not stat.S_ISDIR(os.fstat(fd).st_mode) or
            not stat.S_ISFIFO(os.fstat(life_fd).st_mode) or not stat.S_ISREG(os.fstat(lock_fd).st_mode)):
        raise ValueError('worker requires inherited owned session, lifetime pipe and directory/lock descriptors')
    group=os.getpid()
    def guardian():
        try:
            while os.read(life_fd,1):pass
        finally:os.killpg(group,signal.SIGKILL)
    threading.Thread(target=guardian,daemon=True).start()
    with _worker_boundary()(fd) as authority:
        return _worker_body(authority.fd,fresh)


def _worker_body(fd,fresh):
    plan=_plan(fd);state=_state(fd,plan);started=time.monotonic()
    def progress(value):
        _bytes(value,65536);_save(fd,state,'encoding',progress=value)
    try:
        _save(fd,state,'validating',error=None)
        _check_implementation(plan)
        if _exists(fd,'cancel'):raise ValueError('job cancelled before preparation')
        _workspace_shape(fd)
        if _tools(plan['tools']['ffmpeg']['path'],plan['tools']['ffprobe']['path'])!=plan['tools']:
            raise ValueError('stale media executable version; create a new job')
        if _work_bytes(fd)>plan['budget']['max_work_bytes']:raise ValueError('job workspace exceeds work-byte budget')
        _save(fd,state,'preparing');_cache(fd,plan,state);input_root=_input_view(fd,plan)
        _save(fd,state,'verifying')
        if _recover_delivery(fd,plan,state,input_root):return 0
        extension=media.PROFILES[plan['request']['profile']]['extension']
        previous=f'attempts/{state["attempts"]:04d}/video{extension}'
        receipt=None
        if not fresh and state['attempts'] and _exists(fd,previous):
            receipt=_artifact_receipt(fd,previous,plan,input_root);artifact=previous
        else:
            if state['attempts']>=plan['budget']['max_attempts']:raise ValueError('encode attempt quota exhausted')
            state['attempts']+=1;attempt=f'attempts/{state["attempts"]:04d}'
            with _parent(fd,attempt) as (parent,name):os.mkdir(name,mode=0o700,dir_fd=parent)
            required=plan['estimate']['source_bytes']+2*plan['request']['limits']['max_output_bytes']+4*plan['request']['limits']['max_log_bytes']+MAX_RECEIPT_BYTES
            if _work_bytes(fd)+required>plan['budget']['max_work_bytes']:raise ValueError('retained attempts leave insufficient work-byte budget for whole restart')
            artifact=attempt+'/video'+extension;_save(fd,state,'encoding')
            with _parent(fd,artifact) as (attempt_fd,artifact_name):
                receipt=media.encode(plan['request'],input_root,artifact_name,destination_dir_fd=attempt_fd,
                    ffmpeg=plan['tools']['ffmpeg']['path'],ffprobe=plan['tools']['ffprobe']['path'],
                    cancel=lambda:_exists(fd,'cancel'),progress=progress)
        _save(fd,state,'verifying')
        if receipt.get('audio_stream_count')!=0 or receipt.get('video_stream_count')!=1 or receipt.get('request_sha256')!=media._digest(plan['request']):
            raise ValueError('independent media receipt differs from exact silent request')
        _put_json(fd,f'attempts/{state["attempts"]:04d}/receipt-run-{state["runs"]:04d}.json',receipt,maximum=MAX_RECEIPT_BYTES)
        _save(fd,state,'finalizing',elapsed_seconds=time.monotonic()-started)
        if _exists(fd,'cancel'):raise ValueError('job cancelled before finalization')
        _publish(fd,plan,state,artifact,receipt)
        return 0
    except BaseException as exc:
        phase='cancelled' if _exists(fd,'cancel') else ('stale' if 'stale' in str(exc) else 'failed')
        _save(fd,state,phase,error=str(exc)[:4096],elapsed_seconds=time.monotonic()-started)
        return 2


def run(job_root,*,resume=False,retry=False,progress=None):
    with _locked(job_root) as (fd,lock,plan):
        state=_state(fd,plan)
        if state['phase']=='completed':
            try:_quick_delivery(plan,state)
            except (ValueError,OSError) as exc:_save(fd,state,'stale',error=str(exc)[:4096])
            return {'plan':plan,'state':state,'owned':False}
        if state['phase'] in ACTIVE:_save(fd,state,'interrupted',error='Previous execution lost ownership')
        if retry:
            if state['phase'] not in ('failed','cancelled','interrupted','blocked'):raise ValueError('retry requires terminal unsuccessful job')
            if _exists(fd,'cancel'):
                name=f'attempts/cancel-run-{state["runs"]:04d}.txt'
                if not _exists(fd,name):_write(fd,name,_read(fd,'cancel',16))
                _unlink(fd,'cancel')
        elif resume:
            if state['phase']!='interrupted':raise ValueError('resume requires interrupted job; failed/cancelled need explicit retry')
        elif state['phase']!='queued':raise ValueError('job requires explicit resume or retry')
        if state['runs']>=plan['budget']['max_runs']:raise ValueError('job controller-run quota exhausted')
        _check_implementation(plan)
        try:_worker_boundary()
        except ValueError as exc:
            _save(fd,state,'blocked',error=str(exc)[:4096])
            return {'plan':plan,'state':state,'owned':False,'worker_exit_status':None}
        state['runs']+=1;_save(fd,state,'validating',error=None)
        read_fd,write_fd=os.pipe();process=None
        log_name=f'attempts/worker-{state["runs"]:04d}.log'
        with _parent(fd,log_name) as (parent,name):
            log_fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            with os.fdopen(log_fd,'wb') as log:
                process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'_worker','--root-fd',str(fd),'--life-fd',str(read_fd),
                    '--lock-fd',str(lock),*(['--fresh'] if retry else [])],stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    pass_fds=(fd,read_fd,lock),start_new_session=True)
            os.close(read_fd);read_fd=None;started=time.monotonic();last=None;next_disk=0
            while process.poll() is None:
                current=_state(fd,plan)
                if progress is not None and current['progress']!=last:
                    last=copy.deepcopy(current['progress']);progress(copy.deepcopy(current))
                elapsed=time.monotonic()-started
                if elapsed>plan['budget']['wall_seconds']:raise ValueError('job wall-time budget exceeded')
                if elapsed>=next_disk:
                    if _work_bytes(fd)>plan['budget']['max_work_bytes']:raise ValueError('job work-byte budget exceeded')
                    next_disk=elapsed+2
                with _parent(fd,log_name) as (parent,name):
                    if os.stat(name,dir_fd=parent,follow_symlinks=False).st_size>65536:raise ValueError('job worker log exceeds budget')
                time.sleep(.05)
            current=_state(fd,plan)
            if current['phase'] in ACTIVE:_save(fd,current,'interrupted',error='Worker exited before acknowledging terminal checkpoint')
            return {'plan':plan,'state':current,'owned':False,'worker_exit_status':process.returncode}
        except BaseException as exc:
            if process is not None and process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
            current=_state(fd,plan)
            _save(fd,current,'cancelled' if _exists(fd,'cancel') else 'interrupted',error=str(exc)[:4096])
            raise
        finally:
            os.close(write_fd)
            if read_fd is not None:os.close(read_fd)


def resume(job_root,**options):return run(job_root,resume=True,**options)
def retry(job_root,**options):return run(job_root,retry=True,**options)


def run_batch(job_roots,*,retry_failed=False,progress=None):
    if not isinstance(job_roots,list) or not 1<=len(job_roots)<=MAX_BATCH:raise ValueError('batch requires1..16 independent jobs')
    roots=[str(Path(p).absolute()) for p in job_roots]
    if len(set(roots))!=len(roots):raise ValueError('batch jobs must be distinct')
    inventories=[status(root) for root in roots]
    destinations=[x['plan']['destination'] for x in inventories]
    if len(set(destinations))!=len(destinations):raise ValueError('batch destinations must be independently owned')
    results=[]
    for root,inventory in zip(roots,inventories):
        phase=inventory['state']['phase']
        if phase=='completed':results.append(inventory);continue
        try:
            results.append(run(root,resume=phase=='interrupted',retry=retry_failed and phase in ('failed','cancelled','blocked'),progress=progress))
        except (ValueError,OSError) as exc:results.append({'job_root':root,'phase':'blocked','error':str(exc)[:4096]})
    return {'schema_version':1,'kind':'silent-video-batch-result','concurrency':1,'jobs':results}


def schemas():
    sha={'type':'string','pattern':'^[0-9a-f]{64}$'}
    location={'type':'string','minLength':1,'maxLength':4096}
    tool=media._obj({'path':location,'sha256':sha,'bytes':media._int(1,MAX_TOOL_BYTES),
        'version_stdout':{'type':'string','minLength':1,'maxLength':1024**2}})
    estimate=media._obj({'source_bytes':media._int(0,media.MAX_SOURCE_BYTES),
        'peak_work_bytes':media._int(1,32*1024**3),'decoded_memory_estimate':media._int(1,2*1024**3),
        'threads':media._int(1,2),'gpu_bytes':{'const':0}})
    plan=media._obj({'schema_version':{'const':1},'kind':{'const':'silent-video-job-plan'},'id':sha,
        'request':media.REQUEST,'source_root':location,'destination':location,'context':{'type':'object'},
        'tools':media._obj({'ffmpeg':tool,'ffprobe':tool}),
        'implementation':media._obj({k:sha for k in ('controller','encoder','publication','profiles','path_boundary')}),
        'budget':BUDGET,'estimate':estimate})
    state=media._obj({'schema_version':{'const':1},'kind':{'const':'silent-video-job-state'},'job':sha,
        'phase':{'enum':sorted(STATES)},'attempts':media._int(0,3),'runs':media._int(0,8),
        'cached_created':media._int(0,media.MAX_FRAMES),'cached_reused':media._int(0,media.MAX_FRAMES),
        'progress':{'type':'object'},'error':{'type':['string','null'],'maxLength':4096},
        'receipt':{'anyOf':[{'type':'null'},media._obj({'sha256':sha,'bytes':media._int(1,MAX_RECEIPT_BYTES)})]},
        'artifact':{'anyOf':[{'type':'null'},media._obj({'sha256':sha,'bytes':media._int(1,2*1024**3),
             'path':{'enum':sorted({'video'+value['extension'] for value in media.PROFILES.values()})}})]},
        'elapsed_seconds':{'type':'number','minimum':0,'maximum':86400}})
    return {name:{'$schema':'https://json-schema.org/draft/2020-12/schema',**copy.deepcopy(value)} for name,value in [('plan',plan),('state',state)]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['create','status','run','resume','retry','cancel','_worker'])
    parser.add_argument('--job-root',type=Path);parser.add_argument('--request',type=Path);parser.add_argument('--source-root',type=Path);parser.add_argument('--destination',type=Path)
    parser.add_argument('--root-fd',type=int);parser.add_argument('--life-fd',type=int);parser.add_argument('--lock-fd',type=int);parser.add_argument('--fresh',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='_worker':return _worker(args.root_fd,args.life_fd,args.lock_fd,args.fresh)
        if args.job_root is None:raise ValueError('--job-root required')
        if args.command=='create':
            if any(v is None for v in (args.request,args.source_root,args.destination)):raise ValueError('create requires request/source-root/destination')
            with media._open_source(args.request.parent,args.request.name) as stream:
                data=stream.read(MAX_PLAN_BYTES+1)
                if len(data)>MAX_PLAN_BYTES:raise ValueError('request exceeds byte budget')
                request=json.loads(data)
            result=create(request,args.source_root,args.job_root,args.destination)
        else:
            options={}
            if args.command in ('run','resume','retry'):
                def emit_progress(checkpoint):
                    event={'kind':'silent-job-progress','job':checkpoint['job'],
                           'phase':checkpoint['phase'],'progress':checkpoint['progress']}
                    print(json.dumps(event,allow_nan=False),file=sys.stderr,flush=True)
                options['progress']=emit_progress
            result=globals()[args.command](args.job_root,**options)
        print(json.dumps(result,indent=2,allow_nan=False));return 0
    except (ValueError,OSError) as exc:parser.exit(2,str(exc)+'\n')


if __name__=='__main__':raise SystemExit(main())
