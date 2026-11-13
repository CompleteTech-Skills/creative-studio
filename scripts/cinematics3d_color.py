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
import stat
import tempfile

from rig_errors import finite_json, object_hash

VERSION = 1
OCIO_VERSION = '2.5.2'
MAX_FILES = 256
MAX_FILE_BYTES = 32 * 1024**2
MAX_TOTAL_BYTES = 64 * 1024**2
MAX_CONFIG_BYTES = 1024**2
MAX_PIXELS = 16_777_216
MAX_WORK_BYTES = 512 * 1024**2
REC709_D65 = {'r': [0.64, 0.33], 'g': [0.30, 0.60], 'b': [0.15, 0.06], 'w': [0.3127, 0.3290]}
_ALLOWED_SUFFIXES = {'.ocio', '.cube', '.spi1d', '.spi3d', '.spimtx'}
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
    _need(type(value) in (int, float) and math.isfinite(value) and low <= value <= high, 'invalid bounded number')


def _path(value):
    # A deliberately restricted profile subset keeps the worker independent
    # of optional artwork libraries and excludes device names and aliases.
    _need(type(value) is str and 0 < len(value) <= 512 and bool(_PORTABLE.fullmatch(value)), 'invalid relative color path')
    _need(not value.startswith('/') and all(part not in ('', '.', '..') and not part.endswith('.')
          and part.split('.', 1)[0].casefold() not in _RESERVED for part in value.split('/')), 'unsafe color path')
    return value


def _bytes(root, name, limit):
    _path(name)
    root = Path(root).absolute()
    target = root/name
    _need(root.resolve() == root and target.resolve().is_relative_to(root), 'redirected color path')
    for ancestor in [target, *target.parents]:
        _need(not ancestor.is_symlink(), 'color symlink is unsupported')
    before = target.lstat()
    _need(stat.S_ISREG(before.st_mode) and before.st_size <= limit, 'color payload must be a bounded regular file')
    descriptors = []
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    def identity(info):
        value = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        return value if os.name == 'nt' else value + (info.st_ctime_ns,)
    try:
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptors.append(os.open(root.anchor, directory_flags))
            for part in [*root.parts[1:], *PurePosixPath(name).parts[:-1]]:
                descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
            descriptor = os.open(PurePosixPath(name).name, flags, dir_fd=descriptors[-1])
        else:
            descriptor = os.open(target, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            _need(stat.S_ISREG(opened.st_mode) and identity(opened) == identity(before), 'color file changed while opening')
            payload = stream.read(limit+1)
            _need(len(payload) <= limit and identity(os.fstat(stream.fileno())) == identity(before)
                  and identity(target.lstat()) == identity(before), 'color file changed while reading')
            return payload
    except OSError as exc:
        raise ValueError('unsafe/unavailable color payload: '+str(exc)) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


def _yaml(payload):
    """Parse OCIO's tagged YAML without custom constructors, aliases or expansion."""
    try:
        import yaml
    except ImportError as exc:
        raise ValueError('optional PyYAML color dependency unavailable') from exc
    _need(len(payload) <= MAX_CONFIG_BYTES, 'OCIO config exceeds byte budget')
    text = payload.decode('utf-8', 'strict')
    depth = 0
    for count, token in enumerate(yaml.scan(text), 1):
        _need(count <= 30_000, 'OCIO config token budget exceeded')
        _need(not isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)), 'OCIO YAML aliases are unsupported')
        if isinstance(token, (yaml.tokens.BlockMappingStartToken, yaml.tokens.BlockSequenceStartToken,
                              yaml.tokens.FlowMappingStartToken, yaml.tokens.FlowSequenceStartToken)):
            depth += 1
            _need(depth <= 64, 'OCIO YAML depth exceeded')
        elif isinstance(token, (yaml.tokens.BlockEndToken, yaml.tokens.FlowMappingEndToken, yaml.tokens.FlowSequenceEndToken)):
            depth -= 1
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
    root = Path(root).absolute()
    _path(config_path)
    _need(root.is_dir() and root.resolve() == root, 'color profile root cannot traverse symlinks')
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
    search, roles, references = _yaml(config)
    resolved = []
    for reference in references:
        candidates = [x for x in [reference, *[(PurePosixPath(p)/reference).as_posix() for p in search]] if x in names]
        _need(len(set(candidates)) == 1, 'missing or ambiguous OCIO file transform: '+reference)
        resolved.append({'source': reference, 'path': candidates[0]})
    value = {'kind': 'cinematics-color-config', 'schema_version': VERSION, 'config_path': config_path,
             'config_sha256': _hash(config), 'files': rows, 'search_paths': search,
             'declared_roles': roles, 'file_transforms': resolved}
    value['closure_sha256'] = object_hash(value)
    return value


def validate_inventory(value):
    _exact(value, ['kind', 'schema_version', 'config_path', 'config_sha256', 'files', 'search_paths',
                   'declared_roles', 'file_transforms', 'closure_sha256'], 'color config inventory')
    _need(value['kind'] == 'cinematics-color-config' and value['schema_version'] == VERSION, 'unsupported color config version')
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
    root = Path(root).absolute()
    _need(root.is_dir() and root.resolve() == root, 'color root cannot traverse symlinks')
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
        stage = Path(temporary)
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
        config = ocio.Config.CreateFromFile(str(stage/inventory['config_path']))
        config.validate()
        yield config, ocio


def inspect_config(root, inventory=None):
    inventory = capture_config(root) if inventory is None else validate_inventory(inventory)
    with _config(root, inventory) as (config, ocio):
        return {'kind': 'cinematics-color-capabilities', 'schema_version': VERSION,
                'ocio_version': ocio.GetVersion(), 'config': inventory,
                'roles': {role: config.getColorSpace(role).getName() for role in inventory['declared_roles']},
                'color_spaces': [{'name': space.getName(), 'is_data': space.isData(), 'description': space.getDescription()}
                                 for space in config.getColorSpaces()],
                'displays': [{'name': display, 'views': [{'name': view, 'looks': config.getDisplayViewLooks(display, view)}
                              for view in config.getViews(display)]} for display in config.getDisplays()],
                'looks': [look.getName() for look in config.getLooks()]}


def _primaries(value):
    _exact(value, ['r', 'g', 'b', 'w'], 'chromaticities')
    for pair in value.values():
        _need(type(pair) is list and len(pair) == 2, 'chromaticity must be an xy pair')
        for number in pair:
            _number(number, 0.000001, 1)
        _need(sum(pair) <= 1.000001, 'impossible chromaticity')


def validate_source(value):
    _exact(value, ['kind', 'schema_version', 'config_closure_sha256', 'working_space', 'primaries', 'transfer',
                   'alpha_mode', 'view_applied', 'source_manifest_sha256', 'source_artifact_sha256'], 'color source context')
    _need(value['kind'] == 'cinematics-color-source' and value['schema_version'] == VERSION, 'unsupported source color version')
    for key in ('config_closure_sha256', 'source_manifest_sha256', 'source_artifact_sha256'):
        _sha(value[key])
    _name(value['working_space']); _primaries(value['primaries'])
    _need(value['transfer'] == 'linear' and value['view_applied'] is False,
          'source must be scene-linear with no previously applied view transform')
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
    _need(isinstance(rgba, np.ndarray) and rgba.dtype.kind == 'f' and rgba.ndim in (2, 3) and rgba.shape[-1] == 4 and rgba.flags.c_contiguous,
          'expected floating Nx4 or HxWx4 RGBA pixels')
    count = math.prod(rgba.shape[:-1])
    _need(0 < count <= MAX_PIXELS and type(max_work_bytes) is int and 0 < max_work_bytes <= MAX_WORK_BYTES, 'pixel/work budget exceeded')
    inventory = validate_inventory(plan['config'])
    _need(rgba.nbytes + count*24 + min(count, 65536)*3*4 + 2*sum(row['bytes'] for row in inventory['files']) <= max_work_bytes,
          'color transform exceeds working byte budget')
    _need(bool(np.isfinite(rgba).all()) and bool(((rgba[..., 3] >= 0) & (rgba[..., 3] <= 1)).all()), 'invalid pixels or alpha')
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
                np.divide(rgb, alpha, out=rgb, where=alpha != 0)
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
               'exposure_applications': 1, 'output_transfer': plan['output_transfer'],
               'output_primaries': plan['output_primaries'], 'output_alpha': plan['output_alpha'],
               'rgb_min': float(output[..., :3].min()), 'rgb_max': float(output[..., :3].max()),
               'rgb_below_zero': int((output[..., :3] < 0).sum()), 'rgb_above_one': int((output[..., :3] > 1).sum())}
    return output, receipt


def linear_to_srgb_reference(value):
    """IEC sRGB scalar validation oracle; never an alternative grading engine."""
    _number(value, -65536, 65536)
    return value*12.92 if value <= .0031308 else 1.055*(value**(1/2.4))-.055
