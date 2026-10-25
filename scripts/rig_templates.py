"""Versioned semantic templates and geometry/landmark fitting with reviewable uncertainty."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import numpy as np
from rig_contracts import validate_contract
from rig_errors import read_json, require
from rig_geometry import analyze_geometry
from rig_models import Geometry
from rig_skeleton import skeleton_from_heads, validate_skeleton

ROOT = Path(__file__).resolve().parents[1]/'rig-templates'


def _validate_landmarks(landmarks, bone_ids):
    """Reject malformed authored input before fitting or deriving review status."""
    require(isinstance(landmarks, dict), 'RIG_LANDMARK', 'Landmarks must be an object keyed by bone ID.')
    require(set(landmarks) <= bone_ids, 'RIG_LANDMARK_REFERENCE', 'Landmarks reference an undeclared bone.')
    for name, landmark in landmarks.items():
        require(isinstance(landmark, dict) and
                {'head', 'tail', 'approved'} <= set(landmark) <=
                {'head', 'tail', 'source', 'confidence', 'reference', 'approved'},
                'RIG_LANDMARK', 'Each landmark needs head, tail and approval with no unsupported fields.', asset=name)
        require(landmark['approved'] is True, 'RIG_LANDMARK_REVIEW',
                'Supplied landmarks must explicitly identify approved annotations.', asset=name)
        for field in ('head', 'tail'):
            value = landmark[field]
            require(isinstance(value, list) and len(value) == 3 and
                    all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value),
                    'RIG_LANDMARK', 'Landmark '+field+' must contain three numeric coordinates.', asset=name)


def load_rig_template(name: str | Path, *, directory: Path | None = None, _active=()) -> dict:
    directory = Path(directory or ROOT)
    path = Path(name)
    if path.suffix != '.json':
        require('/' not in str(name) and '\\' not in str(name) and '..' not in str(name), 'RIG_TEMPLATE_PATH', 'Use a template ID or explicit JSON file.')
        path = directory/(str(name)+'.json')
    require(path.is_file() and not path.is_symlink(), 'RIG_TEMPLATE', 'Template does not exist: '+str(path))
    value = validate_contract(read_json(path), 'RigTemplate')
    require(value['id'] not in _active, 'RIG_TEMPLATE_CYCLE', 'Template inheritance contains a cycle.')
    combined = deepcopy(value)
    for key in ('bones', 'constraints', 'ik', 'sockets', 'capabilities'):combined[key] = []
    for parent in value['extends']:
        inherited = load_rig_template(parent, directory=path.parent, _active=(*_active, value['id']))
        for key in ('bones', 'constraints', 'ik', 'sockets', 'capabilities'):combined[key].extend(deepcopy(inherited[key]))
    for key in ('bones', 'constraints', 'ik', 'sockets', 'capabilities'):
        combined[key].extend(value[key])
        if key == 'capabilities':combined[key] = sorted(set(combined[key]));continue
        ids = [v['id'] for v in combined[key]]
        require(len(set(ids)) == len(ids), 'RIG_TEMPLATE_CONFLICT', 'Template composition repeats '+key+' IDs.')
    require(len({b['role'] for b in combined['bones']}) == len(combined['bones']),
            'RIG_TEMPLATE_SEMANTICS', 'Template semantic roles must be unambiguous.')
    return combined


def list_templates(directory=None) -> list[dict]:
    result = []
    for path in sorted(Path(directory or ROOT).glob('*.json')):
        template = load_rig_template(path)
        result.append(dict(id=template['id'], version=template['version'], bones=len(template['bones']),
                           ik_chains=len(template['ik']), capabilities=template['capabilities'],
                           description=template['description']))
    return result


def generate_skeleton(geometry: Geometry, template: dict | str, *, landmarks=None,
                       existing=None, character_id='character') -> dict:
    """Fit approved landmarks first, then semantic parts, then measured bounds.

    Bounding-box fitting is intentionally never presented as anatomical proof.
    All unauthenticated geometric suggestions remain pending visual correction.
    """
    if existing is not None:
        return dict(skeleton=deepcopy(existing), controllers=dict(ik=[], constraints=[]),
                    attachments=[], uncertainties=[], status='preserved-existing-rig')
    template = load_rig_template(template) if isinstance(template, (str, Path)) else deepcopy(template)
    analysis = analyze_geometry(geometry)
    lower, upper = np.array(analysis['bounds'])
    source_bounds = np.array(template.get('rest_bounds', [[-.85, 0, -.35], [.85, 2.05, .35]]))
    dimensions = upper-lower
    require(np.all(source_bounds[1]-source_bounds[0] > 1e-8), 'RIG_TEMPLATE_BOUNDS', 'Template bounds must have positive extent.')
    factor = dimensions/(source_bounds[1]-source_bounds[0])
    # A thin surface is a valid input but cannot supply confident depth anatomy.
    factor = np.maximum(factor, max(float(dimensions.max()), 1e-3)*.01)
    offset = lower-source_bounds[0]*factor
    specs, uncertainties, evidence = [], [], {}
    landmarks = {} if landmarks is None else landmarks
    _validate_landmarks(landmarks, {b['id'] for b in template['bones']})
    for bone in template['bones']:
        spec = deepcopy(bone)
        head = np.asarray(bone['head'])*factor+offset
        tail = np.asarray(bone['tail'])*factor+offset
        origin = dict(source='geometry-fit', reference='Measured mesh bounds; anatomical placement is uncertain', confidence=.4)
        if bone['id'] in landmarks:
            landmark = landmarks[bone['id']]
            head = np.asarray(landmark['head'], dtype=float);tail = np.asarray(landmark['tail'], dtype=float)
            origin = dict(source=landmark.get('source', 'human-landmark'),
                          reference=landmark.get('reference', 'Supplied landmark correction'), confidence=landmark.get('confidence', 1.))
        else:
            parts = [m for m in geometry.meshes if m.region == bone['id']]
            if parts:
                points = np.concatenate([m.positions for m in parts])
                mean = points.mean(axis=0)
                _, _, axes = np.linalg.svd(points-mean, full_matrices=False)
                direction = axes[0]
                if np.dot(direction, tail-head) < 0:direction = -direction
                projection = (points-mean) @ direction
                if projection.max()-projection.min() > 1e-6:
                    head = mean+direction*np.quantile(projection, .08)
                    tail = mean+direction*np.quantile(projection, .92)
                origin = dict(source='semantic-part', reference='Centerline of imported semantic region '+bone['id'], confidence=.7)
        spec.update(head=head.tolist(), tail=tail.tolist())
        evidence[bone['id']] = origin;specs.append(spec)
    skeleton = skeleton_from_heads(specs, identifier=character_id+'-skeleton')
    for bone in skeleton['bones']:bone['evidence'] = evidence[bone['id']]
    # skeleton_from_heads validates default evidence. Validate again with the
    # actual annotations before computing status or returning the fitted rig.
    validate_skeleton(skeleton)
    for name, origin in evidence.items():
        if origin['source'] in ('geometry-fit', 'semantic-part', 'import-metadata') or origin['confidence'] < .9:
            uncertainties.append(dict(element=name, code='RIG_ANATOMY_UNCERTAIN',
                message='Inspect joint position, direction and anatomical role before production approval.',
                evidence=origin, resolution='pending'))
    controllers = dict(ik=deepcopy(template['ik']), constraints=deepcopy(template['constraints']))
    for chain in controllers['ik']:
        chain['pole'] = (np.asarray(chain['pole'])*factor+offset).tolist()
        chain['tolerance'] = max(1e-6, min(.001, analysis['dimensions_m'][1]*1e-5))
    attachments = deepcopy(template['sockets'])
    for socket in attachments:
        socket['owner'] = character_id
        socket['transform']['translation'] = (np.asarray(socket['transform']['translation'])*float(factor[1])).tolist()
    return dict(skeleton=skeleton, controllers=controllers, attachments=attachments,
                uncertainties=uncertainties, status='needs-correction' if uncertainties else 'skeleton-generated',
                analysis=analysis, template_id=template['id'], template_version=template['version'])
