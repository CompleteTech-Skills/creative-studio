"""Persistent bounded scheduling over immutable native Model3D job checkpoints."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from atomic_publish import _rename_no_replace
from rig_errors import (RigError, atomic_json, job_lock, object_hash, read_json,
                        require, safe_path, timestamp)
from model3d_jobs import create_job, status as job_status
import model3d_job_worker as worker

TERMINAL={'completed','failed','cancelled','timed-out','resource-exhausted','interrupted'}


def _id(value):
    require(isinstance(value,str) and 1<=len(value)<=64 and value[0].isalpha()
            and value.isascii() and all(c.islower() or c.isdigit() or c in '_-' for c in value),
            'MODEL3D_BATCH_ID','Item IDs must be portable lowercase ASCII identifiers.')
    return value


def _root(root):
    root=Path(root).absolute()
    require(not root.is_symlink(),'MODEL3D_BATCH_PATH','Batch root cannot be a symbolic link.')
    return root


def create_batch(root, items, *, concurrency=1, resource_limits=None):
    """Freeze requests and source snapshots into a new portable batch directory."""
    root=_root(root)
    require(isinstance(items,list) and 0<len(items)<=128,'MODEL3D_BATCH_LIMIT','Choose one to128 batch items.')
    require(type(concurrency) is int and 1<=concurrency<=4,'MODEL3D_BATCH_LIMIT','CPU concurrency must be one to four.')
    bounds=worker.limits(resource_limits)
    names=[]
    for item in items:
        require(isinstance(item,dict) and {'id','request'}<=set(item)
                and set(item)<={'id','request','inputs','budgets'},'MODEL3D_BATCH_SCHEMA','Invalid batch item.')
        names.append(_id(item['id']))
    require(len(set(names))==len(names),'MODEL3D_BATCH_ID','Duplicate batch item IDs.')
    with job_lock(root):
        require(not root.exists() and not root.is_symlink(),'MODEL3D_BATCH_EXISTS','Choose a new batch destination.')
        with tempfile.TemporaryDirectory(prefix='.model-batch-',dir=root.parent) as temp:
            stage=Path(temp)/'batch';stage.mkdir();entries=[];states={}
            for item in items:
                name=item['id'];job=create_job(stage/'jobs'/name,item['request'],inputs=item.get('inputs'),budgets=item.get('budgets'))
                entries.append(dict(id=name,job_id=job['id'],request_sha256=job['request_sha256'],
                                    inputs_sha256=object_hash(job['inputs']),budgets=job['budgets']))
                states[name]=dict(status='queued',workers=[],active_worker=None,error=None,checkpoint=None)
            request=dict(schema_version=1,kind='model3d-local-batch',id=uuid.uuid4().hex,
                         concurrency=concurrency,resource_limits=bounds,items=entries)
            atomic_json(stage/'batch-request.json',request)
            atomic_json(stage/'batch.json',dict(schema_version=1,kind='model3d-batch-journal',
                        request_sha256=object_hash(request),status='queued',items=states,
                        created_at=timestamp(),updated_at=timestamp()))
            _load(stage);_rename_no_replace(stage,root)
    return status(root)


def _load(root):
    root=_root(root);request=read_json(safe_path(root,'batch-request.json',exists=True))
    require(isinstance(request,dict) and set(request)=={'schema_version','kind','id','concurrency','resource_limits','items'}
            and type(request['schema_version']) is int and request['schema_version']==1
            and request['kind']=='model3d-local-batch' and type(request['concurrency']) is int
            and 1<=request['concurrency']<=4 and isinstance(request['items'],list) and 0<len(request['items'])<=128,
            'MODEL3D_BATCH_SCHEMA','Unsupported immutable batch request.')
    require(worker.limits(request['resource_limits'])==request['resource_limits'],'MODEL3D_BATCH_SCHEMA','Incomplete worker limits.')
    names=[]
    for item in request['items']:
        require(isinstance(item,dict) and set(item)=={'id','job_id','request_sha256','inputs_sha256','budgets'},
                'MODEL3D_BATCH_SCHEMA','Malformed immutable item identity.')
        names.append(_id(item['id']))
    require(len(set(names))==len(names),'MODEL3D_BATCH_SCHEMA','Duplicate immutable item IDs.')
    value=read_json(safe_path(root,'batch.json',exists=True))
    require(isinstance(value,dict) and set(value)=={'schema_version','kind','request_sha256','status','items','created_at','updated_at'}
            and type(value['schema_version']) is int and value['schema_version']==1
            and value['kind']=='model3d-batch-journal' and value['request_sha256']==object_hash(request)
            and isinstance(value['items'],dict) and set(value['items'])==set(names)
            and value['status'] in ('queued','running','completed','finished-with-errors','cancelled'),
            'MODEL3D_BATCH_SCHEMA','Batch journal differs from its immutable request.')
    for name,item in value['items'].items():
        require(isinstance(item,dict) and set(item)=={'status','workers','active_worker','error','checkpoint'}
                and isinstance(item['status'],str) and item['status'] in TERMINAL|{'queued','running'} and isinstance(item['workers'],list)
                and len(item['workers'])<=100 and (item['active_worker'] is None or isinstance(item['active_worker'],dict)),
                'MODEL3D_BATCH_SCHEMA','Malformed item scheduling record.')
        if item['active_worker'] is not None:
            owned=item['active_worker'];relative=owned.get('relative_root')
            require(isinstance(relative,str) and relative.startswith('workers/'+name+'-')
                    and len(Path(relative).parts)==2 and type(owned.get('launched_epoch')) in (int,float),
                    'MODEL3D_BATCH_SCHEMA','Active worker must remain in its item namespace.')
    return request,value


def _save(root,value):
    value['updated_at']=timestamp();atomic_json(Path(root)/'batch.json',value)


def _checkpoint(root,entry,version):
    job=job_status(root/'jobs'/entry['id'])
    require(job['id']==entry['job_id'] and job['request_sha256']==entry['request_sha256']
            and object_hash(job['inputs'])==entry['inputs_sha256'] and job['budgets']==entry['budgets'],
            'MODEL3D_BATCH_INPUT','Native job request, identity or source hashes differ from the immutable batch.')
    receipt=None
    if job['current_revision'] is not None:
        receipt=read_json(safe_path(root/'jobs'/entry['id'],job['current_revision']+'/receipt.json',exists=True))
        require(receipt['handler_version']==version,'MODEL3D_BATCH_CODE','Checkpoint engine signature changed; explicit new batch/revision is required.')
    return dict(status=job['status'],attempts=job['attempts'],current_revision=job['current_revision'],
                request_sha256=job['request_sha256'],inputs_sha256=object_hash(job['inputs']),
                output_files=[] if receipt is None else receipt['files'],
                output_inventory_sha256=None if receipt is None else object_hash(receipt['files']))


def _checked(root,entry,version,item):
    try:return _checkpoint(root,entry,version)
    except (RigError,OSError) as exc:
        item['status']='failed';item['error']=exc.report if isinstance(exc,RigError) else dict(code='MODEL3D_BATCH_IO',message=str(exc))
        return None


def cancel_batch(root):
    root=_root(root);request,_=_load(root)
    atomic_json(root/'cancel.json',dict(schema_version=1,batch_id=request['id'],at=timestamp()))
    return dict(batch_id=request['id'],cancellation_requested=True)


def status(root):
    root=_root(root);request,value=_load(root)
    return dict(request=deepcopy(request),journal=deepcopy(value))


def run_batch(root, *, retry_failed=False, poll_seconds=.05):
    """Resume the durable schedule; existing owned supervisors survive this caller."""
    root=_root(root)
    require(worker.capabilities()['supported'],'MODEL3D_WORKER_PLATFORM','Linux hard workers are required; no unsupervised fallback was launched.')
    require(type(retry_failed) is bool and type(poll_seconds) in (float,int) and .01<=poll_seconds<=1.,
            'MODEL3D_BATCH_PARAMETER','Invalid scheduler options.')
    _,version=worker.engine_api()
    with job_lock(root):
        request,value=_load(root)
        if retry_failed:
            require(not (root/'cancel.json').exists(),'MODEL3D_BATCH_CANCELLED','Create an explicit new batch after cancellation.')
            for item in value['items'].values():
                if item['status'] in TERMINAL-{'completed'} and item['active_worker'] is None:item['status']='queued'
        value['status']='running';_save(root,value)
        while True:
            cancelled=(root/'cancel.json').exists();active=0
            for entry in request['items']:
                name=entry['id'];item=value['items'][name];owned=item['active_worker']
                if owned is not None:
                    folder=safe_path(root,owned['relative_root'])
                    if not (folder/'state.json').exists():
                        # launch() writes state before creating a subprocess.
                        # A missing state under the single scheduler lock proves
                        # this reservation was never launched.
                        item['active_worker']=None;item['status']='queued';continue
                    worker.verify_request(folder,job_root=root/'jobs'/name,
                                          token=owned.get('token'),request_sha256=owned.get('request_sha256'))
                    if cancelled:worker.cancel(folder)
                    record=worker.state(folder)
                    identity=owned.get('supervisor_identity') or record.get('supervisor_identity')
                    live=worker.alive(folder) or worker.owned_identity(identity,folder,'guard',owned.get('request_sha256'))
                    if live:
                        active+=1;item['status']='running';continue
                    if record['status']=='queued':
                        # Scheduler exit after Popen but before its identity
                        # write is ambiguous until the fixed supervisor starts.
                        # Never replay such work based only on a missing PID.
                        if time.time()-owned['launched_epoch']<30.:
                            active+=1;continue
                        item['status']='interrupted';item['error']=dict(code='MODEL3D_BATCH_START_UNKNOWN',
                            message='Worker startup ownership is unknown; no duplicate was launched. Inspect this reservation before retrying.')
                        continue
                    # The supervisor may have just published terminal state before
                    # releasing its lock. Reload after proving it is no longer live.
                    record=worker.state(folder)
                    checkpoint=_checked(root,entry,version,item);item['checkpoint']=checkpoint
                    item['active_worker']=None
                    item['workers'][-1]['outcome']=record
                    if checkpoint is None:continue
                    item['status']='completed' if checkpoint['status']=='completed' and record['status']=='completed' else record['status'] if record['status'] in TERMINAL-{'completed'} else 'interrupted'
                    item['error']=None if item['status']=='completed' else dict(code='MODEL3D_BATCH_WORKER',worker_status=record['status'],returncode=record.get('returncode'))
                elif item['status']=='completed':
                    checkpoint=_checked(root,entry,version,item)
                    if checkpoint is None:continue
                    if checkpoint['status']!='completed':
                        item['status']='failed';item['error']=dict(code='MODEL3D_BATCH_CHECKPOINT',message='Completed item lost its native checkpoint state.');continue
                    item['checkpoint']=checkpoint
                elif cancelled and item['status']=='queued':item['status']='cancelled'
            for entry in request['items']:
                if active>=request['concurrency']:break
                name=entry['id'];item=value['items'][name]
                if cancelled or item['status']!='queued':continue
                checkpoint=_checked(root,entry,version,item);item['checkpoint']=checkpoint
                if checkpoint is None:continue
                if checkpoint['status']=='completed':item['status']='completed';continue
                if len(item['workers'])>=min(100,entry['budgets']['attempts']+2):
                    item['status']='failed';item['error']=dict(code='MODEL3D_BATCH_ATTEMPTS',message='Worker launch budget is exhausted.');continue
                relative=f'workers/{name}-{len(item["workers"])+1:03d}-{uuid.uuid4().hex}'
                # Persist the reserved directory before launching. If this caller
                # exits before launch, resume sees no active worker and safely
                # abandons only the unused reservation.
                folder=safe_path(root,relative)
                reserved=dict(relative_root=relative,supervisor_identity=None,launched_epoch=time.time())
                item['workers'].append(deepcopy(reserved));item['active_worker']=reserved;item['status']='running'
                _save(root,value)
                try:launch=worker.launch(root/'jobs'/name,folder,resource_limits=request['resource_limits'],
                                         retry=retry_failed or checkpoint['status'] not in ('queued','running'))
                except (RigError,OSError) as exc:
                    item['status']='failed';item['error']=exc.report if isinstance(exc,RigError) else dict(code='MODEL3D_BATCH_LAUNCH',message=str(exc))
                    if not (folder/'state.json').exists():item['active_worker']=None
                    _save(root,value);continue
                owned={key:value_ for key,value_ in launch.items() if key!='root'}
                owned['relative_root']=relative
                item['workers'][-1]=deepcopy(owned);item['active_worker']=owned;item['status']='running';active+=1
                _save(root,value)
            _save(root,value)
            if active==0 and all(item['status'] in TERMINAL for item in value['items'].values()):
                value['status']='cancelled' if cancelled else 'completed' if all(item['status']=='completed' for item in value['items'].values()) else 'finished-with-errors'
                _save(root,value);return status(root)
            time.sleep(poll_seconds)


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    create=commands.add_parser('create');create.add_argument('--items',required=True);create.add_argument('--root',required=True)
    create.add_argument('--concurrency',type=int,default=1);create.add_argument('--limits')
    for name in ('run','status','cancel'):
        command=commands.add_parser(name);command.add_argument('--root',required=True)
        if name=='run':command.add_argument('--retry-failed',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='create':result=create_batch(args.root,read_json(args.items),concurrency=args.concurrency,resource_limits=read_json(args.limits) if args.limits else None)
        elif args.command=='run':result=run_batch(args.root,retry_failed=args.retry_failed)
        elif args.command=='cancel':result=cancel_batch(args.root)
        else:result=status(args.root)
        print(json.dumps(result,indent=2,allow_nan=False));return 0
    except (RigError,OSError,ValueError) as exc:
        error=exc.report if isinstance(exc,RigError) else dict(code='MODEL3D_BATCH_IO',message=str(exc))
        print(json.dumps(dict(ok=False,error=error)),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
