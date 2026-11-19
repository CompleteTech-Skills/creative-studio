"""Numerical acceptance measures real deformed geometry and world-space contacts."""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np
from rig_animation import contact_is_active, evaluate_animation, validate_animation
from rig_constraints import apply_constraints
from rig_contracts import validate_contract
from rig_errors import RigError, object_hash, timestamp
from rig_geometry import deformation_metrics
from rig_ik import validate_controllers
from rig_math import axis_angle, decompose, qangle
from rig_models import rig_signature, validate_geometry
from rig_skeleton import evaluate_fk, neutral_pose, rotate_bone, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin

REVIEW_DIMENSIONS = ('skeleton-placement', 'bone-semantics', 'deformation', 'joint-movement',
                     'facial-animation', 'contacts', 'animation-continuity', 'identity', 'export-interoperability')


def _report(rig, kind):
    return dict(schema_version=1, kind=kind, rig_sha256=rig_signature(rig.manifest), ok=True,
                metrics={}, checks=[], artifacts=[], created_at=timestamp())


def _check(report, name, ok, message, value=None, *, unavailable=False, warning=False):
    status = 'unavailable' if unavailable else 'warning' if warning else 'passed' if ok else 'failed'
    report['checks'].append(dict(name=name, status=status, message=message, value=value))
    if status == 'failed':report['ok'] = False


def contact_metrics(rig, clip) -> dict:
    chains = {c['id']: c for c in rig.manifest['controllers']['ik']}
    contacts = clip['contacts']
    endpoints = [chains[contact['chain']]['bones'][-1] for contact in contacts]
    positions = [[] for _ in contacts]
    position_samples = {}
    oriented = {}
    # The solver uses this angular bound. Chain tolerance is positional.
    orientation_tolerance = 1e-5
    orientation_errors = {}
    orientation_converged = {}
    for index, contact in enumerate(contacts):
        for time in np.linspace(contact['start'], contact['end'], 7, endpoint=False):
            position_samples.setdefault(float(time), []).append(index)
        goal = contact.get('orientation')
        if goal is None:
            goal = chains[contact['chain']].get('orientation')
        if goal is not None:
            oriented[index] = goal
            orientation_errors[index] = []
            orientation_converged[index] = True

    # Reuse final FK across contacts. Angular checks cover every active goal at
    # each existing contact sample, while positions retain their own seven times.
    for time, sample_indices in sorted(position_samples.items()):
        evaluated = evaluate_animation(rig, clip, time, _validated=True)
        world = evaluate_fk(rig.skeleton, evaluated['pose'])
        for index in sample_indices:
            positions[index].append(world[endpoints[index]][:3, 3])
        receipts = {(row['id'], row['chain']): row for row in evaluated['contacts']}
        for index, goal in oriented.items():
            contact = contacts[index]
            if not contact_is_active(contact, evaluated['time'], clip['duration']):
                continue
            error = qangle(decompose(world[endpoints[index]])['rotation'], goal)
            if not math.isfinite(error):
                raise RigError('RIG_CONTACT_ORIENTATION', 'Final contact orientation error is not finite.',
                               asset=contact['id'])
            orientation_errors[index].append(error)
            native = receipts.get((contact['id'], contact['chain']), {})
            native_error = native.get('orientation_residual_rad')
            orientation_converged[index] = (
                orientation_converged[index] and error <= orientation_tolerance
                and native.get('converged') is True
                and isinstance(native_error, (int, float))
                and math.isfinite(native_error) and 0 <= native_error <= orientation_tolerance)

    maximum, drift, count = 0., 0., 0
    orientation_maximum, orientation_count = None, 0
    by_contact = []
    for index, contact in enumerate(contacts):
        points = np.array(positions[index])
        local_error = float(np.max(np.linalg.norm(points - np.asarray(contact['target']), axis=1)))
        local_drift = float(np.max(np.linalg.norm(points - points[0], axis=1)))
        maximum = max(maximum, local_error)
        drift = max(drift, local_drift)
        count += len(points)
        angles = orientation_errors.get(index, [])
        local_orientation_maximum = max(angles) if angles else None
        if local_orientation_maximum is not None:
            orientation_maximum = (local_orientation_maximum if orientation_maximum is None
                                   else max(orientation_maximum, local_orientation_maximum))
        orientation_count += len(angles)
        by_contact.append(dict(id=contact['id'], samples=len(points), max_error_m=local_error, max_drift_m=local_drift,
                               max_orientation_error_rad=local_orientation_maximum, orientation_samples=len(angles),
                               orientation_tolerance_rad=orientation_tolerance if index in oriented else None,
                               orientation_converged=orientation_converged[index] and bool(angles)
                               if index in oriented else None))
    metrics = dict(max_contact_error_m=maximum, max_planted_drift_m=drift, samples=count, contacts=by_contact,
                   max_contact_orientation_error_rad=orientation_maximum, orientation_samples=orientation_count)
    if orientation_count:
        metrics.update(clip_id=clip['id'], clip_version=clip['version'], clip_content_sha256=object_hash(clip),
                       orientation_converged=all(c['orientation_converged'] is True for c in by_contact
                                                 if c['orientation_tolerance_rad'] is not None))
    return metrics


def validate_rig(rig, *, require_animation=False) -> dict:
    report = _report(rig, 'hatch-3d-rig-validation')
    try:
        validate_contract(rig.manifest, 'Rig');validate_geometry(rig.geometry, rig.manifest['limits'])
        skeleton_report = validate_skeleton(rig.skeleton, rig.manifest['limits'])
        validate_controllers(rig.skeleton, rig.manifest['controllers'])
        from rig_facial import validate_eyelid_bindings
        validate_eyelid_bindings(rig)
        _check(report, 'structure', True, 'Finite acyclic skeleton, geometry and controller graph.', 0)
        report['metrics'].update(hierarchy_cycles=0, invalid_joint_references=0, bones=skeleton_report['bones'],
                                  vertices=rig.geometry.vertex_count, triangles=rig.geometry.triangle_count)
    except (RigError, ValueError, KeyError) as exc:
        _check(report, 'structure', False, str(exc));return report
    weight_error = 0.;bind_error = 0.
    try:
        if set(rig.weights) != set(rig.geometry.by_id()):raise RigError('RIG_MISSING_SKIN', 'Some geometry has no real skin binding.')
        for mesh in rig.geometry.meshes:
            value = validate_skin(rig.weights[mesh.id], mesh, rig.skeleton, rig.manifest['limits'])
            weight_error = max(weight_error, value['weight_sum_max_error'])
        pose = neutral_pose(rig.skeleton)
        pose['morphs'] = {m.id: {name: 0. for name in m.morphs} for m in rig.geometry.meshes}
        deformed = evaluate_deformation(rig, pose)
        bind_error = max(float(np.abs(deformed[m.id]['positions']-m.positions).max()) for m in rig.geometry.meshes)
        _check(report, 'skin', True, 'Every vertex has normalized real bone weights.', weight_error)
        _check(report, 'bind-pose', bind_error <= 1e-5, 'Canonical bind-pose vertex reconstruction (meters).', bind_error)
    except (RigError, ValueError, KeyError) as exc:
        _check(report, 'skin', False, str(exc));return report
    report['metrics'].update(weight_sum_max_error=weight_error, bind_pose_max_error_m=bind_error, nonfinite_vertices=0)
    height = max(m.positions[:, 1].max() for m in rig.geometry.meshes)-min(m.positions[:, 1].min() for m in rig.geometry.meshes)
    contact_tolerance = min(.01, max(height*.01, 1e-5));maximum = 0.;distinct = set();contact_max = 0.;drift_max = 0.;key_total = 0
    orientation_max, orientation_samples, orientation_ok = None, 0, True
    chains = {c['id']: c for c in rig.manifest['controllers']['ik']}
    for clip in rig.clips.values():
        required_orientation = False
        try:
            result = validate_animation(rig, clip);key_total += result['keyframes']
            required_orientation = any(c.get('orientation') is not None
                                       or chains.get(c['chain'], {}).get('orientation') is not None
                                       for c in clip['contacts'])
            for fraction in (0., .17, .37, .63, .83, 1.):
                evaluated = evaluate_animation(rig, clip, clip['duration']*fraction, _validated=True)
                values = evaluate_deformation(rig, evaluated['pose'])
                signature = []
                for mesh in rig.geometry.meshes:
                    positions = values[mesh.id]['positions']
                    maximum = max(maximum, float(np.linalg.norm(positions-mesh.positions, axis=1).max()))
                    signature.append(np.round(positions[::max(1, len(positions)//64)], 5).tolist())
                distinct.add(object_hash(signature))
            contacts = contact_metrics(rig, clip)
            contact_max = max(contact_max, contacts['max_contact_error_m']);drift_max = max(drift_max, contacts['max_planted_drift_m'])
            if required_orientation:
                if contacts['orientation_samples']:
                    angular_error = contacts['max_contact_orientation_error_rad']
                    orientation_max = angular_error if orientation_max is None else max(orientation_max, angular_error)
                    # Neither a clip identifier nor its digest can contain a slash.
                    prefix = f"contact_orientation/{contacts['clip_id']}/{contacts['clip_content_sha256']}/"
                    report['metrics'].update({
                        prefix + 'clip_version': contacts['clip_version'],
                        prefix + 'max_error_rad': angular_error,
                        prefix + 'samples': contacts['orientation_samples'],
                        prefix + 'converged': contacts['orientation_converged'],
                    })
                orientation_samples += contacts['orientation_samples']
                orientation_ok = orientation_ok and contacts.get('orientation_converged') is True
            _check(report, 'clip:'+clip['id'], True, 'Channel evaluation produces finite real mesh positions.', result['tracks'])
        except (RigError, ValueError, KeyError) as exc:
            if required_orientation:orientation_ok = False
            _check(report, 'clip:'+clip['id'], False, str(exc))
    _check(report, 'key-budget', key_total <= rig.manifest['limits']['keyframes'], 'Total authored keyframe budget.', key_total)
    _check(report, 'contacts', contact_max <= contact_tolerance and drift_max <= contact_tolerance and orientation_ok,
           'Measured IK endpoint error, planted world-space drift and required final orientation/convergence.', max(contact_max, drift_max))
    if require_animation:
        _check(report, 'animated-geometry', bool(rig.clips) and maximum > 1e-5 and len(distinct) >= 2,
               'Acceptance requires executable channels and distinct deformed vertices.', maximum)
    report['metrics'].update(max_vertex_displacement_m=maximum, distinct_sampled_poses=len(distinct),
                              clips=len(rig.clips), contact_tolerance_m=contact_tolerance,
                              max_contact_error_m=contact_max, max_planted_drift_m=drift_max,
                              max_contact_orientation_error_rad=orientation_max, orientation_samples=orientation_samples)
    _check(report, 'visual-review', True, 'Numerical validation does not establish artistic or anatomical approval.',
           rig.manifest['review']['status'], unavailable=rig.manifest['review']['status'] != 'approved')
    return report


def test_motion_ranges(rig) -> dict:
    report = _report(rig, 'hatch-3d-motion-range')
    tests = [];max_stretch = 1.;max_displacement = 0.;nonfinite = 0
    constraints = rig.manifest['controllers']['constraints']
    for constraint in constraints:
        kind = constraint['type']
        if kind in ('hinge', 'swing-twist'):
            values = [constraint['minimum'], constraint['maximum']];axis = constraint.get('axis', [0, 1, 0])
        elif kind == 'ball':values = [-constraint.get('swing', math.pi), constraint.get('swing', math.pi)];axis = [1, 0, 0]
        elif kind == 'translation':values = [constraint['minimum'], constraint['maximum']];axis = None
        else:continue
        for value in values:
            pose = neutral_pose(rig.skeleton, constraint['id']+'-range')
            if axis is not None:pose = rotate_bone(rig.skeleton, pose, constraint['bone'], axis_angle(axis, value))
            else:
                bone = next(b for b in rig.skeleton['bones'] if b['id'] == constraint['bone'])
                translation = np.array(bone['rest']['translation']);translation[constraint.get('axes', [0, 1, 2])] += value
                pose['bones'][bone['id']] = dict(translation=translation.tolist())
            pose, _ = apply_constraints(rig.skeleton, pose, constraints)
            try:
                geometry = evaluate_deformation(rig, pose)
                metrics = [deformation_metrics(mesh, geometry[mesh.id]['positions']) for mesh in rig.geometry.meshes]
                stretch = max(m['max_edge_stretch'] for m in metrics)
                displacement = max(m['max_vertex_displacement_m'] for m in metrics)
                max_stretch = max(max_stretch, stretch);max_displacement = max(max_displacement, displacement)
                tests.append(dict(constraint=constraint['id'], value=value, finite=True,
                                  max_edge_stretch=stretch, max_vertex_displacement_m=displacement,
                                  newly_degenerate_triangles=sum(m['newly_degenerate_triangles'] for m in metrics)))
            except RigError as exc:
                nonfinite += 1;tests.append(dict(constraint=constraint['id'], value=value, finite=False, error=exc.report))
    report['metrics'].update(tested_range_poses=len(tests), nonfinite_poses=nonfinite,
                              max_edge_stretch=max_stretch, max_vertex_displacement_m=max_displacement)
    _check(report, 'range-finiteness', nonfinite == 0, 'Real geometry was evaluated at the declared representative joint limits.', nonfinite)
    _check(report, 'stretch-review', True, 'Extreme range stretching requires visual review; it is measured, not hidden.', max_stretch, warning=max_stretch > 2.)
    _check(report, 'surface-intersections', False, 'Robust arbitrary mesh self-intersection analysis is unavailable in the bounded core.', unavailable=True)
    return dict(report=report, poses=tests)
