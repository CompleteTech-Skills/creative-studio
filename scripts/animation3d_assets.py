"""Bounded exact-byte closure admission for Animation3D and native owners.

Validation runs against private copied bytes, never a mixture of live revisions.
The returned inventory is evidence of admission, not rendering or visual approval.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile
from functools import wraps

from animation3d_contracts import KINDS, _walk, validate_contract
from rig_errors import RigError, decode_json, require, safe_path

MAX_FILES = 4096
MAX_BYTES = 256 * 1024**2
MAX_DEPTH = 64
ALIASES = {'clip': KINDS['clip'], 'state-machine': KINDS['state-machine'],
           'retarget': KINDS['retarget'], 'sequence': KINDS['sequence'],
           'project': KINDS['project'], 'scene': KINDS['scene']}


def _admission(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RigError:
            raise
        except ImportError as exc:
            raise RigError('ANIMATION_DEPENDENCY', 'Install the optional native owner dependencies: '+str(exc), stage='animation3d') from exc
        except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as exc:
            raise RigError('ANIMATION_OWNER', 'Native dependency admission failed: '+str(exc), stage='animation3d') from exc
    return call


def references(document):
    return [node for node in _walk(document)
            if {'path', 'sha256', 'id', 'version', 'kind'} <= node.keys()]


class Snapshot:
    """A temporary immutable-by-convention closure owned only by this call."""
    def __init__(self, root, destination, max_bytes=MAX_BYTES):
        self.root = Path(root).resolve(strict=True)
        self.destination = Path(destination)
        self.max_bytes = min(max_bytes, MAX_BYTES)
        self.inventory = {}
        self.aliases = {}
        self.total = 0
        self.memory_limit = MAX_BYTES
        self.expanded_bytes = 0

    def copy(self, name, digest=None):
        source = safe_path(self.root, name, exists=True)
        require(not any(getattr(p, 'is_junction', lambda: False)() for p in (source, *source.parents) if p != self.root.parent),
                'ANIMATION_PATH', 'Junction sources are not supported.')
        alias = name.casefold()
        require(self.aliases.get(alias, name) == name, 'ANIMATION_PATH', 'Portable path alias collision.')
        self.aliases[alias] = name
        if name in self.inventory:
            require(digest is None or self.inventory[name]['sha256'] == digest,
                    'ANIMATION_HASH', 'Conflicting exact dependency hash.')
            return self.destination / name
        require(len(self.inventory) < MAX_FILES, 'ANIMATION_LIMIT', 'Dependency file limit exceeded.')
        require(source.stat().st_size <= self.max_bytes - self.total,
                'ANIMATION_LIMIT', 'Dependency byte limit exceeded.')
        target = self.destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        hashed = hashlib.sha256(); count = 0
        with source.open('rb') as inp, target.open('xb') as out:
            while block := inp.read(min(1024**2, self.max_bytes - self.total + 1)):
                self.total += len(block); count += len(block)
                require(self.total <= self.max_bytes, 'ANIMATION_LIMIT', 'Dependency grew beyond byte limit.')
                hashed.update(block); out.write(block)
        actual = hashed.hexdigest()
        require(digest is None or actual == digest, 'ANIMATION_HASH', 'Dependency hash differs from reference.')
        self.inventory[name] = {'sha256': actual, 'bytes': count}
        # Account before JSON/NumPy decoders allocate. Compressed rig bundles
        # expand independently of their on-disk byte budget.
        expanded = count
        if target.suffix == '.npz' or zipfile.is_zipfile(target):
            with zipfile.ZipFile(target) as archive:
                expanded = max(expanded, sum(i.file_size for i in archive.infolist()) * 3)
                require(expanded <= self.memory_limit - self.expanded_bytes,
                        'ANIMATION_LIMIT', 'Expanded binary exceeds memory admission budget.')
                import numpy as np
                for info in archive.infolist():
                    if not info.filename.endswith('.npy'): continue
                    with archive.open(info) as stream:
                        version = np.lib.format.read_magic(stream)
                        require(version in ((1, 0), (2, 0)), 'ANIMATION_LIMIT', 'Unsupported binary array header.')
                        shape, _, dtype = (np.lib.format.read_array_header_1_0(stream) if version == (1, 0)
                                           else np.lib.format.read_array_header_2_0(stream))
                        count = 1
                        for size in shape:
                            require(isinstance(size, int) and 0 <= size <= 2_000_000,
                                    'ANIMATION_LIMIT', 'Unsafe binary array dimensions.')
                            count *= size
                        require(not dtype.hasobject and count * dtype.itemsize * 3 <= self.memory_limit - self.expanded_bytes,
                                'ANIMATION_LIMIT', 'Binary array header exceeds memory admission budget.')
        self.expanded_bytes += expanded
        require(self.expanded_bytes <= self.memory_limit, 'ANIMATION_LIMIT', 'Dependency memory admission budget exceeded.')
        require(target.suffix != '.json' or count <= 16 * 1024**2,
                'ANIMATION_LIMIT', 'JSON document exceeds 16 MiB.')
        return target

    def native(self, name, expected=None, depth=0):
        require(depth <= MAX_DEPTH, 'ANIMATION_LIMIT', 'Native dependency depth exceeded.')
        path = self.copy(name, None if expected is None else expected['sha256'])
        value = decode_json(path.read_bytes())
        require(isinstance(value, dict), 'ANIMATION_KIND', 'Native source must be an object.')
        if expected is not None:
            require(value.get('id') == expected['id'] and value.get('version') == expected['version'],
                    'ANIMATION_IDENTITY', 'Native source identity/version differs.')
        parent = Path(name).parent
        kind = value.get('kind')
        # Native reference paths are relative to the owner root, not each nested document.
        owner_prefix = parent.as_posix() if parent != Path('.') else ''
        seen = set()
        def capture(document, document_name, level):
            require(level <= MAX_DEPTH, 'ANIMATION_LIMIT', 'Native dependency depth exceeded.')
            if document_name in seen: return
            seen.add(document_name)
            for node in _walk(document):
                if not {'path','sha256'} <= node.keys(): continue
                relative = str(Path(owner_prefix) / node['path'])
                child = self.copy(relative, node['sha256'])
                if child.suffix == '.json':
                    payload = decode_json(child.read_bytes())
                    require(isinstance(payload, dict), 'ANIMATION_KIND', 'Native dependency must be an object.')
                    capture(payload, relative, level+1)
        capture(value, name, depth)
        if kind == 'hatch-3d-rig':
            identity = str(parent / 'identity.json')
            self.copy(identity, value['character']['identity_sha256'])
            from rig_models import load_rig
            from rig_skeleton import validate_skeleton
            rig = load_rig(path.parent)
            validate_skeleton(rig.skeleton, rig.manifest['limits'])
            return kind, rig
        if isinstance(kind, str) and kind.startswith('scene3d-'):
            from scene3d_contracts import load_project
            require(kind == 'scene3d-project', 'ANIMATION_OWNER', 'Scene source must name its owner project closure.')
            return kind, load_project(path.parent, path.name)
        if kind == 'camera3d-registry':
            from camera3d_contracts import load_registry
            return kind, load_registry(path.parent, value)
        if kind in ('material3d','material-instance3d','material-variant3d','light3d','lighting-rig3d','look3d'):
            from lookdev_contracts import Registry
            entries, payloads = [], {}
            for relative in seen:
                local = (self.destination / relative).relative_to(path.parent).as_posix()
                data = (self.destination / relative).read_bytes()
                entries.append((local, data))
            for relative in self.inventory:
                target = self.destination / relative
                if target.is_relative_to(path.parent) and relative not in seen:
                    payloads[target.relative_to(path.parent).as_posix()] = target.read_bytes()
            registry = Registry(entries, payloads=payloads)
            return kind, registry
        raise RigError('ANIMATION_OWNER', 'No registered native source owner for '+str(kind), stage='animation3d')


@_admission
def verify_native(root, reference):
    """Return detached native owner data after exact closure validation."""
    with TemporaryDirectory(prefix='animation3d-source-') as temporary:
        snapshot = Snapshot(root, temporary)
        kind, value = snapshot.native(reference['path'], reference)
        require(reference['kind'] in (kind, 'rig' if kind=='hatch-3d-rig' else kind),
                'ANIMATION_OWNER', 'Native source kind differs from reference.')
        return kind, value


@_admission
def load_project(root, name='animation-project.json'):
    """Verify complete animation references, budgets and cross-file bindings.

    Foreign owners are admitted through their native closure APIs. No timeline,
    constraint solver, renderer, or provider is executed by this admission step.
    """
    with TemporaryDirectory(prefix='animation3d-project-') as temporary:
        snapshot = Snapshot(root, temporary)
        documents, edges, active, native = {}, {}, set(), {}
        identities = {}
        project = decode_json(snapshot.copy(name).read_bytes())
        validate_contract(project)
        require(project['kind'] == KINDS['project'], 'ANIMATION_KIND', 'Expected animation project root.')
        snapshot.max_bytes = min(MAX_BYTES, project['budgets']['max_work_bytes'])
        snapshot.memory_limit = min(MAX_BYTES, project['budgets']['max_memory_bytes'])
        require(snapshot.expanded_bytes <= snapshot.memory_limit, 'ANIMATION_LIMIT', 'Project exceeds memory admission budget.')
        require(snapshot.total <= snapshot.max_bytes, 'ANIMATION_LIMIT', 'Project exceeds work byte budget.')
        def visit(path, expected=None, depth=0):
            require(depth <= MAX_DEPTH, 'ANIMATION_LIMIT', 'Animation dependency depth exceeded.')
            require(path not in active, 'ANIMATION_CYCLE', 'Animation dependency cycle.')
            file = snapshot.copy(path, None if expected is None else expected['sha256'])
            value = decode_json(file.read_bytes())
            require(isinstance(value, dict), 'ANIMATION_KIND', 'Animation dependency must be an object.')
            if value.get('kind') not in KINDS.values():
                require(expected is not None, 'ANIMATION_KIND', 'Expected typed native source.')
                require(value.get('id') == expected['id'] and value.get('version') == expected['version'],
                        'ANIMATION_IDENTITY', 'Native source identity/version differs.')
                kind, admitted = native[path] if path in native else snapshot.native(path, expected, depth)
                allowed = {'rig': {'hatch-3d-rig'}, 'scene': {'scene3d-project'},
                           'model': {'scene3d-project'}, 'character': {'hatch-3d-rig'},
                           'render-profile': {'camera3d-registry'}}
                require(kind in allowed.get(expected['kind'], {expected['kind']}),
                        'ANIMATION_OWNER', 'Reference points to wrong native owner kind.')
                identity = (kind, expected['id'], expected['version'])
                require(identities.get(identity, path) == path, 'ANIMATION_IDENTITY', 'Duplicate native source identity.')
                identities[identity] = path
                native[path] = (kind, admitted)
                return
            validate_contract(value)
            if expected:
                require(value.get('id') == expected['id'] and value.get('version') == expected['version'] and
                        value['kind'] == ALIASES.get(expected['kind'], expected['kind']),
                        'ANIMATION_IDENTITY', 'Animation reference identity/version/kind differs.')
            if path in documents: return
            identity = (value['kind'], value.get('id'), value.get('version'))
            require(identities.get(identity, path) == path, 'ANIMATION_IDENTITY', 'Duplicate document identity.')
            identities[identity] = path
            documents[path] = value; active.add(path); edges[path] = []
            for reference in references(value):
                edges[path].append(reference['path'])
                visit(reference['path'], reference, depth+1)
            active.remove(path)
        visit(name)
        _validate_bindings(project, documents, native, snapshot.destination)
        return {'project': deepcopy(project), 'documents': deepcopy(documents),
                'inventory': deepcopy(snapshot.inventory), 'bytes': snapshot.total,
                'source_ready': True, 'scope': 'exact dependency and native target admission; no evaluated scene or render'}


def _validate_bindings(project, documents, native, snapshot_root):
    budgets = project['budgets']
    tracks = [n for d in documents.values() for n in _walk(d) if 'keys' in n and 'target' in n]
    actors = [n for d in documents.values() for n in _walk(d) if 'animation_layers' in n]
    require(len(tracks) <= budgets['max_tracks'] and sum(len(t['keys']) for t in tracks) <= budgets['max_keyframes'] and
            len(actors) <= budgets['max_actors'], 'ANIMATION_LIMIT', 'Animation project exceeds track/key/actor budget.')
    rigs = {path: value for path, (kind, value) in native.items() if kind == 'hatch-3d-rig'}
    require(all(len(rig.skeleton['bones']) <= budgets['max_bones_per_rig'] for rig in rigs.values()),
            'ANIMATION_LIMIT', 'Rig exceeds project bone budget.')
    require(sum(r.geometry.vertex_count for r in rigs.values()) <= budgets['max_vertices'] and
            sum(len(m.morphs) for r in rigs.values() for m in r.geometry.meshes) <= budgets['max_morph_targets'],
            'ANIMATION_LIMIT', 'Native geometry exceeds project vertex/morph budget.')
    scene_projects = [value for kind, value in native.values() if kind == 'scene3d-project']
    require(sum(r.geometry.vertex_count for r in rigs.values()) +
            sum(d['resources']['vertices'] for value in scene_projects for d in value['documents'].values()
                if d['kind'] == 'scene3d-asset') <= budgets['max_vertices'],
            'ANIMATION_LIMIT', 'Native rig/scene geometry exceeds project vertex budget.')
    require(sum(len(d['nodes']) for value in scene_projects for d in value['documents'].values()
                if d['kind'] == 'scene3d-scene') <= budgets['max_scene_nodes'],
            'ANIMATION_LIMIT', 'Native scene exceeds project node budget.')
    scenes = {d['id']: d for d in documents.values() if d['kind'] == KINDS['scene']}
    owner_sources = {}
    for path, (kind, _) in native.items():
        if kind in ('camera3d-registry','material3d','material-instance3d','material-variant3d','light3d'):
            data = decode_json((snapshot_root/path).read_bytes())
            owner_sources[path] = dict(path=path,sha256=hashlib.sha256((snapshot_root/path).read_bytes()).hexdigest(),
                                       id=data['id'],version=data['version'],kind=kind)

    def document(reference, kind):
        value = documents.get(reference['path'])
        require(value is not None and value['kind'] == KINDS[kind],
                'ANIMATION_REFERENCE', 'Missing typed animation '+kind+' document.')
        return value

    def rig_for(reference):
        rig = rigs.get(reference['path']) if reference else None
        require(rig is not None, 'ANIMATION_TARGET', 'Skeletal target requires a verified rig binding.')
        return rig

    def native_scene(scene):
        source = native.get(scene['scene_asset']['path'])
        require(source is not None and source[0] == 'scene3d-project',
                'ANIMATION_OWNER', 'Scene needs a verified native project.')
        return source[1]

    def bone(rig, identifier):
        require(identifier in {b['id'] for b in rig.skeleton['bones']},
                'ANIMATION_TARGET', 'Bone is absent from bound rig: '+identifier)

    def target(value, rig=None, scene=None, actor_id=None):
        domain, prop = value['kind'], value['property']
        if domain in ('bone', 'morph'):
            require(rig is not None, 'ANIMATION_TARGET', 'Skeletal target requires a verified rig binding.')
            if actor_id is not None:
                require(value['actor_id'] == actor_id, 'ANIMATION_TARGET', 'Track targets another actor.')
            if domain == 'bone': bone(rig, value['bone_id'])
            else:
                mesh = rig.geometry.by_id().get(value['mesh_id'])
                require(mesh is not None and value['morph_id'] in mesh.morphs,
                        'ANIMATION_TARGET', 'Morph is absent from bound rig.')
            return
        if domain == 'node':
            require(scene is not None, 'ANIMATION_TARGET', 'Node target needs a scene binding.')
            nodes = [n for d in native_scene(scene)['documents'].values() if d['kind'] == 'scene3d-scene'
                     for n in d['nodes'] if n['id'] == value['node_id']]
            require(len(nodes) == 1, 'ANIMATION_TARGET', 'Node target is absent or ambiguous.')
            return
        # Camera and LookDev properties are admitted by the existing adapters,
        # never by assuming an identically named scene node is that owner.
        from animation3d_owners import _resolve_property
        candidates = []
        for path, (kind, admitted) in native.items():
            if ((domain == 'camera' and kind == 'camera3d-registry') or
                (domain == 'material' and kind in ('material3d','material-instance3d','material-variant3d')) or
                (domain == 'light' and kind == 'light3d')):
                source = owner_sources[path]
                if domain == 'camera' and not any(d['kind']=='camera3d-camera' and d['id']==value['camera_id']
                                                  for d in admitted.values()): continue
                if domain != 'camera' and source['id'] != value[domain+'_id']: continue
                candidates.append((dict(owner='camera3d' if domain=='camera' else 'lookdev',target=value,
                                        source=source), (kind, admitted)))
        require(len(candidates) == 1, 'ANIMATION_OWNER', 'Target needs exactly one explicit supported native owner.')
        _resolve_property(*candidates[0])

    def clip_targets(clip, bound=None, scene=None, actor_id=None):
        require(not any(clip.get(field) for field in ('constraints','procedural_modifiers','entry_pose','exit_pose')),
                'ANIMATION_OWNER', 'Named clip constraints, modifiers and poses need explicit native adapters.')
        own = rig_for(clip['rig_binding']) if 'rig_binding' in clip else None
        if own is not None and bound is not None:
            require(own.manifest['id'] == bound.manifest['id'] and own.manifest['version'] == bound.manifest['version'],
                    'ANIMATION_TARGET', 'Clip rig differs from actor rig; retargeted output must bind its actual rig.')
        rig = own if own is not None else bound
        for track in clip['tracks']: target(track['target'], rig, scene, actor_id)
        root = clip['root_motion']
        if root['enabled']:
            if 'source_bone' in root:
                require(rig is not None, 'ANIMATION_TARGET', 'Root motion needs a bound rig.')
                bone(rig, root['source_bone'])
            else: target(dict(kind='node',node_id=root['source_node'],property='translation'), scene=scene)
        for contact in clip['contacts']:
            if 'target_bone' in contact:
                require(rig is not None, 'ANIMATION_TARGET', 'Contact needs a bound rig.')
                bone(rig, contact['target_bone'])
            elif 'target_node' in contact:
                target(dict(kind='node',node_id=contact['target_node'],property='translation'), scene=scene)
            else:
                require(False, 'ANIMATION_OWNER', 'Contact anchors need an explicit native anchor adapter.')
        if 'skeleton_signature' in clip:
            from rig_errors import object_hash
            require(rig is not None and clip['skeleton_signature'] == object_hash(rig.skeleton),
                    'ANIMATION_TARGET', 'Clip skeleton signature differs from bound rig.')

    placed_clips = set()
    def placement(value, scene, actor=None, layer=None, duration=None):
        clip = document(value['clip'], 'clip')
        require(value['source_in_frame'] < clip['duration_frames'] and
                value.get('source_out_frame', clip['duration_frames']) <= clip['duration_frames'],
                'ANIMATION_INTERVAL', 'Placement source trim exceeds referenced clip.')
        require(duration is None or value['start_frame'] + value['duration_frames'] <= duration,
                'ANIMATION_INTERVAL', 'Placement exceeds containing timeline.')
        actor_id = value.get('actor_id', actor['actor_id'] if actor else None)
        if actor is None:
            actor = next((a for a in scene['actors'] if a['actor_id'] == actor_id), None)
        require(actor is not None and actor_id == actor['actor_id'],
                'ANIMATION_REFERENCE', 'Placement actor is absent from scene.')
        layers = {item['id']: item for item in actor['animation_layers']}
        require(value['layer_id'] in layers and (layer is None or layer['id'] == value['layer_id']),
                'ANIMATION_REFERENCE', 'Placement layer is absent from actor.')
        bound = rig_for(actor['rig']) if 'rig' in actor else None
        clip_targets(clip, bound, scene, actor_id)
        placed_clips.add(value['clip']['path'])

    for d in documents.values():
        if 'duration_frames' in d:
            require(d['duration_frames'] <= budgets['max_frames'], 'ANIMATION_LIMIT', 'Animation duration exceeds project budget.')
        if 'fps' in d:
            from animation3d_time import rate
            require(rate(d['fps']) == rate(project['fps']), 'ANIMATION_RATE', 'Project documents use inconsistent frame rates.')
        if d['kind'] == KINDS['scene']:
            native_scene(d)
            for field in ('object_tracks','environment_tracks','camera_tracks','lighting_tracks'):
                for track in d[field]: target(track['target'], scene=d)
            actor_ids = {a['actor_id'] for a in d['actors']}
            for interaction in d['interactions']:
                require({p['actor_id'] for p in interaction['participants']} <= actor_ids,
                        'ANIMATION_REFERENCE', 'Interaction participant is absent from scene.')
                if 'prop_id' in interaction:
                    target(dict(kind='node',node_id=interaction['prop_id'],property='translation'), scene=d)
                constraints = {c['id'] for a in d['actors'] for c in a['constraints']}
                require(set(interaction['constraints']) <= constraints, 'ANIMATION_REFERENCE', 'Interaction constraint is absent.')
                for participant in interaction['participants']:
                    actor = next(a for a in d['actors'] if a['actor_id']==participant['actor_id'])
                    if 'hand_bone' in participant: bone(rig_for(actor.get('rig')), participant['hand_bone'])
                    require('grip_anchor' not in participant, 'ANIMATION_OWNER', 'Grip anchors need an explicit native adapter.')
                    if 'interaction_marker' in participant:
                        require(participant['interaction_marker'] in {m['name'] for m in d['markers']},
                                'ANIMATION_REFERENCE', 'Interaction marker is absent.')
                require(not any('previous_anchor' in v or 'next_anchor' in v for v in interaction.get('ownership_changes', [])),
                        'ANIMATION_OWNER', 'Ownership anchors need an explicit native adapter.')
            for actor in d['actors']:
                rig = rig_for(actor['rig']) if 'rig' in actor else None
                for constraint in actor['constraints']:
                    target(constraint['source'], rig, d, actor['actor_id'])
                    if 'target' in constraint: target(constraint['target'], rig, d)
                    for identifier in constraint.get('chain_bones', []):
                        require(rig is not None, 'ANIMATION_TARGET', 'Constraint chain needs a rig.')
                        bone(rig, identifier)
                for layer in actor['animation_layers']:
                    require('reference_pose' not in layer, 'ANIMATION_OWNER', 'Named layer poses need an explicit native adapter.')
                    mask = layer.get('bone_mask', {})
                    for identifier in mask.get('included_bones', []) + mask.get('excluded_bones', []) + list(mask.get('per_bone_weights', {})):
                        require(rig is not None, 'ANIMATION_TARGET', 'Bone mask needs a bound rig.')
                        bone(rig, identifier)
                    for value in layer['placements']: placement(value, d, actor, layer)
                    for value in layer.get('affected_targets', []): target(value, rig, d, actor['actor_id'])
                if 'state_machine' in actor:
                    machine = document(actor['state_machine'], 'state-machine')
                    for state in machine['states']:
                        require('clip' in state, 'ANIMATION_OWNER', 'Blend-tree admission needs an explicit owner.')
                        clip_targets(document(state['clip'], 'clip'), rig, d, actor['actor_id'])
                        placed_clips.add(state['clip']['path'])
        if d['kind'] == KINDS['sequence']:
            require(d['total_frames'] <= budgets['max_frames'], 'ANIMATION_LIMIT', 'Sequence exceeds project frame budget.')
            for shot in d['shots']:
                require(shot['scene_id'] in scenes, 'ANIMATION_REFERENCE', 'Shot scene is absent from project closure.')
                scene = scenes[shot['scene_id']]
                camera_ids = {node['id'] for doc in native_scene(scene)['documents'].values() if doc['kind']=='scene3d-scene'
                              for node in doc['nodes'] if node['type']=='camera'}
                require(shot['active_camera_id'] in camera_ids, 'ANIMATION_TARGET', 'Shot camera is absent from native scene.')
                for value in shot['clip_placements']: placement(value, scene, duration=shot['duration_frames'])
                for identifier, values in shot.get('actor_overrides', {}).items():
                    actor = next((a for a in scene['actors'] if a['actor_id']==identifier), None)
                    require(actor is not None, 'ANIMATION_REFERENCE', 'Override actor is absent from scene.')
                    for track in values: target(track['target'], rig_for(actor.get('rig')), scene, identifier)
        if d['kind'] == KINDS['retarget']:
            for field, key in (('source_rig','source_bone'),('target_rig','target_bone')):
                rig = rig_for(d[field])
                from rig_errors import object_hash
                require(d[field.replace('_rig','_skeleton_signature')] == object_hash(rig.skeleton),
                        'ANIMATION_TARGET', 'Retarget skeleton signature differs from bound rig.')
                for value in d['mappings']: bone(rig, value[key])
        if d['kind'] == KINDS['state-machine']:
            require(all('clip' in state for state in d['states']), 'ANIMATION_OWNER', 'Blend-tree admission needs an explicit owner.')
    for path, d in documents.items():
        if d['kind'] == KINDS['clip'] and path not in placed_clips: clip_targets(d)
