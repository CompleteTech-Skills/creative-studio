"""Advisory continuity measurements from evaluated Camera3D boundary states."""
from __future__ import annotations

import math

from camera3d_math import _matrix, projection_matrix, project, project_bounds


def boundary_continuity(outgoing, incoming, subjects_before, subjects_after, *,
                        axis=None, props_before=None, props_after=None,
                        gaze_before=None, gaze_after=None,
                        position_tolerance_m=.05, height_tolerance_m=.5,
                        lens_tolerance_mm=30, scale_ratio_limit=2):
    """Compare actual evaluated boundary data, with warnings rather than edits.

    Each subject value has world-space minimum/maximum bounds; movement and
    apparent scale come from these bounds, not user-supplied pixel metadata.
    Optional 180-degree rules use an explicit world-space line of action.
    A warning can be intentional filmmaking; this function grants no approval.
    """
    import numpy as np
    from rig_math import vector
    for value in (position_tolerance_m, height_tolerance_m, lens_tolerance_mm):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('camera3d continuity tolerances must be finite nonnegative numbers')
    if isinstance(scale_ratio_limit, bool) or not isinstance(scale_ratio_limit, (int, float)) or not math.isfinite(scale_ratio_limit) or scale_ratio_limit < 1:
        raise ValueError('camera3d continuity scale ratio must be finite and at least one')
    def view(state):
        if not isinstance(state, dict) or set(state) != {'world_matrix', 'optics', 'render'}:
            raise ValueError('camera3d continuity needs resolved world matrix, optics and render')
        return _matrix(state['world_matrix'], rigid=True), projection_matrix(state['optics'], state['render'])
    left_world, left_projection = view(outgoing)
    right_world, right_projection = view(incoming)
    reports = []
    def warn(code, **details):
        reports.append(dict(severity='warning', code=code, **details))
    height_delta = abs(float(left_world[1, 3] - right_world[1, 3]))
    if height_delta > height_tolerance_m:
        warn('camera-height', difference_m=height_delta)
    left_lens = outgoing['optics']['projection'].get('focal_length_mm')
    right_lens = incoming['optics']['projection'].get('focal_length_mm')
    if left_lens is not None and right_lens is not None and abs(left_lens - right_lens) > lens_tolerance_mm:
        warn('lens-change', difference_mm=abs(left_lens - right_lens))
    if axis is not None:
        if not isinstance(axis, (list, tuple)) or len(axis) != 2:
            raise ValueError('camera3d action axis requires two world points')
        origin, end = map(vector, axis)
        direction = end - origin
        if np.linalg.norm(direction[[0, 2]]) < 1e-8:
            raise ValueError('camera3d action axis has no horizontal direction')
        normal = np.cross(direction, [0, 1, 0])
        sides = [float(np.dot(world[:3, 3] - origin, normal)) for world in (left_world, right_world)]
        if sides[0] * sides[1] < -1e-12:
            warn('action-axis-crossing', signed_sides=sides)
    if (not isinstance(subjects_before, dict) or not isinstance(subjects_after, dict)
            or len(subjects_before) > 128 or len(subjects_after) > 128):
        raise ValueError('camera3d continuity needs at most 128 named subject bounds')
    for subject in sorted(set(subjects_before) | set(subjects_after)):
        if subject not in subjects_before or subject not in subjects_after:
            warn('subject-presence-change', subject=subject)
            continue
        measurements = []
        centers = []
        for bounds, state, world, projection in (
                (subjects_before[subject], outgoing, left_world, left_projection),
                (subjects_after[subject], incoming, right_world, right_projection)):
            if not isinstance(bounds, dict) or set(bounds) != {'minimum', 'maximum'}:
                raise ValueError('camera3d continuity subject needs minimum/maximum bounds')
            minimum, maximum = vector(bounds['minimum']), vector(bounds['maximum'])
            if np.any(minimum > maximum):
                raise ValueError('camera3d continuity subject bounds are inverted')
            center = (minimum + maximum) / 2
            centers.append(center)
            width, height = state['render']['width'], state['render']['height']
            rectangle = project_bounds(minimum, maximum, world, projection, width, height)
            screen = project(center, world, projection, width, height)
            measurements.append((rectangle, screen, width, height))
        displacement = float(np.linalg.norm(centers[1] - centers[0]))
        if displacement > position_tolerance_m:
            warn('subject-position', subject=subject, difference_m=displacement)
        if any(item[0] is None for item in measurements):
            warn('subject-outside-frustum', subject=subject,
                 views=['outgoing' if index == 0 else 'incoming' for index, item in enumerate(measurements) if item[0] is None])
        else:
            heights = [(item[0][3] - item[0][1]) / item[3] for item in measurements]
            ratio = max(heights) / max(min(heights), 1e-12)
            if ratio > scale_ratio_limit:
                warn('subject-screen-scale', subject=subject, ratio=ratio)
        before_x = measurements[0][1]['pixel'][0] / measurements[0][2] - .5
        after_x = measurements[1][1]['pixel'][0] / measurements[1][2] - .5
        if before_x * after_x < -.0001:
            warn('subject-screen-side', subject=subject, screen_x=[before_x, after_x])
        # Project the actual world displacement through both camera frames.
        movement = centers[1] - centers[0]
        screen_directions = [float(np.dot(movement, world[:3, 0])) for world in (left_world, right_world)]
        if screen_directions[0] * screen_directions[1] < -1e-12:
            warn('screen-direction-reversal', subject=subject, directions=screen_directions)
    if (props_before is None) != (props_after is None):
        raise ValueError('camera3d prop continuity needs both evaluated boundary states')
    if props_before is not None:
        if not isinstance(props_before, dict) or not isinstance(props_after, dict) or max(len(props_before), len(props_after)) > 128:
            raise ValueError('camera3d continuity needs at most 128 prop states')
        for prop in sorted(set(props_before) | set(props_after)):
            if props_before.get(prop) != props_after.get(prop):
                warn('prop-state-change', prop=prop)
    if (gaze_before is None) != (gaze_after is None):
        raise ValueError('camera3d eyeline continuity needs both evaluated gaze states')
    eyelines = []
    if gaze_before is not None:
        if (not isinstance(gaze_before, dict) or not isinstance(gaze_after, dict)
                or max(len(gaze_before), len(gaze_after)) > 128):
            raise ValueError('camera3d eyeline continuity needs at most 128 gaze states')
        for subject in sorted(set(gaze_before) | set(gaze_after)):
            if subject not in gaze_before or subject not in gaze_after:
                warn('eyeline-presence-change', subject=subject)
                continue
            directions = []
            for gaze, world in ((gaze_before[subject], left_world), (gaze_after[subject], right_world)):
                if not isinstance(gaze, dict) or set(gaze) != {'origin', 'direction'}:
                    raise ValueError('camera3d eyeline requires actual world origin and direction')
                vector(gaze['origin'])
                direction = vector(gaze['direction'])
                length = float(np.linalg.norm(direction))
                if length < 1e-8:
                    raise ValueError('camera3d eyeline direction must be nonzero')
                direction /= length
                directions.append([float(np.dot(direction, world[:3, index])) for index in (0, 1)])
            eyelines.append(dict(subject=subject, projected_directions=directions))
            if directions[0][0] * directions[1][0] < -1e-8:
                warn('eyeline-screen-reversal', subject=subject, projected_directions=directions)
    return dict(warnings=reports, automatic_approval=False,
                eyelines=eyelines, scope='evaluated boundary geometry and supplied gaze; no occlusion or narrative inference')
