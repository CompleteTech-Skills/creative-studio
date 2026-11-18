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
from asset_library import read_source_bytes
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
            data=hatch.manifest(self.root,recovery=binding is not None)
            retained={entry.get('input_hashes',{}).get('pet_request.json')
                      for job in data['jobs'] for entry in job['history']
                      if entry.get('event')=='claim'}
            historical=(isinstance(binding,dict) and
                        binding.get('request_sha256') in retained)
            if binding is not None and not historical:
                data=hatch.manifest(self.root)
            request_hash=(binding['request_sha256'] if historical else
                          hatch.input_digest(self.root/'pet_request.json'))
            prepared=dict(schema_version=1,kind='director-native-raster-binding',
                          binding_id=data['run_id'],run_id=data['run_id'],
                          request_sha256=request_hash,
                          fixture=data['fixture_mode'],asset_kind='raster')
            if binding is not None and binding != prepared:
                raise RigError('DIRECTOR_ACQUISITION_BINDING','Native run identity or original request changed.',stage='director')
            self.binding=prepared

    @contextmanager
    def _locked(self, *, recovery_job=None):
        with _ownership(self.root):
            data=hatch.manifest(self.root,recovery=recovery_job is not None)
            # Recovery authenticates the request captured by the native claim.
            # Current input files are untrusted and must not precede accounting.
            recorded=None
            if recovery_job is not None:
                selected=hatch.job_for(data,recovery_job)
                claims=[entry for entry in selected['history'] if entry.get('event')=='claim']
                if claims and selected.get('input_hashes')==claims[-1].get('input_hashes'):
                    recorded=selected['input_hashes']
                else:
                    # A pending/unclaimed job receives only ordinary current
                    # validation. Reading recovery history cannot admit work.
                    data=hatch.manifest(self.root)
            request_hash=(recorded.get('pet_request.json') if recorded is not None
                          else hatch.input_digest(self.root/'pet_request.json'))
            if (data['run_id'] != self.binding['run_id'] or
                data['fixture_mode'] != self.binding['fixture'] or
                request_hash != self.binding['request_sha256']):
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

    def prepare(self, job=None, *, _check=None, _admit=None):
        """Bind an existing native run; repeated preparation has no side effects."""
        with self._locked() as data:
            if _check is not None: _check(data)
            if _admit is not None: _admit(data)
            return self._result(data,job)

    def reserve_budget(self, job):
        """Reserve existing native capacity without a claim or automatic enrollment."""
        with self._locked() as data:
            return hatch.reserve_budget(self.root,data,job)

    def claim(self, job, *, _check=None, _admit=None, _checkpoint=None):
        """Return the existing active token, or delegate one native claim."""
        with self._locked() as data:
            if _check is not None: _check(data)
            hatch.reconcile(self.root,data)
            if _checkpoint is not None: _checkpoint()
            selected=hatch.job_for(data,job)
            if selected['status']=='running':
                return self._result(data,job,reused_active_claim=True,handoff=None)
            if _admit is not None: _admit(data)
            if _checkpoint is not None: _checkpoint()
            handoff=hatch.claim(self.root,data,job)
            return self._result(data,job,reused_active_claim=False,handoff=handoff)

    def reconcile(self, job, *, _check=None):
        """Reconcile input/output freshness without releasing an uncertain token."""
        with self._locked(recovery_job=job) as data:
            if _check is not None: _check(data)
            hatch.reconcile(self.root,data)
            return self._result(data,job)

    def fail(self, job, token, reason, *, outcome='known', budget_usage=None, _check=None,
             _checkpoint=None):
        """Only a known failure releases the native token; unknown work stays owned."""
        if outcome not in ('known','uncertain'):
            raise RigError('DIRECTOR_ACQUISITION_OUTCOME','Outcome must be known or uncertain.',stage='director')
        with self._locked(recovery_job=job) as data:
            if _check is not None: _check(data)
            selected=hatch.job_for(data,job)
            if selected['status']!='running' or selected.get('token')!=token:
                raise RigError('DIRECTOR_ACQUISITION_TOKEN','The original active native token is required.',stage='director')
            if not isinstance(reason,str) or not reason.strip():
                raise RigError('DIRECTOR_ACQUISITION_REASON','A preserved failure reason is required.',stage='director')
            budget=hatch._native_budget(self.root,data,recovery=True)
            if _checkpoint is not None: _checkpoint()
            if budget is not None:
                if (not isinstance(budget_usage,dict) or
                    (budget_usage.get('outcome')=='uncertain') != (outcome=='uncertain')):
                    raise RigError('DIRECTOR_ACQUISITION_OUTCOME',
                                   'Failure diagnostic and native accounting outcome must agree.',stage='director')
                hatch.fail(self.root,data,job,token,reason,budget_usage=budget_usage)
                return self._result(data,job)
            if budget_usage is not None:
                raise RigError('DIRECTOR_ACQUISITION_POLICY','Usage requires an enrolled native budget.',stage='director')
            if outcome=='uncertain':
                # Native claim history already records the original owner. Never
                # translate an unknown remote outcome into permission to retry.
                selected['history'].append(dict(event='outcome-uncertain',time=time.time(),
                                                token=token,reason=reason))
                hatch.save(self.root,data)
                return self._result(data,job,diagnostic=reason)
            hatch.fail(self.root,data,job,token,reason)
            return self._result(data,job)

    def ingest(self, job, token, source, *, source_sha256, receipt, note,
               _check=None, _read_source=None, _checkpoint=None):
        """Account an authenticated receipt, then verify and ingest source bytes."""
        if not isinstance(receipt,dict):
            raise RigError('DIRECTOR_ACQUISITION_RECEIPT','An exact native handoff receipt is required.',stage='director')
        with self._locked(recovery_job=job) as data:
            if _check is not None: _check(data)
            selected=hatch.job_for(data,job)
            if selected['status']!='running' or selected.get('token')!=token:
                raise RigError('DIRECTOR_ACQUISITION_TOKEN','The original active native claim is required.',stage='director')
            expected='$fixture' if data['fixture_mode'] else '$imagegen'
            value=deepcopy(receipt.get('receipt_template',receipt))
            if (not isinstance(value,dict) or value.get('generation_skill')!=expected or
                value.get('input_hashes')!=selected['input_hashes'] or
                value.get('token')!=token or value.get('job')!=job):
                raise RigError('DIRECTOR_ACQUISITION_RECEIPT','Receipt does not match the native claim/provenance.',stage='director')
            if not isinstance(note,str) or not note.strip():
                raise RigError('DIRECTOR_ACQUISITION_REASON','A native inspection note is required.',stage='director')
            budget=hatch._native_budget(self.root,data,recovery=True)
            if budget is not None:
                # Accounting authenticates the original claim, independently of
                # missing/stale inputs or rejected candidate bytes. Native ingest
                # replays this exact observation; it cannot charge it twice.
                hatch.record_budget_usage(self.root,data,job,token,value.get('budget_usage'))
                if value['budget_usage']['outcome']!='complete':
                    raise RigError('DIRECTOR_ACQUISITION_OUTCOME',
                                   'Ingestion requires complete known native usage.',stage='director')
            elif 'budget_usage' in value:
                raise RigError('DIRECTOR_ACQUISITION_POLICY','Usage requires an enrolled native budget.',stage='director')
            # A deadline never rolls back an authentic observation already
            # retained by the native owner; the original claim remains recoverable.
            if _checkpoint is not None: _checkpoint()
            try:
                fresh=not selected.get('stale') and hatch.inputs(self.root,selected)==selected['input_hashes']
            except (OSError,ValueError):
                fresh=False
            if _checkpoint is not None: _checkpoint()
            if not fresh:
                raise RigError('DIRECTOR_ACQUISITION_TOKEN','Active unchanged native claim required.',stage='director')
            if not isinstance(source_sha256,str) or len(source_sha256)!=64:
                raise RigError('DIRECTOR_ACQUISITION_HASH','An exact source SHA-256 is required.',stage='director')
            payload=_read_source() if _read_source is not None else read_source_bytes(source)
            if _checkpoint is not None: _checkpoint()
            if hashlib.sha256(payload).hexdigest()!=source_sha256:
                raise RigError('DIRECTOR_ACQUISITION_HASH','Source bytes differ from the declared candidate.',stage='director')
            # Native ingestion owns preservation and decodes the same immutable
            # bytes. No mutable source pathname is reopened at this boundary.
            with tempfile.TemporaryDirectory(prefix='director-native-receipt-') as temporary:
                receipt_path=Path(temporary)/'receipt.json';hatch.atomic(receipt_path,value)
                if _checkpoint is not None: _checkpoint()
                hatch.ingest(self.root,data,job,source,token,note,receipt_path,
                             fixture=data['fixture_mode'],source_bytes=payload)
            target=Path(selected['source_path'])
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
        with self.native._locked(recovery_job=self.job) as data:
            selected=hatch.job_for(data,self.job)
            if selected['status']=='running' and selected.get('input_hashes'):
                return object_hash(selected['input_hashes'])
            try:
                reader=hatch.generation_inputs if selected['status'] in ('pending','failed') else hatch.inputs
                return object_hash(reader(self.root,selected))
            except (OSError,ValueError):
                if selected.get('input_hashes') and selected['status']=='running':
                    return object_hash(selected['input_hashes'])
                raise RigError('DIRECTOR_ACQUISITION_INPUT','Native inputs are unavailable.',stage='director')

    def _execute(self, method, request):
        from director_contracts import (_read_artifact, parse_rfc3339, validate_document,
                                        validate_method_request, validate_method_result)
        from rig_errors import decode_json, object_hash, require
        validate_method_request(method,request)
        from datetime import datetime, timezone
        deadline=parse_rfc3339(request['context']['deadline'],code='ACQUISITION_DEADLINE')
        def checkpoint(message='Acquisition request deadline expired at a cooperative work boundary.'):
            require(deadline > datetime.now(timezone.utc),'DIRECTOR_ACQUISITION_DEADLINE',message)
        checkpoint()
        started=time.process_time()
        params=request['input']
        spec_ref=params['spec']
        spec=validate_document(decode_json(_read_artifact(self.root,spec_ref)))
        checkpoint()
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
        source_reader=None
        budget_usage=None
        if method=='AcquisitionGateway.ingest':
            require(len(candidates)==2 and not params['diagnostics'],
                    'DIRECTOR_ACQUISITION_INPUT','Ingest requires only exact source and receipt candidates.')
            source_ref,receipt_ref=candidates
            source_hash=source_ref['sha256']
            require(receipt_ref['kind']=='acquisition-receipt' and
                    receipt_ref['fixture']==self.native.binding['fixture'],
                    'DIRECTOR_ACQUISITION_PROVENANCE','Receipt provenance must match the native run.')
            def source_reader():
                require(source_ref['fixture']==self.native.binding['fixture'] and
                        source_ref['kind']=='acquisition-source','DIRECTOR_ACQUISITION_PROVENANCE',
                        'Candidate provenance must match the native run.')
                return _read_artifact(self.root,source_ref)
            source=self.root/source_ref['path']
            receipt=decode_json(_read_artifact(self.root,receipt_ref))
            checkpoint()
        elif method=='AcquisitionGateway.fail':
            require(not candidates and len(params['diagnostics'])==1,
                    'DIRECTOR_ACQUISITION_INPUT','Failure requires exactly one native outcome diagnostic.')
            failure=params['diagnostics'][0]
            require(failure['code']=='NATIVE_ACQUISITION_OUTCOME' and
                    failure['field_path']=='outcome' and failure['observed'] in ('known','uncertain') and
                    failure['entity_id']==self.job and not failure['retryable'],
                    'DIRECTOR_ACQUISITION_OUTCOME','Explicit non-retryable known/uncertain native outcome required.')
            evidence=failure['evidence']
            if len(evidence)==1 and evidence[0]['kind']=='acquisition-budget-observation':
                require(evidence[0]['fixture']==self.native.binding['fixture'],
                        'DIRECTOR_ACQUISITION_PROVENANCE','Observation must preserve the native fixture classification.')
                budget_usage=decode_json(_read_artifact(self.root,evidence[0]))
                checkpoint()
        else:
            require(not candidates and not params['diagnostics'],'DIRECTOR_ACQUISITION_INPUT',
                    'This native operation takes no candidates or diagnostics.')

        def check(data):
            checkpoint('Acquisition request deadline expired while waiting for native ownership.')
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
            recovery=method in ('AcquisitionGateway.reconcile','AcquisitionGateway.fail','AcquisitionGateway.ingest') or (
                method=='AcquisitionGateway.claim' and selected['status']=='running')
            recorded=selected.get('input_hashes')
            if recovery:
                # Authenticate original ownership before touching any current
                # generation inputs or retaining a usage observation.
                require(recorded is not None,'DIRECTOR_ACQUISITION_INPUT',
                        'Recovery requires the recorded native claim inputs.')
                current=recorded
            else:
                try:
                    reader=hatch.generation_inputs if selected['status'] in ('pending','failed') else hatch.inputs
                    current=reader(self.root,selected)
                except (OSError,ValueError) as exc:
                    raise RigError('DIRECTOR_ACQUISITION_INPUT',
                                   'Native inputs are unavailable.',stage='director') from exc
                checkpoint()
            accepted=recorded if recovery or selected['status']=='running' else current
            require(params['binding_id'] in (None,data['run_id']) and
                    (params['expected_state']==selected['status'] or duplicate_claim) and
                    params['input_sha256']==object_hash(accepted),
                    'DIRECTOR_ACQUISITION_STALE','Native identity, input hashes or expected state changed.')
            if method=='AcquisitionGateway.reconcile' and selected['status']!='running':
                claims=[entry for entry in selected['history'] if entry.get('event')=='claim']
                latest=claims[-1] if claims else None
                require(latest is not None and params['claim_id']==latest.get('token') and
                        recorded==latest.get('input_hashes'),
                        'DIRECTOR_ACQUISITION_TOKEN',
                        'Terminal reconciliation requires the latest original native claim.')
            elif method in ('AcquisitionGateway.ingest','AcquisitionGateway.fail','AcquisitionGateway.reconcile'):
                require(selected['status']=='running' and params['claim_id'] is not None and
                        params['claim_id']==selected.get('token'),
                        'DIRECTOR_ACQUISITION_TOKEN','The original active native claim is required.')
            else:
                require(params['claim_id'] is None or params['claim_id']==selected.get('token'),
                        'DIRECTOR_ACQUISITION_TOKEN','Request changed the original native claim.')
            budget=hatch._native_budget(self.root,data,recovery=recovery)
            checkpoint()
            policies=(request['context']['policy'],spec['source_policy'])
            paid_claim=(method=='AcquisitionGateway.claim' and
                        (not data['fixture_mode'] or any(policy['paid']!='deny' or
                         policy['network']=='allow-configured' for policy in policies)))
            require(not paid_claim or budget is not None,'DIRECTOR_ACQUISITION_POLICY',
                    'Paid canonical handoff requires an enrolled native reservation.')
            if budget is not None:
                reservation=request['context']['budget_reservation_id']
                require(reservation is not None,'DIRECTOR_ACQUISITION_POLICY',
                        'Every enrolled canonical operation requires its native reservation.')
                bound_inputs=recorded if recovery or selected['status']=='running' else (
                    current if method in ('AcquisitionGateway.prepare','AcquisitionGateway.claim') else None)
                budget.verify_reservation(selected,reservation,input_hashes=bound_inputs)
                checkpoint()
                if paid_claim and selected['status']!='running':
                    require(budget.config['provider']=='imagegen','DIRECTOR_ACQUISITION_PROVIDER',
                            'Native raster acquisition requires the configured imagegen provider.')
                    for policy in policies:
                        require(policy['network']=='allow-configured' and 'imagegen' in policy['providers'] and
                                policy['paid'] in ('bounded','allow-unknown-price'),
                                'DIRECTOR_ACQUISITION_POLICY',
                                'New native handoff requires explicitly permitted imagegen and paid policy.')
                        require(policy['policy_ref'] is not None,'DIRECTOR_ACQUISITION_POLICY',
                                'New native handoff requires exact policy evidence.')
                        _read_artifact(self.root,policy['policy_ref'])
                        checkpoint()
                        require(not (budget.config['unknown_price'] or budget.config['estimate']['amount'] is None) or
                                policy['paid']=='allow-unknown-price','DIRECTOR_ACQUISITION_POLICY',
                                'Unknown native prices require explicit unknown-price permission.')
                        if method=='AcquisitionGateway.claim' and policy['paid']=='bounded' and not data['fixture_mode']:
                            limits=data.get('limits',{})
                            require(type(limits.get('generation_calls')) is int and limits['generation_calls']>0 and
                                    type(limits.get('unit_budget')) in (int,float) and limits['unit_budget']>0 and
                                    type(limits.get('estimated_units_per_call')) in (int,float) and
                                    limits['estimated_units_per_call']>0,
                                    'DIRECTOR_ACQUISITION_POLICY','Bounded handoff requires native call/unit limits.')
                if failure is not None:
                    require(len(failure['evidence'])==1 and
                            failure['evidence'][0]['kind']=='acquisition-budget-observation' and
                            isinstance(budget_usage,dict),'DIRECTOR_ACQUISITION_INPUT',
                            'Budgeted failure requires one exact native usage observation.')
            checkpoint()

        def admit(data):
            # Validation does not bind a new specification. A new claim first
            # reconciles native state, then reaches this deadline-guarded boundary.
            checkpoint()
            selected=hatch.job_for(data,self.job)
            if selected.get('director_spec_binding') is None:
                selected['director_spec_binding']=deepcopy(spec_ref)
                selected['history'].append(dict(event='director-spec-bound',time=time.time(),spec=deepcopy(spec_ref)))
                hatch.save(self.root,data)

        if method=='AcquisitionGateway.prepare':
            result=self.native.prepare(self.job,_check=check,_admit=admit)
        elif method=='AcquisitionGateway.claim':
            result=self.native.claim(self.job,_check=check,_admit=admit,_checkpoint=checkpoint)
        elif method=='AcquisitionGateway.reconcile':
            result=self.native.reconcile(self.job,_check=check)
        elif method=='AcquisitionGateway.fail':
            result=self.native.fail(self.job,params['claim_id'],failure['recommended_action'],
                                    outcome=failure['observed'],budget_usage=budget_usage,_check=check,
                                    _checkpoint=checkpoint)
        else:
            result=self.native.ingest(self.job,params['claim_id'],source,source_sha256=source_hash,
                                      receipt=receipt,note='Exact Director candidate bytes verified; visual review not performed.',
                                      _check=check,_read_source=source_reader,_checkpoint=checkpoint)
        checkpoint()
        # The native manifest is mutable. Preserve an immutable evidence snapshot
        # so later operations cannot invalidate the result's exact ArtifactRef.
        with self.native._locked(recovery_job=self.job) as data:
            checkpoint()
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
        if result['stale'] or (method=='AcquisitionGateway.reconcile' and
                               result['native_state']!=params['expected_state']):
            diagnostics.append(dict(code='NATIVE_INPUTS_STALE',severity='warning',stage='acquisition',
                entity_id=self.job,field_path='input.input_sha256',expected='Original native claim inputs',
                observed='Native input bytes changed or are unavailable.',retryable=False,
                recommended_action='Preserve the original claim until its outcome is known; repair inputs explicitly.',
                evidence=[binding]))
        response=dict(schema_version=1,kind='director-result',method=method,status='succeeded',
                      operation_id=request['context']['request_id'],input_digest=object_hash(request),value=value,
                      artifacts=[binding],diagnostics=diagnostics,usage=usage,provenance_receipts=[binding])
        checkpoint()
        response=validate_method_result(method,response,request)
        checkpoint()
        return response

    def prepare(self,request): return self._execute('AcquisitionGateway.prepare',request)
    def claim(self,request): return self._execute('AcquisitionGateway.claim',request)
    def ingest(self,request): return self._execute('AcquisitionGateway.ingest',request)
    def fail(self,request): return self._execute('AcquisitionGateway.fail',request)
    def reconcile(self,request): return self._execute('AcquisitionGateway.reconcile',request)
