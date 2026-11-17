"""Real GLB animation inspection, including the native morph policy."""
from copy import deepcopy

import pytest

from asset_library import file_record
from asset_library_contracts import seal
from asset_library_validation import inspect_geometry, validate_asset
from rig_animation import validate_animation
from rig_errors import RigError
from rig_gltf import GltfBuilder
from rig_import import load_character_model
from rig_interop import validate_gltf
from rig_models import RigAsset
import test_asset_library


def make_glb(path, weight=1, skeletal=False):
    b = GltfBuilder()
    pos = b.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3')
    delta = b.accessor([[0, 0, .1]] * 3, 'VEC3')
    times = b.accessor([[0], [1]], 'SCALAR')
    values = b.accessor([[0], [weight]], 'SCALAR')
    samplers = [{'input': times, 'output': values}]
    channels = [{'sampler': 0, 'target': {'node': 0, 'path': 'weights'}}]
    if skeletal:
        translation = b.accessor([[0, 0, 0], [1, 0, 0]], 'VEC3')
        samplers.append({'input': times, 'output': translation})
        channels.append({'sampler': 1, 'target': {'node': 0, 'path': 'translation'}})
    b.json.update(nodes=[{'mesh': 0}], meshes=[{'primitives': [{'attributes': {'POSITION': pos},
        'targets': [{'POSITION': delta}]}]}], scenes=[{'nodes': [0]}],
        animations=[{'samplers': samplers, 'channels': channels}])
    for index, low, high in [(pos, [0, 0, 0], [1, 1, 0]),
                              (delta, [0, 0, .1], [0, 0, .1]), (times, [0], [1])]:
        b.json['accessors'][index].update(min=low, max=high)
    for index in (pos, delta):
        b.json['bufferViews'][b.json['accessors'][index]['bufferView']]['target'] = 34962
    b.write(path)


def test_morph_only_real_glb_and_registered_validation(tmp_path):
    path = tmp_path / 'morph.glb'
    make_glb(path)
    report = inspect_geometry(path)
    assert report['independent_validation']['ok']
    assert report['independent_validation']['issues']['numWarnings'] == 0
    assert report['skeleton'] is None
    assert report['clips'][0]['tracks'][0]['path'] == 'weights'
    owner = test_asset_library.AssetLibraryTests('test_actual_geometry_registration_restart_relocation_and_exact_bytes')
    owner.setUp()
    try:
        record = file_record(path, 'mesh/morph.glb', 'geometry')
        manifest = deepcopy(owner.manifest)
        manifest['files'][0] = record
        manifest['provenance']['sources'] = [record]
        manifest['publication']['inventory'][0] = record['path']
        manifest['compatibility']['formats'] = ['glb']
        reference = owner.library.register(seal(manifest), {record['path']: path, owner.files[1]['path']: owner.license})
        result = validate_asset(owner.library, reference, requirements={'capabilities': ['morph', 'animation']})
        assert result['status'] == 'technically-verified'
    finally:
        owner.doCleanups()


def test_invalid_native_morph_range_real_glb(tmp_path):
    path = tmp_path / 'bad.glb'
    make_glb(path, weight=3)
    independent = validate_gltf(path)
    assert independent['ok'] and independent['issues']['numWarnings'] == 0
    with pytest.raises(RigError) as error:
        inspect_geometry(path)
    assert error.value.report['code'] == 'RIG_MORPH_RANGE'


def test_skeletal_tracks_still_require_skeleton_and_matching_version(tmp_path):
    path = tmp_path / 'mixed.glb'
    make_glb(path, skeletal=True)
    assert inspect_geometry(path)['skeleton'] is not None
    model = load_character_model(path)
    clip = next(iter(model.clips.values()))
    rig = RigAsset({'id': 'imported', 'skeleton': None}, model.geometry)
    with pytest.raises(RigError, match='skeleton'):
        validate_animation(rig, clip)
    rig.manifest['skeleton'] = model.skeleton
    stale = deepcopy(clip)
    stale['skeleton_version'] = '2.0.0'
    with pytest.raises(RigError) as error:
        validate_animation(rig, stale)
    assert error.value.report['code'] == 'RIG_CLIP_VERSION'
