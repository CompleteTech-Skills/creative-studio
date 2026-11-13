"""Trusted Blender output configuration; all bpy access remains in the worker.

This module owns output encoding and render-pass sinks, not materials, cameras,
lighting, scene evaluation or cinematic compositing effects.
"""
from __future__ import annotations

from pathlib import Path
import math
import tempfile

from atomic_publish import _rename_no_replace

from render3d_contracts import Render3DError, require, validate

VERSION = '1.0.0'
PASS_SOCKETS = {'depth': 'Depth', 'normal': 'Normal', 'albedo': 'DiffCol'}


def _directory(root, *parts):
    current = root
    for part in parts:
        current = current / part
        require(not current.is_symlink() and not getattr(current,'is_junction',lambda:False)(),
                'ownership', 'Output path traverses a symlink or junction.')
        current.mkdir(exist_ok=True)
        require(current.is_dir(), 'ownership', 'Output parent is not a directory.')
    return current


def output_plan(settings):
    """Validate the exact output policy without importing Blender."""
    validate(settings, 'RenderSettings')
    require(settings['engine'] == 'cycles', 'unsupported-feature',
            'The verified quality adapter currently supports Cycles only.')
    output = settings['output']
    require(not (output['format'] == 'exr' and output['bit_depth'] == 16
                 and set(output['passes']) & {'depth', 'normal'}), 'unsupported-feature',
            'Native Blender depth and normal passes require full-float EXR output.')
    return dict(version=VERSION, format=output['format'], bit_depth=output['bit_depth'],
                passes=list(output['passes']), alpha=output['alpha'],
                color_space=output['color_space'], resolution=list(settings['resolution']),
                view_transform=settings['view_transform'], exposure=settings['exposure'],
                samples=settings['samples'], seed=settings['seed'], denoise=settings['denoise'])


def _validated_plan(value):
    """Admit the narrow serialized worker plan without third-party imports."""
    keys = {'version','format','bit_depth','passes','alpha','color_space','resolution',
            'view_transform','exposure','samples','seed','denoise'}
    if not isinstance(value,dict) or set(value) != keys:
        return output_plan(value)
    require(value['version']==VERSION, 'invalid-input', 'Unknown output plan version.')
    require(type(value['bit_depth']) is int and value['format'] in ('png','exr')
            and value['alpha'] in ('opaque','straight'), 'invalid-input', 'Invalid output plan encoding.')
    require(isinstance(value['passes'],list) and 1<=len(value['passes'])<=4
            and all(isinstance(p,str) and p in ('combined','depth','normal','albedo') for p in value['passes'])
            and len(value['passes'])==len(set(value['passes'])) and 'combined' in value['passes'],
            'invalid-input','Invalid output plan pass selection.')
    require(isinstance(value['resolution'],list) and len(value['resolution'])==2
            and all(type(v) is int and 1<=v<=16384 for v in value['resolution']),
            'invalid-input','Invalid output plan resolution.')
    require(type(value['samples']) is int and 1<=value['samples']<=65536
            and type(value['seed']) is int and 0<=value['seed']<=2**31-1
            and type(value['denoise']) is bool, 'invalid-input','Invalid output plan sampling.')
    require(type(value['exposure']) in (int,float) and -32<=value['exposure']<=32
            and math.isfinite(value['exposure']) and value['view_transform'] in ('standard','agx'),
            'invalid-input','Invalid output plan display transform.')
    if value['format']=='png':
        require(value['bit_depth'] in (8,16) and value['color_space']=='srgb'
                and value['passes']==['combined'],'invalid-input','PNG plan must use sRGB combined output.')
    else:
        require(value['bit_depth'] in (16,32) and value['color_space']=='linear'
                and value['view_transform']=='standard','invalid-input','EXR plan must preserve scene-linear output.')
    require(not (value['format'] == 'exr' and value['bit_depth'] == 16
                 and set(value['passes']) & {'depth', 'normal'}), 'unsupported-feature',
            'Native Blender depth and normal passes require full-float EXR output.')
    return value


def configure_output(scene, settings):
    """Apply validated encodings and enable the requested native Cycles passes."""
    plan = _validated_plan(settings)
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = plan['samples']
    scene.cycles.seed = plan['seed']
    scene.cycles.use_denoising = plan['denoise']
    scene.render.resolution_x, scene.render.resolution_y = plan['resolution']
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = plan['alpha'] == 'straight'
    scene.render.image_settings.file_format = 'PNG' if plan['format'] == 'png' else 'OPEN_EXR'
    scene.render.image_settings.color_mode = 'RGBA' if plan['alpha'] == 'straight' else 'RGB'
    scene.render.image_settings.color_depth = str(plan['bit_depth'])
    scene.render.image_settings.exr_codec = 'ZIP'
    scene.view_settings.view_transform = 'Standard' if plan['view_transform'] == 'standard' else 'AgX'
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = plan['exposure']
    scene.view_settings.gamma = 1
    require(scene.cycles.samples == plan['samples'] and scene.cycles.seed == plan['seed']
            and [scene.render.resolution_x,scene.render.resolution_y] == plan['resolution'],
            'unsupported-feature', 'Blender clamped a requested quality setting.')
    for layer in scene.view_layers:
        if 'depth' in plan['passes']: layer.use_pass_z = True
        if 'normal' in plan['passes']: layer.use_pass_normal = True
        if 'albedo' in plan['passes']: layer.use_pass_diffuse_color = True
    return plan


def render_frame(scene, settings, output_root, index, *, pass_sockets=None):
    """Render one actual frame and write beauty plus explicit EXR data passes.

    Called only inside a trusted Blender worker with a privately owned staging
    directory. Optional pass_sockets comes from the compositor owner; otherwise
    render-layer outputs are used. Existing compositor graphs require an
    explicit socket map so their intended effects are never silently bypassed.
    """
    import bpy
    require(type(index) is int and 0 <= index <= 2**31-1, 'invalid-input', 'Frame index lies outside its supported range.')
    root = Path(output_root).absolute()
    require(root.is_dir() and all(not p.is_symlink() and not getattr(p,'is_junction',lambda:False)()
                                for p in (root,*root.parents)),
            'ownership', 'Use an existing owned output directory without symlink traversal.')
    plan = configure_output(scene, settings)
    suffix = plan['format']
    combined = _directory(root,'frames')/f'{index:06d}.{suffix}'
    require(not combined.exists() and not combined.is_symlink(), 'ownership', 'Frame already exists.')
    targets = {'combined': combined}
    auxiliary = [name for name in plan['passes'] if name != 'combined']
    for name in auxiliary:
        target = _directory(root,'passes',name)/f'{index:06d}.exr'
        require(not target.exists() and not target.is_symlink(), 'ownership', 'Data pass already exists.')
        targets[name] = target
    created = []
    previous_use_nodes = scene.use_nodes
    previous_filepath = scene.render.filepath
    try:
        with tempfile.TemporaryDirectory(prefix='.render3d-quality-',dir=root) as temporary:
            stage = Path(temporary)
            sources = pass_sockets
            if auxiliary:
                had_nodes = scene.node_tree is not None and len(scene.node_tree.nodes) > 0
                require(sources is not None or not had_nodes, 'unsupported-feature',
                        'Existing compositor effects require explicit owner-supplied pass sockets.')
                scene.use_nodes = True
                tree = scene.node_tree
                if not had_nodes and sources is not None:
                    created.extend(list(tree.nodes))
                if sources is None:
                    # Nodes initialized by enabling an empty graph belong to
                    # this call. Never clear a pre-existing disabled graph.
                    for node in list(tree.nodes): tree.nodes.remove(node)
                    layers = tree.nodes.new('CompositorNodeRLayers');created.append(layers)
                    layers.scene = scene
                    layers.layer = scene.view_layers[0].name
                    sources = {name: layers.outputs[socket] for name,socket in PASS_SOCKETS.items() if name in auxiliary}
                    composite = tree.nodes.new('CompositorNodeComposite');created.append(composite)
                    tree.links.new(layers.outputs['Image'],composite.inputs['Image'])
                else:
                    require(all(name in sources for name in auxiliary), 'unsupported-feature',
                            'Compositor owner did not supply every requested data pass.')
                for name in auxiliary:
                    sink = tree.nodes.new('CompositorNodeOutputFile');created.append(sink)
                    sink.base_path = str(stage/name)
                    sink.format.file_format = 'OPEN_EXR';sink.format.color_depth = str(plan['bit_depth'])
                    sink.format.color_mode = ('BW' if name == 'depth' else
                                              'RGBA' if name == 'albedo' and plan['alpha']=='straight' else 'RGB')
                    sink.format.exr_codec = 'ZIP'
                    sink.file_slots[0].path = 'pass-'
                    tree.links.new(sources[name],sink.inputs[0])
            scene.render.filepath = str(stage/f'combined.{suffix}')
            bpy.ops.render.render(write_still=True,scene=scene.name)
            generated = {'combined':stage/f'combined.{suffix}'}
            generated.update({name:stage/name/f'pass-{scene.frame_current:04d}.exr' for name in auxiliary})
            require(all(path.is_file() and not path.is_symlink() for path in generated.values()),
                    'incomplete-output','Blender did not write every requested image.')
            records = []
            for name,path in generated.items():
                try:
                    _rename_no_replace(path,targets[name])
                except OSError as exc:
                    raise Render3DError('ownership','Output publication failed: '+str(exc)) from exc
                records.append(dict(pass_name=name,path=str(targets[name]),format=suffix,bit_depth=plan['bit_depth']))
            return records
    finally:
        if scene.use_nodes and scene.node_tree:
            for node in created:
                scene.node_tree.nodes.remove(node)
        scene.use_nodes = previous_use_nodes
        scene.render.filepath = previous_filepath
