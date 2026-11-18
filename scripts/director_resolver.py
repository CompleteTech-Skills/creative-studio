"""Read-only Director asset resolution through the canonical AssetLibrary.

The registry remains the byte owner. This adapter does not import, generate,
change review state, select latest versions, or invoke a provider.
"""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time

from asset_library import AssetLibrary
from asset_library_contracts import MAX_MANIFEST_BYTES, load_manifest
from director_contracts import (_read_artifact, validate_document,
                                validate_method_request, validate_method_result)
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, decode_json, object_hash, require
from jsonschema import ValidationError
from rig_import import load_character_model
from rig_gltf import read_gltf
import numpy as np

MAX_CANDIDATES = 64


def _same_geometry(left, right):
    # Native GLB round-trips through float32. Keep topology and materials exact,
    # and bound positional round-trip error to one micrometre in native metres.
    a=sorted(left.meshes,key=lambda mesh:mesh.id)
    b=sorted(right.meshes,key=lambda mesh:mesh.id)
    return (object_hash(left.materials)==object_hash(right.materials) and len(a)==len(b) and
            all(x.id==y.id and x.material==y.material and
                x.positions.shape==y.positions.shape and x.triangles.shape==y.triangles.shape and
                np.array_equal(x.triangles,y.triangles) and
                np.allclose(x.positions,y.positions,rtol=0,atol=1e-6) for x,y in zip(a,b)))


def _budget(spec):
    return dict(max_provider_calls=0,max_cpu_seconds=0,max_gpu_seconds=0,
                max_storage_bytes=0,max_memory_bytes=spec['memory_budget'],
                currency=None,max_amount=None,policy_ref=None)


def _validate_ref(ref):
    # Put the ref through the canonical shape/path validator before using it.
    from jsonschema import Draft202012Validator
    from director_contracts import schemas
    schema=schemas()['director.schema.json']
    try: Draft202012Validator({'$defs':schema['$defs'],'$ref':'#/$defs/artifact-ref'}).validate(ref)
    except ValidationError as exc: raise RigError('DIRECTOR_RESOLVER_REFERENCE','Malformed exact artifact reference.') from exc
    return ref


def _read(root,ref):
    _validate_ref(ref)
    return _read_artifact(root,ref)


class AssetResolver:
    def __init__(self, library: AssetLibrary, project_root):
        self.library=library
        self.root=Path(project_root)

    def _candidate(self, spec, ref, licenses, fixtures):
        """Return truthful disposition, deterministic rank and reasons."""
        try:
            _validate_ref(ref)
            require(ref.get('kind')=='asset-library-version' and ref.get('byte_size',MAX_MANIFEST_BYTES+1)<=MAX_MANIFEST_BYTES,
                    'DIRECTOR_RESOLVER_REFERENCE','Expected a bounded asset-library manifest reference.')
            manifest=load_manifest(_read(self.root,ref))
            require(ref['artifact_id']==manifest['identity']['id'] and ref['semantic_version']==manifest['version'] and
                    ref['schema_version']==manifest['schema_version'],'DIRECTOR_RESOLVER_REFERENCE',
                    'ArtifactRef identity/version differs from the actual manifest.')
            require(ref['fixture']==manifest['provenance']['fixture'],
                    'DIRECTOR_RESOLVER_REFERENCE','ArtifactRef must preserve native fixture classification.')
            exact=dict(id=manifest['identity']['id'],version=manifest['version'],sha256=manifest['manifest_sha256'])
            current=self.library.resolve(exact)
            require(current==manifest,'DIRECTOR_RESOLVER_STALE','Library differs from the pinned manifest.')
            reasons=[];score=0
            if manifest['identity']['id']!=spec['logical_id']:
                return 'incompatible',0,['Persistent identity differs from the requested logical identity.'],[]
            score+=100
            if manifest['identity']['kind']!=spec['asset_type']:
                return 'incompatible',score,['Asset type differs from the specification.'],[]
            score+=20
            license_=manifest['metadata']['license']
            if license_ not in licenses or license_!=spec['licensing']:
                return 'incompatible',score,['License is not admitted by both request policy and specification.'],[]
            score+=20
            scale_change=manifest['compatibility']['unit_scale']!=spec['scale_meters']
            score+=0 if scale_change else 10
            payloads=[f for f in manifest['files'] if f['role']=='geometry' and f['format']=='glb']
            if not payloads:
                return 'unavailable',score,['No independently decodable self-contained GLB geometry payload is registered.'],[]
            payload=payloads[0]
            raw=self.library.read_file(exact,payload['path'])
            if spec['memory_budget']<=0 or len(raw)>spec['memory_budget']:
                return 'incompatible',score,['Source exceeds the explicit memory budget.'],[]
            with tempfile.TemporaryDirectory(prefix='director-resolve-') as directory:
                path=Path(directory)/'source.glb';path.write_bytes(raw)
                limits={**DEFAULT_LIMITS,'triangles':min(DEFAULT_LIMITS['triangles'],spec['polygon_budget']),
                        'memory_bytes':min(DEFAULT_LIMITS['memory_bytes'],spec['memory_budget'])}
                imported=load_character_model(path,limits=limits)
            actual={'mesh'}
            # A native exported rig is a distinct registered payload. Decode it;
            # declaration in compatibility metadata never establishes a skeleton.
            rigfiles=[f for f in manifest['files'] if f['role']=='rig' and f['format']=='glb']
            if rigfiles:
                with tempfile.TemporaryDirectory(prefix='director-rig-resolve-') as directory:
                    path=Path(directory)/'rig.glb';path.write_bytes(self.library.read_file(exact,rigfiles[0]['path']))
                    asset=read_gltf(path,limits=limits).json.get('asset')
                    extras=asset.get('extras') if isinstance(asset,dict) else None
                    binding=extras.get('hatchRig') if isinstance(extras,dict) else None
                    require(isinstance(binding,dict),'DIRECTOR_RESOLVER_RIG_BINDING',
                            'Native rig export requires an object source binding.')
                    rig=load_character_model(path,limits=limits)
                require(binding.get('id')==manifest['identity']['id'] and
                        binding.get('source_sha256')==payload['sha256'] and
                        _same_geometry(rig.geometry,imported.geometry),
                        'DIRECTOR_RESOLVER_RIG_BINDING','Rig must bind the exact registered source geometry and identity.')
                if rig.skeleton and rig.weights:actual.update(('rig','skin'))
                if rig.clips:actual.add('animation')
            required=set(spec['capabilities'])
            if not required<=actual:
                return 'incompatible',score,[f'Unimplemented or absent native capabilities: {sorted(required-actual)}.'],[]
            materials={m.get('name','') for m in imported.geometry.materials}
            if not set(spec['materials'])<=materials:
                return 'incompatible',score,['Required material identity is absent.'],[]
            if spec['textures']:
                return 'incompatible',score,['Texture identity requirements have no supported matching adapter.'],[]
            score+=10*len(required)
            conversions=[]
            if scale_change:conversions.append('scale')
            if spec['formats'] and 'glb' not in spec['formats']:conversions.append('format')
            # Structural inspection cannot establish requested appearance or an
            # artistic approval. Preserve that disposition even when conversion
            # is also needed; adaptation must never bypass the review policy.
            if spec['visual_characteristics']:
                return 'unreviewed',score,['Requested visual characteristics require authoritative visual evidence.'],conversions
            if manifest['provenance']['fixture']:
                if not fixtures:
                    return 'unreviewed',score,['Procedural engineering fixture has no production-art approval.'],conversions
                reasons.append('Explicit engineering-fixtures policy admits technical reuse; artistic approval remains absent.')
            else:
                return 'unreviewed',score,['No authoritative current visual approval adapter is available for this asset.'],conversions
            if conversions:
                return 'adaptable',score,reasons+['Exact geometry is verified; explicit native conversion is required.'],conversions
            reasons.extend(['Exact immutable version, license, bytes, geometry and requested capabilities verified.',
                            'No generation, import, purchase or source mutation was performed.'])
            return 'reusable',score,reasons,[]
        except (RigError,OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
            code=exc.report['code'] if isinstance(exc,RigError) else type(exc).__name__
            return 'unavailable',0,['Candidate cannot be independently verified: '+code+'.'],[]

    def resolve(self, request):
        validate_method_request('AssetResolver.resolve',request)
        started=time.process_time()
        data=request['input']
        require(data['reuse_policy'] in ('require-reviewed','engineering-fixtures'),
                'DIRECTOR_RESOLVER_POLICY','Unknown reuse policy; select require-reviewed or engineering-fixtures.')
        licenses=data['license_policy'].split(',')
        require(0<len(licenses)<=16 and all(x and x.strip()==x and len(x)<=128 for x in licenses),
                'DIRECTOR_RESOLVER_POLICY','License policy must list exact comma-separated license IDs.')
        snapshot=decode_json(_read(self.root,data['registry_snapshot']))
        require(isinstance(snapshot,dict) and set(snapshot)=={'schema_version','kind','candidates'} and
                snapshot['schema_version']==1 and snapshot['kind']=='director-registry-snapshot' and
                isinstance(snapshot['candidates'],list) and len(snapshot['candidates'])<=MAX_CANDIDATES,
                'DIRECTOR_RESOLVER_REGISTRY','Expected a bounded exact registry snapshot.')
        registry=snapshot['candidates']
        for ref in registry:
            require(isinstance(ref,dict),'DIRECTOR_RESOLVER_REGISTRY','Candidate must be an ArtifactRef.')
            _validate_ref(ref)
        require(len({object_hash(ref) for ref in registry})==len(registry),
                'DIRECTOR_RESOLVER_REGISTRY','Registry contains duplicate candidate references.')
        requirements=[]
        for spec_ref in data['specs']:
            spec=validate_document(decode_json(_read(self.root,spec_ref)))
            require(spec['kind']=='director-asset-spec' and spec_ref['artifact_id']==spec['id'] and
                    spec_ref['semantic_version']==spec['semantic_version'] and spec_ref['schema_version']==spec['schema_version'],
                    'DIRECTOR_RESOLVER_SPEC','Expected an exact Director asset specification reference.')
            wanted=spec['candidates'] or registry
            require(all(ref in registry for ref in wanted),'DIRECTOR_RESOLVER_REGISTRY','Candidate is outside the pinned registry snapshot.')
            ranked=[]
            for ref in wanted:
                disposition,score,reasons,conversions=self._candidate(spec,ref,licenses,data['reuse_policy']=='engineering-fixtures')
                ranked.append((disposition,score,reasons,conversions,ref))
            priority={'reusable':0,'adaptable':1,'unreviewed':2,'incompatible':3,'unavailable':4}
            ranked.sort(key=lambda x:(x[4]['artifact_id']!=spec['logical_id'],
                                      priority[x[0]],len(x[3]),-x[1],object_hash(x[4])))
            if ranked:
                top=ranked[0];state,reasons,conversions=top[0],top[2],top[3]
                refs=[deepcopy(x[4]) for x in ranked]
            else: state,reasons,conversions,refs='missing',['No exact candidate is present in the pinned registry.'],[],[]
            requirements.append(dict(spec=deepcopy(spec_ref),disposition=state,candidates=refs,
                reasons=reasons,conversions=conversions,capabilities=deepcopy(spec['capabilities']),budget=_budget(spec)))
        unknown=lambda:dict(value=None,explanation='Not measured by this read-only resolver.')
        usage=dict(classification='actual',provider_calls=dict(value=0,explanation=None),
            input_tokens=unknown(),output_tokens=unknown(),cpu_seconds=dict(value=time.process_time()-started,explanation=None),
            gpu_seconds=dict(value=0,explanation=None),storage_bytes=unknown(),currency=None,amount=unknown(),
            measurement_source='Local process CPU clock; zero provider calls and no GPU execution.')
        result=dict(schema_version=1,kind='director-result',method='AssetResolver.resolve',status='succeeded',
            operation_id=request['context']['request_id'],input_digest=object_hash(request),value=dict(requirements=requirements),
            artifacts=[],diagnostics=[],usage=usage,provenance_receipts=[])
        return validate_method_result('AssetResolver.resolve',result,request)
