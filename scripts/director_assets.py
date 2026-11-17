"""Deterministic procedural robot/laboratory assets using native mesh and rig owners.

These are editable engineering fixtures, not provider output or approved artwork.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

import numpy as np

from asset_library import AssetLibrary, file_record
from asset_library_contracts import seal
from atomic_publish import pinned_directory, private_stage, private_stage_supported, rename_no_replace_at
from model3d_geometry import box
from rig_contracts import COORDINATES, DEFAULT_LIMITS
from rig_demo import make_geometry
from rig_errors import RigError, atomic_json, object_hash, require, sha256, timestamp
from rig_geometry import analyze_geometry
from rig_gltf import GltfBuilder, _export_geometry, export_geometry, export_gltf
from rig_import import load_character_model
from rig_jobs import create_run, save_rig
from rig_models import Geometry
from rig_skinning import generate_skin_weights
from rig_templates import generate_skeleton
from rig_validation import validate_rig

VERSION = '1.0.0'
MAX_VERTICES, MAX_TRIANGLES = 12_000, 24_000
MAX_BUNDLE_BYTES, MAX_BUNDLE_FILES = 64 * 1024 * 1024, 256
MAX_ROUTE_POINTS, MAX_CAMERAS = 128, 32
ROBOTS = {'aster': {'proportions': [1., 1., 1.], 'body': [.08, .35, .52, 1.],
                    'accent': [.95, .48, .08, 1.]},
          'moss': {'proportions': [.88, .87, .95], 'body': [.20, .42, .18, 1.],
                   'accent': [.78, .85, .22, 1.]}}
MATERIALS = [dict(name=name, pbrMetallicRoughness=dict(baseColorFactor=color,
                  metallicFactor=metal, roughnessFactor=rough), doubleSided=False)
             for name, color, metal, rough in [
                 ('concrete', [.19, .22, .24, 1.], 0., .85),
                 ('painted-steel', [.12, .17, .20, 1.], .5, .45),
                 ('console', [.20, .25, .28, 1.], .65, .35),
                 ('key-brass', [.95, .62, .12, 1.], .8, .25)]]
# Centered components preserve explicit pivots in the source recipe. The aggregate
# GLB is baked in canonical world space for native import and later scene binding.
LAB_PARTS = [
    ('floor', [0., -.1, 0.], [8., .2, 6.], 0),
    ('rear-wall', [0., 1.5, -3.], [8., 3., .18], 0),
    ('left-wall', [-4., 1.5, 0.], [.18, 3., 6.], 0),
    ('right-wall-rear', [4., 1.5, -2.], [.18, 3., 2.], 0),
    ('right-wall-front', [4., 1.5, 2.], [.18, 3., 2.], 0),
    ('door-lintel', [4., 2.7, 0.], [.18, .6, 2.], 1),
    ('workbench-top', [0., .95, -2.], [2.6, .15, .8], 1),
    ('workbench-leg-left', [-1.1, .45, -2.], [.15, .9, .65], 1),
    ('workbench-leg-right', [1.1, .45, -2.], [.15, .9, .65], 1),
    ('console-base', [2.7, .55, -1.7], [1., 1.1, .8], 2),
    ('console-screen', [2.7, 1.35, -1.85], [.85, .5, .12], 2),
    ('equipment-cabinet', [-2.9, .95, -2.3], [1., 1.9, 1.], 1),
]
ROUTES = {'aster': [[-1.25, 0., 1.4], [-.7, 0., .4], [-.65, 0., -.4]],
          'moss': [[1.2, 0., 1.5], [1.15, 0., .5], [1.25, 0., -.3]]}
CAMERAS = [[0., 2.3, 2.6], [-2.3, 2.1, 1.7]]


def geometry_digest(geometry):
    """Hash normalized geometry independent of timestamps/native ZIP metadata."""
    value = []
    for mesh in sorted(geometry.meshes, key=lambda m: m.id):
        value.append(dict(id=mesh.id, positions=np.round(mesh.positions, 5).tolist(),
                          triangles=mesh.triangles.tolist(), material=int(mesh.material)))
    return object_hash(dict(meshes=value, materials=geometry.materials))


def lab_geometry():
    meshes = []
    for name, center, size, material in LAB_PARTS:
        mesh = box(*size, name=name)
        mesh.positions += np.array(center)
        mesh.material = material
        meshes.append(mesh)
    return Geometry(meshes, deepcopy(MATERIALS))


def export_laboratory(destination):
    """Use native GLB writer with centered editable components and node pivots."""
    meshes=[]
    for name, center, size, material in LAB_PARTS:
        mesh=box(*size,name=name)
        mesh.material=material
        meshes.append(mesh)
    builder=GltfBuilder()
    nodes=_export_geometry(builder,Geometry(meshes,deepcopy(MATERIALS)))
    for name,center,_,_ in LAB_PARTS:
        builder.json["nodes"][nodes[name]]["translation"]=center
    builder.json["scenes"]=[dict(nodes=list(nodes.values()))]
    builder.json["asset"]["extras"]=dict(hatchSource=dict(fixture=True,origin="procedural-fixture",
        generator="director_assets.py",version=VERSION))
    builder.write(destination)


def _inspect(geometry):
    require(geometry.vertex_count <= MAX_VERTICES and geometry.triangle_count <= MAX_TRIANGLES,
            'DIRECTOR_ASSET_LIMIT', 'Procedural geometry exceeds the vertex or triangle budget.')
    report = analyze_geometry(geometry)
    require(all(item['degenerate_triangles'] == item['nonmanifold_edges'] == item['boundary_edges'] == 0
                and item['signed_volume_m3'] > 0 for item in report['meshes']),
            'DIRECTOR_ASSET_TOPOLOGY', 'Expected closed, nondegenerate outward procedural surfaces.')
    require(all(np.isfinite(m.attributes['NORMAL']).all() and
                np.allclose(np.linalg.norm(m.attributes['NORMAL'], axis=1), 1., atol=1e-5)
                for m in geometry.meshes), 'DIRECTOR_ASSET_NORMALS', 'Invalid procedural normals.')
    return report


def _robot_bounds(geometry):
    return [np.min([m.positions.min(axis=0) for m in geometry.meshes], axis=0),
            np.max([m.positions.max(axis=0) for m in geometry.meshes], axis=0)]


def validate_layout(robot_bounds=None):
    """Check complete static sweeps around measured neutral robot bounds."""
    if robot_bounds is None:
        robot_bounds = {
            name: _robot_bounds(make_geometry('humanoid', proportions=spec['proportions'])[0])
            for name, spec in ROBOTS.items()
        }
    require(set(ROUTES) == set(robot_bounds) and
            sum(map(len, ROUTES.values())) <= MAX_ROUTE_POINTS and len(CAMERAS) <= MAX_CAMERAS,
            'DIRECTOR_ASSET_LIMIT', 'Laboratory layout exceeds the route or camera budget.')
    floor = next((np.array(c), np.array(s)) for name, c, s, _ in LAB_PARTS if name == 'floor')
    floor_lower, floor_upper = floor[0]-floor[1]/2, floor[0]+floor[1]/2
    obstacles = [(np.array(c)-np.array(s)/2, np.array(c)+np.array(s)/2)
                 for name, c, s, _ in LAB_PARTS if name != 'floor']
    ceiling = max(hi[1] for _, hi in obstacles)

    def intersects(lower, upper):
        return any(np.all(upper > lo) and np.all(lower < hi) for lo, hi in obstacles)

    samples = 0
    envelopes = {}
    for name, route in ROUTES.items():
        points = np.asarray(route, dtype=float)
        require(points.ndim == 2 and points.shape[1] == 3 and len(points) >= 2 and
                np.isfinite(points).all() and np.allclose(points[:, 1], floor_upper[1]),
                'DIRECTOR_ASSET_ROUTE', 'Routes must contain finite positions on the laboratory floor.')
        measured = np.asarray(robot_bounds[name], dtype=float)
        require(measured.shape == (2, 3) and np.isfinite(measured).all() and np.all(measured[0] < measured[1]),
                'DIRECTOR_ASSET_ROUTE', 'Robot bounds must be measured finite spatial extents.')
        measured_lower, measured_upper = measured
        # Keep the nominal clearance envelope and expand it for larger decoded geometry.
        lower = np.minimum(measured_lower, [-.85, .001, -.35])
        upper = np.maximum(measured_upper, [.85, 2.1, .35])
        require(lower[1] >= 0, 'DIRECTOR_ASSET_ROUTE', 'Robot bounds extend below the floor.')
        envelopes[name] = dict(measured=[measured_lower.tolist(), measured_upper.tolist()],
                               clearance=[lower.tolist(), upper.tolist()])
        for start, end in zip(points, points[1:]):
            swept_lower = np.minimum(start, end)+lower
            swept_upper = np.maximum(start, end)+upper
            require(np.all(swept_lower[[0, 2]] >= floor_lower[[0, 2]]) and
                    np.all(swept_upper[[0, 2]] <= floor_upper[[0, 2]]) and swept_upper[1] <= ceiling and
                    not intersects(swept_lower, swept_upper),
                    'DIRECTOR_ASSET_ROUTE', 'Complete robot sweep must stay clear inside laboratory geometry.')
            for point in np.linspace(start, end, 51):
                require(not intersects(point+lower, point+upper),
                        'DIRECTOR_ASSET_ROUTE', 'Sampled robot sweep intersects laboratory geometry.')
                samples += 1
    for point in CAMERAS:
        p = np.asarray(point, dtype=float)
        lower, upper = p-.12, p+.12
        require(p.shape == (3,) and np.isfinite(p).all() and
                np.all(lower[[0, 2]] >= floor_lower[[0, 2]]) and
                np.all(upper[[0, 2]] <= floor_upper[[0, 2]]) and
                lower[1] >= floor_upper[1] and upper[1] <= ceiling and
                not intersects(lower, upper),
                'DIRECTOR_ASSET_CAMERA', 'Camera clearance must stay clear inside laboratory geometry.')
    return dict(route_samples=samples,
                route_method='complete straight-segment swept AABB with measured bounds and nominal clearance',
                robot_half_width_m=.85, robot_height_m=2.1, camera_clearance_m=.12,
                robot_envelopes=envelopes, routes=ROUTES, camera_positions=CAMERAS,
                collision_free=True)


def _robot(root, name, spec):
    geometry, template, landmarks, regions = make_geometry('humanoid', proportions=spec['proportions'])
    for index, color in [(0, spec['body']), (1, spec['accent'])]:
        geometry.materials[index]['pbrMetallicRoughness'].update(baseColorFactor=color, metallicFactor=.65, roughnessFactor=.35)
    _inspect(geometry)
    source = root / (name+'.glb')
    export_geometry(geometry, source, provenance=dict(fixture=True, origin='procedural-fixture',
                    generator='director_assets.py', version=VERSION, identity=name))
    rig = create_run(source, root / ('rig-'+name), identifier=name, fixture=True,
                     attribution='Procedural engineering robot; no model/provider or artistic approval.')
    fitted = generate_skeleton(rig.geometry, template, landmarks=landmarks, character_id=name)
    for key in ('skeleton', 'controllers', 'attachments', 'uncertainties'):
        rig.manifest[key] = fitted[key]
    rig.manifest['archetype'] = 'humanoid'
    scale = np.array(spec['proportions'])
    for chain, original in zip(rig.manifest['controllers']['ik'], template['ik']):
        chain['pole'] = (np.array(original['pole'])*scale).tolist()
    for socket, original in zip(rig.manifest['attachments'], template['sockets']):
        socket['transform']['translation'] = (np.array(original['transform']['translation'])*scale).tolist()
    rig.weights = generate_skin_weights(rig.geometry, rig.skeleton, method='diffusion', regions=regions, iterations=8)
    save_rig(rig, operation='director-procedural-robot')
    numeric = validate_rig(rig, require_animation=False)
    require(numeric['ok'], 'DIRECTOR_ASSET_RIG', 'Native robot rig validation failed.')
    atomic_json(rig.root/'source/landmarks.json', dict(source_sha256=sha256(source), landmarks=landmarks))
    atomic_json(rig.root/'source/weight-regions.json', regions)
    export_gltf(rig, root/(name+'-rigged.glb'))
    return source, dict(rig='rig-'+name+'/rig.json', skinned_glb=name+'-rigged.glb',
                               bones=len(rig.skeleton['bones']), controls=list(rig.manifest['controllers']),
                               numeric=numeric)


def _manifest(name, root, files):
    records = [file_record(root/path, path, role) for path, role in files]
    license_record = next(item for item in records if item['role']=='license')
    return seal(dict(schema_version=1, kind='asset-library-version', version=VERSION,
        identity=dict(id=name, namespace='director', library='mvp', slug=name, name=name,
                      kind='character' if name in ROBOTS else 'environment' if name=='laboratory' else 'prop', subtype='procedural-mesh',
                      created_at=timestamp(), attribution='Hatch Pet procedural fixture', owner='Hatch Pet'),
        files=records, dependencies=[], metadata=dict(description='Editable procedural engineering fixture',
             category='geometry', author='Hatch Pet', publisher='Hatch Pet', source_url='local:director-procedural',
             license='CC0-1.0', allowed_uses=['test','render'], keywords=[name], style_refs=[],
             capabilities=['mesh'], review_status='unreviewed'),
        compatibility=dict(coordinate_system='right-handed-y-up-z-forward', unit_scale=1,
             rig_signature=[], skeleton_signature=[], material_capabilities=['metallic-roughness'],
             texture_channels=[], animation_channels=[], renderers=[], formats=['glb'], extensions=[],
             consumer_capabilities=[]), previews=[],
        provenance=dict(method='fixture', origin='Deterministic native procedural geometry', fixture=True,
             sources=[item for item in records if item['role']!='license'], provider=[],
             tools=[dict(name='director_assets', version=VERSION)], parents=[], edits=[],
             license_evidence=[license_record], permissions=[], uncertainty=['Engineering fixture; artistic quality unreviewed']),
        publication=dict(visibility='project-local', distribution='not-evaluated', license_evaluation='evidence-retained',
             inventory=[item['path'] for item in records], verification='payload-verified', integration='native-reference')))


def _bundle_inventory(root):
    inventory = []
    size = 0
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                'DIRECTOR_ASSET_PATH', 'Asset bundles require ordinary private files.')
        size += info.st_size
        require(size <= MAX_BUNDLE_BYTES and len(inventory) < MAX_BUNDLE_FILES,
                'DIRECTOR_ASSET_LIMIT', 'Asset bundle exceeds the byte or file budget.')
        inventory.append(dict(path=path.relative_to(root).as_posix(), bytes=info.st_size, sha256=sha256(path)))
    return inventory


def _generate_assets(stage, seed):
    (stage/'license.txt').write_text('CC0-1.0: These procedural engineering fixtures are dedicated to the public domain.\n')
    library = AssetLibrary(stage/'library', create=True)
    assets = {}
    vertices = triangles = 0
    import_limits = {**DEFAULT_LIMITS, 'source_bytes': MAX_BUNDLE_BYTES,
                     'vertices': MAX_VERTICES, 'triangles': MAX_TRIANGLES}
    for name in [*ROBOTS, 'laboratory', 'access-key']:
        native = None
        if name in ROBOTS:
            source, native = _robot(stage, name, ROBOTS[name])
        else:
            geometry = lab_geometry() if name=='laboratory' else Geometry([box(.22,.06,.09,name='access-key')], deepcopy(MATERIALS))
            if name=='access-key': geometry.meshes[0].material=3
            _inspect(geometry)
            source = stage/(name+'.glb')
            if name=='laboratory': export_laboratory(source)
            else: export_geometry(geometry, source, provenance=dict(fixture=True, origin='procedural-fixture', generator='director_assets.py', version=VERSION))
        loaded = load_character_model(source, limits=import_limits)
        decoded_report = _inspect(loaded.geometry)
        vertices += decoded_report['vertices']
        triangles += decoded_report['triangles']
        require(vertices <= MAX_VERTICES and triangles <= MAX_TRIANGLES,
                'DIRECTOR_ASSET_LIMIT', 'Asset set exceeds the aggregate geometry budget.')
        identity = dict(schema_version=1, id=name, seed=seed, version=VERSION,
            source_sha256=sha256(source), geometry_sha256=geometry_digest(loaded.geometry),
            coordinate_system=COORDINATES, fixture=True, origin='procedural-fixture', artistic_approval=False)
        atomic_json(stage/(name+'-identity.json'), identity)
        recipe = dict(generator='director_assets.py', version=VERSION, seed=seed, identity=name,
                      parameters=ROBOTS.get(name, LAB_PARTS if name=='laboratory' else {'size_m':[.22,.06,.09]}),
                      generator_dependencies={filename:sha256(Path(__file__).with_name(filename))
                          for filename in ('director_assets.py','rig_demo.py','model3d_geometry.py')})
        atomic_json(stage/(name+'-recipe.json'), recipe)
        files=[(source.name,'geometry'), (name+'-identity.json','identity'), (name+'-recipe.json','source'), ('license.txt','license')]
        if native: files.append((name+'-rigged.glb','rig'))
        manifest = _manifest(name, stage, files)
        atomic_json(stage/(name+'-asset.json'), manifest)
        reference = library.register(manifest, {f['path']:stage/f['path'] for f in manifest['files']})
        require(library.resolve(reference)==manifest and
                all(library.read_file(reference, path)==(stage/path).read_bytes() for path, _ in files),
                'DIRECTOR_ASSET_LIBRARY', 'Registered manifest or payload differs.')
        assets[name] = dict(identity=identity, identity_sha256=object_hash(identity), source=source.name,
            reference=reference, geometry=decoded_report, native=native)
    layout = validate_layout({name: assets[name]['geometry']['bounds'] for name in ROBOTS})
    report=dict(schema_version=1, kind='director-procedural-assets', version=VERSION, seed=seed,
                fixture=True, coordinate_system=COORDINATES, assets=assets, layout=layout,
                production_approved=False, provider_executed=False,
                inventory=[item for item in _bundle_inventory(stage) if not item['path'].startswith('library/')],
                decoded_vertices=vertices, decoded_triangles=triangles)
    atomic_json(stage/'assets.json', report)
    return report


def _directory_identity(info):
    return info.st_dev, info.st_ino


def build_assets(destination, *, seed=0):
    """Verify and exclusively publish one complete owned asset set."""
    require(type(seed) is int and 0 <= seed <= 2**32-1, 'DIRECTOR_ASSET_SEED', 'Seed must be a uint32.')
    destination = Path(destination).absolute()
    require(destination.name not in ('', '.', '..') and '..' not in destination.parts,
            'DIRECTOR_ASSET_PATH', 'Use a named new destination without parent traversal.')
    require(private_stage_supported(), 'DIRECTOR_ASSET_PLATFORM',
            'Procedural asset publication requires the Linux descriptor-pinned staging adapter.')
    require(not destination.exists() and not destination.is_symlink(), 'DIRECTOR_ASSET_EXISTS', 'Use a new destination.')
    require(destination.parent.is_dir() and all(not p.is_symlink() for p in destination.parents),
            'DIRECTOR_ASSET_PATH', 'Destination parent must be an existing nonsymlink directory.')
    admitted = _directory_identity(destination.parent.stat(follow_symlinks=False))
    published = False
    try:
        with ExitStack() as publication:
            parent_fd = publication.enter_context(pinned_directory(destination.parent))
            require(_directory_identity(os.fstat(parent_fd)) == admitted, 'DIRECTOR_ASSET_PATH',
                    'Destination parent changed before pinning.')
            # Native rig writers and the registry require ordinary nonsymlink paths.
            with tempfile.TemporaryDirectory(prefix='director-assets-', dir=Path(tempfile.gettempdir()).resolve()) as tmp:
                root = Path(tmp)
                report = _generate_assets(root, seed)
                expected = _bundle_inventory(root)
                stage, stage_fd = publication.enter_context(private_stage(parent_fd, prefix='.director-assets-'))
                payload = stage/'payload'
                shutil.copytree(root, payload)
            # Scratch teardown has completed. Only canonical publication resources remain.
            require(_bundle_inventory(payload) == expected, 'DIRECTOR_ASSET_LIBRARY',
                    'Staged bundle differs from verified generated bytes.')
            require(_directory_identity(destination.parent.stat(follow_symlinks=False)) == admitted,
                    'DIRECTOR_ASSET_PATH', 'Destination parent changed before publication.')
            rename_no_replace_at(stage_fd, payload.name, parent_fd, destination.name)
            published = True
    except OSError:
        if not published:
            raise
        report['publication_warning'] = {
            'code': 'DIRECTOR_ASSET_CLEANUP',
            'message': 'The bundle was published. Temporary publication resources may need cleanup.',
        }
    return report


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--seed', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_assets(args.destination, seed=args.seed), indent=2))
    except RigError as exc:
        print(json.dumps({'status': 'failed', 'diagnostic': exc.report}))
        return 1
    except OSError:
        diagnostic = {'code': 'DIRECTOR_ASSET_IO',
                      'message': 'Asset generation or publication could not complete. Check directory access and space.'}
        print(json.dumps({'status': 'failed', 'diagnostic': diagnostic}))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(_main())
