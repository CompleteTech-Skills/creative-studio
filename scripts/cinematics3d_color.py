"""Hash-bound OCIO color processing; renderer provenance remains owner-verified.

No renderer, image decoder, media encoder or global OCIO configuration is used.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from rig_errors import RigError, finite_json, object_hash, normalize_root, open_payload

VERSION = 1
OCIO_VERSION = '2.5.2'
MAX_FILES = 256
MAX_FILE_BYTES = 32 * 1024**2
MAX_TOTAL_BYTES = 64 * 1024**2
MAX_CONFIG_BYTES = 1024**2
MAX_PIXELS = 16_777_216
MAX_WORK_BYTES = 512 * 1024**2
REC709_D65 = {'r': [0.64, 0.33], 'g': [0.30, 0.60], 'b': [0.15, 0.06], 'w': [0.3127, 0.3290]}
_ALLOWED_SUFFIXES = {'.ocio', '.cube', '.spi1d', '.spimtx'}
_SHA = re.compile(r'^[0-9a-f]{64}$')
_PORTABLE = re.compile(r'^[A-Za-z0-9._/-]+$')
_RESERVED = {'con', 'prn', 'aux', 'nul', *('com'+str(i) for i in range(1, 10)), *('lpt'+str(i) for i in range(1, 10))}


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _exact(value, keys, label):
    _need(type(value) is dict and set(value) == set(keys), 'invalid '+label+' fields')


def _name(value):
    _need(type(value) is str and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value), 'invalid color name')
    return value


def _sha(value):
    _need(type(value) is str and bool(_SHA.fullmatch(value)), 'invalid SHA-256')


def _number(value, low, high):
    _need(type(value) in (int, float) and low <= value <= high, 'invalid bounded number')
    _need(math.isfinite(value), 'invalid bounded number')


def _path(value):
    # A deliberately restricted profile subset keeps the worker independent
    # of optional artwork libraries and excludes device names and aliases.
    _need(type(value) is str and 0 < len(value) <= 512 and bool(_PORTABLE.fullmatch(value)), 'invalid relative color path')
    _need(not value.startswith('/') and all(part not in ('', '.', '..') and not part.endswith('.')
          and part.split('.', 1)[0].casefold() not in _RESERVED for part in value.split('/')), 'unsafe color path')
    return value


def _root(root):
    # normalize_root preserves only explicitly registered descriptor roots.
    # Unregistered procfs paths and ordinary symbolic roots remain rejected.
    root = Path(root).absolute()
    try:
        normalized = normalize_root(root)
        _need(normalized == root and root.is_dir(), 'color profile root cannot traverse symlinks')
    except (OSError, RigError) as exc:
        raise ValueError('unsafe/unavailable color root: '+str(exc)) from exc
    return normalized


def _bytes(root, name, limit):
    _path(name)
    root = _root(root)
    try:
        with open_payload(root, name) as (stream, before):
            _need(before.st_size <= limit, 'color payload must be a bounded regular file')
            payload = stream.read(limit+1)
            _need(len(payload) <= limit, 'color payload byte budget exceeded')
            return payload
    except (OSError, RigError) as exc:
        raise ValueError('unsafe/unavailable color payload: '+str(exc)) from exc


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


def _lut(payload, suffix):
    """Bound declared LUT allocation before handing any file to OCIO."""
    lines = [line.split('#', 1)[0].strip() for line in payload.decode('utf-8', 'strict').splitlines()]
    lines = [line for line in lines if line]
    def numbers(tokens, size):
        _need(len(tokens) == size and all(len(token) <= 128 for token in tokens), 'invalid LUT numeric row')
        values = [float(token) for token in tokens]
        _need(all(math.isfinite(value) and abs(value) <= 1e12 for value in values), 'nonfinite/excessive LUT sample')
        return values
    if suffix == '.spimtx':
        _need(len(payload) <= 4096, 'matrix LUT byte budget exceeded')
        numbers(' '.join(lines).split(), 12)
        return
    if suffix == '.spi1d':
        _need('{' in lines and '}' in lines, 'invalid SPI1D braces')
        begin, end = lines.index('{'), lines.index('}')
        _need(begin <= 16 and end == len(lines)-1, 'invalid SPI1D header or trailing data')
        fields = {}
        for line in lines[:begin]:
            key, *tokens = line.split()
            _need(key not in fields, 'duplicate SPI1D field')
            fields[key] = tokens
        _exact(fields, ['Version', 'From', 'Length', 'Components'], 'SPI1D header')
        _need(fields['Version'] == ['1'], 'unsupported SPI1D version')
        domain = numbers(fields['From'], 2); _need(domain[0] < domain[1], 'invalid SPI1D domain')
        _need(len(fields['Length']) == len(fields['Components']) == 1, 'invalid SPI1D size')
        length, channels = int(fields['Length'][0]), int(fields['Components'][0])
        _need(2 <= length <= 65536 and 1 <= channels <= 4 and end-begin-1 == length, 'SPI1D grid budget/count mismatch')
        for line in lines[begin+1:end]:
            numbers(line.split(), channels)
        return
    _need(suffix == '.cube', 'unsupported flat LUT format')
    fields, rows = {}, []
    for line in lines:
        key, *tokens = line.split()
        if key in ('TITLE', 'DOMAIN_MIN', 'DOMAIN_MAX', 'LUT_1D_SIZE', 'LUT_3D_SIZE'):
            _need(not rows and key not in fields and len(fields) < 6, 'duplicate/late CUBE header')
            fields[key] = tokens
        else:
            _need('LUT_1D_SIZE' in fields or 'LUT_3D_SIZE' in fields, 'CUBE grid must precede data')
            rows.append(line)
            _need(len(rows) <= 65**3, 'CUBE data budget exceeded')
    grids = [key for key in ('LUT_1D_SIZE', 'LUT_3D_SIZE') if key in fields]
    _need(len(grids) == 1 and len(fields[grids[0]]) == 1, 'CUBE needs exactly one grid')
    size = int(fields[grids[0]][0])
    dimensions = 1 if grids[0] == 'LUT_1D_SIZE' else 3
    _need(2 <= size <= (65536 if dimensions == 1 else 65) and len(rows) == size**dimensions, 'CUBE grid budget/count mismatch')
    low = numbers(fields.get('DOMAIN_MIN', ['0', '0', '0']), 3)
    high = numbers(fields.get('DOMAIN_MAX', ['1', '1', '1']), 3)
    _need(all(a < b for a, b in zip(low, high)), 'invalid CUBE domain')
    for line in rows:
        numbers(line.split(), 3)


def _yaml(payload):
    """Parse OCIO's tagged YAML without custom constructors, aliases or expansion."""
    try:
        import yaml
    except ImportError as exc:
        raise ValueError('optional PyYAML color dependency unavailable') from exc
    _need(len(payload) <= MAX_CONFIG_BYTES, 'OCIO config exceeds byte budget')
    text = payload.decode('utf-8', 'strict')
    depth = 0
    try:
        for count, token in enumerate(yaml.scan(text), 1):
            _need(count <= 30_000, 'OCIO config token budget exceeded')
            _need(not isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)), 'OCIO YAML aliases are unsupported')
            if isinstance(token, (yaml.tokens.BlockMappingStartToken, yaml.tokens.BlockSequenceStartToken,
                                  yaml.tokens.FlowMappingStartToken, yaml.tokens.FlowSequenceStartToken)):
                depth += 1
                _need(depth <= 64, 'OCIO YAML depth exceeded')
            elif isinstance(token, (yaml.tokens.BlockEndToken, yaml.tokens.FlowMappingEndToken, yaml.tokens.FlowSequenceEndToken)):
                depth -= 1
    except yaml.YAMLError as exc:
        raise ValueError('invalid bounded OCIO YAML: '+str(exc)) from exc
    class Loader(yaml.SafeLoader):
        pass
    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            _need(type(key) is str and key not in result and key != '__ocio_tag__', 'duplicate/non-string OCIO YAML key')
            result[key] = loader.construct_object(value_node, deep=deep)
        return result
    def tagged(loader, suffix, node):
        _need(isinstance(node, yaml.MappingNode), 'unsupported OCIO tagged value')
        result = mapping(loader, node, deep=True)
        result['__ocio_tag__'] = suffix
        return result
    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    Loader.add_multi_constructor('', tagged)
    try:
        result = yaml.load(text, Loader=Loader)
        finite_json(result)
    except Exception as exc:
        raise ValueError('invalid bounded OCIO YAML: '+str(exc)) from exc
    _need(type(result) is dict, 'OCIO config must be a mapping')
    _need(not result.get('environment'), 'OCIO context/environment variables are unsupported')
    refs = []
    def visit(node):
        if isinstance(node, dict):
            if node.get('__ocio_tag__') == 'FileTransform':
                _exact({k: v for k, v in node.items() if k not in ('direction', 'interpolation', 'cccId')},
                       ['__ocio_tag__', 'src'], 'OCIO file transform')
                refs.append(_path(node['src']))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
        elif isinstance(node, str):
            _need('$' not in node and '%' not in node, 'OCIO environment substitutions are unsupported')
    visit(result)
    search = result.get('search_path', '')
    _need(type(search) is str, 'OCIO search path must be a string')
    paths = [] if not search else [_path(x) for x in search.split(':')]
    roles = result.get('roles', {})
    _need(type(roles) is dict and 0 < len(roles) <= 128, 'OCIO roles are missing or excessive')
    for key, value in roles.items():
        _name(key); _name(value)
    _need('scene_linear' in roles and 'cie_xyz_d65_interchange' in roles,
          'explicit scene-linear and CIE XYZ D65 roles are required')
    return paths, roles, sorted(set(refs))


def capture_config(root, config_path='config.ocio'):
    """Inventory a closed, bounded config/LUT profile, independently of OCIO env."""
    root = _root(root)
    _path(config_path)
    names = []
    aliases = set()
    directories = 0
    for directory, subdirs, files in os.walk(root, followlinks=False):
        directories += 1
        _need(directories <= 64, 'color profile directory budget exceeded')
        for child in [*subdirs, *files]:
            path = Path(directory)/child
            _need(not path.is_symlink(), 'color profile symlink is unsupported')
        for child in files:
            name = _path((Path(directory)/child).relative_to(root).as_posix())
            _need(Path(name).suffix.lower() in _ALLOWED_SUFFIXES, 'unsupported color profile file type')
            _need(name.casefold() not in aliases, 'aliased color profile paths')
            aliases.add(name.casefold()); names.append(name)
            _need(len(names) <= MAX_FILES, 'color profile file budget exceeded')
    _need(config_path in names, 'color config is missing')
    rows, total, config = [], 0, None
    for name in sorted(names):
        payload = _bytes(root, name, MAX_CONFIG_BYTES if name == config_path else MAX_FILE_BYTES)
        total += len(payload)
        _need(total <= MAX_TOTAL_BYTES, 'color profile aggregate byte budget exceeded')
        rows.append({'path': name, 'bytes': len(payload), 'sha256': _hash(payload)})
        if name == config_path:
            config = payload
        elif Path(name).suffix.lower() != '.ocio':
            _lut(payload, Path(name).suffix.lower())
    search, roles, references = _yaml(config)
    resolved = []
    for reference in references:
        candidates = [x for x in [reference, *[(PurePosixPath(p)/reference).as_posix() for p in search]] if x in names]
        _need(len(set(candidates)) == 1, 'missing or ambiguous OCIO file transform: '+reference)
        _need(Path(candidates[0]).suffix.lower() in ('.cube', '.spi1d', '.spimtx'), 'unsupported OCIO file-transform resource')
        resolved.append({'source': reference, 'path': candidates[0]})
    value = {'kind': 'cinematics-color-config', 'schema_version': VERSION, 'config_path': config_path,
             'config_sha256': _hash(config), 'files': rows, 'search_paths': search,
             'declared_roles': roles, 'file_transforms': resolved}
    value['closure_sha256'] = object_hash(value)
    return value


def validate_inventory(value):
    _exact(value, ['kind', 'schema_version', 'config_path', 'config_sha256', 'files', 'search_paths',
                   'declared_roles', 'file_transforms', 'closure_sha256'], 'color config inventory')
    _need(value['kind'] == 'cinematics-color-config' and type(value['schema_version']) is int and value['schema_version'] == VERSION, 'unsupported color config version')
    finite_json(value)
    _path(value['config_path']); _sha(value['config_sha256']); _sha(value['closure_sha256'])
    _need(type(value['files']) is list and 0 < len(value['files']) <= MAX_FILES, 'invalid color file inventory')
    seen, total = set(), 0
    for row in value['files']:
        _exact(row, ['path', 'bytes', 'sha256'], 'color file')
        name = _path(row['path']); _sha(row['sha256'])
        _need(name.casefold() not in seen and type(row['bytes']) is int and 0 <= row['bytes'] <= MAX_FILE_BYTES, 'invalid color file row')
        seen.add(name.casefold()); total += row['bytes']
    _need(total <= MAX_TOTAL_BYTES, 'color profile aggregate byte budget exceeded')
    _need(type(value['search_paths']) is list and len(value['search_paths']) <= 64, 'invalid color search paths')
    for path in value['search_paths']:
        _path(path)
    _need(type(value['declared_roles']) is dict and 0 < len(value['declared_roles']) <= 128, 'invalid declared roles')
    for role, space in value['declared_roles'].items():
        _name(role); _name(space)
    _need('scene_linear' in value['declared_roles'] and 'cie_xyz_d65_interchange' in value['declared_roles'], 'missing required color roles')
    _need(type(value['file_transforms']) is list and len(value['file_transforms']) <= MAX_FILES, 'invalid file-transform inventory')
    for row in value['file_transforms']:
        _exact(row, ['source', 'path'], 'file transform reference')
        _path(row['source']); _path(row['path'])
        _need(row['path'].casefold() in seen, 'file-transform target missing from inventory')
    configs = [row for row in value['files'] if row['path'] == value['config_path']]
    _need(len(configs) == 1 and configs[0]['sha256'] == value['config_sha256'] and configs[0]['bytes'] <= MAX_CONFIG_BYTES,
          'config file identity mismatch')
    _need(value['closure_sha256'] == object_hash({k: v for k, v in value.items() if k != 'closure_sha256'}), 'stale color profile closure hash')
    return deepcopy(value)


def verify_config(root, inventory):
    """Verify an already-admitted profile using only standard-library helpers.

    Suitable for a worker consuming a host-admitted inventory. This does not
    independently interpret OCIO/YAML or authenticate who admitted the profile.
    """
    inventory = validate_inventory(inventory)
    root = _root(root)
    names = []
    directories = 0
    for directory, subdirs, files in os.walk(root, followlinks=False):
        directories += 1
        _need(directories <= 64, 'color directory budget exceeded')
        for child in [*subdirs, *files]:
            _need(not (Path(directory)/child).is_symlink(), 'color profile symlink')
        names.extend(_path((Path(directory)/name).relative_to(root).as_posix()) for name in files)
        _need(len(names) <= MAX_FILES, 'color file budget exceeded')
    _need(sorted(names) == [row['path'] for row in inventory['files']], 'color profile inventory changed')
    for row in inventory['files']:
        payload = _bytes(root, row['path'], MAX_FILE_BYTES)
        _need(len(payload) == row['bytes'] and _hash(payload) == row['sha256'], 'color profile bytes changed')
    return inventory['closure_sha256']


verify_config_bytes = verify_config


@contextmanager
def snapshot_config(root, inventory):
    """Yield a private closed profile directory; no OCIO/PyYAML import needed."""
    inventory = validate_inventory(inventory)
    verify_config(root, inventory)
    with tempfile.TemporaryDirectory(prefix='cinematics-color-') as temporary:
        # macOS may spell its system temporary root through /var -> /private.
        # Internal new directories use their canonical physical spelling.
        stage = Path(temporary).resolve()
        for row in inventory['files']:
            payload = _bytes(root, row['path'], MAX_FILE_BYTES)
            _need(len(payload) == row['bytes'] and _hash(payload) == row['sha256'], 'color profile changed during snapshot')
            target = stage/row['path']; target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(payload)
        verify_config(stage, inventory)
        yield stage


def _ocio():
    try:
        import PyOpenColorIO as ocio
    except ImportError as exc:
        raise ValueError('optional PyOpenColorIO dependency unavailable') from exc
    _need(ocio.GetVersion() == OCIO_VERSION, 'unsupported OCIO version; use pinned requirements-color.txt')
    return ocio


@contextmanager
def _config(root, inventory):
    inventory = validate_inventory(inventory)
    _need(capture_config(root, inventory['config_path']) == inventory, 'stale color profile inventory')
    ocio = _ocio()
    with snapshot_config(root, inventory) as stage:
        try:
            config = ocio.Config.CreateFromFile(str(stage/inventory['config_path']))
            config.validate()
        except Exception as exc:
            raise ValueError('invalid/unsupported OCIO config: '+str(exc)) from exc
        yield config, ocio


def inspect_config(root, inventory=None):
    inventory = capture_config(root) if inventory is None else validate_inventory(inventory)
    with _config(root, inventory) as (config, ocio):
        result = {'kind': 'cinematics-color-capabilities', 'schema_version': VERSION,
                'ocio_version': ocio.GetVersion(), 'config': inventory,
                'roles': {role: config.getColorSpace(role).getName() for role in inventory['declared_roles']},
                'color_spaces': [{'name': space.getName(), 'is_data': space.isData(), 'description': space.getDescription()}
                                 for space in config.getColorSpaces()],
                'displays': [{'name': display, 'views': [{'name': view, 'looks': config.getDisplayViewLooks(display, view)}
                              for view in config.getViews(display)]} for display in config.getDisplays()],
                'looks': [look.getName() for look in config.getLooks()]}
        _need(len(result['color_spaces']) <= 1024 and len(result['displays']) <= 64 and len(result['looks']) <= 128,
              'color capability collection budget exceeded')
        for space in result['color_spaces']:
            _name(space['name']); _need(len(space['description']) <= 16384, 'color description budget exceeded')
        for display in result['displays']:
            _name(display['name']); _need(len(display['views']) <= 64, 'view budget exceeded')
            for view in display['views']:
                _name(view['name']); _need(len(view['looks']) <= 1024, 'view look-expression budget exceeded')
        for look in result['looks']:
            _name(look)
        return result


def _primaries(value):
    _exact(value, ['r', 'g', 'b', 'w'], 'chromaticities')
    for pair in value.values():
        _need(type(pair) is list and len(pair) == 2, 'chromaticity must be an xy pair')
        for number in pair:
            _number(number, 0.000001, 1)
        _need(sum(pair) <= 1.000001, 'unsupported chromaticity')


def validate_source(value):
    _exact(value, ['kind', 'schema_version', 'config_closure_sha256', 'working_space', 'primaries', 'transfer',
                   'alpha_mode', 'view_applied', 'exposure_applied', 'source_manifest_sha256', 'source_artifact_sha256'], 'color source context')
    _need(value['kind'] == 'cinematics-color-source' and type(value['schema_version']) is int and value['schema_version'] == VERSION, 'unsupported source color version')
    for key in ('config_closure_sha256', 'source_manifest_sha256', 'source_artifact_sha256'):
        _sha(value[key])
    _name(value['working_space']); _primaries(value['primaries'])
    _need(value['transfer'] == 'linear' and value['view_applied'] is False and value['exposure_applied'] is False,
          'source must be scene-linear with no previously baked exposure/view transform')
    _need(value['alpha_mode'] in ('straight', 'premultiplied'), 'unsupported source alpha mode')
    return deepcopy(value)


def _rgb(cpu, values):
    # OCIO's Python API returns transformed values for list input.
    return list(cpu.applyRGB(values))


def _measure(config, source):
    target = config.getColorSpace('cie_xyz_d65_interchange')
    _need(target is not None, 'CIE XYZ D65 role is unavailable')
    cpu = config.getProcessor(source, target.getName()).getDefaultCPUProcessor()
    basis = [_rgb(cpu, v) for v in ([1., 0., 0.], [0., 1., 0.], [0., 0., 1.])]
    zero, white = _rgb(cpu, [0., 0., 0.]), _rgb(cpu, [1., 1., 1.])
    _need(max(abs(x) for x in zero) < 1e-6, 'source working space is not linear')
    for vector in ([.18, .18, .18], [2., .5, .125]):
        actual = _rgb(cpu, vector)
        expected = [sum(vector[j]*basis[j][i] for j in range(3)) for i in range(3)]
        _need(max(abs(a-b) for a, b in zip(actual, expected)) < 2e-5, 'source working space is not linear')
    result = {}
    for name, xyz in zip(('r', 'g', 'b', 'w'), [*basis, white]):
        _need(all(math.isfinite(x) for x in xyz) and sum(xyz) > 0, 'invalid source XYZ basis')
        result[name] = [xyz[0]/sum(xyz), xyz[1]/sum(xyz)]
    return result


def _calibrate_srgb(config, ocio, source):
    """Verify the declared sRGB display against an independent XYZ/OETF oracle."""
    import numpy as np
    columns = []
    for key in ('r', 'g', 'b'):
        x, y = REC709_D65[key]
        columns.append([x/y, 1., (1-x-y)/y])
    basis = np.array(columns, dtype=np.float64).T
    x, y = REC709_D65['w']
    scale = np.linalg.solve(basis, np.array([x/y, 1., (1-x-y)/y]))
    xyz_to_709 = np.linalg.inv(basis*scale)
    xyz = config.getProcessor(source, config.getColorSpace('cie_xyz_d65_interchange').getName()).getDefaultCPUProcessor()
    standard = _processor(config, ocio, {'source': {'working_space': source}, 'target': 'sdr-srgb',
                                       'display': 'sRGB', 'view': 'Standard', 'look': ''})
    maximum = 0.
    for patch in ([.18, .18, .18], [.5, .1, .03], [1., 1., 1.], [2., .25, .1]):
        expected = [linear_to_srgb_reference(float(value)) for value in xyz_to_709 @ np.array(_rgb(xyz, patch))]
        actual = _rgb(standard, patch)
        maximum = max(maximum, max(abs(a-b) for a, b in zip(actual, expected)))
    _need(maximum < 5e-4, 'declared sRGB display fails independent XYZ/OETF calibration')
    return maximum


def probe_scene_linear(root, inventory=None):
    """Measure the pinned config's source basis; not an image/header inference."""
    inventory = capture_config(root) if inventory is None else validate_inventory(inventory)
    source = inventory['declared_roles']['scene_linear']
    with _config(root, inventory) as (config, ocio):
        primaries = _measure(config, source)
        _primaries(primaries)
        return {'kind': 'cinematics-scene-linear-probe', 'schema_version': VERSION,
                'config_closure_sha256': inventory['closure_sha256'], 'ocio_version': ocio.GetVersion(),
                'working_space': source, 'xyz_role': config.getColorSpace('cie_xyz_d65_interchange').getName(),
                'primaries': primaries, 'method': 'actual-ocio-basis-to-cie-xyz-d65',
                'source_render_verified': False}


def _processor(config, ocio, plan):
    source = plan['source']['working_space']
    if plan['target'] == 'scene-linear':
        return config.getProcessor(source, source).getDefaultCPUProcessor()
    display, view, look = plan['display'], plan['view'], plan['look']
    _need(display == 'sRGB' and display in list(config.getDisplays()), 'only discovered sRGB SDR display is supported')
    _need(view in ('Standard', 'AgX', 'Filmic') and view in list(config.getViews(display)), 'view transform unavailable/unsupported')
    _need(not look or look in [item.getName() for item in config.getLooks()], 'look unavailable')
    transform = ocio.DisplayViewTransform()
    transform.setSrc(source); transform.setDisplay(display); transform.setView(view)
    transform.setLooksBypass(True)
    # Looks are explicit and applied once in the scene-linear reference before
    # the display/view. Implicit configured view looks are bypassed, preventing
    # the same look from executing both implicitly and explicitly.
    if look:
        grade = ocio.LookTransform(); grade.setSrc(source); grade.setDst(source); grade.setLooks(look)
        group = ocio.GroupTransform(); group.appendTransform(grade); group.appendTransform(transform)
        return config.getProcessor(group).getDefaultCPUProcessor()
    return config.getProcessor(transform).getDefaultCPUProcessor()


def make_plan(root, inventory, source, *, target='sdr-srgb', display='sRGB', view='Standard', look='',
              exposure_stops=0., output_alpha='straight'):
    inventory = validate_inventory(inventory); source = validate_source(source)
    _need(source['config_closure_sha256'] == inventory['closure_sha256'], 'source config identity mismatch')
    _need(source['working_space'] == inventory['declared_roles']['scene_linear'], 'source differs from explicit scene-linear role')
    _need(target in ('scene-linear', 'sdr-srgb'), 'HDR video/output transform unsupported')
    _number(exposure_stops, -24, 24)
    _need(output_alpha in ('straight', 'premultiplied'), 'unsupported output alpha mode')
    if target == 'scene-linear':
        _need((display, view, look) == ('', '', ''), 'linear intermediates cannot bake a display/view/look')
    value = {'kind': 'cinematics-color-plan', 'schema_version': VERSION, 'config': inventory, 'source': source,
             'target': target, 'display': display, 'view': view, 'look': look, 'exposure_stops': exposure_stops,
             'output_alpha': output_alpha, 'ocio_version': OCIO_VERSION, 'arithmetic': 'float32',
             'output_primaries': deepcopy(source['primaries'] if target == 'scene-linear' else REC709_D65),
             'output_transfer': 'linear' if target == 'scene-linear' else 'srgb', 'output_matrix': 'rgb', 'output_range': 'full'}
    with _config(root, inventory) as (config, ocio):
        measured = _measure(config, source['working_space'])
        _need(max(abs(a-b) for key in measured for a, b in zip(measured[key], source['primaries'][key])) < 5e-5,
              'declared source primaries disagree with the actual OCIO transform to CIE XYZ D65')
        value['measured_source_primaries'] = measured
        value['output_calibration_max_error'] = None if target == 'scene-linear' else _calibrate_srgb(config, ocio, source['working_space'])
        _processor(config, ocio, value)
    value['plan_sha256'] = object_hash(value)
    return value


def transform_pixels(root, plan, rgba, *, max_work_bytes=MAX_WORK_BYTES):
    """Execute one bounded real OCIO transform; no clipping or file relabeling."""
    import numpy as np
    _exact(plan, ['kind', 'schema_version', 'config', 'source', 'target', 'display', 'view', 'look', 'exposure_stops',
                  'output_alpha', 'ocio_version', 'arithmetic', 'output_primaries', 'output_transfer', 'output_matrix',
                  'output_range', 'measured_source_primaries', 'output_calibration_max_error', 'plan_sha256'], 'color plan')
    _need(isinstance(rgba, np.ndarray) and rgba.dtype.kind == 'f' and rgba.dtype.isnative and rgba.dtype.itemsize in (2, 4, 8)
          and rgba.ndim in (2, 3) and rgba.shape[-1] == 4 and rgba.flags.c_contiguous,
          'expected floating Nx4 or HxWx4 RGBA pixels')
    count = math.prod(rgba.shape[:-1])
    _need(0 < count <= MAX_PIXELS and type(max_work_bytes) is int and 0 < max_work_bytes <= MAX_WORK_BYTES, 'pixel/work budget exceeded')
    inventory = validate_inventory(plan['config'])
    _need(rgba.nbytes + count*24 + min(count, 65536)*3*4 + 2*sum(row['bytes'] for row in inventory['files']) <= max_work_bytes,
          'color transform exceeds working byte budget')
    _need(bool(np.isfinite(rgba).all()) and bool(((rgba[..., 3] >= 0) & (rgba[..., 3] <= 1)).all()), 'invalid pixels or alpha')
    maximum = float(np.finfo(np.float32).max)
    _need(float(rgba.min()) >= -maximum and float(rgba.max()) <= maximum,
          'source values exceed float32 processor precision')
    expected = make_plan(root, inventory, plan['source'], target=plan['target'], display=plan['display'],
                         view=plan['view'], look=plan['look'], exposure_stops=plan['exposure_stops'], output_alpha=plan['output_alpha'])
    _need(plan == expected, 'stale/forged color plan')
    output = np.array(rgba, dtype=np.float32, copy=True, order='C')
    flat = output.reshape(-1, 4)
    before = _hash(memoryview(rgba).cast('B'))
    with _config(root, plan['config']) as (config, ocio):
        cpu = _processor(config, ocio, plan)
        for start in range(0, count, 65536):
            block = flat[start:start+65536]
            rgb = np.ascontiguousarray(block[:, :3])
            alpha = block[:, 3:4]
            if plan['source']['alpha_mode'] == 'premultiplied':
                _need(not bool(((alpha == 0) & (rgb != 0)).any()), 'premultiplied zero-alpha pixels must contain zero RGB')
                with np.errstate(over='ignore', divide='ignore', invalid='ignore'):
                    np.divide(rgb, alpha, out=rgb, where=alpha != 0)
            with np.errstate(over='ignore', invalid='ignore'):
                rgb *= 2.**plan['exposure_stops']
            _need(bool(np.isfinite(rgb).all()), 'exposure/unpremultiplication overflow')
            cpu.applyRGB(rgb)
            if plan['output_alpha'] == 'premultiplied':
                rgb *= alpha
            _need(bool(np.isfinite(rgb).all()), 'OCIO produced nonfinite pixels')
            block[:, :3] = rgb
        processor_id = cpu.getCacheID()
    receipt = {'kind': 'cinematics-color-execution', 'schema_version': VERSION, 'plan_sha256': plan['plan_sha256'],
               'config_closure_sha256': plan['config']['closure_sha256'], 'ocio_version': OCIO_VERSION,
               'numpy_version': np.__version__, 'processor_cache_id': processor_id, 'input_dtype': str(rgba.dtype),
               'output_dtype': 'float32', 'shape': list(rgba.shape), 'input_pixels_sha256': before,
               'output_pixels_sha256': _hash(memoryview(output).cast('B')), 'pixel_count': count,
               'source_context_sha256': object_hash(plan['source']), 'source_render_verified': False,
               'pixel_transform_executed': True, 'view_applied': plan['target'] != 'scene-linear',
               'exposure_applications': 1, 'exposure_stops': plan['exposure_stops'], 'output_transfer': plan['output_transfer'],
               'output_primaries': plan['output_primaries'], 'output_alpha': plan['output_alpha'],
               'rgb_min': float(output[..., :3].min()), 'rgb_max': float(output[..., :3].max()),
               'rgb_below_zero': int((output[..., :3] < 0).sum()), 'rgb_above_one': int((output[..., :3] > 1).sum())}
    return output, receipt


def linear_to_srgb_reference(value):
    """IEC sRGB scalar validation oracle; never an alternative grading engine."""
    _number(value, -65536, 65536)
    return value*12.92 if value <= .0031308 else 1.055*(value**(1/2.4))-.055


def schemas():
    """Structural documents; hashes, config meaning and pixel relations use APIs."""
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    def arr(items, maximum, minimum=0):
        return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}
    sha = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    name = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    path = {'type': 'string', 'minLength': 1, 'maxLength': 512, 'pattern': '^[A-Za-z0-9._/-]+$'}
    version = {'type': 'integer', 'const': VERSION}
    def kind(value):
        return {'type': 'string', 'const': value}
    xy = arr({'type': 'number', 'minimum': .000001, 'maximum': 1}, 2, 2)
    primaries = obj({key: deepcopy(xy) for key in ('r', 'g', 'b', 'w')})
    inventory = obj({'kind': kind('cinematics-color-config'), 'schema_version': version, 'config_path': path,
                     'config_sha256': sha, 'files': arr(obj({'path': path, 'bytes': {'type': 'integer', 'minimum': 0, 'maximum': MAX_FILE_BYTES}, 'sha256': sha}), MAX_FILES, 1),
                     'search_paths': arr(path, 64), 'declared_roles': {'type': 'object', 'minProperties': 1, 'maxProperties': 128, 'required': ['scene_linear', 'cie_xyz_d65_interchange'], 'additionalProperties': name},
                     'file_transforms': arr(obj({'source': path, 'path': path}), MAX_FILES), 'closure_sha256': sha})
    source = obj({'kind': kind('cinematics-color-source'), 'schema_version': version, 'config_closure_sha256': sha,
                  'working_space': name, 'primaries': primaries, 'transfer': {'const': 'linear'},
                  'alpha_mode': {'enum': ['straight', 'premultiplied']}, 'view_applied': {'const': False},
                  'exposure_applied': {'const': False}, 'source_manifest_sha256': sha, 'source_artifact_sha256': sha})
    plan = obj({'kind': kind('cinematics-color-plan'), 'schema_version': version, 'config': inventory, 'source': source,
                'target': {'enum': ['scene-linear', 'sdr-srgb']}, 'display': {'type': 'string', 'maxLength': 256},
                'view': {'type': 'string', 'maxLength': 256}, 'look': {'type': 'string', 'maxLength': 256},
                'exposure_stops': {'type': 'number', 'minimum': -24, 'maximum': 24},
                'output_alpha': {'enum': ['straight', 'premultiplied']}, 'ocio_version': {'const': OCIO_VERSION},
                'arithmetic': {'const': 'float32'}, 'output_primaries': primaries, 'output_transfer': {'enum': ['linear', 'srgb']},
                'output_matrix': {'const': 'rgb'}, 'output_range': {'const': 'full'}, 'measured_source_primaries': primaries,
                'output_calibration_max_error': {'type': ['number', 'null'], 'minimum': 0, 'exclusiveMaximum': .0005}, 'plan_sha256': sha})
    execution = obj({'kind': kind('cinematics-color-execution'), 'schema_version': version, 'plan_sha256': sha,
                     'config_closure_sha256': sha, 'ocio_version': {'const': OCIO_VERSION}, 'numpy_version': name,
                     'processor_cache_id': {'type': 'string', 'maxLength': 16384}, 'input_dtype': {'enum': ['float16', 'float32', 'float64']},
                     'output_dtype': {'const': 'float32'}, 'shape': arr({'type': 'integer', 'minimum': 1, 'maximum': MAX_PIXELS}, 3, 2),
                     'input_pixels_sha256': sha, 'output_pixels_sha256': sha, 'pixel_count': {'type': 'integer', 'minimum': 1, 'maximum': MAX_PIXELS},
                     'source_context_sha256': sha, 'source_render_verified': {'const': False}, 'pixel_transform_executed': {'const': True},
                     'view_applied': {'type': 'boolean'}, 'exposure_applications': {'const': 1}, 'exposure_stops': {'type': 'number', 'minimum': -24, 'maximum': 24},
                     'output_transfer': {'enum': ['linear', 'srgb']}, 'output_primaries': primaries,
                     'output_alpha': {'enum': ['straight', 'premultiplied']}, 'rgb_min': {'type': 'number'}, 'rgb_max': {'type': 'number'},
                     'rgb_below_zero': {'type': 'integer', 'minimum': 0, 'maximum': MAX_PIXELS*3}, 'rgb_above_one': {'type': 'integer', 'minimum': 0, 'maximum': MAX_PIXELS*3}})
    capabilities = obj({'kind': kind('cinematics-color-capabilities'), 'schema_version': version,
                        'ocio_version': {'const': OCIO_VERSION}, 'config': inventory,
                        'roles': {'type': 'object', 'maxProperties': 128, 'additionalProperties': name},
                        'color_spaces': arr(obj({'name': name, 'is_data': {'type': 'boolean'}, 'description': {'type': 'string', 'maxLength': 16384}}), 1024),
                        'displays': arr(obj({'name': name, 'views': arr(obj({'name': name, 'looks': {'type': 'string', 'maxLength': 1024}}), 64)}), 64),
                        'looks': arr(name, 128)})
    probe = obj({'kind': kind('cinematics-scene-linear-probe'), 'schema_version': version,
                 'config_closure_sha256': sha, 'ocio_version': {'const': OCIO_VERSION}, 'working_space': name,
                 'xyz_role': name, 'primaries': primaries, 'method': {'const': 'actual-ocio-basis-to-cie-xyz-d65'},
                 'source_render_verified': {'const': False}})
    result = {'config': inventory, 'source': source, 'plan': plan, 'execution': execution, 'capabilities': capabilities, 'probe': probe}
    return {name+'.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'title': 'Cinematics color '+name+' v1', **schema}
            for name, schema in result.items()}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['capture', 'inspect', 'probe-scene-linear'])
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    operation = {'capture': capture_config, 'inspect': inspect_config, 'probe-scene-linear': probe_scene_linear}[args.command]
    print(json.dumps(operation(args.root), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
