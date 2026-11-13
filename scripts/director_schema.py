"""Authoritative, additive Director v1 data and method contracts.

Only integration records live here: native meshes, rigs, scenes and render jobs
retain their owner's formats. No optional numerical or rendering imports.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import re

from rig_contracts import COORDINATES, arr, obj, ref

VERSION = 1
ID = dict(type='string', pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(?![\s\S])')
HASH = dict(type='string', pattern=r'^[0-9a-f]{64}(?![\s\S])')
SEMVER = dict(type='string', pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?![\s\S])')
TEXT = dict(type='string', minLength=1, maxLength=4096)
INTEGER = dict(type='integer', minimum=0, maximum=2_000_000_000)
NUMBER = dict(type='number', minimum=0, maximum=1e15)
STATUSES = ['succeeded', 'partial', 'blocked_capability', 'blocked_budget',
            'awaiting_external_result', 'awaiting_review', 'failed', 'canceled']
MILESTONES = ['planning', 'assets', 'scene', 'motion', 'rendering', 'video',
              'native_editability', 'visual_qa', 'delivery']


def nullable(value):
    return {'anyOf': [value, {'type': 'null'}]}


def document(name, fields):
    return obj(dict(schema_version={'const': VERSION}, kind={'const': 'director-' + name},
                    id=ID, **{'semantic_version':SEMVER, **fields}))


# The value identifies both the typed request/response and its permitted effects.
# Multiple methods share a shape only when their state-transition envelope agrees.
METHODS = {
    'CapabilityRegistry.probe': ('probe', 'capability-snapshot', 'read-only; requested local probes only'),
    'PlanningProvider.generate_structured': ('planning', 'planning-artifact', 'configured planning invocation only'),
    'AssetResolver.resolve': ('resolve-assets', 'resolution-plan', 'read-only'),
    'AcquisitionGateway.prepare': ('acquisition', 'acquisition-state', 'delegate native acquisition preparation'),
    'AcquisitionGateway.claim': ('acquisition', 'acquisition-state', 'delegate native acquisition claim'),
    'AcquisitionGateway.ingest': ('acquisition', 'acquisition-state', 'verify and delegate native ingestion'),
    'AcquisitionGateway.fail': ('acquisition', 'acquisition-state', 'preserve native failure evidence'),
    'AcquisitionGateway.reconcile': ('acquisition', 'acquisition-state', 'reconcile the existing native claim'),
    'AssetBuildAdapter.build': ('asset-build', 'asset-build-result', 'owned staging and immutable promotion'),
    'RigMotionAdapter.prepare': ('rig-motion', 'rig-motion-bundle', 'versioned derivatives; preserve sources'),
    'SceneAssembler.assemble': ('scene-assembly', 'scene-bundle', 'new native scene revision; no rendering'),
    'FrameEvaluator.evaluate': ('evaluate-frame', 'evaluated-frame', 'pure evaluation'),
    'GraphCompiler.compile': ('compile', 'execution-graph', 'read-only'),
    'BudgetLedger.reserve': ('reservation', 'budget-receipt', 'transactional reservation'),
    'BudgetLedger.settle': ('settlement', 'budget-receipt', 'transactional actual-usage settlement'),
    'BudgetLedger.release': ('release', 'budget-receipt', 'transactional release; preserve uncertain reservations'),
    'ExecutionService.submit': ('submit', 'job-snapshot', 'delegate native jobs and persist queued state'),
    'ExecutionService.status': ('job', 'job-snapshot', 'read-only'),
    'ExecutionService.resume': ('resume', 'job-snapshot', 'verify checkpoints and resume native jobs'),
    'ExecutionService.cancel': ('job', 'job-snapshot', 'bounded native cancellation; preserve outputs'),
    'RenderAdapter.preflight': ('render', 'preflight-report', 'read-only'),
    'RenderAdapter.submit': ('render', 'job-snapshot', 'delegate authoritative render job'),
    'RenderAdapter.status': ('job', 'job-snapshot', 'read-only'),
    'RenderAdapter.cancel': ('job', 'job-snapshot', 'delegate native cancellation'),
    'RenderAdapter.verify': ('render-verification', 'verified-render-result', 'independent output inspection'),
    'ExportService.export': ('export', 'export-receipt', 'owned staging and atomic publication'),
    'ExportService.verify': ('export-verification', 'export-receipt', 'independent reopen/reimport/decode'),
    'QAService.validate': ('qa', 'qa-report', 'append evidence; no artistic approval'),
    'QAService.record_review': ('review', 'review-receipt', 'append hash-bound review; preserve history'),
    'RevisionService.plan': ('revision-plan', 'revision-plan', 'read-only'),
    'RevisionService.apply': ('revision-apply', 'project-revision', 'atomic revision with stale-write rejection'),
    'RevisionService.impact': ('revision-impact', 'impact-report', 'read-only'),
}


@lru_cache(maxsize=1)
def _definitions():
    texts, ids, artifacts = arr(TEXT, 0, 256), arr(ID, 0, 256), arr(ref('artifact-ref'), 0, 256)
    num = dict(type='number', minimum=-1e9, maximum=1e9)
    positive = dict(type='number', exclusiveMinimum=0, maximum=1e9)
    fps = obj(dict(numerator=dict(type='integer', minimum=1, maximum=1_000_000),
                   denominator=dict(type='integer', minimum=1, maximum=1_000_000)))
    rational = obj(dict(numerator=INTEGER, denominator=dict(type='integer', minimum=1, maximum=1_000_000)))
    interval = obj(dict(start_frame=INTEGER, end_frame=INTEGER))
    size = arr(dict(type='integer', minimum=1, maximum=32768), 2, 2)
    coordinates = obj({key: {'const': value} for key, value in COORDINATES.items()})
    # Same portable component restrictions as Model3D and the native package owner.
    from decomposition_package import _RESERVED
    end=r'(?![\s\S])'
    reserved='|'.join(''.join('['+c.lower()+c.upper()+']' if c.isalpha() else re.escape(c)
                            for c in word) for word in sorted(_RESERVED))
    component=(r'(?!(?:'+reserved+r')(?:[./]|'+end+r'))'
               r'[^/\\:<>"|?*\x00-\x1f\x7f\ud800-\udfff]*'
               r'[^/\\:<>"|?*\x00-\x1f\x7f\ud800-\udfff .]')
    path=dict(type='string',minLength=1,maxLength=1024,pattern='^'+component+'(?:/'+component+')*'+end)
    version = dict(type='string', pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?![\s\S])')
    file_ref = obj(dict(artifact_id=ID, kind=ID, schema_version=dict(type='integer', minimum=1, maximum=999),
                        semantic_version=version, path=path, sha256=HASH,
                        byte_size=dict(type='integer', minimum=0, maximum=256*1024**2),
                        media_type=dict(type='string', minLength=1, maxLength=128),
                        provenance_ref=nullable(ref('artifact-ref')), fixture=dict(type='boolean'),
                        dependencies=arr(ref('artifact-ref'), 0, 128)))
    diagnostic = obj(dict(code=ID, severity={'enum': ['info', 'warning', 'error']}, stage=ID,
                          entity_id=nullable(ID), field_path=TEXT, expected=nullable(TEXT),
                          observed=nullable(TEXT), retryable=dict(type='boolean'),
                          recommended_action=TEXT, evidence=artifacts))
    measurement = obj(dict(value=nullable(NUMBER), explanation=nullable(TEXT)))
    usage = obj(dict(classification={'enum': ['actual', 'estimated', 'unavailable']},
                     provider_calls=measurement, input_tokens=measurement, output_tokens=measurement,
                     cpu_seconds=measurement, gpu_seconds=measurement, storage_bytes=measurement,
                     currency=nullable(dict(type='string', pattern=r'^[A-Z]{3}(?![\s\S])')),
                     amount=measurement, measurement_source=TEXT))
    policy = obj(dict(network={'enum': ['deny', 'allow-configured']}, providers=ids,
                      paid={'enum': ['deny', 'bounded', 'allow-unknown-price']},
                      policy_ref=nullable(ref('artifact-ref'))))
    context = obj(dict(project_id=ID, project_revision_sha256=HASH, request_id=ID, idempotency_key=ID,
                       attempt_id=ID, capability_snapshot_sha256=HASH, budget_reservation_id=nullable(ID),
                       cancellation_token=ID, deadline=dict(type='string', format='date-time', maxLength=64),
                       policy=policy))
    disposition = {'enum': ['reusable', 'adaptable', 'missing', 'incompatible', 'unreviewed', 'unavailable']}
    review_state = {'enum': ['unreviewed', 'approved', 'rejected', 'stale', 'not-applicable']}
    status = {'enum': STATUSES}
    requirements = obj(dict(explicit=texts, inferred=texts, locked=texts, ambiguities=texts, assumptions=texts))
    budget = obj(dict(max_provider_calls=INTEGER, max_cpu_seconds=NUMBER, max_gpu_seconds=NUMBER,
                      max_storage_bytes=INTEGER, max_memory_bytes=INTEGER,
                      currency=nullable(TEXT), max_amount=nullable(NUMBER), policy_ref=nullable(ref('artifact-ref'))))
    refs = lambda **extra: dict(artifacts=artifacts, **extra)
    fields = {
        'creative-brief': dict(project_id=ID, request_id=ID, original_prompt=TEXT, requirements=requirements,
            duration=rational, frame_rate=fps, resolution=size, aspect_ratio=fps,
            characters=ids, object_count=INTEGER, locations=texts, actions=texts, mood=TEXT, style=TEXT,
            content_restrictions=texts, outputs=ids, budget=budget, references=artifacts, provenance=artifacts),
        'story-plan': dict(brief=ref('artifact-ref'), logline=TEXT, premise=TEXT, beginning=TEXT, middle=TEXT,
            end=TEXT, emotional_progression=texts, objectives=texts, discoveries=texts, spatial_continuity=texts,
            motifs=texts, establishing_information=texts, outcome=TEXT, silent=dict(type='boolean'),
            communication_methods=texts, beats=arr(obj(dict(id=ID, description=TEXT, actions=texts,
                composition=texts)), 1, 256)),
        'shot-plan': dict(story=ref('artifact-ref'), duration_frames=INTEGER, frame_rate=fps,
            shots=arr(obj(dict(id=ID, scene_id=ID, beat_ids=ids, start_frame=INTEGER, end_frame=INTEGER,
                duration_frames=INTEGER, camera=ref('artifact-ref'), actors=ids, props=ids, actions=texts,
                markers=arr(obj(dict(id=ID, frame=INTEGER, target=ID, action=TEXT)), 0, 256),
                environment=ref('artifact-ref'), lighting=ref('artifact-ref'), composition=texts,
                entry_constraints=texts, exit_constraints=texts,
                transition=obj(dict(kind={'enum': ['cut', 'dissolve']}, overlap_frames=INTEGER)),
                required_assets=artifacts, dependencies=artifacts, render_settings=ref('artifact-ref'),
                qa_requirements=texts, review=review_state)), 1, 256)),
        'asset-spec': dict(logical_id=ID, asset_type=ID, semantic_role=TEXT, visual_characteristics=texts,
            capabilities=ids, source_policy=policy, candidates=artifacts, formats=ids, scale_meters=positive,
            coordinates=coordinates, materials=texts, textures=texts, polygon_budget=INTEGER,
            memory_budget=INTEGER, licensing=TEXT, attribution=TEXT, dependencies=artifacts,
            review=review_state, substitutions=texts, generation_required=dict(type='boolean')),
        'character-identity': dict(character_id=ID, semantic_version=version, canonical_design=ref('artifact-ref'),
            geometry=TEXT, silhouette=TEXT, proportions=arr(obj(dict(name=ID, ratio=positive)), 0, 128),
            scale_meters=positive, palette=texts, materials=artifacts, markings=texts, asymmetries=texts,
            costume=texts, accessories=texts, rig=nullable(ref('artifact-ref')),
            bone_mapping=arr(obj(dict(role=ID, bone=ID)), 0, 256), animation_capabilities=ids,
            expression_capabilities=ids, viewpoints=ids, provenance=artifacts,
            approved_variations=texts, forbidden_transformations=texts),
        'project': dict(original_request=ref('artifact-ref'), brief=ref('artifact-ref'), story=ref('artifact-ref'),
            shot_plan=ref('artifact-ref'), asset_registry=ref('artifact-ref'), characters=artifacts,
            scenes=artifacts, timelines=artifacts, render_configuration=ref('artifact-ref'),
            execution_graph=nullable(ref('artifact-ref')), budget_ledger=nullable(ref('artifact-ref')),
            acquisition_bindings=artifacts, dependencies=artifacts, reviews=artifacts, exports=artifacts,
            inventory=artifacts, status={'enum': ['draft', 'blocked', 'in-progress', 'complete']},
            milestones=arr(ref('milestone'), 9, 9), coordinates=coordinates),
    }
    milestone = obj(dict(name={'enum': MILESTONES}, status={'enum': ['unverified', 'blocked', 'passed', 'failed']},
                         evidence=artifacts, diagnostics=arr(diagnostic, 0, 128),
                         authority={'enum': ['automated', 'assistant', 'human', 'external']},
                         scope=TEXT))
    inputs = {
        'probe': dict(operations=ids, local_probes=ids, provider_configuration_refs=artifacts),
        'planning': dict(stage={'enum': ['brief', 'story', 'shots', 'revision']}, original_prompt=TEXT,
                        context_refs=artifacts, output_schema_id=ID, output_schema_version={'const': 1},
                        locked_constraints=texts, template_sha256=HASH, max_repairs=dict(type='integer',minimum=0,maximum=3)),
        'resolve-assets': dict(specs=artifacts, registry_snapshot=ref('artifact-ref'), reuse_policy=TEXT, license_policy=TEXT),
        'acquisition': dict(spec=ref('artifact-ref'), input_sha256=HASH, binding_id=nullable(ID), claim_id=nullable(ID),
                            expected_state=ID, provider_request_id=nullable(ID), candidates=artifacts,
                            diagnostics=arr(diagnostic,0,128)),
        'asset-build': dict(spec=ref('artifact-ref'), inputs=artifacts, capability=ID, provider=nullable(ID),
                            seed=nullable(INTEGER), limits=budget, acquisition_binding=ref('artifact-ref')),
        'rig-motion': dict(mesh=ref('artifact-ref'), identity=ref('artifact-ref'), required_anatomy=ids,
                            actions=ids, landmarks=artifacts, supplied_rig=nullable(ref('artifact-ref')),
                            motions=artifacts, contact_requirements=texts),
        'scene-assembly': dict(shot_plan=ref('artifact-ref'), story_plan=ref('artifact-ref'), assets=artifacts,
                              rig_motion=artifacts, layout_requirements=texts, look_requirements=texts),
        'evaluate-frame': dict(scene=ref('artifact-ref'), shot=ref('artifact-ref'), frame=rational,
                               frame_rate=fps, dependency_snapshot=ref('artifact-ref')),
        'compile': dict(plan=ref('artifact-ref'), capabilities=ref('artifact-ref'), resolution=ref('artifact-ref'), budget=budget),
        'reservation': dict(operation=ID, estimated_bound=usage, retry_allowance=INTEGER,
                            rate_evidence=artifacts, expected_ledger_revision=HASH),
        'settlement': dict(reservation_id=ID, actual_usage=usage, expected_ledger_revision=HASH,
                           outcome={'enum':['complete','failed','uncertain']}, evidence=artifacts),
        'release': dict(reservation_id=ID, expected_ledger_revision=HASH,
                        outcome={'enum':['complete','failed']}, evidence=artifacts),
        'submit': dict(graph=ref('artifact-ref')),
        'job': dict(job_id=ID, expected_revision=HASH, request_id=ID),
        'resume': dict(job_id=ID, expected_revision=HASH, request_id=ID, dependency_snapshot=ref('artifact-ref'),
                       ownership_evidence=artifacts),
        'render': dict(scene_snapshot=ref('artifact-ref'), shot=ref('artifact-ref'), frame_ranges=arr(interval,1,256),
                       frame_rate=fps, evaluated_state_schema=ID, engine=ID, device_policy=TEXT,
                       settings=ref('artifact-ref'), budget=ref('artifact-ref')),
        'render-verification': dict(job=ref('artifact-ref'), scene_snapshot=ref('artifact-ref'), frames=artifacts,
                                    frame_range=interval, frame_rate=fps, resolution=size),
        'export': dict(sources=artifacts, target={'enum':['native_blend','glb','gltf','png_sequence','mp4','webm','archive']},
                       codec=nullable(ID), options=arr(obj(dict(name=ID,value=TEXT)),0,128), verification=artifacts),
        'export-verification': dict(export=ref('artifact-ref'), sources=artifacts, expected_frames=nullable(INTEGER),
                                    frame_rate=nullable(fps), zero_audio_streams=dict(type='boolean')),
        'qa': dict(artifacts=artifacts, check_profile=ref('artifact-ref'),
                    thresholds=arr(obj(dict(name=ID,value=NUMBER,unit=TEXT)),0,128)),
        'review': dict(reviewer_id=ID, authority={'enum':['assistant','human','external']}, scope=texts,
                        decisions=arr(obj(dict(dimension=ID,verdict={'enum':['approved','rejected','unreviewed']},
                                              rationale=TEXT)),1,128), evidence=artifacts),
        'revision-plan': dict(instruction=TEXT, current_project=ref('artifact-ref'), locked_constraints=texts,
                              budget=ref('artifact-ref'), capabilities=ref('artifact-ref')),
        'revision-apply': dict(patch=ref('artifact-ref'), expected_revision=HASH, authorization=ref('artifact-ref')),
        'revision-impact': dict(patch=ref('artifact-ref'), graph=ref('artifact-ref')),
    }
    outputs = {
        'capability-snapshot': dict(capabilities=arr(obj(dict(operation=ID, version=nullable(TEXT),
            status={'enum':['available','unavailable','untested']}, limits=budget,
            probe_evidence=artifacts, execution_verified=dict(type='boolean'))),0,256),
            expires_at=dict(type='string',format='date-time'), digest=HASH),
        'planning-artifact': dict(content=ref('artifact-ref'), assumptions=texts, provider=nullable(ID),
            model=nullable(TEXT), template_sha256=HASH, raw_response=ref('artifact-ref'), repair_history=artifacts),
        'resolution-plan': dict(requirements=arr(obj(dict(spec=ref('artifact-ref'),disposition=disposition,
            candidates=artifacts,reasons=texts,conversions=texts,capabilities=ids,budget=budget)),0,256)),
        'acquisition-state': dict(binding=ref('artifact-ref'),claim_id=nullable(ID),native_state=ID,
            provider_request_id=nullable(ID),outcome={'enum':['known','uncertain']},receipt=nullable(ref('artifact-ref'))),
        'asset-build-result': refs(meshes=artifacts,materials=artifacts,bounds=obj(dict(minimum=arr(num,3,3),maximum=arr(num,3,3))),
            vertices=INTEGER,triangles=INTEGER,coordinates=coordinates,uncertainty=texts,
            origin={'enum':['imported','generated','procedural-fixture','authored']},backend_receipt=ref('artifact-ref')),
        'rig-motion-bundle': refs(rig=ref('artifact-ref'),bindings=artifacts,clips=artifacts,
            deformation_checks=artifacts,unsupported_actions=ids,contact_diagnostics=arr(diagnostic,0,128)),
        'scene-bundle': refs(scenes=artifacts,node_ids=ids,actor_ids=ids,cameras=artifacts,lights=artifacts,
            materials=artifacts,timelines=artifacts,spatial_feasibility=ref('artifact-ref'),native_exports=artifacts),
        'evaluated-frame': dict(scene=ref('artifact-ref'),frame=rational,node_transforms=artifacts,
            bone_matrices=artifacts,skinning_matrices=artifacts,morph_weights=artifacts,camera_states=artifacts,
            light_states=artifacts,material_states=artifacts,contacts=artifacts,prop_ownership=artifacts,
            active_clips=artifacts,diagnostics=arr(diagnostic,0,128),digest=HASH),
        'execution-graph': dict(nodes=arr(obj(dict(id=ID,operation={'enum':list(METHODS)},inputs=artifacts,
            output_schema=ID,resource_class=ID,cache_key=HASH,gates=ids,native_owner=ID)),1,1024),
            edges=arr(obj(dict(source=ID,target=ID,artifact=ref('artifact-ref'))),0,4096),
            critical_path_seconds=nullable(NUMBER)),
        'budget-receipt': dict(reservation_id=ID,ledger_revision=HASH,remaining=budget,
            state={'enum':['reserved','settled','released','uncertain']},evidence=artifacts),
        'job-snapshot': dict(job_id=ID,revision=HASH,state={'enum':['queued','running','partial','blocked','failed','canceled','completed']},
            nodes=artifacts,attempts=INTEGER,native_workers=artifacts,claims=artifacts,progress=nullable(NUMBER),
            reservations=ids,diagnostics=arr(diagnostic,0,128),artifacts=artifacts),
        'preflight-report': dict(ready=dict(type='boolean'),missing_capabilities=ids,diagnostics=arr(diagnostic,0,128),evidence=artifacts),
        'verified-render-result': dict(scene_snapshot=ref('artifact-ref'),backend=ref('artifact-ref'),
            frame_range=interval,frame_rate=fps,resolution=size,
            frames=arr(obj(dict(index=INTEGER,timestamp=rational,resolution=size,artifact=ref('artifact-ref'),
                decode_evidence=ref('artifact-ref'),dependency_snapshot=ref('artifact-ref'))),1,100000)),
        'export-receipt': dict(target={'enum':['native_blend','glb','gltf','png_sequence','mp4','webm','archive']},
            outputs=artifacts,format_losses=texts,verification=artifacts,verified=dict(type='boolean'),
            decoded_frames=nullable(INTEGER),audio_streams=nullable(INTEGER)),
        'qa-report': dict(measured=arr(obj(dict(name=ID,value=NUMBER,unit=TEXT,evidence=artifacts)),0,128),
            unmeasured=texts,findings=arr(diagnostic,0,128),status={'enum':['passed','failed','partial','unverified']}),
        'review-receipt': inputs['review'],
        'revision-plan': dict(patch=ref('artifact-ref'),changed_ids=ids,assumptions=texts,diagnostics=arr(diagnostic,0,128)),
        'project-revision': dict(project=ref('artifact-ref'),parent=ref('artifact-ref'),changed_ids=ids,
            stale_descendants=artifacts,preserved_outputs=artifacts,diagnostics=arr(diagnostic,0,128)),
        'impact-report': dict(changed_ids=ids,stale_descendants=artifacts,preserved_outputs=artifacts,
            diagnostics=arr(diagnostic,0,128)),
    }
    defs = dict(**{'artifact-ref':file_ref, 'operation-context':context,'diagnostic':diagnostic,'usage':usage,
                    'fps':fps,'rational':rational,'milestone':milestone,'budget':budget})
    defs.update({name:document(name, properties) for name,properties in fields.items()})
    defs.update({'input-'+name:obj(properties) for name,properties in inputs.items()})
    defs.update({'output-'+name:obj(properties) for name,properties in outputs.items()})
    requests, results = [], []
    for method,(request,response,effects) in METHODS.items():
        key=method.replace('.','-')
        defs['request-'+key] = obj(dict(schema_version={'const':1},kind={'const':'director-request'},
            method={'const':method},context=ref('operation-context'),input=ref('input-'+request)))
        defs['result-'+key] = obj(dict(schema_version={'const':1},kind={'const':'director-result'},
            method={'const':method},status=status,operation_id=ID,input_digest=HASH,
            value=nullable(ref('output-'+response)),artifacts=artifacts,
            diagnostics=arr(diagnostic,0,128),usage=usage,provenance_receipts=artifacts))
        requests.append(ref('request-'+key));results.append(ref('result-'+key))
    defs['request']={'oneOf':requests};defs['result']={'oneOf':results}
    return defs


DOCUMENTS = ['creative-brief','story-plan','shot-plan','asset-spec','character-identity','project','request','result']


def definitions():
    """Return isolated definitions; caller mutation never changes admission."""
    return deepcopy(_definitions())


def schemas():
    return {'director.schema.json':dict(
        **{'$schema':'https://json-schema.org/draft/2020-12/schema'},
        title='Creative Studio Director v1 contracts',
        description='Structural admission only; runtime semantics and exact-byte closure are separate checks.',
        **{'$defs':definitions(), 'oneOf':[ref(name) for name in DOCUMENTS]})}
