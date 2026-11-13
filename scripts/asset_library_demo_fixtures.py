"""Deterministic real-file engineering fixtures for the library acceptance demo.

This is fixture construction over the existing native rig and library owners.
No generated fixture is production artwork or an independently approved asset.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path

VERSION = '1.0.0'
ROOT = Path(__file__).resolve().parents[1]


def create_fixtures(destination):
    """Create real skinned motion, textured PBR geometry and Scene3D controls."""
    import numpy as np
    from PIL import Image
    from rig_demo import create_demo, ellipsoid
    from rig_gltf import export_geometry, export_gltf
    from rig_models import Geometry, TextureImage
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from rig_errors import atomic_json, require
    from scene3d_example import node
    root = Path(destination).absolute()
    root.mkdir(parents=True, exist_ok=False)
    license = root / 'license.txt'
    license.write_text('CC0-1.0 procedural engineering fixtures; no production art or artistic approval.\n')
    image = Image.new('RGB', (8, 8))
    image.putdata([(240, 150, 35) if (x // 2 + y // 2) % 2 else (35, 100, 190)
                   for y in range(8) for x in range(8)])
    stream = io.BytesIO(); image.save(stream, format='PNG'); texture = stream.getvalue()
    (root / 'checker.png').write_bytes(texture)
    material = dict(name='fixture-checker-pbr', pbrMetallicRoughness=dict(
        baseColorFactor=[1, 1, 1, 1], baseColorTexture=dict(index=0), metallicFactor=.2, roughnessFactor=.55))
    def textured(geometry):
        geometry.materials = [deepcopy(material)]
        geometry.images = [TextureImage('fixture-checker', 'image/png', texture)]
        geometry.textures = [dict(source=0, sampler=0)]
        geometry.samplers = [dict(magFilter=9728, minFilter=9728, wrapS=10497, wrapT=10497)]
        for mesh in geometry.meshes:
            mesh.material = 0
            low = mesh.positions.min(axis=0); span = np.maximum(np.ptp(mesh.positions, axis=0), 1e-6)
            mesh.attributes['TEXCOORD_0'] = ((mesh.positions - low) / span)[:, [0, 1]]
        return geometry
    rig, numeric = create_demo(root / 'native-rig', 'robotic', fps=4)
    textured(rig.geometry)
    character = root / 'character.glb'; export_gltf(rig, character)
    # Separate identities carry actual reusable rig and animation payloads. The
    # complete GLB is intentional: its compatible skin and channels are executable.
    for name in ('rig', 'animation'):
        (root / (name + '.glb')).write_bytes(character.read_bytes())
    material_mesh = ellipsoid('material-sample', [0, .5, 0], [.5, .5, .5], 0, latitudes=6, longitudes=10)
    export_geometry(textured(Geometry([material_mesh])), root / 'material.glb', provenance=dict(fixture=True))
    prop = ellipsoid('reusable-prop', [0, .22, 0], [.22, .22, .22], 0, latitudes=6, longitudes=10)
    export_geometry(textured(Geometry([prop])), root / 'prop.glb', provenance=dict(fixture=True))
    floor = ellipsoid('environment-platform', [0, -.12, 0], [2.2, .12, 1.8], 0, latitudes=4, longitudes=16)
    export_geometry(textured(Geometry([floor])), root / 'environment.glb', provenance=dict(fixture=True))
    camera = node('library-camera', 'camera', 'root')
    camera['camera'] = dict(projection='orthographic', near=.01, far=100,
                            vertical_fov=.8, orthographic_height=2.6)
    light = node('library-key', 'light', 'root')
    light['light'] = dict(type='point', color=[1, .94, .84], intensity=450)
    for name, value in [('camera', camera), ('light', light)]:
        atomic_json(root / (name + '.json'), value)
    samples = []
    for time in (0., .5):
        pose = evaluate_animation(rig, 'hinge-sweep', time)['pose']
        samples.append(evaluate_deformation(rig, pose))
    displacement = max(float(np.max(np.abs(samples[0][key]['positions'] - samples[1][key]['positions'])))
                       for key in samples[0])
    require(displacement > .01, 'LIBRARY_DEMO_MOTION', 'Fixture skeletal motion must deform actual vertices.')
    result = dict(schema_version=1, fixture=True, generator='asset-library-demo-fixtures-' + VERSION,
        clip='hinge-sweep', samples=['0', '1/2'], deformation_displacement_m=displacement,
        rig_numeric=numeric, texture_sha256=hashlib.sha256(texture).hexdigest(),
        files={name: str(root / path) for name, path in dict(character='character.glb', rig='rig.glb',
            animation='animation.glb', material='material.glb', texture='checker.png',
            environment='environment.glb', prop='prop.glb', camera='camera.json', light='light.json').items()},
        license=str(license), limitations=['Procedural engineering fixtures; no artistic approval.'])
    atomic_json(root / 'fixtures.json', result)
    return result


def register_fixtures(library, fixture):
    """Publish independent exact versions through the sole canonical registry."""
    from asset_library import file_record
    from asset_library_contracts import seal
    base = json.loads((ROOT / 'examples/asset-library-foundation/manifest.json').read_text())
    references = {}
    # Sources are ordered so dependency references always point to real versions.
    for kind in ('texture', 'rig', 'animation', 'material', 'environment', 'prop', 'camera', 'light', 'character'):
        source = Path(fixture['files'][kind])
        logical = 'native/' + kind + '.json' if kind in ('camera', 'light') else (
            'runtime/checker.png' if kind == 'texture' else 'runtime/asset.glb')
        files = [file_record(source, logical, 'runtime'),
                 file_record(fixture['license'], 'source/license.txt', 'license')]
        value = deepcopy(base)
        value.update(version=VERSION, files=files)
        value['identity'].update(id='demo-' + kind, slug='demo-' + kind, name='Library demo ' + kind,
            kind=kind, subtype='procedural-engineering-fixture', library='acceptance-library')
        value['metadata'].update(category=kind, description='Actual reusable ' + kind + ' engineering fixture',
            keywords=['library-acceptance', kind], capabilities=[], source_url='local:procedural-library-demo')
        targets = ('texture',) if kind in ('material', 'environment', 'prop') else (
            ('rig', 'animation', 'material') if kind == 'character' else ())
        value['dependencies'] = [dict(target=references[name], purpose=name, required=True,
            origin='authored', relationship='direct', capabilities=[]) for name in targets]
        value['provenance'].update(method='fixture', fixture=True,
            origin='asset_library_demo_fixtures.py', sources=[files[0]], license_evidence=[files[1]],
            uncertainty=['Engineering fixture; technical validation does not establish artistic approval.'])
        value['publication']['inventory'] = [record['path'] for record in files]
        value['compatibility'].update(formats=[source.suffix[1:]],
            material_capabilities=['gltf-pbr'] if kind not in ('camera', 'light', 'texture') else [],
            texture_channels=['baseColor'] if kind in ('material', 'character', 'environment', 'prop') else [])
        references[kind] = library.register(seal(value), {logical: source, 'source/license.txt': fixture['license']})
    return references
