"""Read-only camera comparison derived from the exact native editor state.

This adapter does not author cameras or interpret their animation. Native scalar
edits remain with the inspector transaction owner; authored Camera3D paths and
targets require their separate adapter.
"""
from camera3d_math import projection_matrix, scene_camera_optics
from rig_errors import require


def evaluate_comparison(server, request):
    import numpy as np
    require(isinstance(request, dict) and set(request) == {
        'expected_revision', 'expected_sha256', 'camera_ids', 'width', 'height'},
        'CAMERA3D_EDITOR_FIELDS', 'Use an exact camera comparison request.')
    snapshot = server.snapshot()
    require(type(request['expected_revision']) is int and
        request['expected_revision'] == snapshot['revision'] and
        request['expected_sha256'] == snapshot['sha256'],
        'CAMERA3D_EDITOR_STALE', 'Reload the saved camera state before comparing.')
    ids = request['camera_ids']
    require(isinstance(ids, list) and 1 <= len(ids) <= 2 and
        all(isinstance(identifier, str) for identifier in ids) and len(set(ids)) == len(ids),
        'CAMERA3D_EDITOR_SELECTION', 'Select one or two distinct native cameras.')
    width, height = request['width'], request['height']
    require(type(width) is int and type(height) is int and 16 <= width <= 640 and 16 <= height <= 640,
        'CAMERA3D_EDITOR_SIZE', 'Comparison dimensions must be integers from 16 to 640.')
    state = snapshot['viewport_state']
    require(state.get('available') is True and state['revision'] == snapshot['revision'] and
        state['sha256'] == snapshot['sha256'], 'CAMERA3D_EDITOR_STATE', 'Exact native evaluation is unavailable.')
    nodes = {node['id']: node for node in snapshot['scene']['nodes']}
    render = dict(schema_version=1, kind='camera3d-render', id='editor-comparison-render', version='1.0.0',
        width=width, height=height, pixel_aspect=dict(numerator=1, denominator=1),
        overscan=1, gate_fit='crop', output='png', color_space='srgb', transparent=False,
        max_pixels=640*640)
    cameras = []
    for identifier in ids:
        require(identifier in nodes and nodes[identifier]['type'] == 'camera',
            'CAMERA3D_EDITOR_SELECTION', 'Choose an existing native scene camera.')
        evaluated = state['nodes'][identifier]
        world = np.asarray(evaluated['world_matrix'], dtype=float)
        require(world.shape == (4, 4) and np.isfinite(world).all() and
            np.allclose(world[3], [0, 0, 0, 1]) and
            np.allclose(world[:3, :3].T @ world[:3, :3], np.eye(3), atol=1e-7, rtol=0) and
            abs(float(np.linalg.det(world[:3, :3]))-1) <= 1e-7,
            'CAMERA3D_EDITOR_POSE', 'A comparison camera requires an unscaled rigid native pose.')
        optics = scene_camera_optics(nodes[identifier]['camera'])
        # Camera3D owns optical conversion and projection. SceneGraph owns pose,
        # including inherited pivots, attachments and native constraints.
        projection = projection_matrix(optics, render)
        view = np.linalg.inv(world)
        cameras.append(dict(id=identifier, name=nodes[identifier]['name'],
            world_matrix=world.tolist(), view_matrix=view.tolist(), projection_matrix=projection.tolist(),
            projection=optics['projection'], editor_visible=evaluated['editor_visible'],
            depth_of_field=optics['depth_of_field']))
    return dict(schema_version=1, revision=snapshot['revision'], sha256=snapshot['sha256'],
        width=width, height=height, cameras=cameras, base_pose_only=True,
        animation_evaluated=False, offline_render=False,
        limitations=['Saved native base pose only; animated camera tracks are not evaluated here.',
            'Browser lighting and antialiasing are previews. Scene cameras have no authored DOF intent.'])


def routes():
    return {'camera-workbench/evaluate': evaluate_comparison}
