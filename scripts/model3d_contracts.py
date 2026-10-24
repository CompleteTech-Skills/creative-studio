"""Additive Model3D v1 contracts; schema discovery imports no numeric backend.

These contracts describe intent and exact payload references. Validation never
turns a declared provider result, job state, license claim or review into evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import math
import re
import unicodedata

from rig_contracts import COORDINATES, obj, arr, ref
from rig_errors import RigError, finite_json, portable_name, require

VERSION = '1.0.0'
IDENTIFIER_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(?![\s\S])'
LIMITS = dict(file_bytes=256 * 1024**2, total_bytes=1024**3, files=4096,
              vertices=1_000_000, triangles=2_000_000, parts=4096,
              dependency_depth=16, memory_bytes=2 * 1024**3)
KINDS = {name: 'model3d-' + name for name in
         ('request', 'source', 'asset', 'part', 'material', 'uncertainty', 'job', 'export',
          'provider-receipt', 'processing-stage', 'resource-usage', 'validation-result',
          'review-decision', 'repair-operation', 'archive')}


def _document(kind, fields, optional=()):
    fields = dict(schema_version={'const': 1}, kind={'const': KINDS[kind]}, **fields)
    fields.setdefault('extensions', arr(obj(dict(namespace=dict(type='string', pattern=IDENTIFIER_PATTERN),
                                              payload=ref('file'))), 0, 32))
    return obj(fields, [key for key in fields if key not in optional and key != 'extensions'])


@lru_cache(maxsize=1)
def _definitions():
    text = dict(type='string', minLength=1, maxLength=4096)
    short = dict(type='string', minLength=1, maxLength=128)
    identifier = dict(type='string', pattern=IDENTIFIER_PATTERN)
    version = dict(type='string', maxLength=64, pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?![\s\S])')
    digest = dict(type='string', pattern=r'^[0-9a-f]{64}(?![\s\S])')
    number = dict(type='number', minimum=-1e9, maximum=1e9)
    positive = dict(type='number', exclusiveMinimum=0, maximum=1e9)
    fraction = dict(type='number', minimum=0, maximum=1)
    integer = dict(type='integer', minimum=0, maximum=2_000_000_000)
    boolean = dict(type='boolean')
    nullable_text = {'anyOf': [text, {'type': 'null'}]}
    nullable_file = {'anyOf': [ref('file'), {'type': 'null'}]}
    review_state = {'enum': ['pending', 'approved', 'rejected', 'not-assessed']}
    evidence_class = {'enum': ['observed', 'inferred', 'synthetic', 'unknown']}
    dimensions = {'anyOf': [arr(dict(type='integer', minimum=1, maximum=32768), 2, 2), {'type': 'null'}]}
    timestamp = {'anyOf': [dict(type='string', format='date-time', maxLength=64), {'type': 'null'}]}
    history = arr(obj(dict(operation=identifier, evidence=ref('file'))), 0, 128)
    vec = arr(number, 3, 3)
    from decomposition_package import _RESERVED
    end = r'(?![\s\S])'
    reserved = '|'.join(''.join('[' + c.lower() + c.upper() + ']' if c.isalpha() else re.escape(c)
                                for c in word) for word in sorted(_RESERVED))
    component = (r'(?!(?:' + reserved + r')(?:[./]|' + end + r'))'
                 r'[^/\\:<>"|?*\x00-\x1f\x7f\ud800-\udfff]*'
                 r'[^/\\:<>"|?*\x00-\x1f\x7f\ud800-\udfff .]')
    path_pattern = '^' + component + '(?:/' + component + ')*' + end
    file = obj(dict(path=dict(type='string', minLength=1, maxLength=1024, pattern=path_pattern),
                    sha256=digest, bytes=dict(type='integer', minimum=1, maximum=LIMITS['file_bytes']),
                    media_type={'enum': ['model/gltf-binary', 'model/gltf+json', 'model/obj',
                                         'application/octet-stream', 'application/json',
                                         'image/png', 'image/jpeg', 'image/webp', 'text/plain', 'application/zip']}))
    budget = obj({key: dict(type='integer', minimum=1, maximum=value) for key, value in LIMITS.items()})
    transform = obj(dict(translation=vec, rotation=arr(number, 4, 4), scale=arr(positive, 3, 3)))
    bounds = obj(dict(minimum=vec, maximum=vec))
    tool = obj(dict(name=short, version=nullable_text))
    license_claim = obj(dict(identifier=nullable_text, attribution=nullable_text,
                             redistribution={'enum': ['unknown', 'restricted', 'permitted']},
                             evidence={'anyOf': [ref('file'), {'type': 'null'}]}))
    provenance = obj(dict(origin={'enum': ['imported', 'generated', 'derived', 'authored', 'procedural-fixture']},
                          fixture=boolean, description=text, tools=arr(tool, 0, 32),
                          evidence=arr(ref('file'), 0, 128), license=license_claim))
    asset_ref = obj(dict(id=identifier, version=version, manifest=ref('file')))
    source = _document('source', dict(id=identifier, version=version, file=ref('file'),
        role={'enum': ['geometry', 'front', 'back', 'left', 'right', 'top', 'bottom',
                       'detail', 'identity', 'sketch', 'texture', 'annotation', 'mask', 'depth', 'profile', 'viewpoint']},
        provenance=provenance,
        original_filename=short, dimensions_px=dimensions,
        acquisition=obj(dict(method={'enum': ['imported', 'authored', 'generated', 'recovered', 'unknown']},
                             timestamp=timestamp)),
        evidence_class=evidence_class, mask=nullable_file, annotations=arr(ref('file'), 0, 128),
        camera=obj(dict(status={'enum': ['unknown', 'supplied', 'estimated']},
                        projection={'enum': ['perspective', 'orthographic']},
                        camera_to_world=arr(number, 16, 16),
                        image_size=arr(dict(type='integer', minimum=1, maximum=32768), 2, 2),
                        intrinsics=arr(number, 9, 9), orthographic_size=arr(positive, 2, 2),
                        evidence=ref('file')), ['status'])))
    uncertainty = _document('uncertainty', dict(id=identifier, version=version, target=identifier,
        classification={'enum': ['observed', 'multi-view-constrained', 'procedurally-inferred',
                                  'model-inferred', 'manually-authored', 'unknown']},
        message=text, evidence=arr(identifier, 0, 128),
        confidence={'anyOf': [fraction, {'type': 'null'}]},
        confidence_method=nullable_text,
        status={'enum': ['pending', 'accepted', 'corrected']},
        scope=obj(dict(level={'enum': ['asset', 'part', 'surface-region', 'faces', 'vertices']},
                       target=identifier, indices=arr(integer, 0, LIMITS['vertices']))),
        inferred_method=nullable_text, missing_viewpoints=arr(short, 0, 128),
        recommended_actions=arr(text, 0, 128)),
        optional=())
    texture = obj(dict(file=ref('file'), texcoord={'enum': [0, 1]},
                       color_space={'enum': ['srgb', 'linear-data']}))
    material = _document('material', dict(id=identifier, version=version, name=short,
        slot=dict(type='integer', minimum=0, maximum=4095),
        base_color=arr(fraction, 4, 4), metallic=fraction, roughness=fraction,
        alpha_mode={'enum': ['OPAQUE', 'MASK', 'BLEND']}, alpha_cutoff=fraction,
        double_sided=boolean,
        textures=obj({name: texture for name in ('base_color', 'normal', 'metallic_roughness',
                                                'occlusion', 'emissive')}, []),
        provenance=provenance, evidence_class=evidence_class,
        texture_dimensions=obj({name: dimensions for name in ('base_color', 'normal', 'metallic_roughness', 'occlusion', 'emissive')}, []),
        procedural_parameters=arr(obj(dict(name=identifier, value={'anyOf': [number, text, boolean, vec]})), 0, 128),
        projection_evidence=arr(ref('file'), 0, 128)),
        optional=())
    part = _document('part', dict(id=identifier, name=short,
        parent={'anyOf': [identifier, {'type': 'null'}]}, local_transform=transform,
        mesh_ids=arr(identifier, 0, 4096), material_ids=arr(identifier, 0, 4096),
        source_ids=arr(identifier, 0, 128), uncertainty_ids=arr(identifier, 0, 128),
        version=version, category=short, pivot_m=vec,
        attachments=arr(obj(dict(id=identifier, local_transform=transform)), 0, 128),
        anatomical_side={'enum': ['left', 'right', 'center', 'not-applicable', 'unknown']},
        decomposition_components=arr(obj(dict(component_id=identifier, manifest=ref('file'))), 0, 128),
        mesh_regions=arr(obj(dict(mesh_id=identifier, face_indices=arr(integer, 1, LIMITS['triangles']))), 0, 128),
        visible_evidence=arr(ref('file'), 0, 128), inferred_geometry=arr(ref('file'), 0, 128),
        confidence={'anyOf': [fraction, {'type': 'null'}]}, confidence_method=nullable_text,
        rig_association=nullable_file, review_state=review_state, edit_history=history),
        optional=())
    request = _document('request', dict(id=identifier, version=version, asset_id=identifier,
        category={'enum': ['character', 'prop', 'environment', 'architecture', 'general']},
        method={'enum': ['import', 'procedural', 'silhouette', 'revolution', 'heightfield',
                         'visual-hull', 'provider']},
        description=text, negative_constraints=arr(text, 0, 128),
        sources=arr(ref('source'), 0, 128), coordinate_system={'const': COORDINATES},
        dimensions_m=arr(positive, 3, 3),
        scale_evidence={'enum': ['supplied', 'estimated', 'measured']},
        intended_use={'enum': ['realtime', 'cinematic', 'web', 'mobile', 'print', 'editing']},
        formats=arr({'enum': ['glb', 'gltf', 'obj', 'blend']}, 1, 4),
        semantic_parts=arr(identifier, 0, 4096), allowed_assumptions=arr(text, 0, 128),
        identity={'anyOf': [ref('file'), {'type': 'null'}]},
        style={'anyOf': [ref('file'), {'type': 'null'}]},
        provider={'anyOf': [obj(dict(id=identifier, model=nullable_text,
                                      network_authorized=boolean)), {'type': 'null'}]},
        seed=dict(type='integer', minimum=0, maximum=2**32-1), limits=ref('limits'),
        existing_asset_version={'anyOf': [version, {'type': 'null'}]},
        source_type={'enum': ['text', 'image', 'sketch', 'multiview', 'geometry', 'mixed']},
        provenance=provenance,
        material_requirements=arr(ref('material'), 0, 128),
        execution_budgets=obj({key: {'anyOf': [dict(type='number', exclusiveMinimum=0, maximum=1e12), {'type': 'null'}]}
                               for key in ('texture_pixels', 'runtime_seconds', 'compute_seconds')}),
        lod_requirements=arr(obj(dict(level=dict(type='integer', minimum=0, maximum=32),
                                     triangles=dict(type='integer', minimum=1, maximum=LIMITS['triangles']),
                                     maximum_deviation_m={'anyOf': [positive, {'type': 'null'}]})), 0, 33),
        fallback_policy={'enum': ['reject', 'import-only', 'procedural', 'limited-reconstruction']}))
    asset = _document('asset', dict(id=identifier, version=version,
        category={'enum': ['character', 'prop', 'environment', 'architecture', 'general']},
        coordinate_system={'const': COORDINATES},
        sources=arr(ref('source'), 1, 128), files=arr(ref('file'), 1, LIMITS['files']),
        geometry=ref('file'),
        measurements=obj(dict(vertices=dict(type='integer', minimum=1, maximum=LIMITS['vertices']),
                              triangles=dict(type='integer', minimum=1, maximum=LIMITS['triangles']),
                              bounds=ref('bounds'))),
        parts=arr(ref('part'), 1, LIMITS['parts']), materials=arr(ref('material'), 0, 4096),
        dependencies=arr(ref('asset-reference'), 0, 256),
        uncertainties=arr(ref('uncertainty'), 0, 1024), provenance=provenance,
        review=obj(dict(status={'enum': ['pending', 'approved', 'rejected']},
                        evidence=arr(ref('file'), 0, 128))), limits=ref('limits'),
        generation_revision=version, identity=nullable_file, pivot_m=vec,
        mesh_details=arr(obj(dict(mesh_id=identifier, geometry=ref('file'), lod=dict(type='integer', minimum=0, maximum=32),
                                  vertices=dict(type='integer', minimum=1, maximum=LIMITS['vertices']),
                                  triangles=dict(type='integer', minimum=1, maximum=LIMITS['triangles']),
                                  material_slot=dict(type='integer', minimum=0, maximum=4095),
                                  normals=boolean, tangents=boolean, uv_sets=arr({'enum': [0, 1]}, 0, 2),
                                  topology=obj(dict(boundary_edges=integer, nonmanifold_edges=integer, degenerate_faces=integer)),
                                  evidence_class=evidence_class)), 1, LIMITS['parts']),
        provider_receipts=arr(ref('file'), 0, 128), previews=arr(ref('file'), 0, 128),
        quality_reports=arr(ref('file'), 0, 128), repair_history=history,
        available_formats=arr({'enum': ['glb', 'gltf', 'obj', 'blend', 'archive']}, 0, 5),
        completeness={'enum': ['partial', 'complete', 'unknown']}),
        optional=())
    job = _document('job', dict(id=identifier, version=version, request=ref('file'), asset_id=identifier,
        status={'enum': ['queued', 'preparing', 'running', 'waiting-provider', 'validating',
                         'awaiting-review', 'completed', 'failed', 'cancelled', 'interrupted']},
        attempts=dict(type='integer', minimum=0, maximum=100),
        maximum_attempts=dict(type='integer', minimum=1, maximum=100),
        outputs=arr(ref('file'), 0, LIMITS['files']),
        error={'anyOf': [obj(dict(code=identifier, message=text, retryable=boolean)), {'type': 'null'}]},
        dependencies=arr(ref('asset-reference'), 0, 256), limits=ref('limits')))
    export = _document('export', dict(id=identifier, version=version, asset=ref('asset-reference'),
        format={'enum': ['glb', 'gltf', 'obj', 'blend', 'archive']},
        status={'enum': ['partial', 'complete', 'failed']}, files=arr(ref('file'), 0, LIMITS['files']),
        tools=arr(tool, 1, 32), losses=arr(text, 0, 128),
        independent_inspection={'anyOf': [ref('file'), {'type': 'null'}]}, limits=ref('limits')))
    # Versioned evidence envelopes retain the native owner's exact document, not
    # a replacement scheduler, review authority, provider or archive engine.
    subject = obj(dict(id=identifier, version=version))
    duration = dict(type='number', minimum=0, maximum=1e12)
    nullable_number = {'anyOf': [duration, {'type': 'null'}]}
    nullable_bytes = {'anyOf': [dict(type='integer', minimum=0, maximum=10**12), {'type': 'null'}]}
    extensions = arr(obj(dict(namespace=identifier, payload=ref('file'))), 0, 32)

    def evidence_document(kind, fields):
        return _document(kind, dict(id=identifier, version=version, subject=subject,
            owner={'enum': ['model3d', 'rig', 'production', 'external-provider']},
            native_evidence=arr(ref('file'), 1, 128), extensions=extensions, **fields))

    provider_receipt = evidence_document('provider-receipt', dict(
        provider=identifier, model=nullable_text, provider_version=nullable_text,
        request=ref('file'), status={'enum': ['unavailable', 'failed', 'completed']},
        executed=boolean, outputs=arr(ref('file'), 0, LIMITS['files']),
        seed={'anyOf': [dict(type='integer', minimum=0, maximum=2**32-1), {'type': 'null'}]},
        usage=nullable_file, error=nullable_text))
    processing_stage = evidence_document('processing-stage', dict(
        operation=identifier, status={'enum': ['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted']},
        inputs=arr(ref('file'), 1, LIMITS['files']), outputs=arr(ref('file'), 0, LIMITS['files']),
        tools=arr(tool, 1, 32), usage=nullable_file, error=nullable_text))
    resource_usage = evidence_document('resource-usage', dict(
        status={'enum': ['measured', 'unavailable', 'partial']},
        wall_seconds=nullable_number, cpu_seconds=nullable_number,
        peak_memory_bytes=nullable_bytes, input_bytes=nullable_bytes, output_bytes=nullable_bytes,
        compute_device=nullable_text, measurement_method=nullable_text))
    validation_result = evidence_document('validation-result', dict(
        validator=tool, status={'enum': ['passed', 'failed', 'unverified']},
        contract_valid=boolean, payloads_verified=boolean, geometry_inspected=boolean,
        diagnostics=arr(obj(dict(code=identifier, severity={'enum': ['info', 'warning', 'error']},
                                  message=text, target=nullable_text)), 0, 1024)))
    review_decision = evidence_document('review-decision', dict(
        reviewer=short, reviewer_kind={'enum': ['human', 'model', 'automated']},
        timestamp=timestamp, verdict={'enum': ['pending', 'approved', 'rejected']},
        dimensions=arr(identifier, 1, 128), inspected=arr(ref('file'), 1, LIMITS['files']),
        notes=text))
    repair_operation = evidence_document('repair-operation', dict(
        operation=identifier, before=asset_ref, after={'anyOf': [asset_ref, {'type': 'null'}]},
        status={'enum': ['proposed', 'completed', 'failed', 'cancelled']},
        changed_parts=arr(identifier, 0, LIMITS['parts']), reason=text, tools=arr(tool, 1, 32)))
    archive = evidence_document('archive', dict(
        archive=ref('file'), format={'enum': ['zip']}, asset=asset_ref,
        inventory=arr(ref('file'), 1, LIMITS['files']),
        status={'enum': ['created', 'restored', 'failed', 'unverified']},
        restoration_evidence=nullable_file, preserved_reviews=arr(ref('file'), 0, 128)))
    return dict(file=file, limits=budget, transform=transform, bounds=bounds,
                provenance=provenance, **{'asset-reference': asset_ref},
                source=source, uncertainty=uncertainty, material=material, part=part,
                request=request, asset=asset, job=job, export=export,
                **{'provider-receipt': provider_receipt, 'processing-stage': processing_stage,
                   'resource-usage': resource_usage, 'validation-result': validation_result,
                   'review-decision': review_decision, 'repair-operation': repair_operation, 'archive': archive})


def schemas():
    """Fresh schema objects; callers cannot mutate the canonical definitions."""
    return {'model3d.schema.json': {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'Hatch Pet Model3D v1 contracts',
        'description': 'Structural contracts only. Runtime checks semantic references and actual payloads separately.',
        '$defs': deepcopy(_definitions()), 'oneOf': [ref(name) for name in KINDS]}}


def file_references(value):
    """Yield declared payloads, including nested source, license and review files."""
    if isinstance(value, dict):
        if set(value) == {'path', 'sha256', 'bytes', 'media_type'}:
            yield value
        else:
            for child in value.values():
                yield from file_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from file_references(child)


def _unique(items, label):
    identifiers = [item['id'] for item in items]
    require(len(identifiers) == len(set(identifiers)), 'MODEL3D_DUPLICATE',
            'Duplicate ' + label + ' identity.', stage='model3d')
    return set(identifiers)


def _transform(value):
    require(abs(math.hypot(*value['rotation']) - 1) <= 1e-6,
            'MODEL3D_QUATERNION', 'Rotation must be a unit XYZW quaternion.', stage='model3d')
    require(all(v >= 1e-9 for v in value['scale']), 'MODEL3D_TRANSFORM',
            'Scale is below the supported nonsingular transform bound.', stage='model3d')


def _timestamp(value):
    if value is None:
        return
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require('T' in value and parsed.utcoffset() is not None,
                'MODEL3D_ACQUISITION', 'Timestamp requires an ISO 8601 time and timezone.')
    except ValueError as exc:
        raise RigError('MODEL3D_ACQUISITION', 'Timestamp must be an ISO 8601 datetime.') from exc


def _semantic(value):
    kind = value['kind']
    for item in (value.get('provenance'),):
        if item is not None and item['origin'] == 'procedural-fixture':
            require(item['fixture'], 'MODEL3D_PROVENANCE', 'Procedural fixture origin requires fixture=true.')
    if kind == KINDS['part']:
        _transform(value['local_transform'])
        require(value['parent'] != value['id'], 'MODEL3D_CYCLE', 'A part cannot parent itself.')
        if 'confidence' in value or 'confidence_method' in value:
            require((value.get('confidence') is None) == (value.get('confidence_method') is None),
                    'MODEL3D_CONFIDENCE', 'Part confidence requires its measurement method.')
            require(value.get('confidence') is None or bool(value.get('visible_evidence')),
                    'MODEL3D_CONFIDENCE', 'Part confidence requires measured evidence.')
        attachments = [item['id'] for item in value.get('attachments', [])]
        require(len(attachments) == len(set(attachments)), 'MODEL3D_DUPLICATE', 'Duplicate attachment identity.')
        for item in value.get('attachments', []):
            _transform(item['local_transform'])
        for field in ('mesh_ids', 'material_ids', 'source_ids', 'uncertainty_ids'):
            require(len(value[field]) == len(set(value[field])), 'MODEL3D_DUPLICATE', 'Duplicate ' + field)
    elif kind == KINDS['source']:
        media = value['file']['media_type']
        if value['role'] == 'geometry':
            require(media in ('model/gltf-binary', 'model/gltf+json', 'model/obj'),
                    'MODEL3D_SOURCE', 'A geometry source must declare a supported real model format.')
        if value['role'] in ('front', 'back', 'left', 'right', 'top', 'bottom', 'detail', 'sketch', 'texture', 'mask', 'depth', 'viewpoint'):
            require(media.startswith('image/'), 'MODEL3D_SOURCE', 'This source role requires an image payload.')
        require(not media.startswith('image/') or value['dimensions_px'] is not None,
                'MODEL3D_DIMENSIONS', 'Image source dimensions must be declared.')
        require(media.startswith('image/') or value['dimensions_px'] is None,
                'MODEL3D_DIMENSIONS', 'Pixel dimensions only describe image sources.')
        _timestamp(value['acquisition']['timestamp'])
        require(value['acquisition']['method'] == 'unknown' or value['acquisition']['timestamp'] is not None,
                'MODEL3D_ACQUISITION', 'Known acquisition methods require an actual timestamp.')
        require(value['mask'] is None or value['mask']['media_type'].startswith('image/'),
                'MODEL3D_SOURCE', 'Foreground masks must be images.')
        camera = value['camera']
        if camera['status'] == 'unknown':
            require(set(camera) == {'status'}, 'MODEL3D_CAMERA', 'Unknown calibration cannot contain camera measurements.')
        else:
            required = {'projection', 'camera_to_world', 'image_size', 'evidence'}
            require(required <= camera.keys(), 'MODEL3D_CAMERA', 'Calibration requires measured or supplied evidence and a complete camera.')
            optical = 'intrinsics' if camera['projection'] == 'perspective' else 'orthographic_size'
            other = 'orthographic_size' if optical == 'intrinsics' else 'intrinsics'
            require(optical in camera and other not in camera, 'MODEL3D_CAMERA', 'Camera optics must match projection.')
            matrix = camera['camera_to_world']
            require(matrix[12:] == [0, 0, 0, 1], 'MODEL3D_CAMERA', 'Row-major camera-to-world matrix must be affine.')
            columns = [[matrix[row * 4 + col] for row in range(3)] for col in range(3)]
            for i in range(3):
                for j in range(3):
                    require(abs(sum(a*b for a, b in zip(columns[i], columns[j])) - (i == j)) <= 1e-6,
                            'MODEL3D_CAMERA', 'Camera rotation must be orthonormal.')
            a, b, c = columns
            determinant = a[0]*(b[1]*c[2]-b[2]*c[1])-b[0]*(a[1]*c[2]-a[2]*c[1])+c[0]*(a[1]*b[2]-a[2]*b[1])
            require(determinant > 0, 'MODEL3D_CAMERA', 'Camera basis must be right-handed.')
            if optical == 'intrinsics':
                k = camera[optical]
                require(k[0] > 0 and k[4] > 0 and k[3] == 0 and k[6:] == [0, 0, 1],
                        'MODEL3D_CAMERA', 'Invalid perspective intrinsic matrix.')
    elif kind == KINDS['uncertainty']:
        require((value['confidence'] is None) == (value['confidence_method'] is None),
                'MODEL3D_CONFIDENCE', 'A confidence number requires its measurement method; unknown confidence stays null.')
        require(value['confidence'] is None or bool(value['evidence']), 'MODEL3D_CONFIDENCE',
                'Measured confidence requires source evidence.')
    elif kind == KINDS['material']:
        require(set(value['texture_dimensions']) == set(value['textures']) and
                all(size is not None for size in value['texture_dimensions'].values()),
                'MODEL3D_DIMENSIONS', 'Each texture requires its declared pixel dimensions.')
        for name, texture in value['textures'].items():
            expected = 'srgb' if name in ('base_color', 'emissive') else 'linear-data'
            require(texture['color_space'] == expected, 'MODEL3D_COLOR_SPACE', 'Incorrect texture interpretation: ' + name)
            require(texture['file']['media_type'].startswith('image/'), 'MODEL3D_TEXTURE', 'A texture must reference image data.')
    elif kind in (KINDS['request'], KINDS['asset']):
        sources = _unique(value['sources'], 'source')
        for source in value['sources']:
            _semantic(source)
        if kind == KINDS['request']:
            require((value['provider'] is not None) == (value['method'] == 'provider'),
                    'MODEL3D_PROVIDER', 'Only provider requests may select a provider, and must name one.')
            if not sources:
                require(value['source_type'] == 'text', 'MODEL3D_SOURCE', 'Requests without supplied sources must declare text intent.')
            if value['source_type'] == 'geometry':
                require(any(s['role'] == 'geometry' for s in value['sources']), 'MODEL3D_SOURCE', 'Geometry modality needs geometry evidence.')
            if value['source_type'] in ('image', 'sketch', 'multiview'):
                require(any(s['file']['media_type'].startswith('image/') for s in value['sources']), 'MODEL3D_SOURCE', 'Image modality needs image evidence.')
            if value['method'] not in ('provider', 'procedural'):
                require(bool(sources), 'MODEL3D_SOURCE', 'This method requires supplied source evidence.')
            if value['method'] == 'import':
                require(any(s['role'] == 'geometry' for s in value['sources']),
                        'MODEL3D_SOURCE', 'Mesh import requests need a genuine geometry source.')
            if value['method'] in ('silhouette', 'heightfield'):
                require(any(s['file']['media_type'].startswith('image/') for s in value['sources']),
                        'MODEL3D_SOURCE', 'This reconstruction request needs image evidence.')
            if value['method'] == 'visual-hull':
                require(sum(s['camera']['status'] != 'unknown' and s['file']['media_type'].startswith('image/')
                            for s in value['sources']) >= 2,
                        'MODEL3D_CAMERA', 'Visual hull requests require at least two calibrated observations.')
            for material in value['material_requirements']:
                _semantic(material)
            levels = [item['level'] for item in value['lod_requirements']]
            require(len(levels) == len(set(levels)), 'MODEL3D_DUPLICATE', 'LOD levels must be unique.')
            if any(source['provenance']['fixture'] for source in value['sources']):
                require(value['provenance']['fixture'], 'MODEL3D_PROVENANCE', 'Fixture request sources must remain identified as fixtures.')
            for field in ('formats', 'semantic_parts'):
                require(len(value[field]) == len(set(value[field])), 'MODEL3D_DUPLICATE', 'Duplicate ' + field)
            return
        parts = _unique(value['parts'], 'part')
        materials = _unique(value['materials'], 'material')
        uncertainties = _unique(value['uncertainties'], 'uncertainty')
        require(len({m['slot'] for m in value['materials']}) == len(value['materials']),
                'MODEL3D_DUPLICATE', 'Material slots must be unique.')
        parent = {part['id']: part['parent'] for part in value['parts']}
        assigned, whole_meshes, regional_owners = set(), {}, {}
        for part in value['parts']:
            _semantic(part)
            require(part['parent'] is None or part['parent'] in parts, 'MODEL3D_REFERENCE', 'Missing part parent.')
            for field, allowed in [('material_ids', materials), ('source_ids', sources), ('uncertainty_ids', uncertainties)]:
                require(set(part[field]) <= allowed, 'MODEL3D_REFERENCE', 'Missing ' + field + ' reference.')
            require(not set(whole_meshes).intersection(part['mesh_ids']), 'MODEL3D_PART', 'Whole-mesh bindings cannot have multiple semantic owners.')
            assigned.update(part['mesh_ids'])
            whole_meshes.update({name: part['id'] for name in part['mesh_ids']})
            for region in part['mesh_regions']:
                assigned.add(region['mesh_id'])
                indices = set(region['face_indices'])
                require(len(indices) == len(region['face_indices']), 'MODEL3D_REGION', 'Repeated regional face index.')
                previous = regional_owners.setdefault(region['mesh_id'], {})
                require(all(index not in previous or previous[index] == part['id'] for index in indices),
                        'MODEL3D_REGION', 'Two semantic parts cannot own the same surface face.')
                previous.update({index: part['id'] for index in indices})
            seen, current = set(), part['id']
            while current is not None:
                require(current not in seen, 'MODEL3D_CYCLE', 'Part hierarchy contains a cycle.')
                require(len(seen) < 64, 'MODEL3D_DEPTH', 'Part hierarchy exceeds 64 levels.')
                seen.add(current)
                current = parent.get(current)
        for name, owners in regional_owners.items():
            require(name not in whole_meshes or all(owner == whole_meshes[name] for owner in owners.values()),
                    'MODEL3D_REGION', 'A regional part cannot take faces owned by another whole-mesh part.')
        details = value['mesh_details']
        require(all(sum(item[key] for item in details) <= value['limits'][key] for key in ('vertices', 'triangles')),
                'MODEL3D_BUDGET', 'Aggregate LOD geometry exceeds the document geometry budget.')
        detail_keys = [(item['lod'], item['mesh_id']) for item in details]
        require(len(detail_keys) == len(set(detail_keys)), 'MODEL3D_DUPLICATE', 'Duplicate mesh/LOD measurement.')
        require({item['mesh_id'] for item in details if item['lod'] == 0} == assigned,
                'MODEL3D_MESH_BINDING', 'Base mesh measurements must cover semantic mesh bindings.')
        for item in details:
            require(item['geometry'] in value['files'], 'MODEL3D_INVENTORY', 'LOD geometry must occur in the exact file inventory.')
            require(item['lod'] != 0 or item['geometry'] == value['geometry'],
                    'MODEL3D_MESH_BINDING', 'LOD zero must reference canonical geometry.')
        for material in value['materials']:
            _semantic(material)
        for uncertainty in value['uncertainties']:
            _semantic(uncertainty)
            require(uncertainty['target'] in parts | {value['id']}, 'MODEL3D_REFERENCE', 'Missing uncertainty target.')
            require(set(uncertainty['evidence']) <= sources, 'MODEL3D_REFERENCE', 'Missing uncertainty source evidence.')
            scope = uncertainty['scope']
            allowed = {value['id']} if scope['level'] == 'asset' else parts if scope['level'] == 'part' else assigned
            require(scope['target'] in allowed, 'MODEL3D_REFERENCE', 'Missing scoped uncertainty target.')
            if scope['level'] in ('asset', 'part'):
                require(scope['target'] == uncertainty['target'], 'MODEL3D_REFERENCE', 'Uncertainty scope and target must agree.')
            elif uncertainty['target'] != value['id']:
                owner = next(part for part in value['parts'] if part['id'] == uncertainty['target'])
                owned = set(owner['mesh_ids']) | {region['mesh_id'] for region in owner['mesh_regions']}
                require(scope['target'] in owned, 'MODEL3D_REFERENCE', 'Scoped mesh must belong to the uncertainty target part.')
            require(scope['level'] in ('faces', 'vertices') or not scope['indices'],
                    'MODEL3D_UNCERTAINTY', 'Only face/vertex scopes may contain element indices.')
        require(value['geometry'] in value['files'], 'MODEL3D_INVENTORY', 'Primary geometry must occur in the exact file inventory.')
        require(value['geometry']['media_type'] in ('model/gltf-binary', 'model/gltf+json', 'model/obj'),
                'MODEL3D_GEOMETRY', 'Canonical inspection requires a real GLB/glTF/OBJ model.')
        bounds = value['measurements']['bounds']
        require(all(a <= b for a, b in zip(bounds['minimum'], bounds['maximum'])), 'MODEL3D_BOUNDS', 'Bounds are inverted.')
        require(all(value['measurements'][k] <= value['limits'][k] for k in ('vertices', 'triangles')) and
                len(parts) <= value['limits']['parts'], 'MODEL3D_BUDGET', 'Declared geometry exceeds its budget.')
        if value['review']['status'] == 'approved':
            require(bool(value['review']['evidence']), 'MODEL3D_REVIEW', 'An approval claim must reference evidence; it is not authenticated by schema validation.')
        if any(s['provenance']['fixture'] for s in value['sources']):
            require(value['provenance']['fixture'], 'MODEL3D_PROVENANCE', 'Fixture sources cannot become non-fixture assets.')
    elif kind == KINDS['job']:
        require(value['attempts'] <= value['maximum_attempts'], 'MODEL3D_ATTEMPTS', 'Attempt budget exceeded.')
        if value['status'] == 'completed':
            require(bool(value['outputs']) and value['error'] is None, 'MODEL3D_JOB', 'Completed jobs require outputs and cannot contain an active error.')
        if value['status'] == 'failed':
            require(value['error'] is not None, 'MODEL3D_JOB', 'Failed jobs require an actionable error.')
    elif kind == KINDS['provider-receipt']:
        complete = value['status'] == 'completed'
        require(not complete or (value['executed'] and value['model'] is not None and bool(value['outputs']) and value['error'] is None),
                'MODEL3D_RECEIPT', 'Completed provider claims need execution, outputs and no error.')
        require(value['status'] != 'unavailable' or not value['executed'],
                'MODEL3D_RECEIPT', 'Unavailable providers cannot claim execution.')
        require(value['status'] != 'failed' or value['error'] is not None,
                'MODEL3D_RECEIPT', 'Failed providers need diagnostics.')
    elif kind == KINDS['processing-stage']:
        require(value['status'] != 'completed' or bool(value['outputs']) and value['error'] is None,
                'MODEL3D_STAGE', 'Completed stages need outputs and no error.')
        require(value['status'] != 'failed' or value['error'] is not None,
                'MODEL3D_STAGE', 'Failed stages need diagnostics.')
    elif kind == KINDS['resource-usage']:
        measured = [value[key] for key in ('wall_seconds', 'cpu_seconds', 'peak_memory_bytes', 'input_bytes', 'output_bytes')]
        require(value['status'] != 'measured' or all(item is not None for item in measured) and value['measurement_method'] is not None,
                'MODEL3D_USAGE', 'Measured usage needs measurements and method.')
        require(value['status'] != 'unavailable' or all(item is None for item in measured),
                'MODEL3D_USAGE', 'Unavailable usage cannot claim measurements.')
    elif kind == KINDS['validation-result']:
        require(not value['geometry_inspected'] or value['payloads_verified'],
                'MODEL3D_VALIDATION', 'Geometry inspection requires verified payloads.')
        require(not value['payloads_verified'] or value['contract_valid'],
                'MODEL3D_VALIDATION', 'Payload verification requires valid contracts.')
        require(value['status'] != 'passed' or value['contract_valid'] and not any(d['severity'] == 'error' for d in value['diagnostics']),
                'MODEL3D_VALIDATION', 'Passing validation cannot contain errors or invalid contracts.')
    elif kind == KINDS['review-decision']:
        _timestamp(value['timestamp'])
        require(value['verdict'] == 'pending' or value['timestamp'] is not None,
                'MODEL3D_REVIEW', 'A recorded decision requires its actual timestamp.')
        require(len(set(value['dimensions'])) == len(value['dimensions']), 'MODEL3D_DUPLICATE', 'Duplicate review dimensions.')
    elif kind == KINDS['repair-operation']:
        require(value['subject'] == {key: value['before'][key] for key in ('id', 'version')},
                'MODEL3D_REFERENCE', 'Repair subjects identify the immutable input revision.')
        if value['after'] is not None:
            require(value['after']['id'] == value['before']['id'] and value['after']['version'] != value['before']['version'],
                    'MODEL3D_REPAIR', 'Repairs preserve asset identity and create a different immutable version.')
        require(value['status'] != 'completed' or value['after'] is not None and value['after'] != value['before'],
                'MODEL3D_REPAIR', 'Completed repairs require a different immutable output revision.')
    elif kind == KINDS['archive']:
        require(value['subject'] == {key: value['asset'][key] for key in ('id', 'version')},
                'MODEL3D_REFERENCE', 'Archive subjects must match the exact source asset revision.')
        require(value['archive']['media_type'] == 'application/zip', 'MODEL3D_ARCHIVE', 'ZIP archives require the correct media type.')
        require(value['status'] != 'restored' or value['restoration_evidence'] is not None,
                'MODEL3D_ARCHIVE', 'Restoration claims require native verification evidence.')
    elif kind == KINDS['export'] and value['status'] == 'complete':
        require(bool(value['files']), 'MODEL3D_EXPORT', 'A complete export needs actual file references.')
    dependencies = value.get('dependencies', [])
    identities = [(item['id'], item['version']) for item in dependencies]
    require(len(identities) == len(set(identities)), 'MODEL3D_DUPLICATE', 'Duplicate exact asset dependency.')
    require((value.get('id'), value.get('version')) not in identities,
            'MODEL3D_CYCLE', 'An asset cannot depend on itself.')


def validate_contract(value):
    """Validate strict shape and local semantics; never read files or run tools."""
    finite_json(value)
    require(isinstance(value, dict) and value.get('kind') in KINDS.values(),
            'MODEL3D_KIND', 'Expected an independently versioned model3d document.', stage='model3d')
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RigError('MODEL3D_DEPENDENCY', 'Install requirements-rig.txt for contract validation.', stage='model3d') from exc
    name = next(key for key, kind in KINDS.items() if kind == value['kind'])
    schema = schemas()['model3d.schema.json']
    schema.pop('oneOf')
    schema['$ref'] = '#/$defs/' + name
    error = next(Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(value), None)
    if error:
        path = '/' + '/'.join(map(str, error.absolute_path))
        raise RigError('MODEL3D_SCHEMA', error.message[:2048], stage='model3d', asset=path)
    _semantic(value)
    files, aliases = {}, {}
    for item in file_references(value):
        path = portable_name(item['path'])
        alias = unicodedata.normalize('NFC', path).casefold()
        require(path == unicodedata.normalize('NFC', path), 'MODEL3D_PATH', 'Paths must use Unicode NFC.')
        require(alias not in aliases or aliases[alias] == path, 'MODEL3D_PATH', 'Case-colliding portable paths.')
        require(path not in files or files[path] == item, 'MODEL3D_HASH_CONFLICT', 'Conflicting records for the same path.')
        aliases[alias], files[path] = path, item
    limits = value.get('limits', LIMITS)
    require(len(files) <= limits['files'] and sum(item['bytes'] for item in files.values()) <= limits['total_bytes'] and
            all(item['bytes'] <= limits['file_bytes'] for item in files.values()),
            'MODEL3D_BUDGET', 'Declared payload inventory exceeds its resource budget.')
    return value
