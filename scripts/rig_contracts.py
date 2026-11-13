"""Strict, additive version-1 contracts. Importing schemas needs no 3D dependency."""
from __future__ import annotations

from functools import lru_cache
from typing import Any
from rig_errors import RigError, finite_json, require

COORDINATES = dict(handedness='right', right='+X', up='+Y', forward='+Z', unit='meter',
                   angles='radian', quaternion='XYZW', matrices='column-vectors', time='second')
DEFAULT_LIMITS = dict(source_bytes=256*1024**2, dependency_bytes=1024**3, vertices=1_000_000,
                      triangles=2_000_000, bones=256, influences=4, morphs=128, clips=64,
                      keyframes=250_000, duration=120, fps=60, memory_bytes=2*1024**3,
                      solver_iterations=128, timeout_seconds=300, archive_files=10000,
                      archive_bytes=1024**3, attempts=3)


def obj(properties: dict, required=None) -> dict:
    return dict(type='object', additionalProperties=False, properties=properties,
                required=list(properties if required is None else required))


def arr(items: dict, minimum=0, maximum=250000) -> dict:
    return dict(type='array', items=items, minItems=minimum, maxItems=maximum)


def ref(name: str) -> dict:
    return {'$ref': '#/$defs/' + name}


def mapping(value: dict, maximum=10000) -> dict:
    return dict(type='object', additionalProperties=value, maxProperties=maximum)


@lru_cache(maxsize=1)
def definitions() -> dict:
    text = dict(type='string', minLength=1, maxLength=4096)
    identifier = dict(type='string', pattern='^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$')
    version = dict(type='string', pattern=r'^\d+\.\d+\.\d+$')
    digest = dict(type='string', pattern='^[0-9a-f]{64}$')
    num = dict(type='number', minimum=-1e9, maximum=1e9)
    positive = dict(type='number', exclusiveMinimum=0, maximum=1e9)
    integer = dict(type='integer', minimum=0, maximum=2_000_000_000)
    vec = arr(num, 3, 3)
    quat = arr(num, 4, 4)
    boolean = {'type': 'boolean'}
    nullable_id = {'anyOf': [identifier, {'type': 'null'}]}
    transform = obj(dict(translation=vec, rotation=quat, scale=arr(positive, 3, 3)))
    file_ref = obj(dict(path=text, sha256=digest))
    evidence = obj(dict(source={'enum': ['existing-rig', 'human-landmark', 'semantic-part',
                    'import-metadata', 'geometry-fit', 'procedural-fixture']}, reference=text,
                    confidence=dict(type='number', minimum=0, maximum=1)))
    bone = obj(dict(schema_version={'const': 1}, id=identifier, role=identifier, parent=nullable_id,
                    rest=transform, head=vec, tail=vec, length=positive, roll=num, deform=boolean,
                    mirror=nullable_id, evidence=evidence))
    constraint = obj(dict(schema_version={'const': 1}, id=identifier, bone=identifier,
                         type={'enum': ['hinge', 'ball', 'swing-twist', 'translation', 'locked',
                                        'aim', 'copy-rotation', 'copy-position']},
                         axis=vec, minimum=num, maximum=num, swing=positive, target=identifier,
                         point=vec, influence=dict(type='number', minimum=0, maximum=1),
                         axes=arr({'enum': [0, 1, 2]}, 1, 3)),
                     ['schema_version', 'id', 'bone', 'type'])
    ik = obj(dict(schema_version={'const': 1}, id=identifier, bones=arr(identifier, 3, 256),
                  solver={'enum': ['two-bone', 'fabrik']}, pole=vec,
                  tolerance=positive, iterations=dict(type='integer', minimum=1, maximum=128),
                  target=vec, orientation=quat),
              ['schema_version', 'id', 'bones', 'solver', 'pole', 'tolerance', 'iterations'])
    pose_transform = obj(dict(translation=vec, rotation=quat, scale=arr(positive, 3, 3)), [])
    pose = obj(dict(schema_version={'const': 1}, kind={'const': 'hatch-3d-pose'}, id=identifier,
                    rig_version=version, bones=mapping(pose_transform, 256),
                    morphs=mapping(mapping(num, 128), 4096)))
    track = obj(dict(schema_version={'const': 1}, target=identifier,
                    path={'enum': ['translation', 'rotation', 'scale', 'weights']},
                    interpolation={'enum': ['STEP', 'LINEAR', 'CUBICSPLINE']},
                    times=arr(dict(type='number', minimum=0, maximum=120), 1),
                    values=arr(arr(num, 1, 128), 1),
                    in_tangents=arr(arr(num, 1, 128), 2), out_tangents=arr(arr(num, 1, 128), 2)),
                ['schema_version', 'target', 'path', 'interpolation', 'times', 'values'])
    event = obj(dict(schema_version={'const': 1}, time=dict(type='number', minimum=0, maximum=120),
                    type={'enum': ['foot_contact', 'foot_release', 'hand_contact', 'grasp', 'release',
                                   'land', 'takeoff', 'attach', 'detach', 'marker']},
                    target=identifier, value=text), ['schema_version', 'time', 'type', 'target'])
    contact = obj(dict(schema_version={'const': 1}, id=identifier, chain=identifier,
                      start=dict(type='number', minimum=0, maximum=120),
                      end=dict(type='number', minimum=0, maximum=120), target=vec,
                      orientation=quat, kind={'enum': ['foot', 'hand', 'paw', 'grasp']}),
                  ['schema_version', 'id', 'chain', 'start', 'end', 'target', 'kind'])
    provenance = obj(dict(schema_version={'const': 1}, tool_version=version, operation=text,
                          created_at=text, input_hashes=mapping(digest), ancestry=arr(digest, 0, 1000)))
    clip = obj(dict(schema_version={'const': 1}, kind={'const': 'hatch-3d-animation'}, id=identifier,
                    version=version, rig_id=identifier, skeleton_version=version,
                    duration=dict(type='number', exclusiveMinimum=0, maximum=120),
                    fps=dict(type='integer', minimum=1, maximum=60), loop=boolean,
                    tracks=arr(track, 1), events=arr(event), contacts=arr(contact),
                    root_motion={'enum': ['embedded', 'in-place']},
                    interruptible=boolean, reduced_motion_pose=identifier, provenance=provenance))
    socket = obj(dict(schema_version={'const': 1}, id=identifier, bone=identifier, transform=transform,
                      role=text, owner=identifier))
    expression = obj(dict(schema_version={'const': 1}, morphs=mapping(num, 128),
                          bones=mapping(pose_transform, 256), incompatible=arr(identifier, 0, 128)))
    eyelid = obj(dict(eye=identifier, mesh=identifier, morph=identifier,
                     up_weight=dict(type='number', minimum=-2, maximum=2),
                     down_weight=dict(type='number', minimum=-2, maximum=2),
                     up_radians=dict(type='number', exclusiveMinimum=0, maximum=1.5707963267948966),
                     down_radians=dict(type='number', exclusiveMinimum=0, maximum=1.5707963267948966)))
    facial = obj(dict(schema_version={'const': 1}, eyes=arr(identifier, 0, 32),
                     morph_targets=mapping(arr(identifier, 0, 128), 4096),
                     expressions=mapping(expression, 128), eyelids=arr(eyelid, 0, 128)),
                 ['schema_version', 'eyes', 'morph_targets', 'expressions'])
    skin = obj(dict(schema_version={'const': 1}, id=identifier, version=version,
                    mesh=identifier, joints=arr(identifier, 1, 256), data=file_ref,
                    method={'enum': ['distance', 'diffusion', 'segmented', 'rigid', 'imported', 'manual']},
                    locked_vertices=arr(integer), fallback_reason={'type': ['string', 'null']}))
    uncertainty = obj(dict(element=text, code=text, message=text, evidence=evidence,
                           resolution={'enum': ['pending', 'accepted', 'corrected']}))
    report = obj(dict(schema_version={'const': 1}, kind=text, rig_sha256=digest,
                      ok=boolean, metrics=mapping({'type': ['number', 'integer', 'boolean', 'null', 'string']}),
                      checks=arr(obj(dict(name=text, status={'enum': ['passed', 'failed', 'unavailable', 'warning']},
                                          message=text, value={'type': ['number', 'integer', 'boolean', 'null', 'string']}))),
                      artifacts=arr(file_ref), created_at=text))
    character = obj(dict(schema_version={'const': 1}, id=identifier, version=version,
                         identity_sha256=digest))
    source = obj(dict(schema_version={'const': 1}, path=text, sha256=digest,
                      origin={'enum': ['import', 'provider', 'procedural-fixture', 'derived']},
                      fixture=boolean, attribution=text, dependencies=arr(file_ref, 1, 10000)))
    geometry = obj(dict(schema_version={'const': 1}, version=version, data=file_ref,
                        meshes=arr(identifier, 1, 4096), vertices=integer, triangles=integer))
    skeleton = obj(dict(schema_version={'const': 1}, id=identifier, version=version,
                        root=identifier, bones=arr(bone, 1, 256)))
    controller = obj(dict(ik=arr(ik, 0, 256), constraints=arr(constraint, 0, 1024)))
    clip_ref = obj(dict(id=identifier, version=version, path=text, sha256=digest))
    state_map = obj(dict(status={'enum': ['supported', 'supported-with-adaptation', 'requires-rig-capability',
                     'requires-animation', 'needs-review', 'unavailable']}, clips=arr(identifier, 0, 64), reason=text))
    limits = obj({key: dict(type='integer', minimum=1, maximum=max(value, 8 if key == 'influences' else value))
                  for key, value in DEFAULT_LIMITS.items()})
    rig = obj(dict(schema_version={'const': 1}, kind={'const': 'hatch-3d-rig'}, id=identifier,
                   version=version, revision=integer, character=character, source=source,
                   coordinate_system={'const': COORDINATES}, archetype=identifier,
                   geometry=geometry, skeleton={'anyOf': [skeleton, {'type': 'null'}]},
                   skins=arr(skin, 0, 4096), controllers=controller, facial=facial,
                   attachments=arr(socket, 0, 1024), animations=arr(clip_ref, 0, 64),
                   state_mapping=mapping(state_map, 416), uncertainties=arr(uncertainty, 0, 1024),
                   provenance=provenance, limits=limits,
                   review=obj(dict(status={'enum': ['pending', 'approved', 'rejected']}, receipts=arr(file_ref))),
                   validation=arr(file_ref)))
    template_bone = obj(dict(id=identifier, role=identifier, parent=nullable_id, head=vec, tail=vec,
                             deform=boolean, mirror=nullable_id, roll=num),
                        ['id', 'role', 'parent', 'head', 'tail', 'deform'])
    template = obj(dict(schema_version={'const': 1}, kind={'const': 'hatch-3d-rig-template'},
                        id=identifier, version=version, extends=arr(identifier, 0, 8),
                        description=text, rest_pose={'enum': ['T', 'A', 'stance', 'mechanical', 'custom']},
                        bones=arr(template_bone, 0, 256), constraints=arr(constraint, 0, 1024),
                        ik=arr(ik, 0, 256), capabilities=arr(identifier, 0, 128),
                        sockets=arr(socket, 0, 1024), retarget_group=identifier,
                        symmetry={'enum': ['explicit', 'none']}, fitting_notes=text, rest_bounds=arr(vec, 2, 2)))
    correction = obj(dict(schema_version={'const': 1}, kind={'const': 'hatch-3d-rig-correction'},
                          rig_id=identifier, rig_version=version, expected_sha256=digest,
                          author=text, reason=text, created_at=text,
                          edits=arr(obj(dict(operation={'enum': ['bone-update', 'bone-add', 'bone-remove',
                          'weights', 'constraint', 'ik', 'attachment', 'facial', 'expression', 'morph', 'template']},
                          target=identifier, value={'type': ['object', 'string', 'null']})), 1, 1024)))
    review = obj(dict(schema_version={'const': 1}, kind={'const': 'hatch-3d-rig-review'},
                      rig_id=identifier, rig_version=version, expected_sha256=digest,
                      author=text, reviewer_kind={'enum': ['human', 'agent', 'synthetic-test']},
                      created_at=text, fixture=boolean, evidence=arr(file_ref, 1, 1000),
                      dimensions=mapping(obj(dict(verdict={'enum': ['approved', 'rejected', 'unreviewed']},
                                                       note=text)), 16)))
    step = obj(dict(status={'enum': ['running', 'succeeded', 'failed', 'stale', 'interrupted']},
                    input_sha256=digest, output_sha256={'anyOf': [digest, {'type': 'null'}]},
                    parameters={'type': 'object'}, parameters_sha256=digest, code_sha256=digest,
                    attempts=integer, token=text, started_at=text, finished_at={'type': ['string', 'null']},
                    error={'type': ['object', 'null']}, artifacts=arr(file_ref)))
    job = obj(dict(schema_version={'const': 1}, kind={'const': 'hatch-3d-rig-job'}, id=identifier,
                   status={'enum': ['imported', 'geometry-analyzed', 'skeleton-generated', 'weights-generated',
                    'structurally-valid', 'motion-tested', 'needs-correction', 'review-approved', 'animation-ready',
                    'exported', 'roundtrip-verified', 'failed', 'blocked']},
                   rig_sha256=digest, steps=mapping(step, 64), limits=limits, history=arr({'type': 'object'}, 0, 10000)))
    retarget = obj(dict(schema_version={'const': 1}, kind={'const': 'hatch-3d-retarget-profile'},
                       id=identifier, source_rig=identifier, target_rig=identifier,
                       source_skeleton_version=version, target_skeleton_version=version,
                       mapping=mapping(identifier, 256), root_scale=positive,
                       orientation_offsets=mapping(quat, 256), omissions=arr(text, 0, 256),
                       morph_mapping=mapping(identifier, 128)))
    # Export receipts describe the files actually written by each adapter. They
    # are not validation reports and do not confer independent verification.
    count = dict(type='integer', minimum=1, maximum=2_000_000_000)
    error_distance = dict(type='number', minimum=0, maximum=1e9)
    gltf_export = obj(dict(schema_version={'const': 1}, format={'enum': ['glb', 'gltf']},
                           path=text, sha256=digest, rig_sha256=digest, vertices=count,
                           skins=count, joints=count, animations=arr(identifier, 0, 64),
                           keyframes=integer, independently_verified={'const': False}))
    blender_sample = obj(dict(clip=identifier, time=dict(type='number', minimum=0, maximum=120),
                              matched=arr(text), meshes=mapping(arr(vec)), bones=mapping(vec),
                              sockets=mapping(arr(arr(num,4,4),4,4))), ['clip','time','matched','meshes','bones'])
    blender_fields = dict(schema_version={'const': 1}, format={'const': 'blend'},
                          blender_version=text, meshes=count, vertices=count, armatures=count,
                          bones=count, armature_modifiers=count, vertex_groups=count,
                          shape_keys=mapping(arr(text)),
                          actions=arr(obj(dict(name=text, slots=arr(text)))),
                          nla=mapping(arr(text)), controls=arr(text), constraints=integer,
                          samples=arr(blender_sample), ok=boolean, command=arr(text, 1, 64),
                          path=text, sha256=digest, independent={'const': True},
                          reader={'const': 'Blender'}, reopened=boolean,
                          rig_sha256=digest, source_glb_sha256=digest)
    verification_fields = dict(max_vertex_error_m=error_distance, max_joint_error_m=error_distance,
                               max_socket_transform_error=error_distance,
                               compared_vertices=count, tolerance_m=positive,
                               roundtrip_path=text, roundtrip_sha256=digest)
    blender_export = obj({**blender_fields, **verification_fields}, list(blender_fields))
    blender_export['allOf'] = [{
        'if': {'properties': {'reopened': {'const': True}}},
        'then': {'required': [field for field in verification_fields if field != 'max_socket_transform_error']},
        'else': {'not': {'anyOf': [{'required': [field]} for field in verification_fields]}}
    }]
    export_receipt = {'oneOf': [ref('RigGltfExportReceipt'), ref('RigBlenderExportReceipt')]}
    return dict(Character3D=character, GeometryAsset=geometry, Skeleton=skeleton, Bone=bone,
                SkinBinding=skin, VertexInfluence=obj(dict(joint=identifier, weight=dict(type='number', minimum=0, maximum=1))),
                RigTemplate=template, RigController=controller, IKChain=ik, JointConstraint=constraint,
                ContactTarget=contact, AttachmentPoint=socket, FacialController=facial,
                MorphTarget=obj(dict(schema_version={'const': 1}, id=identifier, mesh=identifier, data=file_ref)),
                Pose=pose, AnimationClip=clip, AnimationTrack=track, AnimationEvent=event,
                RetargetProfile=retarget, RigValidationReport=report, RigCorrection=correction,
                RigReviewReceipt=review, RigJob=job, RigExportReceipt=export_receipt,
                RigGltfExportReceipt=gltf_export, RigBlenderExportReceipt=blender_export, Rig=rig,
                Provenance=provenance, Limits=limits)


KINDS = {'rig': 'Rig', 'rig-template': 'RigTemplate', 'rig-animation': 'AnimationClip',
         'rig-correction': 'RigCorrection', 'rig-job': 'RigJob', 'rig-review': 'RigReviewReceipt',
         'rig-pose': 'Pose', 'rig-retarget': 'RetargetProfile', 'rig-export': 'RigExportReceipt'}


@lru_cache(maxsize=1)
def schemas() -> dict:
    defs = definitions()
    return {name + '.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'https://gethatch.pet/schemas/' + name + '-v1', 'title': kind + ' v1',
            '$defs': defs, '$ref': '#/$defs/' + kind} for name, kind in KINDS.items()}


@lru_cache(maxsize=32)
def _validator(kind: str):
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RigError('RIG_DEPENDENCY', '3D contracts need the optional requirements-rig.txt dependencies.') from exc
    defs = definitions()
    require(kind in defs, 'RIG_SCHEMA_KIND', 'Unknown contract kind: ' + kind)
    return Draft202012Validator({'$defs': defs, '$ref': '#/$defs/' + kind})


def validate_contract(value: Any, kind: str) -> Any:
    finite_json(value)
    error = next(_validator(kind).iter_errors(value), None)
    if error:
        raise RigError('RIG_SCHEMA', error.message, stage=kind,
                       asset='.'.join(map(str, error.absolute_path)))
    return value
