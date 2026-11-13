"""Fixed local-engine subprocess supervisor with Linux process-group limits.

This is resource supervision for trusted application engines, not a sandbox for
arbitrary uploaded code. Other platforms fail before execution until equivalent
owned-tree and resource mechanisms are implemented and tested.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

# Isolated Python (-I) omits implicit script-directory imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig_errors import (RigError, atomic_json, job_lock, object_hash, read_json,
                        require, safe_path, timestamp)

DEFAULT_LIMITS = dict(runtime_seconds=120., cpu_seconds=60, memory_bytes=1024**3,
                      file_bytes=256*1024**2, threads=1, log_bytes=65536)
_SUPERVISORS={}


def limits(value=None):
    value = {} if value is None else value
    require(isinstance(value, dict) and set(value) <= set(DEFAULT_LIMITS),
            'MODEL3D_WORKER_LIMIT', 'Unknown worker limit; GPU and aggregate-tree memory/disk quotas are unsupported.')
    result = {**DEFAULT_LIMITS, **value}
    for key, maximum in dict(runtime_seconds=86400., cpu_seconds=86400,
                             memory_bytes=16*1024**3, file_bytes=4*1024**3,
                             threads=2, log_bytes=1024**2).items():
        item = result[key]
        require(type(item) in ((int,float) if key == 'runtime_seconds' else (int,))
                and math.isfinite(item) and 0 < item <= maximum,
                'MODEL3D_WORKER_LIMIT', 'Invalid worker limit: '+key)
    require(result['memory_bytes'] >= 64*1024**2, 'MODEL3D_WORKER_LIMIT', 'Worker address-space limit must be at least 64MiB.')
    require(result['file_bytes'] >= 65536, 'MODEL3D_WORKER_LIMIT', 'Per-file limit must allow at least 64KiB of supervisor metadata.')
    return result


def capabilities():
    return dict(schema_version=1, supported=sys.platform.startswith('linux'),
                hard=['owned-process-group termination', 'per-process RLIMIT_AS',
                      'per-process RLIMIT_CPU', 'per-file RLIMIT_FSIZE', 'inherited CPU affinity'],
                monitored=['wall runtime', 'stdout/stderr byte ceiling'],
                unavailable=['GPU quota', 'aggregate process-tree RAM/disk quota',
                             'Windows/macOS hard worker supervision', 'arbitrary-code sandbox'])


def engine_api():
    # Both imports are literal application modules, never decoded request values.
    try:
        import model3d_job_engines as api
        return api.dispatch, api.handler_version()
    except ModuleNotFoundError as exc:
        if exc.name != 'model3d_job_engines': raise
        from model3d_job_cli import native_primitive, handler_version
        return native_primitive, handler_version()


def _request(root, expected_hash=None):
    value = read_json(safe_path(root, 'request.json', exists=True))
    fields = {'schema_version','kind','token','job_root','limits','handler_version','retry','diagnostic'}
    require(isinstance(value,dict) and set(value)==fields and type(value['schema_version']) is int
            and value['schema_version']==1 and value['kind']=='model3d-owned-worker'
            and isinstance(value['token'],str) and len(value['token'])==32
            and all(c in '0123456789abcdef' for c in value['token'])
            and isinstance(value['job_root'],str) and Path(value['job_root']).is_absolute()
            and isinstance(value['handler_version'],str) and 0<len(value['handler_version'])<=128
            and type(value['retry']) is bool and value['limits']==limits(value['limits'])
            and value['diagnostic'] in (None,'sleep','memory','cpu','tree','file'),
            'MODEL3D_WORKER_SCHEMA', 'Invalid fixed-worker request.')
    require(expected_hash is None or object_hash(value)==expected_hash,
            'MODEL3D_WORKER_CHANGED','Fixed worker request changed after launch authorization.')
    return value


def launch(job_root, root, *, resource_limits=None, retry=False, diagnostic=None):
    """Launch only this fixed supervisor; return its durable request identity."""
    require(capabilities()['supported'], 'MODEL3D_WORKER_PLATFORM',
            'Hard local worker supervision currently requires Linux; no unsupervised fallback was launched.')
    from model3d_jobs import status
    job_root=Path(job_root).absolute(); status(job_root)
    root=Path(root).absolute()
    require(not root.exists() and not root.is_symlink(), 'MODEL3D_WORKER_EXISTS', 'Choose a new worker directory.')
    require(diagnostic in (None,'sleep','memory','cpu','tree','file'), 'MODEL3D_WORKER_DIAGNOSTIC', 'Unknown fixed diagnostic.')
    require(type(retry) is bool, 'MODEL3D_WORKER_SCHEMA', 'Retry must be explicit boolean.')
    _, version=engine_api(); token=uuid.uuid4().hex
    value=dict(schema_version=1,kind='model3d-owned-worker',token=token,job_root=str(job_root),
               limits=limits(resource_limits),handler_version=version,retry=retry,diagnostic=diagnostic)
    root.mkdir(parents=True); atomic_json(root/'request.json',value)
    atomic_json(root/'state.json',dict(schema_version=1,token=token,status='queued',created_at=timestamp()))
    environment=dict(os.environ)
    for name in ('PYTHONPATH','PYTHONHOME'):environment.pop(name,None)
    for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        environment[name]=str(value['limits']['threads'])
    process=subprocess.Popen([sys.executable,'-I',str(Path(__file__).resolve()),'guard','--root',str(root),
                              '--request-hash',object_hash(value)],
                cwd=str(Path(__file__).resolve().parents[1]),env=environment,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                start_new_session=True,close_fds=True)
    _SUPERVISORS[str(root)]=process
    # Parent owns this process handle only for launch; the durable supervisor
    # continues independently if its batch scheduler exits.
    return dict(root=str(root),token=token,pid=process.pid,request_sha256=object_hash(value),
                supervisor_identity=_identity(process.pid),launched_epoch=time.time())


def state(root):
    root=Path(root).absolute(); request=_request(root)
    _reap(root)
    result=read_json(safe_path(root,'state.json',exists=True))
    require(isinstance(result,dict) and result.get('schema_version')==1
            and result.get('token')==request['token'] and result.get('status') in
            ('queued','running','completed','failed','cancelled','timed-out','resource-exhausted'),
            'MODEL3D_WORKER_SCHEMA','Invalid worker state identity.')
    return result


def cancel(root):
    root=Path(root).absolute(); request=_request(root)
    atomic_json(root/'cancel.json',dict(schema_version=1,token=request['token'],at=timestamp()))
    return dict(cancellation_requested=True,token=request['token'])


def alive(root):
    """Check the supervisor's cooperative OS lock, never infer ownership from PID."""
    _reap(Path(root).absolute())
    try:
        with job_lock(Path(root).absolute()):return False
    except RigError as exc:
        if exc.report['code']=='RIG_BUSY':return True
        raise


def _reap(root):
    process=_SUPERVISORS.get(str(root))
    if process is not None and process.poll() is not None:_SUPERVISORS.pop(str(root),None)


def _identity(pid):
    try:
        text=(Path('/proc')/str(pid)/'stat').read_text()
        fields=text[text.rfind(')')+2:].split()
        return dict(pid=pid,start=fields[19],group=int(fields[2]))
    except (FileNotFoundError,ProcessLookupError):return None


def owned_identity(identity, root, mode, request_sha256=None):
    """Birth identity plus the literal fixed executable/mode/root command."""
    if not isinstance(identity,dict) or set(identity)!={'pid','start','group'}:
        return False
    if type(identity['pid']) is not int or identity['pid']<=0 or _identity(identity['pid'])!=identity:
        return False
    try:
        with (Path('/proc')/str(identity['pid'])/'cmdline').open('rb') as stream:
            raw=stream.read(65537)
        if len(raw)>65536:return False
        actual=[os.fsdecode(item) for item in raw.rstrip(b'\0').split(b'\0')]
        prefix=[sys.executable,'-I',str(Path(__file__).resolve()),mode,'--root',str(Path(root).absolute()),'--request-hash']
        if len(actual)!=8 or actual[:7]!=prefix:return False
        digest=request_sha256 or object_hash(_request(Path(root).absolute()))
        expected=prefix+[digest]
        return actual==expected
    except (FileNotFoundError,ProcessLookupError):return False


def verify_request(root, *, job_root, token=None, request_sha256=None):
    value=_request(Path(root).absolute())
    require(Path(value['job_root']).resolve()==Path(job_root).resolve()
            and (token is None or value['token']==token)
            and (request_sha256 is None or object_hash(value)==request_sha256),
            'MODEL3D_WORKER_OWNERSHIP','Worker request does not belong to this item/root/token.')
    return value


def _descendants(identity):
    known={}; pending=[identity['pid']]
    while pending:
        pid=pending.pop()
        if pid in known:continue
        actual=_identity(pid)
        if actual is None or actual['group']!=identity['group']:continue
        require(len(known)<512,'MODEL3D_WORKER_TREE','Owned worker tree exceeds its process budget.')
        known[pid]=actual
        try:children=(Path('/proc')/str(pid)/'task'/str(pid)/'children').read_text()
        except (FileNotFoundError,ProcessLookupError):continue
        require(len(children)<=65536,'MODEL3D_WORKER_TREE','Owned child inventory exceeds its byte budget.')
        pending.extend(int(value) for value in children.split())
    return known


def _stop(process, identity, root, known=None, request_sha256=None):
    """Signal only a process group with a live, birth-checked owned member."""
    known={} if known is None else dict(known)
    if identity and identity['group']==process.pid and owned_identity(identity,root,'execute',request_sha256):
        known.update(_descendants(identity))
    def owned():return any(_identity(pid)==record for pid,record in known.items())
    if owned():
        try:os.killpg(identity['group'],signal.SIGTERM)
        except ProcessLookupError:pass
    try:process.wait(timeout=.25)
    except subprocess.TimeoutExpired:pass
    if owned():
        try:os.killpg(identity['group'],signal.SIGKILL)
        except ProcessLookupError:pass
    try:process.wait(timeout=2.)
    except subprocess.TimeoutExpired:
        raise RigError('MODEL3D_WORKER_OWNERSHIP','Worker could not be terminated with verified process-group ownership.')


def _apply(value):
    import resource
    bound=value['limits']
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_AS,(bound['memory_bytes'],bound['memory_bytes']))
    resource.setrlimit(resource.RLIMIT_CPU,(bound['cpu_seconds'],bound['cpu_seconds']+1))
    resource.setrlimit(resource.RLIMIT_FSIZE,(bound['file_bytes'],bound['file_bytes']))
    allowed=sorted(os.sched_getaffinity(0));os.sched_setaffinity(0,allowed[:bound['threads']])
    return dict(address_space_bytes=bound['memory_bytes'],cpu_seconds_per_process=bound['cpu_seconds'],
                file_bytes_per_file=bound['file_bytes'],cpu_affinity=sorted(os.sched_getaffinity(0)),
                gpu_quota=None,aggregate_tree_memory_quota=None,aggregate_tree_disk_quota=None)


def _execute(root,expected_hash):
    import resource
    from model3d_jobs import run_job
    value=_request(root,expected_hash);enforced=_apply(value)
    atomic_json(root/'enforced.json',enforced)
    dispatch,version=engine_api()
    require(version==value['handler_version'],'MODEL3D_WORKER_CODE','Engine code changed since worker launch.')
    diagnostic=value['diagnostic']
    if diagnostic=='sleep':time.sleep(30.)
    elif diagnostic=='cpu':
        while True:pass
    elif diagnostic=='memory':
        allocation=bytearray(value['limits']['memory_bytes']*2)
        require(not allocation,'MODEL3D_WORKER_DIAGNOSTIC','Unreachable memory diagnostic.')
    elif diagnostic=='tree':
        child=subprocess.Popen([sys.executable,'-I',str(Path(__file__).resolve()),'sleep-child','--root',str(root)],
                               stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        atomic_json(root/'diagnostic-child.json',dict(pid=child.pid))
        time.sleep(30.)
    elif diagnostic=='file':
        with (root/'diagnostic-large.bin').open('wb') as stream:
            stream.write(b'x'*(value['limits']['file_bytes']+65536));stream.flush()
    result=run_job(value['job_root'],dispatch,handler_version=version,retry=value['retry'])
    usage=resource.getrusage(resource.RUSAGE_SELF)
    atomic_json(root/'result.json',dict(schema_version=1,token=value['token'],status=result['status'],
                attempts=result['attempts'],current_revision=result['current_revision'],
                cpu_seconds=usage.ru_utime+usage.ru_stime,max_rss_bytes=usage.ru_maxrss*1024))


def _guard(root,expected_hash):
    import resource
    value=_request(root,expected_hash)
    with job_lock(root):
        atomic_json(root/'state.json',dict(schema_version=1,token=value['token'],status='running',
                    supervisor_identity=_identity(os.getpid()),started_at=timestamp()))
        before=resource.getrusage(resource.RUSAGE_CHILDREN)
        process=subprocess.Popen([sys.executable,'-I',str(Path(__file__).resolve()),'execute','--root',str(root),
                                  '--request-hash',expected_hash],
                    cwd=str(Path(__file__).resolve().parents[1]),stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,close_fds=True)
        identity=_identity(process.pid); started=time.monotonic(); reason=None
        running=dict(schema_version=1,token=value['token'],status='running',pid=process.pid,
                     started_at=timestamp(),started_epoch=time.time(),limits=value['limits'],process_identity=identity,
                     supervisor_identity=_identity(os.getpid()))
        atomic_json(root/'state.json',running)
        selector=selectors.DefaultSelector();captured={'stdout':0,'stderr':0}
        known={};leader_finished=None
        for name,stream in [('stdout',process.stdout),('stderr',process.stderr)]:
            os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ,name)
        logs={name:(root/(name+'.log')).open('wb') for name in captured}
        try:
            while process.poll() is None or selector.get_map():
                if identity and owned_identity(identity,root,'execute',expected_hash):known.update(_descendants(identity))
                if process.poll() is None and reason is None:
                    if (root/'cancel.json').exists():reason='cancelled'
                    elif time.monotonic()-started>value['limits']['runtime_seconds']:reason='timed-out'
                    if reason:_stop(process,identity,root,known,expected_hash)
                for key,_ in selector.select(timeout=.025):
                    chunk=os.read(key.fileobj.fileno(),65536)
                    if not chunk:selector.unregister(key.fileobj);key.fileobj.close();continue
                    name=key.data;remaining=value['limits']['log_bytes']-captured[name]
                    logs[name].write(chunk[:remaining]);captured[name]+=min(len(chunk),remaining)
                    if len(chunk)>remaining and reason is None:
                        reason='resource-exhausted';_stop(process,identity,root,known,expected_hash)
                # A descendant retaining a pipe after leader completion must not
                # keep the supervisor alive beyond the same owned-tree deadline.
                if process.poll() is not None and leader_finished is None:leader_finished=time.monotonic()
                if leader_finished is not None and selector.get_map() and time.monotonic()-leader_finished>.5:
                    reason=reason or 'resource-exhausted';_stop(process,identity,root,known,expected_hash)
                    for key in list(selector.get_map().values()):selector.unregister(key.fileobj);key.fileobj.close()
            code=process.wait();after=resource.getrusage(resource.RUSAGE_CHILDREN)
            result=read_json(root/'result.json') if (root/'result.json').exists() else None
            error=read_json(root/'error.json') if (root/'error.json').exists() else None
            succeeded=code==0 and isinstance(result,dict) and result.get('token')==value['token'] and result.get('status')=='completed'
            exhausted=code in (-signal.SIGXCPU,-signal.SIGXFSZ,-signal.SIGKILL) or (isinstance(error,dict) and error.get('code') in ('MODEL3D_WORKER_MEMORY','MODEL3D_WORKER_FILE'))
            final=dict(running,status=reason or ('completed' if succeeded else 'resource-exhausted' if exhausted else 'failed'),
                       finished_at=timestamp(),finished_epoch=time.time(),wall_seconds=time.monotonic()-started,returncode=code,
                       stdout_bytes=captured['stdout'],stderr_bytes=captured['stderr'],
                       waited_cpu_seconds=(after.ru_utime+after.ru_stime)-(before.ru_utime+before.ru_stime),
                       reported_child_max_rss_bytes=after.ru_maxrss*1024,
                       enforced=read_json(root/'enforced.json') if (root/'enforced.json').exists() else None,
                       result=result,error=error)
            atomic_json(root/'state.json',final)
        finally:
            if process.poll() is None:_stop(process,identity,root,known,expected_hash)
            selector.close()
            for stream in logs.values():stream.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['guard','execute','sleep-child']);parser.add_argument('--root',required=True)
    parser.add_argument('--request-hash')
    args=parser.parse_args();root=Path(args.root).absolute()
    try:
        require(capabilities()['supported'],'MODEL3D_WORKER_PLATFORM','Linux hard-worker primitives are required.')
        if args.mode!='sleep-child':
            require(isinstance(args.request_hash,str) and len(args.request_hash)==64
                    and all(c in '0123456789abcdef' for c in args.request_hash),
                    'MODEL3D_WORKER_SCHEMA','A pinned authorized request hash is required.')
        if args.mode=='guard':_guard(root,args.request_hash)
        elif args.mode=='execute':_execute(root,args.request_hash)
        else:
            signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30.)
        return 0
    except Exception as exc:
        import errno
        code='MODEL3D_WORKER_MEMORY' if isinstance(exc,MemoryError) else 'MODEL3D_WORKER_FILE' if isinstance(exc,OSError) and exc.errno==errno.EFBIG else 'MODEL3D_WORKER_EXECUTION'
        error=exc.report if isinstance(exc,RigError) else dict(code=code,message=str(exc)[:2048])
        if args.mode=='execute':atomic_json(root/'error.json',error)
        else:
            try:value=_request(root)
            except (RigError,OSError):value=None
            if value is not None:atomic_json(root/'state.json',dict(schema_version=1,token=value['token'],status='failed',error=error,finished_at=timestamp()))
        print(json.dumps(error),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
