"""Read-only numerical and retained-media gates for the fixed Director narrative.

Native Scene3D, rig, camera, collision and Render3D owners execute the work.
This module neither publishes files nor grants artistic approval. The caller
persists its returned proof outside the immutable project through its existing
publication authority, then pins that proof with an exact Director ArtifactRef.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from fractions import Fraction
from functools import lru_cache
import hashlib
import inspect
import math
from pathlib import Path

import numpy as np

from director_narrative import (ACTORS, CONTACT_END, CONTACT_START, FPS, FRAMES,
                                GRIPS, HANDOFF, PICKUP, SHOTS, world_pose)
from director_contracts import _read_artifact
from director_schema import schemas
from rig_animation import evaluate_animation, validate_animation
import rig_attachments
from rig_errors import RigError, decode_json, finite_json, object_hash, require
from rig_ik import validate_controllers
from rig_math import decompose, qangle, transform_matrix, transform_points
from rig_models import load_rig, validate_geometry
from rig_skeleton import evaluate_fk, validate_skeleton
from rig_skinning import evaluate_deformation, validate_skin
from scene3d_contracts import load_project, portable_path
from scene3d_evaluate import evaluate_scene, camera_matrices

LIMITS = dict(foot_ground_m=.002, plant_drift_m=.001, grip_m=.01,
              grip_rotation_rad=.01, prop_matrix_error=1e-7,
              prop_translation_step_m=.03, prop_rotation_step_rad=.1,
              pose_equivalence=1e-7, minimum_root_travel_m=2.5,
              max_vertices_per_actor=10000, max_bones_per_actor=128)
CRITICAL_FRAMES = (360, 516, 527, 528, 529, 540, 564, 648)
GEOMETRY_NODES = frozenset((*ACTORS, 'laboratory', 'access-key', 'laboratory-dressing'))
VIEW_PROFILE = {name: (name, None) for name, _, _ in SHOTS}
VIEW_PROFILE.update({'cooperation': ('cooperation', HANDOFF),
    'geometry-control-before': ('cooperation', 480),
    'handoff-before': ('cooperation', HANDOFF-1),
    'handoff-after': ('cooperation', HANDOFF+1),
    'proof-left': ('proof-left', HANDOFF), 'proof-right': ('proof-right', HANDOFF),
    'proof-light-off': ('proof-light-off', 648), 'proof-light-on': ('proof-light-on', 648)})


def _need(condition, code, message, **details):
    require(condition, 'DIRECTOR_NARRATIVE_'+code, message,
            stage='director-narrative', **details)


def _number(value, *, minimum=0., maximum=1e15):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def _root(path):
    path = Path(path).absolute()
    _need(path.is_dir() and not path.is_symlink(), 'PATH', 'Expected a real project/evidence directory.')
    _need(all(not parent.is_symlink() for parent in path.parents),
          'PATH', 'Project/evidence parents must use their canonical directory spelling.')
    return path


@lru_cache(maxsize=1)
def _reference_validator():
    from jsonschema import Draft202012Validator
    schema = schemas()['director.schema.json']
    return Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/artifact-ref'})


def _artifact(root, reference, *, maximum=32*1024**2):
    try:
        finite_json(reference)
        _need(_reference_validator().is_valid(reference), 'REFERENCE', 'Expected an exact Director ArtifactRef.')
        _need(reference['byte_size'] <= maximum, 'LIMIT', 'Evidence artifact exceeds its bounded reader profile.')
        portable_path(reference['path'])
        return _read_artifact(root, reference)
    except RigError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise RigError('DIRECTOR_NARRATIVE_REFERENCE', 'Invalid bounded evidence reference.',
                       stage='director-narrative') from exc


def _file_bytes(root, inventory, path):
    portable_path(path)
    _need(path in inventory, 'REFERENCE', 'Native dependency is outside the verified project closure.', asset=path)
    record = inventory[path]
    _need(record['bytes'] <= 64*1024**2, 'LIMIT', 'Native dependency exceeds the 64 MiB file profile.', asset=path)
    return _read_artifact(root, dict(path=path, byte_size=record['bytes'], sha256=record['sha256']))


def _recheck(root, inventory):
    # Comparing the full exact closure also rechecks project.json. Re-loading
    # every Scene3D document on every frame is neither necessary nor permitted.
    for path in sorted(inventory):
        _file_bytes(root, inventory, path)


def _geometry_digest(meshes):
    digest = hashlib.sha256()
    for identifier, points in sorted(meshes.items()):
        raw = identifier.encode('utf-8')
        digest.update(len(raw).to_bytes(4, 'little')); digest.update(raw)
        array = np.asarray(points, dtype='<f8')
        _need(array.ndim == 2 and array.shape[1] == 3 and np.isfinite(array).all(),
              'GEOMETRY', 'Native sampling returned invalid actual vertices.')
        digest.update(len(array).to_bytes(8, 'little')); digest.update(array.tobytes())
    return digest.hexdigest()


def _event_frames(clip, target, types):
    events = []
    for event in clip['events']:
        if event['target'] != target or event['type'] not in types:
            continue
        position = event['time']*FPS
        frame = round(position)
        _need(abs(position-frame) <= 1e-7 and 0 <= frame <= FRAMES,
              'EVENT', 'A native narrative event is not on its exact frame boundary.')
        events.append((frame, event['type'], event.get('value')))
    _need(len({(f, t) for f, t, _ in events}) == len(events), 'EVENT', 'Native events repeat an authority transition.')
    return sorted(events)


class _Context:
    def __init__(self, root, bundle):
        self.root, self.bundle = root, bundle
        self.inventory = deepcopy(bundle['inventory'])
        self.project_sha = self.inventory['project.json']['sha256']
        project, docs = bundle['project'], bundle['documents']
        _need(len(project['scenes']) == 1, 'SCENE', 'Select exactly one production scene/version.')
        self.scene_ref = project['scenes'][0]
        self.scene = docs[self.scene_ref['path']]
        _need(self.scene['id'] == 'director-laboratory-narrative', 'SCENE', 'Select the actual narrative scene.')
        self.nodes = {node['id']: node for node in self.scene['nodes']}
        _need({r['id'] for r in project['characters']} == set(ACTORS) and len(project['characters']) == 2,
              'IDENTITY', 'Exactly the two persistent robot character identities are required.')
        geometry = {n['id'] for n in self.scene['nodes'] if 'asset' in n or n['type'] in
                    ('skinned_character', 'mesh_instance', 'animated_prop', 'billboard_2d')}
        _need(geometry == GEOMETRY_NODES, 'IDENTITY', 'Unexpected/missing geometry instance or hidden clone.')
        self.assets = {r['id']: docs[r['path']] for r in project['assets']}
        _need(set(self.assets) == GEOMETRY_NODES, 'IDENTITY', 'Narrative geometry asset inventory differs.')
        self.shots = {r['id']: r for r in project['shots']}
        _need(len(self.shots) == len(project['shots']) and
              set(self.shots) == {s for s, _, _ in SHOTS} | {'proof-left', 'proof-right', 'proof-light-off', 'proof-light-on'},
              'SHOTS', 'Five exact production shots and four separately identified proof shots are required.')
        self.production = []
        self.resolution = None
        for name, start, end in SHOTS:
            reference = self.shots[name]; shot = docs[reference['path']]
            _need(shot['scene'] == self.scene_ref and shot['duration_frames'] == end-start
                  and shot['fps'] == dict(numerator=FPS, denominator=1), 'TIMING', 'Production shot timing or exact scene binding differs.')
            resolution = [shot['render']['width'], shot['render']['height']]
            _need(resolution[0] >= 1280 and resolution[1] >= 720 and
                  (self.resolution is None or resolution == self.resolution),
                  'RESOLUTION', 'Final shots must share an actual resolution of at least 1280 by 720.')
            self.resolution = resolution
            self.production.append(dict(id=name, start_frame=start, end_frame=end,
                source_start=dict(numerator=0, denominator=1), playback_rate=dict(numerator=1, denominator=1),
                scene=deepcopy(self.scene_ref), shot=deepcopy(reference)))
        self.rigs, self.full, self.native_refs, self.identities = {}, {}, {}, {}
        for actor in ACTORS:
            asset = self.assets[actor]; reference = asset['skeleton']
            _need(asset['asset_type'] == 'character' and self.nodes[actor]['type'] == 'skinned_character'
                  and self.nodes[actor]['asset'] in project['characters'], 'IDENTITY', 'Character instance is not the exact registered native rig asset.')
            raw = decode_json(_file_bytes(root, self.inventory, reference['path']))
            rig = load_rig((root/portable_path(reference['path'])).parent)
            _need(rig.manifest == raw and raw['id'] == reference['id'] and raw['version'] == reference['version']
                  and self.inventory[reference['path']]['sha256'] == reference['sha256'],
                  'IDENTITY', 'Loaded rig does not match the complete exact native reference.')
            _need(rig.manifest['character']['id'] == actor and rig.geometry.vertex_count <= LIMITS['max_vertices_per_actor']
                  and len(rig.skeleton['bones']) <= LIMITS['max_bones_per_actor'],
                  'LIMIT', 'Native actor identity or bounded numerical profile differs.')
            original = load_rig(root/'source-original'/'native'/actor)
            _need(rig.manifest['geometry']['data']['sha256'] == original.manifest['geometry']['data']['sha256']
                  and rig.manifest['character'] == original.manifest['character']
                  and rig.manifest['source'] == original.manifest['source']
                  and rig.skeleton == original.skeleton and rig.geometry.materials == original.geometry.materials,
                  'IDENTITY', 'Narrative changed original geometry, skeleton, source, identity or materials.')
            validate_geometry(rig.geometry, rig.manifest['limits'])
            validate_skeleton(rig.skeleton, rig.manifest['limits'])
            validate_controllers(rig.skeleton, rig.manifest['controllers'])
            _need(set(rig.weights) == set(rig.geometry.by_id()), 'SKIN', 'Every actual character mesh needs native skin weights.')
            for mesh in rig.geometry.meshes:
                validate_skin(rig.weights[mesh.id], mesh, rig.skeleton, rig.manifest['limits'])
            refs = {}
            for name in [*(name for name, _, _ in SHOTS), 'full-narrative']:
                identifier = actor+'-'+name
                matches = [r for r in asset['animations'] if r['id'] == identifier]
                _need(len(matches) == 1 and identifier in rig.clips, 'CLIP', 'Required exact native motion is missing.')
                ref = matches[0]; descriptor = next(d for d in rig.manifest['animations'] if d['id'] == identifier)
                expected_path = str(Path(reference['path']).parent/descriptor['path'])
                _need(ref['path'] == expected_path and ref['version'] == descriptor['version']
                      and ref['sha256'] == descriptor['sha256']
                      and self.inventory[ref['path']]['sha256'] == ref['sha256'],
                      'CLIP', 'A native clip path, version or byte hash differs from its owner.')
                _need(decode_json(_file_bytes(root, self.inventory, ref['path'])) == rig.clips[identifier],
                      'CLIP', 'Loaded clip bytes changed during native loading.')
                clip = rig.clips[identifier]
                validate_animation(rig, clip)
                _need(clip['loop'] is False and clip['root_motion'] == 'in-place',
                      'CLIP', 'Scene root travel must not be doubled by looping or embedded native root motion.')
                duration_frames = FRAMES if name == 'full-narrative' else next(b-a for n, a, b in SHOTS if n == name)
                _need(abs(clip['duration']-duration_frames/FPS) <= 1e-9,
                      'TIMING', 'Native clip does not cover its exact approved shot duration.')
                refs[name] = ref
            self.rigs[actor], self.full[actor], self.native_refs[actor] = rig, rig.clips[actor+'-full-narrative'], refs
            _need(abs(self.full[actor]['duration']-FRAMES/FPS) <= 1e-9, 'TIMING', 'Full native clip does not cover exactly thirty seconds.')
            self.identities[actor] = dict(asset=deepcopy(self.nodes[actor]['asset']), rig=deepcopy(reference),
                geometry_sha256=rig.manifest['geometry']['data']['sha256'],
                skeleton_sha256=object_hash(rig.skeleton), materials_sha256=object_hash(rig.geometry.materials),
                character_identity_sha256=rig.manifest['character']['identity_sha256'], source_sha256=rig.manifest['source']['sha256'])
        for identifier in ('laboratory', 'access-key'):
            previous = decode_json(_file_bytes(root, self.inventory, 'source-original/'+identifier+'-asset.json'))
            _need(self.assets[identifier] == previous, 'IDENTITY', 'Original laboratory or prop identity changed.')
        for name, reference in self.shots.items():
            shot = docs[reference['path']]
            source = ('resolution' if name.startswith('proof-light-') else 'cooperation' if name.startswith('proof-') else name)
            expected = {a: self.native_refs[a][source] for a in ACTORS}
            _need(len(shot['bindings']) == 2 and {b['target'] for b in shot['bindings']} == set(ACTORS),
                  'CLIP', 'Each shot must bind exactly the two real native actors.')
            for binding in shot['bindings']:
                _need(binding['clip'] == expected[binding['target']] and binding['loop'] is False
                      and binding['speed'] == 1 and binding['start_frame'] == 0
                      and binding['end_frame'] == shot['duration_frames'],
                      'CLIP', 'Shot does not retain its exact non-looping native clip clock.')
        self.foot_events = {a: {side: _event_frames(self.full[a], 'foot.'+side, ('foot_contact', 'foot_release'))
                               for side in ('left', 'right')} for a in ACTORS}
        for actor in ACTORS:
            for side in ('left', 'right'):
                events = self.foot_events[actor][side]
                _need(events and events[0][0] == 0 and all(x[1] != y[1] for x, y in zip(events, events[1:])),
                      'EVENT', 'Foot events must initialize and alternate actual contact/release authority.')
        giver = _event_frames(self.full['aster'], 'access-key', ('grasp', 'release', 'hand_contact'))
        receiver = _event_frames(self.full['moss'], 'access-key', ('attach', 'hand_contact'))
        _need(giver == [(PICKUP, 'grasp', 'pickup-single-owner'), (CONTACT_START, 'hand_contact', 'paired-grip-contact-start'),
                        (HANDOFF, 'release', 'ownership-transfer-to-moss'), (CONTACT_END+1, 'release', 'giver-contact-end')]
              and receiver == [(CONTACT_START, 'hand_contact', 'paired-grip-contact-start'),
                               (HANDOFF, 'attach', 'ownership-transfer-from-aster')],
              'OWNERSHIP', 'Duplicate, stale or missing native ownership/contact events.')
        _recheck(root, self.inventory)


def _paired_transfer_available():
    return {'source_grip', 'target_grip'} <= set(inspect.signature(rig_attachments.transfer_prop).parameters)


def _active_plant(events, frame):
    return next(event[1] == 'foot_contact' for event in reversed(events) if event[0] <= frame)


def _sample(context, production, frame):
    local = frame-production['start_frame']
    evaluation = evaluate_scene(context.bundle, context.scene_ref['path'], production['shot']['path'],
                                local, source_root=context.root, sample_geometry=False)
    _need(evaluation['frame'] == dict(numerator=local, denominator=1), 'TIMING', 'Canonical scene frame differs.')
    nodes = evaluation['nodes']; actors = {}; raw = {}
    for actor, rig in context.rigs.items():
        state = nodes[actor]; world = np.asarray(state['world_matrix'], dtype=float)
        _need(np.allclose(world[:3, :3].T@world[:3, :3], np.eye(3), atol=1e-7, rtol=0)
              and abs(np.linalg.det(world[:3, :3])-1) <= 1e-7, 'SCALE', 'Actor scale or world handedness changed.')
        binding = state.get('native_clip')
        expected = context.native_refs[actor][production['id']]
        _need(binding is not None and binding['reference'] == expected and binding['loop'] is False
              and Fraction(**binding['time']) == Fraction(local, FPS), 'CLIP', 'Canonical native sample clock/pin differs.')
        pose = evaluate_animation(rig, rig.clips[expected['id']], float(Fraction(local, FPS)), loop=False, _validated=True)['pose']
        full_pose = evaluate_animation(rig, context.full[actor], float(Fraction(frame, FPS)), loop=False, _validated=True)['pose']
        local_fk, full_fk = evaluate_fk(rig.skeleton, pose), evaluate_fk(rig.skeleton, full_pose)
        pose_error = max(float(abs(local_fk[n]-full_fk[n]).max()) for n in local_fk)
        _need(pose_error <= LIMITS['pose_equivalence'] and pose['morphs'] == full_pose['morphs'],
              'CONTINUITY', 'Shot-native motion differs from its full-sequence pose.')
        fk = {n: world@matrix for n, matrix in local_fk.items()}
        meshes = {n: transform_points(world, m['positions']) for n, m in evaluate_deformation(rig, pose).items()}
        points = np.concatenate(list(meshes.values()))
        _need(np.isfinite(points).all() and points[:, 1].min() >= -LIMITS['foot_ground_m'],
              'GROUND', 'Actual character geometry penetrates the floor.', asset=actor)
        actors[actor] = dict(root_matrix=world.tolist(), root_position=world[:3, 3].tolist(),
            geometry_sha256=_geometry_digest(meshes), bones_sha256=object_hash({n: m.tolist() for n, m in fk.items()}),
            bounds=dict(minimum=points.min(axis=0).tolist(), maximum=points.max(axis=0).tolist()),
            native_clip=deepcopy(expected), native_time=deepcopy(binding['time']), pose_equivalence_error=pose_error,
            feet={side: dict(planted=_active_plant(context.foot_events[actor][side], frame),
                 sole_y_m=float(meshes['boot-'+side][:, 1].min())) for side in ('left', 'right')})
        raw[actor] = dict(pose=pose, world=world, fk=fk, meshes=meshes)
    camera = camera_matrices(evaluation, context.bundle['documents'][production['shot']['path']]['render'])
    for actor in ACTORS:
        points = np.concatenate(list(raw[actor]['meshes'].values()))
        view = np.column_stack((points, np.ones(len(points))))@np.linalg.inv(np.asarray(camera['world_matrix'])).T
        clip = view@np.asarray(camera['projection_matrix']).T
        front = clip[:, 3] > 1e-12
        _need(np.any(front), 'CAMERA', 'Actual character is entirely behind the selected camera.', asset=actor)
        ndc = clip[front, :3]/clip[front, 3, None]
        actors[actor]['camera_ndc_bounds'] = dict(minimum=ndc.min(axis=0).tolist(), maximum=ndc.max(axis=0).tolist())
        actors[actor]['camera_front_vertex_fraction'] = float(front.mean())
        if production['id'] in ('entry', 'exploration'):
            _need(np.all(front) and np.all(np.abs(ndc) <= 1+1e-6), 'CAMERA',
                  'An actual character is clipped in a declared full-body traversal shot.', asset=actor,
                  observed=actors[actor]['camera_ndc_bounds'])
        else:
            # Close-ups intentionally crop bodies. Check actual head and active
            # hand/prop targets, without promoting projection to occlusion or
            # rendered narrative-readability evidence.
            targets = [np.asarray(raw[actor]['meshes']['head-mesh']).mean(axis=0)]
            owner = None if frame < PICKUP else 'aster' if frame < HANDOFF else 'moss'
            if actor == owner or CONTACT_START <= frame <= CONTACT_END:
                socket = raw[actor]['world']@rig_attachments.socket_world(context.rigs[actor], raw[actor]['pose'],
                                  'grip.'+('left' if actor == 'aster' else 'right'))
                targets.append(socket[:3, 3])
            if production['id'] in ('discovery', 'cooperation', 'resolution'):
                targets.append(np.asarray(nodes['access-key']['world_matrix'])[:3, 3])
            target_view = np.column_stack((targets, np.ones(len(targets))))@np.linalg.inv(np.asarray(camera['world_matrix'])).T
            target_clip = target_view@np.asarray(camera['projection_matrix']).T
            _need(np.all(target_clip[:, 3] > 0) and np.all(np.abs(target_clip[:, :3]/target_clip[:, 3, None]) <= 1+1e-6),
                  'CAMERA', 'A required close-up head, hand or prop target is outside the real frustum.', asset=actor)
    row = dict(frame=frame, time=dict(numerator=frame, denominator=FPS), shot=production['id'],
        local_frame=local, camera=evaluation['camera'], camera_sha256=object_hash(camera), actors=actors,
        scene_geometry_matrices={n: deepcopy(nodes[n]['world_matrix']) for n in sorted(GEOMETRY_NODES)},
        prop_matrix=deepcopy(nodes['access-key']['world_matrix']),
        environment_sha256=object_hash({n: nodes[n] for n in context.scene['lights']}|{'world': evaluation['world']}))
    return row, raw


def _collision_authority():
    try:
        from scene3d_spatial import SpatialIndex
        return SpatialIndex
    except ImportError:
        return None


def _environment(context):
    from rig_import import load_character_model
    result = {}
    for identifier in ('laboratory', 'laboratory-dressing'):
        asset = context.assets[identifier]
        geometry = load_character_model(context.root/portable_path(asset['content']['path'])).geometry
        result[identifier] = geometry
    return result


def _collision(context, row, raw, environment, authority):
    """Feed actual sampled triangles to the existing canonical query authority."""
    from scene3d_example import node
    from scene3d_graph import SceneGraph
    scene = deepcopy(context.scene)
    scene.update(nodes=[node('root')], roots=['root'], cameras=[], lights=[], shots=[], dependencies=[], collections=[], layers=[])
    meshes, actors, environment_ids = {}, {}, []
    for actor, value in raw.items():
        actors[actor] = []
        for name, points in value['meshes'].items():
            identifier = actor+'-'+name; actors[actor].append(identifier)
            meshes[identifier] = dict(vertices=points, triangles=context.rigs[actor].geometry.by_id()[name].triangles)
    for identifier, geometry in environment.items():
        world = np.asarray(row['scene_geometry_matrices'][identifier])
        for mesh in geometry.meshes:
            if identifier == 'laboratory' and mesh.id == 'floor':
                continue  # Exact floor contact is independently measured on actual boot vertices.
            name = identifier+'-'+mesh.id; environment_ids.append(name)
            meshes[name] = dict(vertices=transform_points(world, mesh.positions), triangles=mesh.triangles)
    boxes = {n: (v['vertices'].min(axis=0), v['vertices'].max(axis=0)) for n, v in meshes.items()}
    candidates = []
    for first in actors['aster']+actors['moss']:
        choices = (actors['moss'] if first in actors['aster'] else [])+environment_ids
        for second in choices:
            a, b = boxes[first], boxes[second]
            if np.all(a[0] <= b[1]) and np.all(b[0] <= a[1]):
                candidates.append((first, second))
    if not candidates and row['frame'] not in CRITICAL_FRAMES:
        return dict(broad_candidates=0, exact_queries=0, passed=True)
    if authority is None:
        return dict(broad_candidates=len(candidates), exact_queries=0, passed=False, reason='canonical-scene62-exact-query-unavailable')
    for identifier in meshes:
        scene['nodes'].append(node(identifier, parent='root'))
    index = authority(SceneGraph(scene), meshes)
    _need(set(meshes) <= set(index.meshes), 'COLLISION', 'Canonical collision index omitted required actual triangle geometry.')
    from scene3d_spatial import _QueryBudget
    budget = _QueryBudget()
    queries = 0
    for first in sorted({a for a, _ in candidates}):
        allowed = {b for a, b in candidates if a == first}
        hits = index.mesh_overlap(first, exclude=[n for n in meshes if n not in allowed], solid=True, _budget=budget)
        queries += 1
        _need(not hits, 'COLLISION', 'Actual deformed native geometry intersects another robot or laboratory surface.',
              asset=first, observed=dict(frame=row['frame'], hits=hits))
    # Critical empty candidate sets are an actual conservative disjointness
    # proof: no triangle intersection is possible between disjoint exact bounds.
    return dict(broad_candidates=len(candidates), exact_queries=queries, passed=True)


def validate_narrative(project_root, *, evidence_root=None):
    """Compute all 720 delivery rows. Optional external views are read-only.

    Numeric success alone returns a partial, non-ready proof. This function
    never writes into the source or evidence directory and never starts Blender.
    """
    root = _root(project_root)
    context = _Context(root, load_project(root))
    _need(_paired_transfer_available(), 'PAIRED_GRIP_API',
          'The canonical native transfer owner must provide both object-local grip parameters.')
    authority = _collision_authority(); environment = _environment(context)
    rows, previous, planted, prop = [], {}, {}, None
    travel = {a: 0. for a in ACTORS}; ownership = []; previous_prop = None
    metrics = dict(max_foot_ground_error_m=0., max_plant_drift_m=0., max_grip_error_m=0.,
        max_grip_rotation_error_rad=0., max_prop_matrix_error=0., max_prop_translation_step_m=0.,
        max_prop_rotation_step_rad=0., max_geometry_frame_step_m=0., max_bone_translation_step_m=0.,
        max_bone_rotation_step_rad=0., max_pose_equivalence_error=0., exact_collision_queries=0,
        collision_candidate_frames=0, collision_unavailable_frames=0)
    for production in context.production:
        for frame in range(production['start_frame'], production['end_frame']):
            row, raw = _sample(context, production, frame)
            key = np.asarray(row['prop_matrix'])
            if prop is None:
                prop = dict(owner=None, socket=None, transform=decompose(key))
            worlds = {a: world_pose(context.rigs[a], raw[a]['pose'], raw[a]['world']) for a in ACTORS}
            if frame == PICKUP:
                prop = rig_attachments.attach_prop(context.rigs['aster'], worlds['aster'], prop,
                    'grip.left', expected_owner=None, keep_world=True)
                ownership.append(dict(frame=frame, previous_owner=None, next_owner=prop['owner']))
            if frame == HANDOFF:
                before = rig_attachments.prop_world(context.rigs['aster'], worlds['aster'], prop)
                prop = rig_attachments.transfer_prop(context.rigs['aster'], worlds['aster'], context.rigs['moss'],
                    worlds['moss'], prop, 'grip.right', tolerance=LIMITS['grip_m'],
                    source_grip=GRIPS['aster'], target_grip=GRIPS['moss'],
                    orientation_tolerance=LIMITS['grip_rotation_rad'])
                after = rig_attachments.prop_world(context.rigs['moss'], worlds['moss'], prop)
                _need(float(abs(before-after).max()) <= LIMITS['prop_matrix_error'], 'OWNERSHIP', 'Native transfer changed the actual prop world transform.')
                ownership.append(dict(frame=frame, previous_owner='aster', next_owner=prop['owner']))
            owner = None if frame < PICKUP else 'aster' if frame < HANDOFF else 'moss'
            _need(prop['owner'] == owner, 'OWNERSHIP', 'Prop owner differs from the single ordered native transfer.')
            actual = transform_matrix(prop['transform']) if owner is None else rig_attachments.prop_world(context.rigs[owner], worlds[owner], prop)
            row['owner'] = owner; row['prop_native_matrix'] = actual.tolist()
            row['prop_matrix_error'] = float(abs(key-actual).max())
            _need(row['prop_matrix_error'] <= LIMITS['prop_matrix_error'], 'PROP', 'Scene prop channels differ from the canonical native attachment.')
            metrics['max_prop_matrix_error'] = max(metrics['max_prop_matrix_error'], row['prop_matrix_error'])
            row['grip_errors_m'], row['grip_rotation_errors_rad'] = {}, {}
            for actor in ACTORS:
                data, value = row['actors'][actor], raw[actor]
                for side in ('left', 'right'):
                    foot = data['feet'][side]; points = value['meshes']['boot-'+side]; identity = (actor, side)
                    if foot['planted']:
                        _need(abs(foot['sole_y_m']) <= LIMITS['foot_ground_m'], 'GROUND', 'A planted actual boot leaves its floor contact.')
                        if identity not in planted: planted[identity] = points.copy()
                        drift = float(np.linalg.norm(points-planted[identity], axis=1).max())
                        _need(drift <= LIMITS['plant_drift_m'], 'FOOT_DRIFT', 'Actual planted boot vertices slide in world space.')
                        metrics['max_foot_ground_error_m'] = max(metrics['max_foot_ground_error_m'], abs(foot['sole_y_m']))
                    else:
                        planted.pop(identity, None); drift = 0.
                    foot['plant_drift_m'] = drift
                    metrics['max_plant_drift_m'] = max(metrics['max_plant_drift_m'], drift)
                if actor == owner or CONTACT_START <= frame <= CONTACT_END:
                    socket = rig_attachments.socket_world(context.rigs[actor], worlds[actor], 'grip.'+('left' if actor == 'aster' else 'right'))
                    expected = key@transform_matrix(GRIPS[actor])
                    error = float(np.linalg.norm(socket[:3, 3]-expected[:3, 3])); angle = float(qangle(decompose(socket)['rotation'], decompose(expected)['rotation']))
                    _need(error <= LIMITS['grip_m'] and angle <= LIMITS['grip_rotation_rad'], 'CONTACT', 'A real native hand no longer contacts its own prop grip.')
                    row['grip_errors_m'][actor], row['grip_rotation_errors_rad'][actor] = error, angle
                    metrics['max_grip_error_m'] = max(metrics['max_grip_error_m'], error)
                    metrics['max_grip_rotation_error_rad'] = max(metrics['max_grip_rotation_error_rad'], angle)
                data['geometry_frame_step_m'] = data['bone_translation_step_m'] = data['bone_rotation_step_rad'] = 0.
                if actor in previous:
                    old = previous[actor]
                    travel[actor] += float(np.linalg.norm(value['world'][:3, 3]-old['world'][:3, 3]))
                    data['geometry_frame_step_m'] = max(float(np.linalg.norm(points-old['meshes'][n], axis=1).max()) for n, points in value['meshes'].items())
                    data['bone_translation_step_m'] = max(float(np.linalg.norm(m[:3, 3]-old['fk'][n][:3, 3])) for n, m in value['fk'].items())
                    data['bone_rotation_step_rad'] = max(float(qangle(decompose(m)['rotation'], decompose(old['fk'][n])['rotation'])) for n, m in value['fk'].items())
                previous[actor] = value
                for suffix in ('geometry_frame_step_m', 'bone_translation_step_m', 'bone_rotation_step_rad'):
                    metrics['max_'+suffix] = max(metrics['max_'+suffix], data[suffix])
                metrics['max_pose_equivalence_error'] = max(metrics['max_pose_equivalence_error'], data['pose_equivalence_error'])
            if previous_prop is not None:
                translation = float(np.linalg.norm(key[:3, 3]-previous_prop[:3, 3]))
                rotation = float(qangle(decompose(key)['rotation'], decompose(previous_prop)['rotation']))
                _need(translation <= LIMITS['prop_translation_step_m'] and rotation <= LIMITS['prop_rotation_step_rad'],
                      'PROP_JUMP', 'Actual scene prop jumps between adjacent delivery frames.')
                metrics['max_prop_translation_step_m'] = max(metrics['max_prop_translation_step_m'], translation)
                metrics['max_prop_rotation_step_rad'] = max(metrics['max_prop_rotation_step_rad'], rotation)
            previous_prop = key
            row['collision'] = _collision(context, row, raw, environment, authority)
            metrics['exact_collision_queries'] += row['collision']['exact_queries']
            metrics['collision_candidate_frames'] += int(row['collision']['broad_candidates'] > 0)
            metrics['collision_unavailable_frames'] += int(not row['collision']['passed'])
            rows.append(row)
    _need(all(distance >= LIMITS['minimum_root_travel_m'] for distance in travel.values()), 'TRAVEL', 'Both actual roots must traverse at least 2.5 metres.')
    _need(Counter(row['owner'] for row in rows) == Counter({None: 360, 'aster': 168, 'moss': 192})
          and ownership == [dict(frame=PICKUP, previous_owner=None, next_owner='aster'),
                            dict(frame=HANDOFF, previous_owner='aster', next_owner='moss')],
          'OWNERSHIP', 'Actual ownership intervals or exactly-once transfer differ.')
    metrics['root_travel_m'] = travel
    report = dict(schema_version=1, kind='director-narrative-acceptance', project_sha256=context.project_sha,
        project_inventory=deepcopy(context.inventory), frames=FRAMES, fps=dict(numerator=FPS, denominator=1),
        resolution=context.resolution, scene=deepcopy(context.scene_ref), production_shots=context.production,
        identities=context.identities, limits=deepcopy(LIMITS), ownership_changes=ownership,
        metrics=metrics, rows=rows, numeric_status='passed', ready_for_final_render=False,
        native_views_status='absent', artistic_approval=False,
        limitations=['Numerical projection does not establish visibility through occluders or narrative readability.',
                     'Actual native proof images and a separate exact engineering inspection are required.',
                     'No artistic approval or completed final movie is granted.'])
    if metrics['collision_unavailable_frames']:
        report['numeric_status'] = 'partial'
        report['limitations'].append('Canonical Scene62 exact collision authority is unavailable for required candidates/critical frames.')
    if evidence_root is not None:
        report['native_views'] = _verify_views(context, report, _root(evidence_root))
        report['native_views_status'] = 'verified'
        report['ready_for_final_render'] = report['numeric_status'] == 'passed'
    _recheck(root, context.inventory)
    return report


def _verify_rows_unchecked(report):
    """Reject forged flags, missing rows, nonfinite metrics and unsafe thresholds."""
    _need(isinstance(report, dict) and type(report.get('schema_version')) is int and report['schema_version'] == 1
          and report.get('kind') == 'director-narrative-acceptance'
          and type(report.get('frames')) is int and report['frames'] == FRAMES
          and report.get('fps') == dict(numerator=FPS, denominator=1)
          and all(type(report['fps'][key]) is int for key in ('numerator', 'denominator')),
          'PROOF', 'Expected a complete exact 720-frame numerical proof.')
    _need(report.get('limits') == LIMITS and report.get('numeric_status') == 'passed',
          'PROOF', 'Numerical coverage or approved thresholds are incomplete.')
    production = report.get('production_shots')
    _need(isinstance(production, list) and len(production) == len(SHOTS), 'PROOF', 'Missing explicit production shot clock inventory.')
    for item, (name, start, end) in zip(production, SHOTS):
        _need(isinstance(item, dict) and item.get('id') == name
              and type(item.get('start_frame')) is int and item['start_frame'] == start
              and type(item.get('end_frame')) is int and item['end_frame'] == end
              and item.get('source_start') == dict(numerator=0, denominator=1)
              and item.get('playback_rate') == dict(numerator=1, denominator=1)
              and all(type(item[clock][key]) is int for clock in ('source_start', 'playback_rate')
                      for key in ('numerator', 'denominator')),
              'PROOF', 'Production shot clock inventory differs.')
    _need(report.get('ownership_changes') == [dict(frame=PICKUP, previous_owner=None, next_owner='aster'),
          dict(frame=HANDOFF, previous_owner='aster', next_owner='moss')], 'OWNERSHIP', 'Retained authority transitions differ.')
    rows = report.get('rows')
    _need(isinstance(rows, list) and len(rows) == FRAMES, 'PROOF', 'Numerical proof must contain every actual delivery row.')
    counts = Counter(); previous_prop = None; previous_roots = {}; travel = Counter()
    for frame, row in enumerate(rows):
        _need(isinstance(row, dict) and type(row.get('frame')) is int and row['frame'] == frame
              and row.get('time') == dict(numerator=frame, denominator=FPS)
              and all(type(row['time'][key]) is int for key in ('numerator', 'denominator')),
              'PROOF', 'Numerical row order or exact rational time differs.')
        production = next(item for item in report['production_shots'] if item['start_frame'] <= frame < item['end_frame'])
        _need(row.get('shot') == production['id'] and row.get('local_frame') == frame-production['start_frame']
              and set(row.get('actors', {})) == set(ACTORS), 'PROOF', 'Numerical frame/shot/actor identity differs.')
        owner = None if frame < PICKUP else 'aster' if frame < HANDOFF else 'moss'
        _need(row.get('owner') == owner, 'OWNERSHIP', 'Retained proof has a stale or duplicate owner.')
        counts[owner] += 1
        key, native = _matrix(row.get('prop_matrix')), _matrix(row.get('prop_native_matrix'))
        _need(float(abs(key-native).max()) <= LIMITS['prop_matrix_error'], 'PROOF', 'Retained native prop matrices disagree.')
        if previous_prop is not None:
            _need(float(np.linalg.norm(key[:3, 3]-previous_prop[:3, 3])) <= LIMITS['prop_translation_step_m']
                  and float(qangle(decompose(key)['rotation'], decompose(previous_prop)['rotation'])) <= LIMITS['prop_rotation_step_rad'],
                  'PROOF', 'Retained prop has an adjacent frame jump.')
        previous_prop = key
        _need(_number(row.get('prop_matrix_error'), maximum=LIMITS['prop_matrix_error'])
              and row.get('collision', {}).get('passed') is True,
              'PROOF', 'Prop or collision evidence is incomplete.')
        expected_contacts = set(ACTORS) if CONTACT_START <= frame <= CONTACT_END else {owner} if owner else set()
        _need(set(row.get('grip_errors_m', {})) == expected_contacts
              and set(row.get('grip_rotation_errors_rad', {})) == expected_contacts,
              'CONTACT', 'Retained proof omits a required real hand contact.')
        _need(all(_number(x, maximum=LIMITS['grip_m']) for x in row['grip_errors_m'].values())
              and all(_number(x, maximum=LIMITS['grip_rotation_rad']) for x in row['grip_rotation_errors_rad'].values()),
              'CONTACT', 'Retained contact measurements exceed approved tolerances.')
        for actor in ACTORS:
            value = row['actors'][actor]
            _need(isinstance(value, dict), 'PROOF', 'Actor row must contain actual native measurements.')
            root = _matrix(value.get('root_matrix'))
            if actor in previous_roots:
                travel[actor] += float(np.linalg.norm(root[:3, 3]-previous_roots[actor][:3, 3]))
            previous_roots[actor] = root
            _need(_number(value.get('pose_equivalence_error'), maximum=LIMITS['pose_equivalence']),
                  'PROOF', 'Native full/segment motion equivalence failed.')
            for side in ('left', 'right'):
                foot = value.get('feet', {}).get(side, {})
                _need(type(foot.get('planted')) is bool and _number(foot.get('plant_drift_m'), maximum=LIMITS['plant_drift_m'])
                      and _number(foot.get('sole_y_m'), minimum=-LIMITS['foot_ground_m'])
                      and (not foot['planted'] or abs(foot['sole_y_m']) <= LIMITS['foot_ground_m']),
                      'GROUND', 'Retained actual boot contact failed.')
    _need(counts == Counter({None: 360, 'aster': 168, 'moss': 192}), 'OWNERSHIP', 'Owner frame counts differ.')
    for actor in ACTORS:
        measured = report.get('metrics', {}).get('root_travel_m', {}).get(actor)
        _need(_number(measured, minimum=LIMITS['minimum_root_travel_m']) and abs(measured-travel[actor]) <= 1e-7,
              'TRAVEL', 'Retained root travel is incomplete.')


def _verify_rows(report):
    try:
        finite_json(report)
        return _verify_rows_unchecked(report)
    except RigError:
        raise
    except (ValueError, TypeError, KeyError, StopIteration, IndexError, OverflowError, AttributeError) as exc:
        raise RigError('DIRECTOR_NARRATIVE_PROOF', 'Malformed retained numerical proof.', stage='director-narrative') from exc


def _matrix(value):
    try:
        matrix = np.asarray(value, dtype=float)
        valid = (matrix.shape == (4, 4) and np.isfinite(matrix).all()
                 and np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-8, rtol=0)
                 and np.allclose(matrix[:3, :3].T@matrix[:3, :3], np.eye(3), atol=1e-7, rtol=0)
                 and abs(np.linalg.det(matrix[:3, :3])-1) <= 1e-7)
    except (ValueError, TypeError):
        valid = False
    _need(valid, 'PROOF', 'Retained world transform must be finite, rigid and right handed.')
    return matrix


def _verify_views(context, report, root):
    """Delegate retained native receipt/pixel checking to the one view owner."""
    _need(not root.is_relative_to(context.root), 'PATH', 'Native proof views must live outside the project.')
    try:
        from director_narrative_views import verify_views
    except ImportError as exc:
        raise RigError('DIRECTOR_NARRATIVE_NATIVE_VIEWS', 'The first-party retained native view verifier is unavailable.', stage='director-narrative') from exc
    return verify_views(context.root, context.bundle, report, root, view_profile=VIEW_PROFILE,
                        expected_manifest_ref=report.get('native_views', {}).get('manifest_ref'))


def verify_narrative_acceptance(project_root, evidence_root, expectedProofRef):
    """Read-only admission over an exact proof, actual native rows and images."""
    root, evidence = _root(project_root), _root(evidence_root)
    _need(isinstance(expectedProofRef, dict), 'REFERENCE', 'Expected a pinned numerical proof reference.')
    _need(not (evidence/portable_path(expectedProofRef.get('path', ''))).is_relative_to(root),
          'PATH', 'Acceptance proof must be outside the immutable source project.')
    proof = decode_json(_artifact(evidence, expectedProofRef))
    finite_json(proof); _verify_rows(proof)
    _need(proof.get('native_views_status') == 'verified' and isinstance(proof.get('native_views'), dict)
          and isinstance(proof['native_views'].get('manifest_ref'), dict),
          'NATIVE_VIEWS', 'A ready flag cannot replace exact current native views and their engineering inspection.')
    context = _Context(root, load_project(root))
    _need(proof.get('project_sha256') == context.project_sha and proof.get('project_inventory') == context.inventory
          and proof.get('scene') == context.scene_ref and proof.get('production_shots') == context.production
          and proof.get('identities') == context.identities and proof.get('resolution') == context.resolution,
          'STALE', 'Proof no longer binds the complete exact current narrative project.')
    _need(_paired_transfer_available(), 'PAIRED_GRIP_API', 'Canonical paired-grip transfer authority is unavailable.')
    for row in proof['rows']:
        production = next(item for item in context.production if item['id'] == row['shot'])
        for actor in ACTORS:
            retained = row['actors'][actor]
            _need(retained.get('native_clip') == context.native_refs[actor][row['shot']]
                  and Fraction(**retained['native_time']) == Fraction(row['frame']-production['start_frame'], FPS),
                  'STALE', 'Retained per-frame native clip identity or clock differs.')
            for side in ('left', 'right'):
                _need(retained['feet'][side]['planted'] == _active_plant(context.foot_events[actor][side], row['frame']),
                      'STALE', 'Retained foot plant disagrees with actual native event authority.')
    critical = set(CRITICAL_FRAMES) | {f for _, a, b in SHOTS for f in (a, b-1)}
    for frame in sorted(critical):
        production = next(item for item in context.production if item['start_frame'] <= frame < item['end_frame'])
        row, _ = _sample(context, production, frame)
        retained = proof['rows'][frame]
        _need(row['camera_sha256'] == retained.get('camera_sha256')
              and row['prop_matrix'] == retained.get('prop_matrix')
              and row['scene_geometry_matrices'] == retained.get('scene_geometry_matrices'),
              'STALE', 'A critical retained scene sample differs from actual current evaluation.')
        for actor in ACTORS:
            _need(all(row['actors'][actor][key] == retained['actors'][actor].get(key)
                      for key in ('geometry_sha256', 'bones_sha256', 'native_clip', 'native_time')),
                  'STALE', 'A critical retained native sample differs from actual geometry/rig evaluation.')
    _verify_views(context, proof, evidence)
    _recheck(root, context.inventory)
    return dict(schema_version=1, kind='director-narrative-acceptance-verification',
                project_sha256=context.project_sha, proof_sha256=expectedProofRef['sha256'],
                frames=FRAMES, fps=dict(numerator=FPS, denominator=1), resolution=context.resolution,
                ready_for_final_render=True, artistic_approval=False)
