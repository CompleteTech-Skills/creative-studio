"""Actual Blender projection and random-seek check for Camera3D motion.

Run with Blender --background --python this_file -- --output DIRECTORY.
This fixture uses native camera matrices and renders, with no mocked renderer.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
if os.environ.get('HATCH_CAMERA_TEST_SITE'):
    sys.path.append(os.environ['HATCH_CAMERA_TEST_SITE'])


def main():
    import bpy
    import numpy as np
    from mathutils import Matrix, Vector
    from bpy_extras.object_utils import world_to_camera_view
    from camera3d_math import blender_camera_transform, world_transform, project, projection_matrix
    from camera3d_motion import sample_motion
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    args.output.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = 8
    scene.cycles.seed = 0
    scene.cycles.use_animated_seed = False
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 1
    scene.render.resolution_x = 160
    scene.render.resolution_y = 120
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.world = bpy.data.worlds.new('World')
    scene.world.color = (.15, .15, .15)
    data = bpy.data.cameras.new('MotionCamera')
    camera = bpy.data.objects.new('MotionCamera', data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    data.sensor_fit = 'HORIZONTAL'
    data.sensor_width = 36
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0))
    cube = bpy.context.object
    material = bpy.data.materials.new('Marker')
    material.use_nodes = True
    nodes = material.node_tree.nodes
    shader = nodes.get('Principled BSDF')
    shader.inputs['Base Color'].default_value = (.8, .1, .05, 1)
    shader.inputs['Emission Color'].default_value = (.8, .1, .05, 1)
    shader.inputs['Emission Strength'].default_value = .5
    cube.data.materials.append(material)
    optics = json.loads((ROOT/'examples/camera3d/optics.json').read_text())
    optics['projection'].update(sensor_width_mm=36, sensor_height_mm=27,
                                sensor_fit='horizontal', shift_x=0, shift_y=0)
    render = json.loads((ROOT/'examples/camera3d/render.json').read_text())
    render.update(width=160, height=120, overscan=1)
    base = dict(translation=[0,0,10], rotation=[0,0,0,1], scale=[1,1,1])
    observed = {}
    hashes = {}
    pixels = {}
    attempts = []
    for frame in (24, 0, 12, 24):
        state = sample_motion(base, optics, {'frame': frame}, 24, 'dolly', {'distance_m':4})
        camera.matrix_world = Matrix(blender_camera_transform(world_transform(state['transform'])).tolist())
        data.lens = state['optics']['projection']['focal_length_mm']
        scene.frame_set(frame+1)
        bpy.context.view_layer.update()
        point = np.array([.5,.5,0.])
        native_point = Vector((point[0], -point[2], point[1]))
        native = world_to_camera_view(scene, camera, native_point)
        actual = [native.x*160, (1-native.y)*120]
        expected = project(point, world_transform(state['transform']),
                           projection_matrix(state['optics'],render),160,120)['pixel']
        np.testing.assert_allclose(actual,expected,atol=2e-4)
        if frame in observed:
            np.testing.assert_array_equal(actual,observed[frame])
        observed[frame] = actual
        scene.render.filepath = str(args.output/f'motion-{frame}.png')
        bpy.ops.render.render(write_still=True)
        digest = hashlib.sha256(Path(scene.render.filepath).read_bytes()).hexdigest()
        # PNG metadata includes wall-clock render duration; compare decoded pixels.
        image = bpy.data.images.load(scene.render.filepath, check_existing=False)
        rgba = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(rgba)
        pixel_digest = hashlib.sha256(rgba.tobytes()).hexdigest()
        bpy.data.images.remove(image)
        if frame in pixels and pixels[frame] != pixel_digest:
            raise AssertionError('Repeated random seek changed decoded native pixels')
        pixels[frame] = pixel_digest
        hashes[frame] = digest
        attempts.append(dict(frame=frame, png_sha256=digest, decoded_rgba_sha256=pixel_digest))
    if np.linalg.norm(np.array(observed[0])-observed[24]) < 3:
        raise AssertionError('Native camera movement did not change the projected marker')
    source_names = ['verify_camera3d_motion.py','camera3d_motion.py','camera3d_paths.py',
                    'camera3d_math.py','camera3d_contracts.py','rig_math.py','rig_errors.py']
    sources = {name: hashlib.sha256((ROOT/'scripts'/name).read_bytes()).hexdigest()
               for name in source_names}
    try:
        execution_head = subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],
                                                 text=True,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.CalledProcessError):
        execution_head = None
    report = dict(blender=bpy.app.version_string, source_sha256=sources,
                  execution_git_head=execution_head, frames=observed, png_sha256=hashes,
                  decoded_rgba_sha256=pixels, attempts=attempts,
                  check='native projections agree; repeated frame 24 decoded pixels match')
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,sort_keys=True))


if __name__ == '__main__':
    main()
