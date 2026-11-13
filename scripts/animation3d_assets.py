"""Bounded exact-byte closure admission for Animation3D and native owners.

Validation runs against private copied bytes, never a mixture of live revisions.
The returned inventory is evidence of admission, not rendering or visual approval.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from animation3d_contracts import KINDS, _walk, validate_contract
from rig_errors import RigError, decode_json, require, safe_path

MAX_FILES = 4096
MAX_BYTES = 256 * 1024**2
MAX_DEPTH = 64
ALIASES = {'clip': KINDS['clip'], 'state-machine': KINDS['state-machine'],
           'retarget': KINDS['retarget'], 'sequence': KINDS['sequence'],
           'project': KINDS['project'], 'scene': KINDS['scene']}


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
        return target

    def native(self, name, expected=None, depth=0):
        require(depth <= MAX_DEPTH, 'ANIMATION_LIMIT', 'Native dependency depth exceeded.')
        path = self.copy(name, None if expected is None else expected['sha256'])
        value = decode_json(path.read_bytes())
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


def verify_native(root, reference):
    """Return detached native owner data after exact closure validation."""
    with TemporaryDirectory(prefix='animation3d-source-') as temporary:
        snapshot = Snapshot(root, temporary)
        kind, value = snapshot.native(reference['path'], reference)
        require(reference['kind'] in (kind, 'rig' if kind=='hatch-3d-rig' else kind),
                'ANIMATION_OWNER', 'Native source kind differs from reference.')
        return kind, value


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
        require(snapshot.total <= snapshot.max_bytes, 'ANIMATION_LIMIT', 'Project exceeds work byte budget.')
        def visit(path, expected=None, depth=0):
            require(depth <= MAX_DEPTH, 'ANIMATION_LIMIT', 'Animation dependency depth exceeded.')
            require(path not in active, 'ANIMATION_CYCLE', 'Animation dependency cycle.')
            file = snapshot.copy(path, None if expected is None else expected['sha256'])
            value = decode_json(file.read_bytes())
            if value.get('kind') not in KINDS.values():
                require(expected is not None, 'ANIMATION_KIND', 'Expected typed native source.')
                kind, admitted = snapshot.native(path, expected, depth)
                allowed = {'rig': {'hatch-3d-rig'}, 'scene': {'scene3d-project'},
                           'model': {'scene3d-project'}, 'character': {'hatch-3d-rig'},
                           'render-profile': {'camera3d-registry'}}
                require(kind in allowed.get(expected['kind'], {expected['kind']}),
                        'ANIMATION_OWNER', 'Reference points to wrong native owner kind.')
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
        _validate_bindings(project, documents, native)
        return {'project': deepcopy(project), 'documents': deepcopy(documents),
                'inventory': deepcopy(snapshot.inventory), 'bytes': snapshot.total,
                'source_ready': True, 'scope': 'exact dependency and native target admission; no evaluated scene or render'}


def _validate_bindings(project, documents, native):
    budgets = project['budgets']
    tracks = [n for d in documents.values() for n in _walk(d) if 'keys' in n and 'target' in n]
    actors = [n for d in documents.values() for n in _walk(d) if 'animation_layers' in n]
    require(len(tracks) <= budgets['max_tracks'] and sum(len(t['keys']) for t in tracks) <= budgets['max_keyframes'] and
            len(actors) <= budgets['max_actors'], 'ANIMATION_LIMIT', 'Animation project exceeds track/key/actor budget.')
    scenes = {d['id']: d for d in documents.values() if d['kind'] == KINDS['scene']}
    for d in documents.values():
        if 'duration_frames' in d:
            require(d['duration_frames'] <= budgets['max_frames'], 'ANIMATION_LIMIT', 'Animation duration exceeds project budget.')
        if 'fps' in d:
            from animation3d_time import rate
            require(rate(d['fps']) == rate(project['fps']), 'ANIMATION_RATE', 'Project documents use inconsistent frame rates.')
        if d['kind'] == KINDS['sequence']:
            require(d['total_frames'] <= budgets['max_frames'], 'ANIMATION_LIMIT', 'Sequence exceeds project frame budget.')
            for shot in d['shots']:
                require(shot['scene_id'] in scenes, 'ANIMATION_REFERENCE', 'Shot scene is absent from project closure.')
                scene = scenes[shot['scene_id']]
                source = native.get(scene['scene_asset']['path'])
                require(source is not None and source[0] == 'scene3d-project', 'ANIMATION_OWNER', 'Scene needs a verified native project.')
                camera_ids = {node['id'] for doc in source[1]['documents'].values() if doc['kind']=='scene3d-scene'
                              for node in doc['nodes'] if node['type']=='camera'}
                require(shot['active_camera_id'] in camera_ids, 'ANIMATION_TARGET', 'Shot camera is absent from native scene.')
        if d['kind'] == KINDS['scene']:
            actor_ids = {a['actor_id'] for a in d['actors']}
            for interaction in d['interactions']:
                require({p['actor_id'] for p in interaction['participants']} <= actor_ids,
                        'ANIMATION_REFERENCE', 'Interaction participant is absent from scene.')
