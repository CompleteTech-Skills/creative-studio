"""Export a verified Scene3D frame as static GLB with measured round-trip checks.

The animation/prefab/constraint state is baked at one exact frame. Source projects
are never changed; editable animation, light units and world shading are reported
as format losses rather than claimed to survive a static geometry interchange.
"""
from __future__ import annotations
from copy import deepcopy
from fractions import Fraction
import hashlib
from pathlib import Path
import tempfile
import numpy as np

from atomic_publish import _rename_no_replace
from asset_library import read_source_bytes
from rig_errors import RigError, atomic_json, decode_json, sha256
from rig_gltf import GltfBuilder, _export_geometry
from rig_math import transform_points
from rig_models import Geometry, Mesh, load_rig
from scene3d_contracts import load_project, portable_path
from scene3d_evaluate import evaluate_scene
from scene3d_lifecycle import dependency_receipt
from scene3d_prefabs import PrefabLibrary


def _require(condition, message):
    if not condition:
        raise RigError('SCENE3D_EXPORT', message, stage='scene3d-export')


def _texture_indices(value, offset):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith('Texture') and isinstance(child, dict) and 'index' in child:
                child['index'] += offset
            else:
                _texture_indices(child, offset)
    elif isinstance(value, list):
        for child in value:_texture_indices(child, offset)


def _append_materials(output, geometry):
    material_offset, image_offset = len(output.materials), len(output.images)
    texture_offset, sampler_offset = len(output.textures), len(output.samplers)
    materials = deepcopy(geometry.materials)
    for material in materials:_texture_indices(material, texture_offset)
    output.materials.extend(materials)
    output.images.extend(deepcopy(geometry.images))
    textures = deepcopy(geometry.textures)
    for texture in textures:
        if 'source' in texture:texture['source'] += image_offset
        if 'sampler' in texture:texture['sampler'] += sampler_offset
    output.textures.extend(textures);output.samplers.extend(deepcopy(geometry.samplers))
    output.extensions_used = sorted(set(output.extensions_used)|set(geometry.extensions_used))
    return material_offset


def _native_asset(root, asset):
    """Reuse the native importer/run for actual topology, materials and textures."""
    native_refs = [r for r in asset['dependencies'] if r['path'].endswith('/native/rig.json')]
    _require(len(native_refs) <= 1, 'Asset has ambiguous native rig dependencies.')
    reference = asset['skeleton'] or (native_refs[0] if native_refs else None)
    if reference is not None:
        path = Path(root)/portable_path(reference['path'])
        raw = read_source_bytes(path, 16*1024*1024)
        _require(hashlib.sha256(raw).hexdigest() == reference['sha256'], 'Native rig manifest hash changed.')
        rig = load_rig(path.parent)
        if 'id' in reference:
            _require(rig.manifest['id'] == reference['id'] and rig.manifest['version'] == reference['version'],
                     'Native rig identity/version changed.')
        return rig.geometry, path.parent
    from rig_import import load_character_model
    path = Path(root)/portable_path(asset['content']['path'])
    return load_character_model(path).geometry, None


def _component_positions(root, node, asset, asset_reference, native_root, evaluation):
    from scene3d_import import ImportedScene, evaluate_imported_vertices
    def source(suffix):
        refs = [r for r in asset['dependencies'] if r['path'].endswith(suffix)]
        _require(len(refs) == 1, 'Imported source record is missing: '+suffix)
        raw = read_source_bytes(Path(root)/portable_path(refs[0]['path']), 16*1024*1024)
        _require(hashlib.sha256(raw).hexdigest() == refs[0]['sha256'], 'Imported source record changed.')
        return decode_json(raw)
    report, hierarchy = source('/import-report.json'), source('/hierarchy.json')
    imported = ImportedScene({},asset_reference,asset,[],hierarchy['nodes'],report)
    class View:
        def document(self):return {'nodes':[node]}
        def evaluate_base(self, identifier):return evaluation['nodes'][identifier]
    return evaluate_imported_vertices(View(),imported,native_root)['nodes'][node['id']]


def export_scene_frame(source_root, scene, shot, frame, destination, *, max_vertices=2000000):
    """Publish a new directory containing scene.glb and exact validation receipt.

    Scene and shot selectors are immutable persisted paths or unique IDs; frame
    is int/Fraction. The output includes a Khronos validator result and a measured
    isolated native reimport, clearly labeled as a non-independent decoder check.
    """
    _require(type(max_vertices) is int and 0 < max_vertices <= 2000000, 'Invalid export vertex budget.')
    source_root = Path(source_root).resolve(strict=True)
    destination = Path(destination).absolute()
    _require(not destination.exists() and not destination.is_symlink() and destination.parent.is_dir(),
             'Export requires a new destination below an existing trusted parent.')
    destination = destination.parent.resolve(strict=True)/destination.name
    bundle = load_project(source_root)
    evaluation = evaluate_scene(bundle,scene,shot,frame,source_root=source_root,
                                sample_geometry=True,max_geometry_vertices=max_vertices)
    authored = bundle['documents'][evaluation['scene']['path']]
    expanded = (PrefabLibrary.from_files(source_root,bundle).expand(authored)['scene']
                if any('prefab' in n for n in authored['nodes']) else authored)
    nodes = {n['id']:n for n in expanded['nodes']}
    output = Geometry([]);cache={};expected={};mesh_owner={};vertex_count=0
    losses = ['Editable animation, skinning, procedural recipes, prefab links and constraints are baked to a static frame.',
              'Native world/background/exposure and unitless lights are retained as provenance extras, not portable glTF lighting.',
              'Normals are regenerated for the sampled surface; tangent attributes are omitted.']
    for identifier, state in evaluation['nodes'].items():
        node = nodes[identifier]
        _require(not state.get('material_overrides'), 'Material overrides require the LookDev export adapter.')
        if 'asset' not in state or not state['visible']:continue
        reference = state['asset'];asset = bundle['documents'][reference['path']]
        key = reference['path']
        if key not in cache:
            geometry,native_root=_native_asset(source_root,asset)
            cache[key]=(geometry,native_root,_append_materials(output,geometry))
        geometry,native_root,material_offset=cache[key]
        world=np.asarray(state['world_matrix'],dtype=float)
        _require(np.isfinite(world).all() and np.linalg.cond(world[:3,:3]) <= 1e8,
                 'Export transform is singular or ill-conditioned.')
        sampled=evaluation.get('native_geometry',{}).get(identifier)
        if sampled is None and 'source_component' in node.get('metadata',{}):
            _require(native_root is not None, 'Imported component requires its exact native rig.')
            sampled=_component_positions(source_root,node,asset,reference,native_root,evaluation)
        chosen=geometry.by_id()
        if sampled is not None:
            _require(set(sampled) <= set(chosen), 'Sampled native topology is absent from the exact rig.')
            chosen={name:chosen[name] for name in sampled}
        elif asset['skeleton'] is not None:
            from rig_skinning import evaluate_deformation
            rig=load_rig(native_root)
            sampled={name:transform_points(world,v['positions']).tolist()
                     for name,v in evaluate_deformation(rig).items()}
        for mesh_id, mesh in chosen.items():
            world_positions=(np.asarray(sampled[mesh_id],dtype=float) if sampled is not None else
                             transform_points(world,mesh.positions))
            _require(world_positions.shape == mesh.positions.shape and np.isfinite(world_positions).all(),
                     'Evaluated topology differs from exact native source.')
            vertex_count+=len(world_positions)
            _require(vertex_count<=max_vertices,'Export vertex budget exceeded.')
            local=transform_points(np.linalg.inv(world),world_positions)
            export_id='scene-mesh-'+hashlib.sha256((identifier+'\0'+mesh_id).encode()).hexdigest()[:24]
            attributes={key:deepcopy(value) for key,value in mesh.attributes.items()
                        if key not in ('NORMAL','TANGENT')}
            output.meshes.append(Mesh(export_id,local,mesh.triangles.copy(),mesh.material+material_offset,
                                      attributes=attributes,name=mesh.name,region=mesh.region))
            expected[export_id]=world_positions;mesh_owner[export_id]=identifier
    _require(output.meshes,'A scene export requires visible actual geometry.')
    builder=GltfBuilder();mesh_nodes=_export_geometry(builder,output)
    semantic={identifier:len(builder.json['nodes'])+index for index,identifier in enumerate(nodes)}
    children_by_parent={identifier:[] for identifier in nodes}
    for identifier,node in nodes.items():
        if node['parent'] is not None:children_by_parent[node['parent']].append(semantic[identifier])
    for mesh_id,index in mesh_nodes.items():children_by_parent[mesh_owner[mesh_id]].append(index)
    for identifier,node in nodes.items():
        state=evaluation['nodes'][identifier];world=np.asarray(state['world_matrix'])
        parent=node['parent'];local=(np.linalg.inv(np.asarray(evaluation['nodes'][parent]['world_matrix']))@world
                                     if parent is not None else world)
        value=dict(name=identifier,matrix=local.T.ravel().tolist(),
                   extras=dict(hatchSceneNode=identifier,visible=bool(state['visible'])))
        children=children_by_parent[identifier]
        if children:value['children']=children
        if 'camera' in state:
            camera=state['camera']
            if camera['projection']=='perspective':
                definition=dict(type='perspective',perspective=dict(yfov=camera['vertical_fov'],znear=camera['near'],zfar=camera['far']))
            else:
                shot_doc=bundle['documents'][evaluation['shot']['path']];settings=shot_doc['render']
                half=camera['orthographic_height']/2
                definition=dict(type='orthographic',orthographic=dict(ymag=half,xmag=half*settings['width']/settings['height'],znear=camera['near'],zfar=camera['far']))
            value['camera']=len(builder.json.setdefault('cameras',[]));builder.json['cameras'].append(definition)
        if 'light' in state:value['extras']['nativeLight']=deepcopy(state['light'])
        builder.json['nodes'].append(value)
    builder.json['scenes']=[dict(nodes=[semantic[i] for i,n in nodes.items() if n['parent'] is None])]
    inputs=dependency_receipt(source_root,[evaluation['shot']['path']],purpose='static-scene-export')
    builder.json['asset']['extras']=dict(hatchSceneFrame=dict(scene=evaluation['scene'],shot=evaluation['shot'],
        frame=evaluation['frame'],inputs_sha256=inputs['sha256'],world=evaluation['world'],losses=losses))
    with tempfile.TemporaryDirectory(prefix='.scene-export-',dir=destination.parent) as temporary:
        stage=Path(temporary)/'export';stage.mkdir()
        output_file=stage/'scene.glb';builder.write(output_file)
        from rig_interop import validate_gltf
        validation=validate_gltf(output_file)
        _require(validation['ok'],'Independent Khronos validator rejected the exported scene.')
        from rig_import import load_character_model
        reopened=load_character_model(output_file).geometry
        actual=reopened.by_id()
        _require(set(actual)==set(expected),'Isolated reimport changed exported mesh identities.')
        errors=[]
        for identifier,points in expected.items():
            _require(actual[identifier].positions.shape==points.shape,'Isolated reimport changed topology.')
            errors.append(float(np.max(np.abs(actual[identifier].positions-points))))
        error=max(errors)
        _require(error <= 1e-4,'Isolated reimport changed world-space vertices beyond tolerance.')
        _require(load_project(source_root)['inventory']==bundle['inventory'],'Source project changed during export.')
        report=dict(schema_version=1,kind='scene3d-frame-export',scene=evaluation['scene'],shot=evaluation['shot'],
                    frame=evaluation['frame'],status='static-frame-validated',production_approved=False,
                    output=dict(path='scene.glb',sha256=sha256(output_file),bytes=output_file.stat().st_size),
                    vertices=output.vertex_count,triangles=output.triangle_count,meshes=len(output.meshes),
                    dependency_receipt=inputs,losses=losses,khronos=validation,
                    native_reimport=dict(independent=False,decoder='hatch-native',meshes=len(actual),
                                         max_world_vertex_error_m=error,tolerance_m=1e-4,ok=True))
        atomic_json(stage/'report.json',report)
        _rename_no_replace(stage,destination)
    return report
