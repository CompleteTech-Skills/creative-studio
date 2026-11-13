"""Director adapter for existing Hatch raster acquisition records.

This adapter never calls a provider, creates a new controller, or treats raster
acquisition as a 3D generation backend. A native 3D acquisition owner is required
before 3D generation can be admitted.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time

import hatch
from asset_library import detect_format, read_source_bytes
from atomic_publish import _rename_no_replace
from rig_errors import RigError


@contextmanager
def _ownership(root):
    # Native ownership uses a nonblocking kernel lock. A short bounded wait lets
    # concurrent duplicate requests observe the first claim after it commits.
    deadline=time.monotonic()+2.0
    while True:
        lock=hatch.ownership(root)
        try:
            lock.__enter__()
        except ValueError as exc:
            if str(exc) != 'run is owned by another controller' or time.monotonic() >= deadline:
                raise
            time.sleep(.01)
        else: break
    try: yield
    finally: lock.__exit__(None,None,None)


class NativeAcquisitionGateway:
    """Operate one existing run using its native lock, tokens and provenance.

    Persist the returned preparation binding with the Director project and pass
    it on reopening. It pins run identity, fixture status and the original request;
    no additional mutable acquisition state is introduced.
    """
    def __init__(self, root, *, asset_kind, binding=None):
        if asset_kind != 'raster':
            raise RigError('DIRECTOR_ACQUISITION_UNAVAILABLE',
                           'The installed native acquisition owner supports raster artwork only; '
                           'a configured native 3D acquisition owner is unavailable.',stage='director')
        self.root=Path(root).absolute()
        for path in [self.root,*self.root.parents]:
            info=path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RigError('DIRECTOR_ACQUISITION_PATH','Native run paths cannot contain symlinks.',stage='director')
        if not self.root.is_dir():
            raise RigError('DIRECTOR_ACQUISITION_PATH','An existing native run is required.',stage='director')
        with _ownership(self.root):
            data=hatch.manifest(self.root)
            prepared=dict(schema_version=1,kind='director-native-raster-binding',
                          binding_id=data['run_id'],run_id=data['run_id'],
                          request_sha256=hatch.digest(self.root/'pet_request.json'),
                          fixture=data['fixture_mode'],asset_kind='raster')
            if binding is not None and binding != prepared:
                raise RigError('DIRECTOR_ACQUISITION_BINDING','Native run identity or original request changed.',stage='director')
            self.binding=prepared

    @contextmanager
    def _locked(self):
        with _ownership(self.root):
            data=hatch.manifest(self.root)
            if (data['run_id'] != self.binding['run_id'] or
                data['fixture_mode'] != self.binding['fixture'] or
                hatch.digest(self.root/'pet_request.json') != self.binding['request_sha256']):
                raise RigError('DIRECTOR_ACQUISITION_BINDING','Bound native identity/request changed.',stage='director')
            yield data

    def _result(self, data, name=None, **extra):
        job=hatch.job_for(data,name) if name is not None else None
        running=job is not None and job['status']=='running'
        path=self.root/'imagegen-jobs.json'
        native=dict(artifact_id=data['run_id'],kind='hatch-raster-acquisition',schema_version=hatch.SCHEMA,
                    semantic_version='1.0.0',path='imagegen-jobs.json',sha256=hatch.digest(path),
                    byte_size=path.stat().st_size,media_type='application/json',provenance_ref=None,
                    fixture=data['fixture_mode'],dependencies=[])
        return dict(binding=deepcopy(self.binding),native_binding=native,
                    claim_id=job.get('token') if job else None,
                    native_state=job['status'] if job else 'prepared',
                    outcome='uncertain' if running else 'known',provider_request_id=None,
                    provider_called=False,provider_retry_safety='unavailable; native token replay only',
                    native_manifest=deepcopy(data),
                    stale=bool(job and job.get('stale')),history=deepcopy(job['history']) if job else [],
                    **extra)

    def prepare(self, job=None, *, _check=None):
        """Bind an existing native run; repeated preparation has no side effects."""
        with self._locked() as data:
            if _check is not None: _check(data)
            return self._result(data,job)

    def claim(self, job, *, _check=None):
        """Return the existing active token, or delegate one native claim."""
        with self._locked() as data:
            if _check is not None: _check(data)
            hatch.reconcile(self.root,data)
            selected=hatch.job_for(data,job)
            if selected['status']=='running':
                return self._result(data,job,reused_active_claim=True,handoff=None)
            handoff=hatch.claim(self.root,data,job)
            return self._result(data,job,reused_active_claim=False,handoff=handoff)

    def reconcile(self, job, *, _check=None):
        """Reconcile input/output freshness without releasing an uncertain token."""
        with self._locked() as data:
            if _check is not None: _check(data)
            hatch.reconcile(self.root,data)
            return self._result(data,job)

    def fail(self, job, token, reason, *, outcome='known', _check=None):
        """Only a known failure releases the native token; unknown work stays owned."""
        if outcome not in ('known','uncertain'):
            raise RigError('DIRECTOR_ACQUISITION_OUTCOME','Outcome must be known or uncertain.',stage='director')
        with self._locked() as data:
            if _check is not None: _check(data)
            selected=hatch.job_for(data,job)
            if selected['status']!='running' or selected.get('token')!=token:
                raise RigError('DIRECTOR_ACQUISITION_TOKEN','The original active native token is required.',stage='director')
            if not isinstance(reason,str) or not reason.strip():
                raise RigError('DIRECTOR_ACQUISITION_REASON','A preserved failure reason is required.',stage='director')
            if outcome=='uncertain':
                # Native claim history already records the original owner. Never
                # translate an unknown remote outcome into permission to retry.
                selected['history'].append(dict(event='outcome-uncertain',time=time.time(),
                                                token=token,reason=reason))
                hatch.save(self.root,data)
                return self._result(data,job,diagnostic=reason)
            hatch.fail(self.root,data,job,token,reason)
            return self._result(data,job)

    def ingest(self, job, token, source, *, source_sha256, receipt, note, _check=None):
        """Preserve verified source bytes, then delegate native raster ingestion."""
        if not isinstance(source_sha256,str) or len(source_sha256)!=64:
            raise RigError('DIRECTOR_ACQUISITION_HASH','An exact source SHA-256 is required.',stage='director')
        payload=read_source_bytes(source)
        if hashlib.sha256(payload).hexdigest()!=source_sha256:
            raise RigError('DIRECTOR_ACQUISITION_HASH','Source bytes differ from the declared candidate.',stage='director')
        if not isinstance(receipt,dict):
            raise RigError('DIRECTOR_ACQUISITION_RECEIPT','An exact native handoff receipt is required.',stage='director')
        with self._locked() as data:
            if _check is not None: _check(data)
            selected=hatch.job_for(data,job)
            # Reject invalid/stale claims before any publication or source copy.
            if (selected['status']!='running' or selected.get('token')!=token or
                selected.get('stale') or hatch.inputs(self.root,selected)!=selected['input_hashes']):
                raise RigError('DIRECTOR_ACQUISITION_TOKEN','Active unchanged native claim required.',stage='director')
            expected='$fixture' if data['fixture_mode'] else '$imagegen'
            value=deepcopy(receipt.get('receipt_template',receipt))
            if (value.get('generation_skill')!=expected or value.get('input_hashes')!=selected['input_hashes'] or
                value.get('token')!=token or value.get('job')!=job):
                raise RigError('DIRECTOR_ACQUISITION_RECEIPT','Receipt does not match the native claim/provenance.',stage='director')
            if not isinstance(note,str) or not note.strip():
                raise RigError('DIRECTOR_ACQUISITION_REASON','A native inspection note is required.',stage='director')
            # Preserve exact candidate bytes inside the owned run, independently
            # of subsequent changes to an external source path. Existing native
            # ingestion validates actual image content before accepting it.
            folder=self.root/'director-inputs'
            if folder.exists() and (folder.is_symlink() or not folder.is_dir()):
                raise RigError('DIRECTOR_ACQUISITION_PATH','Unsafe owned source folder.',stage='director')
            folder.mkdir(exist_ok=True)
            format_name,_=detect_format(payload)
            if format_name not in ('png','webp','jpeg'):
                raise RigError('DIRECTOR_ACQUISITION_FORMAT','Native raster ingestion requires PNG, WebP or JPEG.',stage='director')
            suffix={'png':'.png','webp':'.webp','jpeg':'.jpg'}[format_name]
            target=folder/(source_sha256+suffix)
            if target.exists() or target.is_symlink():
                if read_source_bytes(target)!=payload:
                    raise RigError('DIRECTOR_ACQUISITION_HASH','Existing immutable candidate was changed.',stage='director')
            else:
                fd,name=tempfile.mkstemp(prefix='.candidate-',dir=folder)
                try:
                    with os.fdopen(fd,'wb') as stream:
                        stream.write(payload);stream.flush();os.fsync(stream.fileno())
                    _rename_no_replace(name,target)
                finally:
                    Path(name).unlink(missing_ok=True)
            with tempfile.TemporaryDirectory(prefix='director-native-receipt-') as temporary:
                receipt_path=Path(temporary)/'receipt.json';hatch.atomic(receipt_path,value)
                hatch.ingest(self.root,data,job,target,token,note,receipt_path,fixture=data['fixture_mode'])
            return self._result(data,job,source_sha256=source_sha256,
                                preserved_source=target.relative_to(self.root).as_posix())


class AcquisitionGateway:
    """Canonical Director envelope adapter around one named native raster job.

    ``input_sha256`` is the hash of the current native input-hash mapping. The
    specification logical ID selects the job. Ingest candidates are exactly the
    source image and native receipt. Failure diagnostics carry the explicit
    known/uncertain marker; see references/director-acquisition.md.
    """
    def __init__(self, native: NativeAcquisitionGateway, project_root, job):
        self.native = native
        self.root = Path(project_root).absolute()
        self.job = job
        if self.root != native.root:
            raise RigError('DIRECTOR_ACQUISITION_PATH',
                           'The native run must be the exact artifact project root.',stage='director')

    def input_sha256(self):
        from rig_errors import object_hash
        with self.native._locked() as data:
            selected=hatch.job_for(data,self.job)
            try: return object_hash(hatch.inputs(self.root,selected))
            except (OSError,ValueError):
                if selected.get('input_hashes') and selected['status']=='running':
                    return object_hash(selected['input_hashes'])
                raise RigError('DIRECTOR_ACQUISITION_INPUT','Native inputs are unavailable.',stage='director')

    def _execute(self, method, request):
        from director_contracts import (_read_artifact, validate_document,
                                        validate_method_request, validate_method_result)
        from rig_errors import decode_json, object_hash, require
        validate_method_request(method,request)
        from datetime import datetime, timezone
        require(datetime.fromisoformat(request['context']['deadline'].replace('Z','+00:00')) >
                datetime.now(timezone.utc),'DIRECTOR_ACQUISITION_DEADLINE','Acquisition request deadline expired.')
        started=time.process_time()
        params=request['input']
        spec_ref=params['spec']
        spec=validate_document(decode_json(_read_artifact(self.root,spec_ref)))
        require(spec['kind']=='director-asset-spec' and spec['logical_id']==self.job and
                spec['asset_type']=='raster' and spec_ref['artifact_id']==spec['id'] and
                spec_ref['schema_version']==spec['schema_version'] and
                spec_ref['semantic_version']==spec['semantic_version'],
                'DIRECTOR_ACQUISITION_SPEC','Expected the exact raster specification for this native job.')
        require(params['provider_request_id'] is None,'DIRECTOR_ACQUISITION_PROVIDER',
                'Native handoff does not expose provider request identifiers.')
        candidates=params['candidates']
        failure=None
        source=None
        receipt=None
        source_hash=None
        if method=='AcquisitionGateway.ingest':
            require(len(candidates)==2 and not params['diagnostics'],
                    'DIRECTOR_ACQUISITION_INPUT','Ingest requires only exact source and receipt candidates.')
            source_ref,receipt_ref=candidates
            source_bytes=_read_artifact(self.root,source_ref)
            source_hash=hashlib.sha256(source_bytes).hexdigest()
            require(source_ref['fixture']==self.native.binding['fixture'] and
                    source_ref['kind']=='acquisition-source' and receipt_ref['kind']=='acquisition-receipt' and
                    receipt_ref['fixture']==self.native.binding['fixture'],
                    'DIRECTOR_ACQUISITION_PROVENANCE','Candidate provenance must match the native run.')
            source=self.root/source_ref['path']
            receipt=decode_json(_read_artifact(self.root,receipt_ref))
        elif method=='AcquisitionGateway.fail':
            require(not candidates and len(params['diagnostics'])==1,
                    'DIRECTOR_ACQUISITION_INPUT','Failure requires exactly one native outcome diagnostic.')
            failure=params['diagnostics'][0]
            require(failure['code']=='NATIVE_ACQUISITION_OUTCOME' and
                    failure['field_path']=='outcome' and failure['observed'] in ('known','uncertain') and
                    failure['entity_id']==self.job and not failure['retryable'],
                    'DIRECTOR_ACQUISITION_OUTCOME','Explicit non-retryable known/uncertain native outcome required.')
        else:
            require(not candidates and not params['diagnostics'],'DIRECTOR_ACQUISITION_INPUT',
                    'This native operation takes no candidates or diagnostics.')

        recovery_stale=[False]
        def check(data):
            require(datetime.fromisoformat(request['context']['deadline'].replace('Z','+00:00')) >
                    datetime.now(timezone.utc),'DIRECTOR_ACQUISITION_DEADLINE',
                    'Acquisition request deadline expired while waiting for native ownership.')
            selected=hatch.job_for(data,self.job)
            pinned=selected.get('director_spec_binding')
            require(pinned is None or pinned==spec_ref,'DIRECTOR_ACQUISITION_SPEC',
                    'This native job is bound to a different exact Director specification.')
            require(pinned is not None or selected['status'] in ('pending','failed'),
                    'DIRECTOR_ACQUISITION_SPEC',
                    'An existing active or generated acquisition cannot be relabeled with a new Director specification.')
            require(pinned is not None or method in ('AcquisitionGateway.prepare','AcquisitionGateway.claim'),
                    'DIRECTOR_ACQUISITION_SPEC','Prepare or claim must establish the exact specification binding first.')
            duplicate_claim=(method=='AcquisitionGateway.claim' and selected['status']=='running' and
                             params['expected_state'] in ('pending','failed') and params['claim_id'] is None)
            recovery=method in ('AcquisitionGateway.reconcile','AcquisitionGateway.fail')
            recorded=selected.get('input_hashes')
            try:
                current=hatch.inputs(self.root,selected)
            except (OSError,ValueError) as exc:
                require(recovery and recorded is not None,'DIRECTOR_ACQUISITION_INPUT',
                        'Native inputs are unavailable; only token-bound recovery is permitted.')
                current=None
            recovery_stale[0]=recorded is not None and current!=recorded
            accepted_hashes={object_hash(current)} if current is not None else set()
            if recovery and recorded is not None: accepted_hashes.add(object_hash(recorded))
            require(params['binding_id'] in (None,data['run_id']) and
                    (params['expected_state']==selected['status'] or duplicate_claim) and
                    params['input_sha256'] in accepted_hashes,
                    'DIRECTOR_ACQUISITION_STALE','Native identity, input hashes or expected state changed.')
            if (method in ('AcquisitionGateway.ingest','AcquisitionGateway.fail') or
                    (recovery and recovery_stale[0] and selected['status']=='running')):
                require(params['claim_id'] is not None and params['claim_id']==selected.get('token'),
                        'DIRECTOR_ACQUISITION_TOKEN','The original active native claim is required.')
            else:
                require(params['claim_id'] is None or params['claim_id']==selected.get('token'),
                        'DIRECTOR_ACQUISITION_TOKEN','Request changed the original native claim.')
            if method=='AcquisitionGateway.claim' and not data['fixture_mode']:
                for policy in (request['context']['policy'],spec['source_policy']):
                    require(policy['network']=='allow-configured' and 'imagegen' in policy['providers'] and
                            policy['paid']!='deny','DIRECTOR_ACQUISITION_POLICY',
                            'Production native handoff requires explicitly permitted imagegen and paid policy.')
                    require(policy['policy_ref'] is not None,'DIRECTOR_ACQUISITION_POLICY',
                            'Production handoff requires exact policy evidence.')
                    _read_artifact(self.root,policy['policy_ref'])
                    if policy['paid']=='bounded':
                        limits=data.get('limits',{})
                        require(request['context']['budget_reservation_id'] is not None and
                                type(limits.get('generation_calls')) is int and limits['generation_calls']>0 and
                                type(limits.get('unit_budget')) in (int,float) and limits['unit_budget']>0 and
                                type(limits.get('estimated_units_per_call')) in (int,float) and
                                limits['estimated_units_per_call']>0,
                                'DIRECTOR_ACQUISITION_POLICY','Bounded handoff requires a reservation and native call/unit limits.')
                    else:
                        require(policy['policy_ref'] is not None,'DIRECTOR_ACQUISITION_POLICY',
                                'Unknown-price handoff requires exact policy evidence.')
                        _read_artifact(self.root,policy['policy_ref'])
            if pinned is None:
                selected['director_spec_binding']=deepcopy(spec_ref)
                selected['history'].append(dict(event='director-spec-bound',time=time.time(),spec=deepcopy(spec_ref)))
                hatch.save(self.root,data)

        if method=='AcquisitionGateway.prepare':
            result=self.native.prepare(self.job,_check=check)
        elif method=='AcquisitionGateway.claim':
            result=self.native.claim(self.job,_check=check)
        elif method=='AcquisitionGateway.reconcile':
            result=self.native.reconcile(self.job,_check=check)
        elif method=='AcquisitionGateway.fail':
            result=self.native.fail(self.job,params['claim_id'],failure['recommended_action'],
                                    outcome=failure['observed'],_check=check)
        else:
            result=self.native.ingest(self.job,params['claim_id'],source,source_sha256=source_hash,
                                      receipt=receipt,note='Exact Director candidate bytes verified; visual review not performed.',_check=check)
        # The native manifest is mutable. Preserve an immutable evidence snapshot
        # so later operations cannot invalidate the result's exact ArtifactRef.
        with self.native._locked() as data:
            import json
            payload=(json.dumps(result['native_manifest'],sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
            digest=hashlib.sha256(payload).hexdigest()
            folder=self.root/'director-records'
            require(not folder.is_symlink(),'DIRECTOR_ACQUISITION_PATH','Evidence folder cannot be a link.')
            folder.mkdir(exist_ok=True)
            target=folder/(digest+'.json')
            if target.exists() or target.is_symlink():
                require(read_source_bytes(target)==payload,'DIRECTOR_ACQUISITION_HASH','Existing native evidence changed.')
            else:
                fd,name=tempfile.mkstemp(prefix='.native-',dir=folder)
                try:
                    with os.fdopen(fd,'wb') as stream:
                        stream.write(payload);stream.flush();os.fsync(stream.fileno())
                    _rename_no_replace(name,target)
                finally: Path(name).unlink(missing_ok=True)
        binding={**result['native_binding'],'path':target.relative_to(self.root).as_posix(),
                 'sha256':digest,'byte_size':len(payload)}
        unknown=lambda:dict(value=None,explanation='Not exposed by the native acquisition owner.')
        usage=dict(classification='actual',provider_calls=dict(value=0,explanation=None),
                   input_tokens=unknown(),output_tokens=unknown(),
                   cpu_seconds=dict(value=time.process_time()-started,explanation=None),
                   gpu_seconds=dict(value=0,explanation=None),storage_bytes=unknown(),currency=None,amount=unknown(),
                   measurement_source='Local adapter CPU; no provider invocation by this operation.')
        value=dict(binding=binding,claim_id=result['claim_id'] or params['claim_id'],
                   native_state=result['native_state'],provider_request_id=None,
                   outcome=result['outcome'],receipt=binding if method=='AcquisitionGateway.ingest' else None)
        diagnostics=[]
        if recovery_stale[0] or result['stale']:
            diagnostics.append(dict(code='NATIVE_INPUTS_STALE',severity='warning',stage='acquisition',
                entity_id=self.job,field_path='input.input_sha256',expected='Original native claim inputs',
                observed='Native input bytes changed or are unavailable.',retryable=False,
                recommended_action='Preserve the original claim until its outcome is known; repair inputs explicitly.',
                evidence=[binding]))
        response=dict(schema_version=1,kind='director-result',method=method,status='succeeded',
                      operation_id=request['context']['request_id'],input_digest=object_hash(request),value=value,
                      artifacts=[binding],diagnostics=diagnostics,usage=usage,provenance_receipts=[binding])
        return validate_method_result(method,response,request)

    def prepare(self,request): return self._execute('AcquisitionGateway.prepare',request)
    def claim(self,request): return self._execute('AcquisitionGateway.claim',request)
    def ingest(self,request): return self._execute('AcquisitionGateway.ingest',request)
    def fail(self,request): return self._execute('AcquisitionGateway.fail',request)
    def reconcile(self,request): return self._execute('AcquisitionGateway.reconcile',request)
