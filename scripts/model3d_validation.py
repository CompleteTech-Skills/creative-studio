"""Read-only Model3D verification over a bounded, hash-verified temporary snapshot.

All manifest paths are relative to one explicitly selected project root, including
paths inside dependency manifests. No registry, job store or renderer is created.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.metadata
import math
import platform
import re
import os
from pathlib import Path
import stat
import struct
import tempfile
import unicodedata

from model3d_contracts import KINDS, LIMITS, VERSION, file_references, validate_contract
from rig_errors import RigError, MAX_JSON_BYTES, object_hash, read_json, require, safe_path, sha256


def _integer_count(value):
    """Normalize JSON-Schema integers without truncating fractions or booleans."""
    require(not isinstance(value, bool) and
            (isinstance(value, int) or isinstance(value, float) and math.isfinite(value) and value.is_integer()),
            'MODEL3D_SCHEMA', 'An operational count must be a whole JSON number, not a boolean or fraction.')
    require(value >= 1, 'MODEL3D_SCHEMA', 'An operational resource count must be positive.')
    return int(value)


def _runtime_limits(value):
    return {name: _integer_count(count) for name, count in value.get('limits', LIMITS).items()}


def _file_fingerprint(info):
    # Windows st_ctime is creation time, and CRT fstat/path stat can expose it
    # with different precision. It cannot detect writes there. Identity, size,
    # nanosecond mtime and the mandatory content digest remain authoritative.
    fields = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return fields if os.name == 'nt' else fields + (info.st_ctime_ns,)


@contextmanager
def _open_payload(root, name):
    """Use existing portable-path rules and reject link/special-file payloads.

On platforms with openat/O_NOFOLLOW, hold each directory descriptor so a parent
rename cannot redirect the read. The portable fallback rechecks the path and
file identity; hostile concurrent directory replacement still needs OS isolation.
    """
    target = safe_path(root, name, exists=True)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptors = []
    try:
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptors.append(os.open(root, directory_flags))
            parts = Path(name).parts
            for part in parts[:-1]:
                descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
            fd = os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=descriptors[-1])
        else:
            fd = os.open(target, flags)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode), 'MODEL3D_FILE', 'Payload must be a regular file.', asset=name)
            current = safe_path(root, name, exists=True).stat()
            require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino),
                    'MODEL3D_CHANGED', 'Payload changed while opening.', asset=name)
            yield stream, before
            after = os.fstat(stream.fileno())
            current = safe_path(root, name, exists=True).stat()
            require(_file_fingerprint(before) == _file_fingerprint(after) == _file_fingerprint(current),
                    'MODEL3D_CHANGED', 'Payload changed during verification.', asset=name)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class _Snapshot:
    def __init__(self, root, destination, limits):
        self.root, self.destination = root, Path(destination).resolve()
        self.limits = {name: _integer_count(count) for name, count in limits.items()}
        self.files, self.aliases = {}, {}
        self.total = 0
        self.image_dimensions = {}

    def capture(self, record, *, memory_bytes=None, file_bytes=None):
        image_budget = min(self.limits['memory_bytes'], memory_bytes if memory_bytes is not None else self.limits['memory_bytes'])
        file_budget = min(self.limits['file_bytes'], file_bytes if file_bytes is not None else self.limits['file_bytes'])
        declared_bytes = _integer_count(record['bytes'])
        require(declared_bytes <= file_budget, 'MODEL3D_BUDGET', 'Payload exceeds its inherited per-file budget.', asset=record['path'])
        name = record['path']
        alias = unicodedata.normalize('NFC', name).casefold()
        require(alias not in self.aliases or self.aliases[alias] == name,
                'MODEL3D_PATH', 'Dependency closure contains path aliases.', asset=name)
        require(name not in self.files or self.files[name] == record,
                'MODEL3D_HASH_CONFLICT', 'Dependency closure contains conflicting payload declarations.', asset=name)
        if name in self.files:
            dimensions = self.image_dimensions.get(name)
            require(dimensions is None or dimensions[0] * dimensions[1] <= min(16_777_216, image_budget // 16),
                    'MODEL3D_BUDGET', 'Cached image exceeds this document memory budget.', asset=name)
            return self.destination / name
        require(len(self.files) < self.limits['files'] and declared_bytes <= file_budget and
                self.total + declared_bytes <= self.limits['total_bytes'],
                'MODEL3D_BUDGET', 'Expanded payload closure exceeds its resource budget.', asset=name)
        target = self.destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, total = hashlib.sha256(), 0
        with _open_payload(self.root, name) as (source, info), target.open('xb') as output:
            require(info.st_size == declared_bytes, 'MODEL3D_SIZE', 'Payload size does not match its declaration.', asset=name)
            for chunk in iter(lambda: source.read(min(1024**2, declared_bytes + 1)), b''):
                total += len(chunk)
                require(total <= declared_bytes, 'MODEL3D_SIZE', 'Payload grew beyond its declared size.', asset=name)
                digest.update(chunk)
                output.write(chunk)
        require(total == declared_bytes and digest.hexdigest() == record['sha256'],
                'MODEL3D_HASH', 'Payload content does not match its declared SHA-256.', asset=name)
        if record['media_type'].startswith('image/'):
            try:
                from PIL import Image, UnidentifiedImageError
                with Image.open(target) as image:
                    expected = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}
                    require(image.format == expected[record['media_type']], 'MODEL3D_FORMAT',
                            'Image encoding does not match its media declaration.', asset=name)
                    require(image.width * image.height <= min(16_777_216, image_budget // 16),
                            'MODEL3D_BUDGET', 'Decoded texture exceeds its pixel/memory budget.', asset=name)
                    require(getattr(image, 'n_frames', 1) == 1, 'MODEL3D_TEXTURE',
                            'This contract accepts static source textures only.', asset=name)
                    self.image_dimensions[name] = [image.width, image.height]
                    image.load()
            except ImportError as exc:
                raise RigError('MODEL3D_DEPENDENCY', 'Pillow is required to inspect source images.') from exc
            except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
                raise RigError('MODEL3D_IMAGE', 'Image payload cannot be decoded safely.', asset=name) from exc
        self.files[name] = dict(record)
        self.aliases[alias] = name
        self.total += total
        return target


def _inspect_asset(document, snapshot, memory_bytes=None, file_bytes=None):
    """Decode genuine geometry through the repository's existing import authority."""
    try:
        from rig_contracts import DEFAULT_LIMITS
        from rig_import import load_character_model
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise RigError('MODEL3D_DEPENDENCY', 'Install Pillow and requirements-rig.txt for geometry inspection.', stage='model3d') from exc
    record = document['geometry']
    expected = {'model/gltf-binary': '.glb', 'model/gltf+json': '.gltf', 'model/obj': '.obj'}
    require(Path(record['path']).suffix.lower() == expected[record['media_type']],
            'MODEL3D_FORMAT', 'Declared model media type and supported extension disagree.')
    limits = _runtime_limits(document)
    backend_limits = dict(DEFAULT_LIMITS, source_bytes=min(limits['file_bytes'], file_bytes if file_bytes is not None else limits['file_bytes']),
                          dependency_bytes=limits['total_bytes'], vertices=limits['vertices'],
                          triangles=limits['triangles'], memory_bytes=min(limits['memory_bytes'], memory_bytes if memory_bytes is not None else limits['memory_bytes']),
                          influences=8)  # Inspect/preserve supported skins; this is not a viewer profile.
    def inspect_native(name):
        try:
            return load_character_model(snapshot.destination / name, limits=backend_limits)
        except RigError as exc:
            # Keep the Model3D boundary stable when the native texture authority
            # replaces Pillow exceptions with actionable structured diagnostics.
            # Other native limits retain their established public codes.
            if exc.report['code'] in {'RIG_TEXTURE_DECODE', 'RIG_TEXTURE_FORMAT',
                                      'RIG_LIMIT_TEXTURE', 'RIG_TEXTURE_POLICY',
                                      'RIG_TEXTURE_VERIFIER_UNAVAILABLE'}:
                unavailable = exc.report['code'] == 'RIG_TEXTURE_VERIFIER_UNAVAILABLE'
                raise RigError('MODEL3D_DEPENDENCY' if unavailable else 'MODEL3D_MODEL',
                               'Required native texture verifier is unavailable.' if unavailable
                               else 'Texture rejected at the native model importer boundary.',
                               stage='model3d', asset=name, observed=exc.report,
                               correction=exc.report['correction']) from exc
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError,
                struct.error, Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
            raise RigError('MODEL3D_MODEL', 'Malformed model rejected at the existing importer boundary.',
                           stage='model3d', asset=name) from exc
    model = inspect_native(record['path'])
    actual_dependencies = {p.relative_to(snapshot.destination).as_posix() for p in model.source_files}
    inventory = {r['path'] for r in document['files']}
    require(actual_dependencies <= inventory, 'MODEL3D_INVENTORY',
            'Actual model resources are absent from the asset file inventory.',
            observed=sorted(actual_dependencies - inventory))
    geometry = model.geometry
    require(all(isinstance(mesh.id, str) and 1 <= len(mesh.id) <= 128 and
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*', mesh.id)
                for mesh in geometry.meshes), 'MODEL3D_MESH_ID',
            'Imported mesh identities must be portable Model3D identifiers.')
    ids = {mesh.id for mesh in geometry.meshes}
    require(len(ids) == len(geometry.meshes), 'MODEL3D_MESH_ID', 'Imported mesh identities must be unique.')
    declared_ids = ({name for part in document['parts'] for name in part['mesh_ids']} |
                    {region['mesh_id'] for part in document['parts'] for region in part['mesh_regions']})
    require(declared_ids == ids, 'MODEL3D_MESH_BINDING',
            'Semantic part bindings must cover the actual decoded mesh identities exactly.',
            expected=sorted(ids), observed=sorted(declared_ids))
    # Every declared LOD is decoded using the same import authority. The metadata
    # below is measured from those bytes, never promoted from manifest assertions.
    details_by_file = {}
    for detail in document['mesh_details']:
        details_by_file.setdefault(detail['geometry']['path'], []).append(detail)
    lod_reports = []
    for name, details in details_by_file.items():
        geometry_record = details[0]['geometry']
        if name == record['path']:
            imported = model
        else:
            require(geometry_record['media_type'] in expected and
                    Path(name).suffix.lower() == expected[geometry_record['media_type']],
                    'MODEL3D_FORMAT', 'LOD media type and geometry extension disagree.')
            imported = inspect_native(name)
            dependencies = {p.relative_to(snapshot.destination).as_posix() for p in imported.source_files}
            require(dependencies <= inventory, 'MODEL3D_INVENTORY', 'LOD resources are absent from the file inventory.')
        require(not imported.report.get('fixture') or document['provenance']['fixture'],
                'MODEL3D_PROVENANCE', 'Embedded fixture provenance in any LOD cannot be relabeled as production.')
        require(all(isinstance(mesh.id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', mesh.id)
                    for mesh in imported.geometry.meshes), 'MODEL3D_MESH_ID', 'LOD mesh identities must be portable identifiers.')
        declared = {item['mesh_id'] for item in details}
        require(declared == set(imported.geometry.by_id()), 'MODEL3D_MESH_BINDING',
                'Mesh measurements must cover every decoded mesh in each LOD file.')
        for detail in details:
            mesh = imported.geometry.by_id()[detail['mesh_id']]
            analysis = next(item for item in imported.report['meshes'] if item['id'] == detail['mesh_id'])
            actual = dict(vertices=len(mesh.positions), triangles=len(mesh.triangles), material_slot=mesh.material,
                          normals='NORMAL' in mesh.attributes, tangents='TANGENT' in mesh.attributes,
                          uv_sets=[i for i in (0,1) if 'TEXCOORD_' + str(i) in mesh.attributes],
                          topology=dict(boundary_edges=analysis['boundary_edges'],
                                        nonmanifold_edges=analysis['nonmanifold_edges'],
                                        degenerate_faces=analysis['degenerate_triangles']))
            require(all(detail[key] == observed for key, observed in actual.items()), 'MODEL3D_MEASUREMENTS',
                    'Per-mesh topology, attribute or count declarations differ from decoded geometry.',
                    asset=detail['mesh_id'], expected=actual)
        lod_reports.append(dict(path=name, sha256=geometry_record['sha256'],
                                levels=sorted({item['lod'] for item in details}), inspection=imported.report))
        del imported  # Do not retain every decoded LOD simultaneously.
    for part in document['parts']:
        for region in part['mesh_regions']:
            mesh = geometry.by_id().get(region['mesh_id'])
            require(mesh is not None, 'MODEL3D_REFERENCE', 'Surface region references an absent decoded mesh.')
            require(len(set(region['face_indices'])) == len(region['face_indices']) and
                    max(region['face_indices']) < len(mesh.triangles), 'MODEL3D_REGION',
                    'Surface region contains repeated or out-of-range face indices.')
    for uncertainty in document['uncertainties']:
        scope = uncertainty['scope']
        if scope['level'] in ('faces', 'vertices'):
            mesh = geometry.by_id()[scope['target']]
            count = len(mesh.triangles) if scope['level'] == 'faces' else len(mesh.positions)
            require(bool(scope['indices']) and len(set(scope['indices'])) == len(scope['indices']) and
                    max(scope['indices']) < count, 'MODEL3D_UNCERTAINTY',
                    'Scoped uncertainty has repeated, empty or out-of-range element indices.')
    for material in document['materials']:
        require(material['slot'] < len(geometry.materials), 'MODEL3D_MATERIAL', 'Material slot is absent from the model.')
    measured = document['measurements']
    require(measured['vertices'] == geometry.vertex_count and measured['triangles'] == geometry.triangle_count,
            'MODEL3D_MEASUREMENTS', 'Declared counts differ from actual decoded geometry.',
            expected=dict(vertices=geometry.vertex_count, triangles=geometry.triangle_count), observed=measured)
    actual_bounds = model.report['bounds']
    for name, actual in [('minimum', actual_bounds[0]), ('maximum', actual_bounds[1])]:
        require(all(abs(a-b) <= 1e-6 * max(1., abs(a), abs(b)) for a, b in zip(actual, measured['bounds'][name])),
                'MODEL3D_MEASUREMENTS', 'Declared bounds differ from actual geometry; tolerance is 1e-6 relative/absolute meters.')
    require(not model.report.get('fixture') or document['provenance']['fixture'],
            'MODEL3D_PROVENANCE', 'Embedded fixture provenance cannot be relabeled as production.')
    from rig_jobs import code_signature
    return dict(asset_id=document['id'], version=document['version'],
                inspection_memory_budget_bytes=backend_limits['memory_bytes'],
                inspection_file_budget_bytes=backend_limits['source_bytes'],
                rig_implementation_sha256=code_signature(),
                software={name: importlib.metadata.version(name) for name in ('numpy', 'Pillow')},
                geometry_sha256=record['sha256'], importer='rig_import.load_character_model',
                inspection=model.report, lod_geometry=lod_reports, mesh_ids=sorted(ids),
                skin_bindings=len(model.weights), animation_clips=len(model.clips),
                material_parameter_equivalence='not-assessed', visual_approval='not-assessed')


def validate_document(document, root=None, *, structural_only=False):
    """Validate shape, semantics and the complete declared immutable file closure.

The default path decodes each referenced asset's real geometry. A structural-only
report explicitly leaves payloads and geometry unverified. Returns observations,
not production readiness, an authenticated review, or a successful generation job.
Raises RigError for expected invalid input; does not modify the selected root.
    """
    validate_contract(document)
    report = dict(schema_version=1, kind='model3d-validation', implementation_version=VERSION,
                  document_kind=document['kind'], document_sha256=object_hash(document),
                  implementation_sha256=object_hash({p.name: sha256(p) for p in
                      sorted(Path(__file__).parent.glob('model3d*.py'))}),
                  software=dict(python=platform.python_version(), jsonschema=importlib.metadata.version('jsonschema')),
                  contract_valid=True, payloads_verified=False, geometry_inspected=False,
                  provider_execution='not-assessed', visual_approval='not-assessed',
                  licensing_approval='not-assessed', geometry=[], files=[])
    if structural_only:
        report['mode'] = 'structural-only'
        return report
    require(root is not None, 'MODEL3D_ROOT', 'Payload validation needs an explicit project root.')
    root = Path(root).absolute()
    require(root.is_dir() and not root.is_symlink(), 'MODEL3D_ROOT', 'Choose a regular project directory, not a link.')
    root = root.resolve()
    active, identities, results, geometry_positions = set(), {}, {}, {}
    with tempfile.TemporaryDirectory(prefix='hatch-model3d-verify-') as temporary:
        snapshot = _Snapshot(root, Path(temporary), document.get('limits', LIMITS))

        def visit(value, depth, inherited_memory=None, inherited_file_bytes=None):
            validate_contract(value)
            current_limits = _runtime_limits(value)
            effective_file_bytes = min(snapshot.limits['file_bytes'], current_limits['file_bytes'],
                                       inherited_file_bytes if inherited_file_bytes is not None else snapshot.limits['file_bytes'])
            effective_memory = min(snapshot.limits['memory_bytes'], current_limits['memory_bytes'],
                                   inherited_memory if inherited_memory is not None else snapshot.limits['memory_bytes'])
            require(depth <= snapshot.limits['dependency_depth'], 'MODEL3D_DEPTH', 'Asset dependency depth exceeds the selected budget.')
            key = (value.get('id'), value.get('version')) if value['kind'] == KINDS['asset'] else None
            require(key is None or key not in active, 'MODEL3D_CYCLE', 'Asset dependency cycle detected.')
            if key is not None:
                active.add(key)
            names, height = set(), 0
            for record in file_references(value):
                snapshot.capture(record, memory_bytes=effective_memory, file_bytes=effective_file_bytes)
                names.add(record['path'])
            for material in ([value] if value['kind'] == KINDS['material'] else value.get('materials', []) + value.get('material_requirements', [])):
                for role, dimensions in material['texture_dimensions'].items():
                    require(role in material['textures'], 'MODEL3D_TEXTURE', 'Texture dimensions reference a missing texture.')
                    require(dimensions is not None and snapshot.image_dimensions.get(material['textures'][role]['file']['path']) == dimensions,
                            'MODEL3D_DIMENSIONS', 'Material texture dimensions differ from decoded pixels.')
            sources = [value] if value['kind'] == KINDS['source'] else value.get('sources', [])
            for source in sources:
                dimensions = snapshot.image_dimensions.get(source['file']['path'])
                if dimensions is not None:
                    require(source['dimensions_px'] == dimensions, 'MODEL3D_DIMENSIONS',
                            'Declared source dimensions differ from the decoded image.',
                            asset=source['file']['path'], expected=dimensions, observed=source['dimensions_px'])
                    require(source['camera'].get('image_size', dimensions) == dimensions, 'MODEL3D_CAMERA',
                            'Calibration image size differs from the decoded observation.',
                            asset=source['file']['path'])
            if value['kind'] in (KINDS['job'], KINDS['provider-receipt']):
                record = value['request']
                require(record['media_type'] == 'application/json' and record['bytes'] <= MAX_JSON_BYTES,
                        'MODEL3D_MANIFEST', 'Job requests require bounded JSON documents.')
                request = read_json(snapshot.destination / record['path'])
                target_id = value['asset_id'] if value['kind'] == KINDS['job'] else value['subject']['id']
                require(isinstance(request, dict) and request.get('kind') == KINDS['request'] and
                        request.get('asset_id') == target_id,
                        'MODEL3D_REFERENCE', 'Job request must identify the same target asset.')
                validate_contract(request)
                if value['kind'] == KINDS['provider-receipt']:
                    require(request.get('method') == 'provider' and request.get('provider') is not None and
                            request['provider'].get('id') == value['provider'] and
                            (request['provider'].get('model') is None or request['provider']['model'] == value['model']),
                            'MODEL3D_REFERENCE', 'Provider receipts must reference the matching provider request.')
                request_names, request_height = visit(request, depth + 1, effective_memory, effective_file_bytes)
                names.update(request_names)
                height = request_height + 1
            dependencies = list(value.get('dependencies', []))
            if value['kind'] in (KINDS['export'], KINDS['archive']):
                dependencies.append(value['asset'])
            if value['kind'] == KINDS['repair-operation']:
                dependencies.append(value['before'])
                if value['after'] is not None:
                    dependencies.append(value['after'])
            for dependency in dependencies:
                record = dependency['manifest']
                require(record['media_type'] == 'application/json' and record['bytes'] <= MAX_JSON_BYTES,
                        'MODEL3D_MANIFEST', 'Dependency manifests require bounded JSON payloads.')
                child = read_json(snapshot.destination / record['path'])
                child_key = dependency['id'], dependency['version']
                require(isinstance(child, dict) and child.get('kind') == KINDS['asset'] and
                        (child.get('id'), child.get('version')) == child_key,
                        'MODEL3D_REFERENCE', 'Dependency manifest does not match its exact asset identity.')
                validate_contract(child)
                require(child_key not in active, 'MODEL3D_CYCLE', 'Asset dependency cycle detected.')
                require(child_key not in identities or identities[child_key] == record['sha256'],
                        'MODEL3D_VERSION_CONFLICT', 'An immutable asset version has conflicting manifest hashes.')
                identities[child_key] = record['sha256']
                child_limits = _runtime_limits(child)
                child_memory = min(effective_memory, child_limits['memory_bytes'])
                child_file_bytes = min(effective_file_bytes, child_limits['file_bytes'])
                context_key = child_key, child_memory, child_file_bytes
                if context_key not in results:
                    results[context_key] = visit(child, depth + 1, child_memory, child_file_bytes)
                child_names, child_height = results[context_key]
                height = max(height, child_height + 1)
                require(depth + height <= snapshot.limits['dependency_depth'],
                        'MODEL3D_DEPTH', 'Cached dependency closure exceeds the selected depth budget.')
                names.update(child_names)
                if value['kind'] == KINDS['asset'] and child['provenance']['fixture']:
                    require(value['provenance']['fixture'], 'MODEL3D_PROVENANCE', 'Fixture dependencies cannot become production assets.')
            limits = current_limits
            require(all(name not in snapshot.image_dimensions or
                        snapshot.image_dimensions[name][0] * snapshot.image_dimensions[name][1] <=
                        min(16_777_216, limits['memory_bytes'] // 16) for name in names),
                    'MODEL3D_BUDGET', 'Image payload closure exceeds this document memory budget.')
            require(height <= limits['dependency_depth'], 'MODEL3D_DEPTH', 'Transitive dependencies exceed a document depth budget.')
            require(len(names) <= limits['files'] and
                    all(snapshot.files[n]['bytes'] <= limits['file_bytes'] for n in names) and
                    sum(snapshot.files[n]['bytes'] for n in names) <= limits['total_bytes'],
                    'MODEL3D_BUDGET', 'Transitive payload closure exceeds an asset budget.')
            if value['kind'] == KINDS['asset']:
                observation = _inspect_asset(value, snapshot, effective_memory, effective_file_bytes)
                if key not in geometry_positions:
                    geometry_positions[key] = len(report['geometry'])
                    report['geometry'].append(observation)
                else:
                    position = geometry_positions[key]
                    if (observation['inspection_memory_budget_bytes'], observation['inspection_file_budget_bytes']) < (
                            report['geometry'][position]['inspection_memory_budget_bytes'],
                            report['geometry'][position]['inspection_file_budget_bytes']):
                        report['geometry'][position] = observation
                active.remove(key)
            return names, height

        visit(document, 0)
        report.update(mode='verified-snapshot', payloads_verified=True,
                      geometry_inspected=bool(report['geometry']),
                      files=[snapshot.files[name] for name in sorted(snapshot.files)],
                      verified_bytes=snapshot.total,
                      dependency_asset_count=len(identities),
                      path_race_protection='directory-descriptors' if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW')
                      else 'portable-recheck-needs-isolation-against-hostile-writers')
    return report


def validate_file(path, root=None, *, structural_only=False):
    path = Path(path).absolute()
    selected_root = Path(root).absolute() if root is not None else path.parent
    require(not selected_root.is_symlink() and selected_root.is_dir(), 'MODEL3D_ROOT', 'Choose a regular project directory.')
    try:
        relative = path.relative_to(selected_root).as_posix()
    except ValueError as exc:
        raise RigError('MODEL3D_PATH', 'Manifest is outside the selected project root.', stage='model3d') from exc
    with _open_payload(selected_root, relative) as (stream, info):
        require(info.st_size <= MAX_JSON_BYTES, 'MODEL3D_MANIFEST', 'Manifest exceeds the JSON budget.')
        from rig_errors import decode_json
        document = decode_json(stream.read(MAX_JSON_BYTES + 1))
    return validate_document(document, selected_root, structural_only=structural_only)
