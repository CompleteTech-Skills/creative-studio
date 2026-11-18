"""Model3D acquisition adapter over the existing native import authority.

No alternate parser, registry or normalization engine lives here. A returned
snapshot owns its temporary source closure: use it as a context manager or close
it after saving required source evidence through the authoritative asset owner.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import copy
import hashlib
import math
import tempfile
from typing import Any

from rig_contracts import DEFAULT_LIMITS
from rig_errors import MAX_JSON_BYTES, RigError, object_hash, require, sha256
from rig_load_limits import LoadBudget


_NORMALIZED = 'normalized.glb'
_IMPORT_REPORT = 'import-report.json'
_NATIVE_IMPORT_TOOL = 'hatch-native-gltf-import'
_BLENDER_ORIGINAL = 'source/fixture.blend'
_BLENDER_INTERMEDIATE = 'source/model.glb'
_BLENDER_ORIGIN = 'App-authored packed Blender triangle fixture'
_BLENDER_OPERATION = 'Reopen packed triangle and export GLB'


def _limits(values):
    result = dict(DEFAULT_LIMITS)
    require(values is None or isinstance(values, dict), 'MODEL3D_LIMITS', 'Limits must be an object.')
    for key, value in (values or {}).items():
        require(key in result and not isinstance(value, bool) and isinstance(value, (int, float))
                and (isinstance(value, int) or math.isfinite(value)) and value == int(value) and 0 < value <= 2**63-1,
                'MODEL3D_LIMITS', 'Limits must be positive whole JSON numbers with known names.', asset=key)
        result[key] = int(value)
    return result


def _temporary():
    return tempfile.TemporaryDirectory(prefix='model3d-import-')


@dataclass
class MeshImport:
    model: Any
    source_records: list[dict]
    provenance: dict
    root: Path
    _temporary: Any = field(repr=False)

    def close(self):
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _load(path, *, scale, limits):
    from rig_import import load_character_model
    try:
        return load_character_model(path, scale=scale, limits=limits)
    except RigError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, OSError) as exc:
        raise RigError('MODEL3D_MODEL', 'Native importer rejected the model payload.', asset=Path(path).name) from exc


def import_mesh(path, *, scale=1., limits=None, blender=None):
    """Snapshot sources through the shared native import authority."""
    from rig_source_snapshot import import_snapshot
    return import_snapshot(path, scale=scale, limits=limits, blender=blender)


def _blender_source(root, inventory, provenance, report, conversion):
    from asset_import_blender import BLENDER_BUILD, BLENDER_VERSION

    original = inventory.get(_BLENDER_ORIGINAL, {})
    intermediate = inventory.get(_BLENDER_INTERMEDIATE, {})
    source_records = provenance.get('source_records', [])
    blender = report.get('blender')
    blender_tool = dict(name='Blender', version=BLENDER_VERSION)
    require(provenance['fixture'] and provenance['method'] == 'fixture'
            and provenance['origin'] == _BLENDER_ORIGIN
            and original.get('role') == 'source' and original.get('format') == 'binary'
            and intermediate.get('role') == 'evidence' and intermediate.get('format') == 'glb'
            and provenance['sources'] == [original]
            and {path for path in inventory if path.startswith('source/')} ==
            {_BLENDER_ORIGINAL, _BLENDER_INTERMEDIATE}
            and len(source_records) == 1 and source_records[0]['file'] == original
            and source_records[0]['original_filename'] == 'fixture.blend'
            and blender_tool in provenance['tools']
            and conversion['tool'] == blender_tool['name']
            and conversion['version'] == blender_tool['version']
            and conversion['operation'] == _BLENDER_OPERATION
            and conversion['inputs'] == [original] and conversion['outputs'] == [intermediate],
            'MODEL3D_NORMALIZATION', 'Registry provenance must bind the pinned Blender fixture export.')
    require(isinstance(blender, dict) and blender.get('inspection') == dict(
                version=BLENDER_VERSION, build=BLENDER_BUILD, vertices=3, triangles=1,
                unit_scale=1, external_dependencies=0)
            and blender.get('original_filename') == 'fixture.blend'
            and blender.get('original_path') == _BLENDER_ORIGINAL
            and blender.get('source_bytes') == original['bytes']
            and blender.get('source_sha256') == original['sha256']
            and blender.get('derivative_path') == _BLENDER_INTERMEDIATE
            and blender.get('derivative_sha256') == intermediate['sha256']
            and report.get('original_filename') == 'roundtrip.glb'
            and report.get('source_sha256') == intermediate['sha256'],
            'MODEL3D_NORMALIZATION', 'Import report must bind the exact Blender source and intermediate GLB.')
    with (root/_BLENDER_ORIGINAL).open('rb') as retained:
        require(retained.read(7) == b'BLENDER', 'MODEL3D_NORMALIZATION',
                'Retained original is missing the Blender file signature.')
    return {_BLENDER_INTERMEDIATE: intermediate}


def _gltf_sources(inventory, provenance, report):
    sources = {record['path']: record for record in provenance['sources']}
    require(sources and set(sources) == {path for path in inventory if path.startswith('source/')}
            and all(record['role'] == 'source' for record in sources.values()),
            'MODEL3D_NORMALIZATION', 'Native conversion requires the exact original source inventory.')
    original_name = report.get('original_filename')
    require(isinstance(original_name, str) and Path(original_name).suffix.lower() in ('.glb', '.gltf'),
            'MODEL3D_NORMALIZATION', 'Import report requires a native glTF source.')
    primary = 'model' + Path(original_name).suffix.lower()
    primary_record = sources.get('source/'+primary)
    primary_evidence = [item for item in provenance.get('source_records', [])
                        if item['file'] == primary_record]
    require(primary_record is not None and report.get('source_sha256') == primary_record['sha256']
            and len(primary_evidence) == 1 and primary_evidence[0]['original_filename'] == original_name,
            'MODEL3D_NORMALIZATION', 'Import report does not bind the original model entry.')
    return primary, sources


def _verify_normalized(root, manifest, inventory, limits, *, blender):
    """Reproduce GLB packing from the retained glTF input and verify the exact output."""
    from asset_library_import import (BLENDER_VALIDATOR, TOOL, VERSION,
                                      _inspect, _normalization_plan, _write_glb)
    from rig_interop import validate_gltf

    report_record = inventory.get(_IMPORT_REPORT, {})
    require(report_record.get('role') == 'evidence' and report_record.get('format') == 'json',
            'MODEL3D_NORMALIZATION', 'Native conversion requires its inventoried import report.')
    normalized = inventory[_NORMALIZED]
    provenance = manifest['provenance']
    conversions = provenance.get('conversions', [])
    require(len(conversions) == (2 if blender else 1), 'MODEL3D_NORMALIZATION',
            'Registry provenance must bind the supported native conversion chain.')
    normalization = conversions[-1]

    report = LoadBudget(limits).json(root/_IMPORT_REPORT, expected_hash=report_record['sha256'],
                                     hash_code='MODEL3D_HASH')
    require(isinstance(report, dict) and type(report.get('schema_version')) is int
            and report['schema_version'] == 1 and ('blender' in report) == blender
            and report.get('operation') == 'pack glTF buffers and textures into GLB and apply explicit scene-root units'
            and report.get('coordinate_system') == 'glTF right-handed Y-up'
            and report.get('unit_scale_scope') == 'scene-root transforms and native world-space bone tails'
            and report.get('normalized_sha256') == normalized['sha256']
            and report.get('fixture') is provenance['fixture'],
            'MODEL3D_NORMALIZATION', 'Import report does not describe the exact supported normalized payload.')
    scale = report.get('source_unit_scale')
    require(type(scale) in (int, float) and 1e-3 <= scale <= 1e6
            and scale == manifest['compatibility']['unit_scale'],
            'MODEL3D_COORDINATES', 'Import report and registry disagree on the original unit scale.')
    if blender:
        primary = Path(_BLENDER_INTERMEDIATE).name
        sources = _blender_source(root, inventory, provenance, report, conversions[0])
        require(scale == 1, 'MODEL3D_COORDINATES', 'Blender fixture requires meter geometry.')
    else:
        primary, sources = _gltf_sources(inventory, provenance, report)
    require(normalization['tool'] == TOOL and normalization['version'] == VERSION
            and normalization['operation'] == 'Pack and normalize GLB'
            and len(normalization['inputs']) == len(sources)
            and {record['path'] for record in normalization['inputs']} == set(sources)
            and normalization['outputs'] == [normalized, report_record],
            'MODEL3D_NORMALIZATION', 'Registry provenance must bind the supported native conversion.')
    resource_evidence = object_hash([report.get('source_files'), report.get('embedded')])
    del report

    source_inventory = {name.removeprefix('source/'): dict(bytes=record['bytes'], sha256=record['sha256'])
                        for name, record in sources.items()}
    source = dict(path=root/'source'/primary, inventory=source_inventory,
                  total_bytes=sum(record['bytes'] for record in source_inventory.values()))
    try:
        document, images, files, embedded, preflight = _inspect(source, limits)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, OSError) as exc:
        raise RigError('MODEL3D_NORMALIZATION', 'Cannot verify the retained glTF resource closure.') from exc
    require(object_hash([files, embedded]) == resource_evidence,
            'MODEL3D_NORMALIZATION', 'Report inventory differs from the retained glTF resource closure.')
    # The exact registered length is enough for the producer's packing plan.
    # It also avoids reserving its full configured output limit for a small GLB.
    document.limits = {**document.limits, 'source_bytes': int(normalized['bytes'])}
    plan = _normalization_plan(document, images, scale, provenance['fixture'], preflight)
    with tempfile.TemporaryDirectory(prefix='model3d-normalize-') as verification:
        regenerated = Path(verification)/_NORMALIZED
        _write_glb(regenerated, plan)
        require(sha256(regenerated) == normalized['sha256'], 'MODEL3D_NORMALIZATION',
                'Registered GLB differs from the native conversion at the reported scale.')
    del plan, document, images
    path = root/_NORMALIZED
    decode_limits = _normalized_decode_limits(path, int(normalized['bytes']), limits)
    if blender:
        checked = validate_gltf(source['path'], timeout=min(120, limits['timeout_seconds']))
        require(checked['ok'] and checked['validator'] == BLENDER_VALIDATOR
                and checked['asset_sha256'] == sources[_BLENDER_INTERMEDIATE]['sha256'],
                'MODEL3D_NORMALIZATION', 'Pinned Khronos validation did not verify the retained intermediate GLB.')
    checked = validate_gltf(path, timeout=min(120, limits['timeout_seconds']))
    require(checked['ok'] and checked['validator'] == BLENDER_VALIDATOR
            and checked['asset_sha256'] == normalized['sha256'],
            'MODEL3D_NORMALIZATION', 'Pinned Khronos validation did not verify the registered normalized GLB.')
    return scale, decode_limits


def _normalized_decode_limits(path, size, limits):
    """Reserve the native geometry and encoded images before decoding pixels."""
    from rig_gltf import read_gltf, preflight_gltf

    budget = LoadBudget(limits)
    budget.add(4 * size, 'normalized GLB encoded copies')
    output = read_gltf(path, limits)
    preflight = preflight_gltf(output, encoded_bytes=size, limits=limits)
    encoded_images = sum(output.view(image['bufferView'])[2] for image in output.json.get('images', []))
    reserve = preflight['estimated_memory_bytes'] + 4 * size + encoded_images
    require(reserve < limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
            'Normalized GLB geometry and encoded images exhaust the native decode budget.',
            asset=path.name, expected=limits['memory_bytes'], observed=reserve + 1)
    return {**limits, 'memory_bytes': limits['memory_bytes'] - reserve}


def import_canonical(library, ref, *, entry=None, limits=None):
    """Decode an exact canonical-library version through its public reader.

    The library authenticates its manifest and payloads. This adapter verifies
    the returned bytes again, retains the complete manifest (including namespace,
    original source provenance and uncertainty), and applies no second coordinate
    conversion to an already canonical asset. Legacy-2D records are rejected.
    """
    from asset_library_contracts import validate, load_manifest, canonical_bytes, portable_path
    limits = _limits(limits)
    validate(ref, 'AssetRef')
    manifest = load_manifest(canonical_bytes(library.resolve(copy.deepcopy(ref))))
    require(manifest['manifest_sha256'] == ref['sha256'] and
            manifest['identity']['id'] == ref['id'] and manifest['version'] == ref['version'],
            'MODEL3D_REFERENCE', 'Registry returned a different immutable version.')
    inventory = {item['path']: item for item in manifest['files']}
    normalized = inventory.get(_NORMALIZED, {})
    native_payload = normalized.get('role') == 'payload' and normalized.get('format') == 'glb'
    provenance = manifest['provenance']
    conversions = provenance.get('conversions', [])
    producer_claim = (
        any(tool['name'] == _NATIVE_IMPORT_TOOL for tool in provenance['tools'])
        or any(conversion['tool'] == _NATIVE_IMPORT_TOOL for conversion in conversions)
    )
    blender = provenance['origin'] == _BLENDER_ORIGIN or (
        producer_claim and any(conversion['tool'] == 'Blender'
                               and conversion['operation'] == _BLENDER_OPERATION
                               for conversion in conversions)
    )
    if blender:
        require(entry is None or entry == _NORMALIZED, 'MODEL3D_ENTRY',
                'Blender fixture geometry requires the normalized GLB entry.')
        entry = _NORMALIZED
    if entry is None:
        candidates = [item['path'] for item in manifest['files']
                      if item['role'] in ('geometry', 'runtime') and item['format'] in ('glb','gltf','obj')]
        if native_payload and producer_claim:
            candidates.append(_NORMALIZED)
        require(len(candidates) == 1, 'MODEL3D_ENTRY', 'Select one inventoried geometry entry explicitly.')
        entry = candidates[0]
    portable_path(entry)
    require(entry in inventory and inventory[entry]['format'] in ('glb','gltf','obj'),
            'MODEL3D_ENTRY', 'Entry must name an inventoried supported geometry file.')
    unit_scale = manifest['compatibility']['unit_scale']
    verify_native = entry == _NORMALIZED and (blender or producer_claim or (native_payload and unit_scale != 1))
    require(not verify_native or native_payload, 'MODEL3D_NORMALIZATION',
            'Native conversion requires an inventoried normalized GLB payload.')
    require(manifest['compatibility']['coordinate_system'] == 'right-handed-y-up-z-forward'
            and (verify_native or unit_scale == 1),
            'MODEL3D_COORDINATES', 'Canonical mesh import requires normalized Y-up meter geometry.')
    require(len(inventory) <= limits['archive_files'] and
            sum(int(item['bytes']) for item in inventory.values()) <= min(limits['dependency_bytes'], limits['memory_bytes']//3) and
            all(item['bytes'] <= limits['source_bytes'] for item in inventory.values()) and
            (not verify_native or inventory.get(_IMPORT_REPORT, {}).get('bytes', 0) <= MAX_JSON_BYTES),
            'MODEL3D_BUDGET', 'Canonical inventory exceeds encoded-byte limits before reading.')
    if verify_native and _IMPORT_REPORT in inventory:
        LoadBudget(limits).charge_json(Path(_IMPORT_REPORT), captured_size=int(inventory[_IMPORT_REPORT]['bytes']))
    temporary = _temporary(); root = Path(temporary.name).resolve()
    try:
        records = []
        for name,item in inventory.items():
            payload = library.read_file(copy.deepcopy(ref), name)
            require(isinstance(payload, bytes) and len(payload) == item['bytes']
                    and hashlib.sha256(payload).hexdigest() == item['sha256'],
                    'MODEL3D_HASH', 'Public reader bytes disagree with immutable inventory.', asset=name)
            target = root/name; target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output: output.write(payload)
            records.append(dict(path=name, bytes=len(payload), sha256=item['sha256']))
            del payload
        normalization = 'retained canonical bytes; no additional conversion'
        evidence = {}
        decode_limits = limits
        if verify_native:
            scale, decode_limits = _verify_normalized(root, manifest, inventory, limits, blender=blender)
            normalization = 'verified native GLB with source unit scale baked into geometry'
            evidence = dict(source_unit_scale=scale, normalized_sha256=normalized['sha256'],
                            import_report_sha256=inventory[_IMPORT_REPORT]['sha256'])
        model = _load(root/entry, scale=1., limits=decode_limits)
        actual = {p.relative_to(root).as_posix() for p in model.source_files}
        require(actual <= set(inventory), 'MODEL3D_INVENTORY', 'Model references a non-inventoried dependency.')
        if verify_native:
            require(actual == {_NORMALIZED} and model.report['source_sha256'] == normalized['sha256'],
                    'MODEL3D_INVENTORY', 'Native import must decode the verified standalone GLB.')
        return MeshImport(model, records, dict(authority='AssetLibrary.resolve/read_file',
                          canonical_ref=copy.deepcopy(ref), manifest=copy.deepcopy(manifest),
                          source_entry=entry, normalization=normalization, **evidence),
                          root, temporary)
    except BaseException:
        temporary.cleanup(); raise
