"""Local asset operations through canonical owners and durable managed jobs.

This facade selects explicit owner APIs. It never resolves a missing exact
version to latest and never interprets a request as Python code.
"""
from __future__ import annotations

from copy import deepcopy
import importlib
import importlib.util
import inspect
import os
from pathlib import Path
from typing import Any, TypedDict

from asset_library import AssetLibrary, read_source_bytes
from asset_library_contracts import canonical_bytes, validate
from rig_errors import RigError, atomic_bytes, object_hash

VERSION = '1.0.0'
MAX_REQUEST_BYTES = 64 * 1024


class OperationRequest(TypedDict):
    operation: str
    parameters: dict[str, Any]


class OperationResult(TypedDict):
    schema_version: int
    operation: str
    result: Any


MODULES = {
    'import': 'asset_library_import', 'validation': 'asset_library_validation',
    'versions': 'asset_library_versions', 'dependencies': 'asset_library_dependencies',
    'search': 'asset_library_search', 'preview': 'asset_library_preview',
    'export': 'asset_library_export', 'packages': 'asset_library_packages',
    'browser': 'asset_library_browser', 'jobs': 'asset_library_jobs', 'management':'asset_library_management',
}
MUTATIONS = frozenset(('register', 'import', 'import-batch', 'generate', 'preview', 'thumbnail',
    'turntable', 'index', 'version', 'version.create', 'version.duplicate', 'version.promote', 'version.rollback',
    'metadata.update', 'tags.add', 'tags.remove', 'collections.create', 'project.bind',
    'project.unbind', 'project.undo-unbind', 'replace', 'repair', 'migrate', 'export', 'bundle.create',
    'bundle.restore', 'storage.gc.apply', 'storage.upgrade', 'storage.recover'))
MANAGED = frozenset(('generate','import', 'preview', 'thumbnail', 'turntable', 'export', 'index', 'bundle.create', 'bundle.restore','version','replace','repair'))


def fail(code, message):
    raise RigError('LIBRARY_OPERATIONS_' + code, message, stage='asset-operations')


def request(value):
    if not isinstance(value, dict): fail('REQUEST', 'Expected a JSON request object.')
    if len(canonical_bytes(value)) > MAX_REQUEST_BYTES: fail('LIMIT', 'Request exceeds 64 KiB.')
    return deepcopy(value)


def module(name):
    try:
        return importlib.import_module(MODULES[name])
    except ImportError as exc:
        fail('UNAVAILABLE', f'{name} requires its canonical adapter and declared optional dependencies: {exc.name}.')


def call(function, arguments):
    try:
        inspect.signature(function).bind(**arguments)
    except TypeError as exc:
        fail('REQUEST', str(exc))
    return function(**arguments)


def _path(value, *, exists=False):
    if not isinstance(value, (str, Path)) or not str(value): fail('PATH', 'Supply an explicit filesystem path.')
    if '..' in Path(value).parts:fail('PATH','Parent traversal is not permitted in operation paths.')
    path = Path(value).absolute()
    for part in (path, *path.parents):
        if part.is_symlink(): fail('PATH', 'Operation paths cannot contain symlinks.')
        if part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400:
            fail('PATH', 'Operation paths cannot contain reparse points.')
    if exists and not path.exists(): fail('PATH', 'Required operation path is absent.')
    return path


class AssetOperations:
    """Versioned local API. All mutations need explicit apply or managed submit.

    The caller owns local filesystem authority. HTTP hosts must separately
    restrict request paths to authorized roots and authenticate writes.
    """
    def __init__(self, library: str | Path | AssetLibrary, *, jobs_root=None, job_limits=None, project_ids=()):
        self.library = library if isinstance(library, AssetLibrary) else AssetLibrary(_path(library, exists=True))
        self.jobs_root = _path(jobs_root) if jobs_root is not None else self.library.root / 'jobs'
        self.job_limits=deepcopy(job_limits)
        self.project_ids=self._project_ids(project_ids)

    @property
    def jobs(self):
        return module('jobs').JobManager(self.jobs_root,limits=self.job_limits)

    @staticmethod
    def capabilities() -> dict:
        return dict(schema_version=1, api_version=VERSION, local_only=True,
            exact_versions=True, production_approval=False, implicit_publication=False,
            adapters={name: dict(available=importlib.util.find_spec(value) is not None,
                module=value) for name,value in MODULES.items()},
            managed_operations=sorted(MANAGED), version_actions=['create','duplicate','promote','rollback'],
            project_pin_operations=['bind','unbind','undo-unbind'], request_bytes=MAX_REQUEST_BYTES,
            default_limits=dict(file_bytes=64*1024**2, version_bytes=256*1024**2,
                versions=4096, source_bytes=256*1024**2, json_depth=64),
            unsupported=['remote-service', 'paid-generation', 'implicit-installation'])

    def _reference(self, value):
        validate(value, 'AssetRef')
        return deepcopy(value)

    @staticmethod
    def _project_ids(values):
        if not isinstance(values,(list,tuple)) or len(values)>128 or any(not isinstance(v,str) for v in values) or len(set(values))!=len(values):
            fail('REQUEST','Project IDs must be an explicit bounded unique sequence.')
        for value in values:module('versions').identifier(value)
        return list(values)

    def _search_index(self,root,*,create=False,project_ids=None):
        projects=self._project_ids(self.project_ids if project_ids is None else project_ids)
        versions=module('versions')
        resolver=versions.ScopedResolver([('local',self.library)],project_library='local') if projects else None
        return module('search').SearchIndex(root,{'local':self.library},create=create,
            versions={'local':versions.AssetVersions(self.library)},usage_resolver=resolver,project_ids=projects)

    def _index_signature(self,project_ids):
        versions=module('versions');lifecycle=versions.AssetVersions(self.library)
        resolver=versions.ScopedResolver([('local',self.library)],project_library='local') if project_ids else None
        return object_hash(dict(entries=[dict(reference=r,annotations=self.library.annotations(r),collections=lifecycle.collections(r)) for r in self.library.list_references()],
            usage={key:resolver.bindings(key) for key in project_ids}))

    def _graph(self):
        return module('dependencies').DependencyGraph({'local': self.library})

    def execute(self, operation: str, parameters: dict | None = None, *, apply=False, dry_run=False) -> OperationResult:
        parameters = request(parameters or {})
        if not isinstance(operation, str): fail('REQUEST', 'Operation must be a string.')
        if type(apply) is not bool or type(dry_run) is not bool: fail('REQUEST', 'Apply/dry-run must be booleans.')
        if operation in MUTATIONS:
            if dry_run:
                return dict(schema_version=1, operation=operation, result=dict(status='plan',
                    parameters=parameters, requires_apply=True, executed=False))
            if not apply: fail('APPLY', 'Mutations require an explicit apply action.')
        if operation in ('version.create','version.duplicate','version.promote','version.rollback'):
            if 'action' in parameters:fail('REQUEST','Version action is selected by the explicit operation.')
            result=self.submit('version',dict(action=operation.split('.')[1],**parameters))
        elif operation in MANAGED:
            result = self.submit(operation, parameters)
        else:
            result = self._execute(operation, parameters)
        return dict(schema_version=1, operation=operation, result=result)

    def _execute(self, operation, p):
        if operation in ('capabilities', 'doctor'):
            if p: fail('REQUEST', 'Capability discovery takes no parameters.')
            result = self.capabilities()
            if operation == 'doctor': result['library'] = self.library.storage_status()
            return result
        if operation in ('list', 'library.list', 'version.list'):
            if set(p)-{'limit','asset_id'}: fail('REQUEST', 'Unknown list parameter.')
            rows = self.library.list_references(limit=p.get('limit',4096))
            return [r for r in rows if p.get('asset_id') is None or r['id']==p['asset_id']]
        if operation in ('library.inspect','library.stats','storage.stats','storage.audit'):
            if p: fail('REQUEST', 'Library inspection takes no parameters.')
            return self.library.storage_status()
        if operation in ('library.verify',):
            if p: fail('REQUEST', 'Library verification takes no parameters.')
            return dict(references=[self.library.resolve(r)['manifest_sha256'] for r in self.library.list_references()], verified=True)
        if operation in ('show','inspect','verify','files','validate'):
            if set(p) != {'reference'}: fail('REQUEST', 'Supply one exact reference.')
            ref=self._reference(p['reference']); manifest=self.library.resolve(ref)
            if operation=='files': return manifest['files']
            if operation=='validate': return call(module('validation').validate_asset,dict(library=self.library,reference=ref))
            return dict(reference=ref, verified=True) if operation=='verify' else manifest
        if operation=='register':
            if set(p)!={'manifest','sources'}: fail('REQUEST','Registration requires manifest and sources.')
            return self.library.register(p['manifest'],p['sources'])
        if operation in ('metadata.update','tags.add','tags.remove'):
            p=deepcopy(p);ref=self._reference(p.pop('reference',None))
            if operation=='metadata.update': return call(self.library.update_annotations,dict(reference=ref,**p))
            if set(p)!={'tags','expected_revision'} or not isinstance(p['tags'],list) or len(p['tags'])>128 or any(not isinstance(v,str) for v in p['tags']): fail('REQUEST','Tags require tags and expected_revision.')
            current=self.library.annotations(ref)
            if current['revision']!=p['expected_revision']:fail('CONFLICT','Reload annotation revision.')
            tags=sorted(set(current['tags']) | set(p['tags'])) if operation=='tags.add' else [v for v in current['tags'] if v not in p['tags']]
            return self.library.update_annotations(ref,expected_revision=p['expected_revision'],tags=tags,favorite=current['favorite'],note=current['note'])
        if operation.startswith('version.') or operation=='collections.create':
            owner=module('versions').AssetVersions(self.library)
            method={'version.create':'create','version.compare':'compare','version.promote':'promote',
                'version.rollback':'rollback','version.state':'state','collections.create':'set_collections'}.get(operation)
            if method:return call(getattr(owner,method),p)
        if operation in ('dependencies','dependents','project.lock','project.verify','project.impact','compatibility'):
            graph=self._graph()
            if operation=='dependencies':return call(graph.closure,p)
            if operation=='dependents':return call(graph.reverse,p)
            if operation=='project.lock':return call(graph.lock,p)
            if operation=='project.verify':return call(graph.verify_lock,p)
            if operation=='project.impact':return call(graph.reverse,p)
            return call(module('dependencies').check_compatibility,dict(graph=graph,**p))
        if operation in ('project.bind','project.unbind','project.undo-unbind'):
            resolver=module('versions').ScopedResolver([('local',self.library)],project_library='local')
            function={'project.bind':resolver.bind,'project.unbind':resolver.unbind,'project.undo-unbind':resolver.undo_unbind}[operation]
            return call(function,p)
        if operation in ('search','semantic-search'):
            p=deepcopy(p);root=_path(p.pop('index',None),exists=True)
            index=self._search_index(root)
            if operation=='semantic-search':fail('UNAVAILABLE','Semantic search requires an explicitly configured trusted provider instance through the search API.')
            return call(index.search,p)
        if operation=='bundle.verify':
            if set(p)!={'root'}:fail('REQUEST','Package verification requires root.')
            return module('packages').verify_package(_path(p['root'],exists=True))
        if operation=='storage.gc':
            if p:fail('REQUEST','GC analysis takes no parameters.')
            return self.library.analyze_gc()
        if operation=='storage.gc.apply':
            if set(p)!={'plan'}:fail('REQUEST','GC apply requires exact analyzed plan.')
            return self.library.apply_gc(p['plan'])
        if operation=='storage.upgrade':return call(self.library.upgrade_storage,p)
        if operation=='storage.recover':return call(self.library.recover,p)
        if operation=='repair':return call(self.library.recover_blob,p)
        if operation=='replace':
            return call(module('dependencies').replacement_plan,dict(graph=self._graph(),**p))
        if operation=='migrate':
            owner=module('versions').LegacyMigration(self.library)
            return call(owner.apply if p.get('apply') else owner.preview,{k:v for k,v in p.items() if k!='apply'})
        if operation=='import-batch':
            if set(p)!={'requests'} or not isinstance(p['requests'],list) or not 1<=len(p['requests'])<=8:fail('REQUEST','Batch requires one to eight requests.')
            return dict(jobs=[self.submit('import',v) for v in p['requests']])
        if operation=='generate':fail('UNAVAILABLE','Use the bounded canonical generator API with explicit fixture policy; paid calls are unavailable.')
        if operation=='browser':fail('UNAVAILABLE','Start the canonical asset_library_browser entry point explicitly; operation requests never start a background HTTP service.')
        fail('UNAVAILABLE','No canonical operation adapter is available for '+operation+'.')

    def submit(self, operation: str, parameters: dict) -> dict:
        p=request(parameters)
        if not isinstance(operation,str):fail('REQUEST','Operation must be a string.')
        if operation not in MANAGED:fail('UNAVAILABLE','Operation is not managed: '+str(operation))
        # Admission constructs the fixed adapter, validates parameters and binds
        # mutable source bytes before any durable request is queued.
        adapter=self._adapter(operation,p)
        p=adapter.admit()
        resources={'generate':('cpu','blender','io','hash'),'import':('cpu','blender','io','hash'),'preview':('preview','blender'),
            'thumbnail':('preview','blender'),'turntable':('preview','blender'),
            'export':('cpu','blender','io'),'index':('index','io'),'bundle.create':('archive','io'),
            'bundle.restore':('archive','io'),'version':('io','hash'),'replace':('io','hash'),'repair':('io','hash')}[operation]
        return module('jobs').JobManager(self.jobs_root,limits=self.job_limits,create=True).submit(operation.replace('.','-'),p,resources=resources,key=object_hash(dict(operation=operation,parameters=p)))

    def list_jobs(self):return self.jobs.list()
    def job_status(self,identifier):return self.jobs.status(identifier)
    def cancel_job(self,identifier):return self.jobs.cancel(identifier)
    def resume_job(self,identifier):return self.jobs.resume(identifier)
    def run_job(self,identifier):
        record=self.jobs.status(identifier)
        operation=next((value for value in MANAGED if value.replace('.', '-')==record['kind']),None)
        if operation is None:fail('UNAVAILABLE','Unknown managed job kind.')
        return self.jobs.run(identifier,self._adapter(operation,record['parameters']))

    def _adapter(self,operation,parameters):
        if operation in ('version','replace','repair'):
            return module('management').management_adapter(self,operation,parameters)
        return OperationAdapter(self,operation,parameters)


class OperationAdapter:
    """Fixed trusted adapter; requests cannot select code or arbitrary modules.

    Import work is staged in a private canonical library. Publication registers
    the verified exact bytes in the final library with its existing transaction
    authority. Recovery recognizes only that same immutable digest.
    """
    def __init__(self, owner, operation, parameters):
        self.owner=owner;self.operation=operation;self.parameters=deepcopy(parameters)

    def admit(self):
        p=self.parameters
        schemas={
            'generate':({'identity','version','metadata','license_file','parameters'}, {'fixture','timeout','memory_bytes','blender'}),
            'import':({'source','identity','version','metadata','license_file'}, {'fixture','unit_scale','basis','ground','blender_roundtrip','trusted_packed_blend','timeout','memory_bytes','blender'}),
            'preview':({'reference','cache_root'}, {'config','logical_path','executable'}),
            'thumbnail':({'reference','cache_root'}, {'config','logical_path','executable'}),
            'turntable':({'reference','cache_root'}, {'config','logical_path','executable'}),
            'export':({'reference','destination'}, {'format','runtime_path','required_capabilities','allowed_losses','trusted_native','blender','timeout','memory_bytes'}),
            'index':({'index'}, {'locations','project_ids'}),
            'bundle.create':({'roots','destination','package_id'}, {'package_type','policy','context_files'}),
            'bundle.restore':({'root','destination'}, set()),
        }
        required,optional=schemas[self.operation]
        if not required<=p.keys() or set(p)-required-optional-{'input_hashes'}:fail('REQUEST','Missing or unknown '+self.operation+' fields.')
        if 'reference' in p:self.owner.library.resolve(p['reference'])
        if self.operation=='import':
            from rig_errors import sha256
            # The import owner snapshots and bounds the complete source closure;
            # this early pin detects a changed primary/license before conversion.
            p['source']=str(_path(p['source'],exists=True));p['license_file']=str(_path(p['license_file'],exists=True))
            source=read_source_bytes(p['source']);license_data=read_source_bytes(p['license_file'])
            import hashlib
            probe=module('import').ImportPipeline(self.owner.library,timeout=p.get('timeout',60),memory_bytes=p.get('memory_bytes',1024**3),blender=p.get('blender')).probe(p['source'])
            hashes=dict(source=hashlib.sha256(source).hexdigest(),license_file=hashlib.sha256(license_data).hexdigest(),closure=probe['files'])
            if p.get('input_hashes',hashes)!=hashes:fail('STALE','Queued source/license bytes changed; submit a new request.')
            p['input_hashes']=hashes
        if self.operation=='index':
            p['project_ids']=self.owner._project_ids(p.get('project_ids',self.owner.project_ids))
            signature=self.owner._index_signature(p['project_ids'])
            hashes=dict(index_sources=signature)
            if p.get('input_hashes',hashes)!=hashes:fail('STALE','Index sources changed; submit a new index generation to a new destination.')
            p['input_hashes']=hashes
        if self.operation=='bundle.restore':
            hashes=dict(package_sha256=module('packages').verify_package(_path(p['root'],exists=True))['package_sha256'])
            if p.get('input_hashes',hashes)!=hashes:fail('STALE','Package changed since queue admission.')
            p['input_hashes']=hashes
        if self.operation=='generate':
            validate(p['identity'],'AssetIdentity');validate(p['metadata'],'AssetMetadata')
            validate(dict(id=p['identity']['id'],version=p['version'],sha256='0'*64),'AssetRef')
            p['parameters']=module('import').generation_parameters(p['parameters'])
            if type(p.get('fixture',False))is not bool:fail('REQUEST','Fixture policy must be a boolean.')
            p['license_file']=str(_path(p['license_file'],exists=True))
            import hashlib
            hashes=dict(license_file=hashlib.sha256(read_source_bytes(p['license_file'],module('import').MAX_REPORT)).hexdigest())
            if p.get('input_hashes',hashes)!=hashes:fail('STALE','Generation license bytes changed since admission.')
            p['input_hashes']=hashes
        for key in ('destination','index','cache_root'):
            if key in p:p[key]=str(_path(p[key]))
        return p

    def _stage_library(self,context):
        root=context.stage_dir/'library'
        return AssetLibrary(root,create=not root.exists())

    def stage(self,context):
        p=self.admit();context.check_cancelled();context.progress(0.05,'staging '+self.operation)
        if self.operation in ('import','generate'):
            library=self._stage_library(context)
            existing=library.list_references()
            if existing:
                if len(existing)!=1:fail('STAGE','Unexpected staged library inventory.')
                self._prepare_import_payloads(context,library,existing[0])
                return dict(reference=existing[0])
            args={k:v for k,v in p.items() if k not in ('timeout','memory_bytes','blender','input_hashes')}
            pipeline=module('import').ImportPipeline(library,timeout=p.get('timeout',60),memory_bytes=p.get('memory_bytes',1024**3),blender=p.get('blender'),cancelled=lambda: self._cancelled(context))
            result=pipeline.generate(**args) if self.operation=='generate' else pipeline.import_asset(**args)
            self._prepare_import_payloads(context,library,result['reference'])
            return result
        if self.operation in ('preview','thumbnail','turntable'):
            args={k:v for k,v in p.items() if k!='cache_root'}
            args['config']=dict(args.get('config') or {})
            if self.operation=='thumbnail':args['config'].setdefault('times',['0'])
            args['cache_root']=str(context.stage_dir/'cache')
            return module('preview').create_preview(self.owner.library,**args,cancelled=lambda:self._cancelled(context))
        if self.operation=='export':
            pipeline=module('export').ExportPipeline(self.owner.library,**{k:p[k] for k in ('blender','timeout','memory_bytes') if k in p},cancelled=lambda:self._cancelled(context))
            return pipeline.export(destination=context.stage_dir/'output',**{k:v for k,v in p.items() if k not in ('destination','blender','timeout','memory_bytes')})
        if self.operation=='index':
            index=self.owner._search_index(context.stage_dir/'output',create=True,project_ids=p['project_ids'])
            return index.rebuild(p.get('locations'))
        if self.operation=='bundle.create':
            return module('packages').create_package(self.owner._graph(),destination=context.stage_dir/'output',**{k:v for k,v in p.items() if k not in ('destination','input_hashes')})
        if self.operation=='bundle.restore':
            return module('packages').restore_package(_path(p['root'],exists=True),context.stage_dir/'output')
        fail('UNAVAILABLE','Managed operation adapter unavailable.')

    @staticmethod
    def _prepare_import_payloads(context,library,reference):
        manifest=library.resolve(reference);root=context.stage_dir/'payloads';root.mkdir(exist_ok=True)
        for file in manifest['files']:
            context.check_cancelled();target=root/file['path'];target.parent.mkdir(parents=True,exist_ok=True)
            atomic_bytes(target,library.read_file(reference,file['path']))

    @staticmethod
    def _cancelled(context):
        try:context.check_cancelled();return False
        except RigError as exc:
            if exc.report['code']=='ASSET_JOB_CANCELLED':return True
            raise

    def verify(self,context,staged):
        context.check_cancelled();context.progress(0.8,'verifying '+self.operation)
        if self.operation in ('import','generate'):
            ref=staged['reference'];library=self._stage_library(context);manifest=library.resolve(ref)
            report=module('import').decode_json(library.read_file(ref,'evidence/import.json'))
            expected=self.parameters['input_hashes']
            import hashlib
            if (self.operation=='import' and report['source_inventory']!=expected['closure']) or hashlib.sha256(library.read_file(ref,'source/license.txt')).hexdigest()!=expected['license_file']:
                fail('STALE','Imported snapshot differs from queued source closure/license; final publication refused.')
            if self.operation=='generate':
                generator=report.get('generator',{})
                if generator.get('provider')!='blender-box-sphere-v1' or generator.get('parameters')!=self.parameters['parameters'] or generator.get('actual_output_sha256')!=report['source_sha256']:
                    fail('VERIFY','Generated evidence does not match the queued native generation profile.')
                if manifest['identity']!=self.parameters['identity'] or manifest['version']!=self.parameters['version'] or manifest['metadata']!=self.parameters['metadata'] or manifest['provenance']['fixture']!=self.parameters.get('fixture',False):
                    fail('VERIFY','Generated staged identity differs from the queued request.')
            return dict(reference=ref,manifest_sha256=manifest['manifest_sha256'])
        if self.operation in ('preview','thumbnail','turntable'):
            return module('preview').verify_preview(context.stage_dir/'cache'/staged['key'],staged['key'])
        if self.operation=='index':
            # Reopen through the canonical owner and resolve every indexed input.
            index=self.owner._search_index(context.stage_dir/'output',project_ids=self.parameters['project_ids'])
            index.search('',limit=1)
            if self.owner._index_signature(self.parameters['project_ids'])!=self.parameters['input_hashes']['index_sources']:
                fail('STALE','Index snapshot source revisions differ from queue admission; submit a new generation.')
        if self.operation=='bundle.create':
            module('packages').verify_package(context.stage_dir/'output')
        if self.operation=='bundle.restore':
            _,manifests,report=module('packages')._verify(_path(self.parameters['root'],exists=True))
            expected=self.parameters['input_hashes']['package_sha256']
            if staged.get('package_sha256')!=expected or report['package_sha256']!=expected:
                fail('STALE','Staged or current package differs from admitted package; final publication refused.')
            restored=AssetLibrary(context.stage_dir/'output'/'library')
            if len(restored.list_references())!=len(manifests):fail('VERIFY','Restored library inventory differs.')
            for manifest in manifests:
                ref=dict(id=manifest['identity']['id'],version=manifest['version'],sha256=manifest['manifest_sha256'])
                if restored.resolve(ref)!=manifest:fail('VERIFY','Restored canonical version differs from original package.')
        root=context.stage_dir/'output'
        return dict(inventory=self._inventory(root))

    @staticmethod
    def _inventory(root):
        import hashlib
        result=[]
        for path in sorted(Path(root).rglob('*')):
            if path.is_symlink():fail('PATH','Staged output contains a symlink.')
            if path.is_file():
                data=read_source_bytes(path)
                result.append(dict(path=path.relative_to(root).as_posix(),sha256=hashlib.sha256(data).hexdigest(),bytes=len(data)))
                if len(result)>4096:fail('LIMIT','Output file inventory exceeds 4096 files.')
        if not result:fail('VERIFY','No verified output files exist.')
        return result

    def publish(self,context,staged,verified):
        context.check_cancelled()
        if self.operation in ('import','generate'):
            library=self._stage_library(context);ref=verified['reference'];manifest=library.resolve(ref)
            sources={file['path']:context.stage_dir/'payloads'/file['path'] for file in manifest['files']}
            try:result=self.owner.library.register(manifest,sources)
            except RigError as exc:
                if exc.report['code']!='LIBRARY_IMMUTABLE':raise
                self.owner.library.resolve(ref);result=ref
            return dict(reference=result,manifest_sha256=ref['sha256'])
        from atomic_publish import _rename_no_replace
        p=self.parameters
        if self.operation in ('preview','thumbnail','turntable'):
            parent=_path(p['cache_root']);parent.mkdir(mode=0o700,exist_ok=True)
            target=parent/staged['key'];source=context.stage_dir/'cache'/staged['key']
            if target.exists():
                module('preview').verify_preview(target,staged['key'])
            else:self._publish_copy(context,source,target)
            return dict(path=str(target),key=staged['key'])
        target=_path(p['index'] if self.operation=='index' else p['destination'])
        self._publish_copy(context,context.stage_dir/'output',target)
        return dict(path=str(target),inventory=verified['inventory'])

    @staticmethod
    def _publish_copy(context,source,target):
        # Keep verified staging intact across the publication/journal crash gap.
        # The exclusive rename helper is the same authority as native archives.
        import shutil,tempfile
        from atomic_publish import _rename_no_replace
        if not target.parent.is_dir():fail('PATH','Publication parent must already exist.')
        with tempfile.TemporaryDirectory(prefix='.asset-publish-',dir=target.parent) as temp:
            stage=Path(temp)/'output'
            shutil.copytree(source,stage)
            context.check_cancelled()
            if OperationAdapter._inventory(source)!=OperationAdapter._inventory(stage):fail('VERIFY','Publication copy differs from retained stage.')
            _rename_no_replace(stage,target)

    def recover(self,context,staged,verified):
        if self.operation in ('import','generate'):
            ref=verified['reference']
            try:self.owner.library.resolve(ref)
            except RigError as exc:
                if exc.report['code']=='LIBRARY_MISSING':return dict(status='unpublished')
                raise
            return dict(status='published',receipt=dict(reference=ref,manifest_sha256=ref['sha256']))
        p=self.parameters
        if self.operation in ('preview','thumbnail','turntable'):
            target=_path(p['cache_root'])/staged['key']
            if not target.exists():return dict(status='unpublished')
            module('preview').verify_preview(target,staged['key'])
            return dict(status='published',receipt=dict(path=str(target),key=staged['key']))
        target=_path(p['index'] if self.operation=='index' else p['destination'])
        if not target.exists():return dict(status='unpublished')
        if self._inventory(target)!=verified['inventory']:fail('CONFLICT','Existing destination differs; it was preserved.')
        return dict(status='published',receipt=dict(path=str(target),inventory=verified['inventory']))


class ScopedAssetOperations:
    """HTTP bridge with explicit intake/output roots and no executable overrides.

    Authentication/origin controls belong to the HTTP host. This bridge adds
    filesystem scope; the unrestricted local CLI facade must not be injected
    directly into a network request handler.
    """
    def __init__(self,owner: AssetOperations,*,intake_root,output_root):
        self.owner=owner;self.intake=_path(intake_root,exists=True);self.output=_path(output_root,exists=True)
        if not self.intake.is_dir() or not self.output.is_dir():fail('PATH','HTTP roots must be existing directories.')
        self._inside(owner.jobs_root,self.output)
        self.supported_operations=sorted(MANAGED)

    @staticmethod
    def _inside(value,root):
        path=_path(value)
        try:path.relative_to(root)
        except ValueError:fail('SCOPE','Request path lies outside its configured HTTP root.')
        return str(path)

    def submit(self,operation,parameters):
        p=request(parameters)
        if not isinstance(operation,str):fail('REQUEST','Operation must be a string.')
        if operation not in MANAGED:fail('UNAVAILABLE','No scoped managed adapter for '+str(operation)+'.')
        if 'project_ids' in p:
            p['project_ids']=self.owner._project_ids(p['project_ids'])
            if not set(p['project_ids'])<=set(self.owner.project_ids):
                fail('SCOPE','HTTP indexing may only inspect explicitly authorized project IDs.')
        if set(p)&{'blender','executable'}:fail('SCOPE','HTTP requests cannot select executables.')
        for name in ('source','license_file','root'):
            if name in p:p[name]=self._inside(p[name],self.intake)
        for name in ('destination','cache_root','index'):
            if name in p:p[name]=self._inside(p[name],self.output)
        if 'sources' in p:
            if not isinstance(p['sources'],dict):fail('REQUEST','Sources must be an explicit mapping.')
            p['sources']={k:self._inside(v,self.intake) for k,v in p['sources'].items()}
        if 'context_files' in p:
            if not isinstance(p['context_files'],dict):fail('REQUEST','Context files must be an explicit mapping.')
            p['context_files']={k:self._inside(v,self.intake) for k,v in p['context_files'].items()}
        return self.owner.submit(operation,p)

    def list_jobs(self):return self.owner.list_jobs()
    def job_status(self,identifier):return self.owner.job_status(identifier)
    def cancel_job(self,identifier):return self.owner.cancel_job(identifier)
