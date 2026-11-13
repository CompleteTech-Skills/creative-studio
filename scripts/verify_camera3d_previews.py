"""Render actual camera preset and storyboard-plan previews with Blender.

Run in trusted Blender Python; this is an engineering visibility/projection gate,
not visual approval, motion-preset acceptance, or production sequence evidence.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
if os.environ.get('HATCH_CAMERA_TEST_SITE'):
    sys.path.append(os.environ['HATCH_CAMERA_TEST_SITE'])


def main():
    import bpy
    import numpy as np
    from camera3d_blender import apply_camera
    from camera3d_math import world_transform, projection_matrix, project_bounds
    from camera3d_presets import CAMERA_PRESETS, SHOT_PRESETS, MOVEMENT_PRESETS, camera_preset, plan_storyboard_panel, sample_movement_preset

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    destination = args.destination.absolute()
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('preview evidence requires a new nonsymlink destination')
    destination.mkdir(parents=True)
    bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'; scene.cycles.device = 'CPU'
    scene.cycles.samples = 8; scene.cycles.seed = 42; scene.cycles.use_denoising = False
    scene.render.threads_mode = 'FIXED'; scene.render.threads = 2
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = 'PNG'; scene.render.image_settings.color_mode = 'RGBA'
    bpy.ops.mesh.primitive_cube_add(size=.6)
    cube = bpy.context.object
    cube.name = 'Original procedural unit subject'
    material = bpy.data.materials.new('Visible red subject'); material.use_nodes = True
    nodes = material.node_tree.nodes; nodes.clear()
    output = nodes.new('ShaderNodeOutputMaterial'); emission = nodes.new('ShaderNodeEmission')
    emission.inputs['Color'].default_value = (1, .05, .02, 1)
    material.node_tree.links.new(emission.outputs[0], output.inputs[0])
    cube.data.materials.append(material)
    # Symmetric cube occupies the same bounds in the canonical and Blender bases.
    render = json.loads((ROOT / 'examples/camera3d/render.json').read_text())
    render.update(width=192, height=128)
    bounds = {'subject': dict(minimum=[-.3, -.3, -.3], maximum=[.3, .3, .3])}
    plans = []
    for name in CAMERA_PRESETS:
        plans.append(('camera-' + name, camera_preset(name)))
    for name in SHOT_PRESETS:
        panel = dict(id=name, subject_ids=['subject'], shot_preset=name, duration_frames=24, notes='Engineering preview')
        plans.append(('shot-' + name, plan_storyboard_panel(panel, subject_bounds=bounds, aspect_ratio=1.5)))
    for angle in ('eye-level', 'high', 'low', 'top-down'):
        panel = dict(id=angle, subject_ids=['subject'], shot_preset='medium-shot', camera_angle=angle,
                     composition='third-left', duration_frames=24, notes='Explicit angle and composition preview')
        plans.append(('panel-' + angle, plan_storyboard_panel(panel, subject_bounds=bounds, aspect_ratio=1.5)))
    records = []
    for name, plan in plans:
        world = world_transform(plan['transform'])
        camera = apply_camera(scene, plan['optics'], render, world_matrix=world, name=name)
        scene.camera = camera
        path = destination / (name + '.png'); scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(str(path), check_existing=False)
        pixels = np.array(image.pixels[:], dtype=float).reshape(image.size[1], image.size[0], 4)
        yy, xx = np.where(pixels[..., 3] > .1)
        if len(xx) < 4:
            raise AssertionError(name + ': actual camera did not render the subject')
        actual = [int(xx.min()), int(image.size[1]-1-yy.max()), int(xx.max()+1), int(image.size[1]-yy.min())]
        predicted = project_bounds([-.3]*3, [.3]*3, world, projection_matrix(plan['optics'], render), 192, 128)
        if predicted is None or max(abs(float(a)-float(b)) for a,b in zip(actual,predicted)) > 3:
            raise AssertionError(f'{name}: raster/projection mismatch {actual} != {predicted}')
        record = dict(name=name, path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                      alpha_pixels=int(len(xx)), actual_bounds=actual, predicted_bounds=list(predicted),
                      plan=plan)
        records.append(record)
        bpy.data.images.remove(image)
    # Fixed depth anchors make dolly/follow parallax observable independently
    # of keeping the foreground subject centered or preserving its image size.
    for location, color in [((-1.5, 1, .8), (.02, .2, 1, 1)), ((1.5, 2, -.8), (.02, 1, .2, 1))]:
        bpy.ops.mesh.primitive_cube_add(size=.8, location=location)
        anchor=bpy.context.object
        anchor_material=material.copy()
        anchor_material.node_tree.nodes.get('Emission').inputs['Color'].default_value=color
        anchor.data.materials.clear();anchor.data.materials.append(anchor_material)
    base=camera_preset('cinematic-35mm');base['transform']['translation']=[0,0,8]
    movements=[]
    for name in MOVEMENT_PRESETS:
        frames=[];reference_pixels=None;maximum_difference=0.
        for frame in range(8):
            target=[frame/14,0,0] if name in ('character-follow','over-the-shoulder-tracking') else [0,0,0]
            cube.location=(target[0],-target[2],target[1])
            sample=sample_movement_preset(name,base['transform'],base['optics'],{'frame':frame},8,
                                          target_position=target,fps={'numerator':8,'denominator':1})
            world=world_transform(sample['transform'])
            camera=apply_camera(scene,sample['optics'],render,world_matrix=world,name=name)
            scene.camera=camera
            path=destination/f'movement-{name}-{frame:03d}.png';scene.render.filepath=str(path)
            bpy.ops.render.render(write_still=True)
            image=bpy.data.images.load(str(path),check_existing=False)
            pixels=np.array(image.pixels[:],dtype=float)
            if reference_pixels is None:reference_pixels=pixels.copy()
            maximum_difference=max(maximum_difference,float(np.mean(np.abs(pixels-reference_pixels))))
            projected=project_bounds(np.asarray(target)-.3,np.asarray(target)+.3,world,
                                     projection_matrix(sample['optics'],render),192,128)
            if projected is None:
                raise AssertionError(name+': movement lost its actual foreground subject')
            rgba=pixels.reshape(128,192,4)
            foreground=(rgba[...,0]>1.5*rgba[...,1]) & (rgba[...,0]>1.5*rgba[...,2]) & (rgba[...,3]>.1)
            fy,fx=np.where(foreground)
            if len(fx)<4:raise AssertionError(name+': actual foreground subject is not visible')
            actual_bounds=[int(fx.min()),int(127-fy.max()),int(fx.max()+1),int(128-fy.min())]
            error=max(abs(float(a)-float(b)) for a,b in zip(actual_bounds,projected))
            if error>3:raise AssertionError(f'{name}/{frame}: foreground tracking bounds mismatch {actual_bounds} != {projected}')
            frames.append(dict(frame=frame,path=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                               target_m=target,sample=sample,projected_subject_bounds=list(projected),
                               actual_subject_bounds=actual_bounds,subject_pixel_count=int(len(fx)),projection_error_pixels=error))
            bpy.data.images.remove(image)
        if name=='static-tripod' and maximum_difference>1e-9:
            raise AssertionError('Static movement preset changed its actual raster')
        if name!='static-tripod' and maximum_difference<=1e-5:
            raise AssertionError(name+': no measured actual movement/parallax raster difference')
        movements.append(dict(name=name,fps={'numerator':8,'denominator':1},duration_frames=8,
                              maximum_pixel_difference=maximum_difference,frames=frames))
    bpy.ops.wm.save_as_mainfile(filepath=str(destination / 'previews.blend'))
    report = dict(ok=True, blender_version=bpy.app.version_string, previews=records, movements=movements,
                  geometry='Original procedural cube, canonical bounds [-.3,.3] metres',
                  scope='Actual camera/shot/storyboard previews and twelve sampled movement previews; no production film approval',
                  visual_approval=False)
    (destination / 'acceptance.json').write_text(json.dumps(report, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
