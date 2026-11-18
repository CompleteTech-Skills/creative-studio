"""Bounded, immutable sampling of existing Camera3D path documents.

Arc-length mode inverts a deterministic sampled polyline, not the spline's
parameter. The table resolution and convergence estimate are reported so callers
can distinguish numerical approximation from exact constant-speed motion.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import math

MAX_TABLE_SAMPLES = 65537


def _number(value, name, low=0.0, high=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'camera3d {name} must be finite in [{low},{high}]')
    return float(value)


class CameraPath:
    """Frozen path and reusable arc-length table; sampling has no playback state."""

    def __init__(self, document, *, samples_per_segment=64):
        from camera3d_contracts import validate
        import numpy as np
        validate(document, 'path')
        if type(samples_per_segment) is not int or not 8 <= samples_per_segment <= 256:
            raise ValueError('camera3d path table resolution must be an integer in 8..256')
        self._document = deepcopy(document)
        self.points = np.array(document['control_points_m'], dtype=float)
        self.mode = document['interpolation']
        self.closed = document['closed']
        if self.closed:
            if self.mode == 'bezier' and not np.array_equal(self.points[0], self.points[-1]):
                raise ValueError('camera3d closed Bezier must end at its first control point')
            if self.mode == 'linear' and not np.array_equal(self.points[0], self.points[-1]):
                self.points = np.vstack((self.points, self.points[0]))
            if self.mode == 'catmull-rom':
                if np.array_equal(self.points[0], self.points[-1]):
                    self.points = self.points[:-1]
                if len(self.points) < 3:
                    raise ValueError('camera3d closed Catmull-Rom needs three distinct control positions')
        self.segments = ((len(self.points)-1)//3 if self.mode == 'bezier' else
                         len(self.points) if self.mode == 'catmull-rom' and self.closed else len(self.points)-1)
        self.table_samples = self.segments*samples_per_segment+1
        if self.table_samples > MAX_TABLE_SAMPLES:
            raise ValueError('camera3d path arc table exceeds 65537 samples; reduce resolution or control points')
        self.parameters = np.linspace(0., 1., self.table_samples)
        values = np.array([self._curve(float(t))[0] for t in self.parameters])
        distances = np.linalg.norm(np.diff(values, axis=0), axis=1)
        self.lengths = np.r_[0., np.cumsum(distances)]
        self.length_m = float(self.lengths[-1])
        if not math.isfinite(self.length_m) or self.length_m <= 1e-9:
            raise ValueError('camera3d path has zero or nonfinite length')
        coarse = values[::2]
        if not np.array_equal(coarse[-1], values[-1]):
            coarse = np.vstack((coarse, values[-1]))
        coarse_length = float(np.linalg.norm(np.diff(coarse, axis=0), axis=1).sum())
        self.length_refinement_delta_m = abs(self.length_m-coarse_length)
        self.points.setflags(write=False)
        self.parameters.setflags(write=False)
        self.lengths.setflags(write=False)

    @property
    def document(self):
        return deepcopy(self._document)

    def _curve(self, parameter):
        import numpy as np
        segment = min(int(parameter*self.segments), self.segments-1)
        t = parameter*self.segments-segment
        if self.mode == 'linear':
            a,b = self.points[segment:segment+2]
            return a+(b-a)*t, (b-a)*self.segments, np.zeros(3)
        if self.mode == 'bezier':
            a,b,c,d = self.points[segment*3:segment*3+4]
            position = (1-t)**3*a+3*(1-t)**2*t*b+3*(1-t)*t*t*c+t**3*d
            derivative = 3*((1-t)**2*(b-a)+2*(1-t)*t*(c-b)+t*t*(d-c))
            second = 6*((1-t)*(c-2*b+a)+t*(d-2*c+b))
        else:
            n = len(self.points)
            at = lambda i: self.points[i % n if self.closed else min(max(i,0),n-1)]
            a,b,c,d = [at(segment+i) for i in (-1,0,1,2)]
            position = .5*((2*b)+(-a+c)*t+(2*a-5*b+4*c-d)*t*t+(-a+3*b-3*c+d)*t**3)
            derivative = .5*((-a+c)+2*(2*a-5*b+4*c-d)*t+3*(-a+3*b-3*c+d)*t*t)
            second = (2*a-5*b+4*c-d)+3*(-a+3*b-3*c+d)*t
        return position, derivative*self.segments, second*self.segments**2

    def sample(self, progress, *, reverse=False, offset_m=(0,0,0), parent_world=None):
        """Sample [0,1], retaining the terminal endpoint even for closed paths.

        Parent-space paths require a rigid parent transform. World-space paths
        reject a parent, making coordinate conversion explicit and singular.
        Offset is expressed in the authored path space before parent conversion.
        """
        import numpy as np
        from rig_math import vector
        progress = _number(progress, 'path progress')
        if type(reverse) is not bool:
            raise ValueError('camera3d reverse must be boolean')
        fraction = 1-progress if reverse else progress
        parameter = fraction
        if self._document['speed'] == 'arc-length':
            target = fraction*self.length_m
            right = min(bisect_right(self.lengths, target), len(self.lengths)-1)
            left = max(0,right-1)
            span = float(self.lengths[right]-self.lengths[left])
            weight = (target-self.lengths[left])/span if span else 0.
            parameter = float(self.parameters[left]+weight*(self.parameters[right]-self.parameters[left]))
        point, derivative, second = self._curve(parameter)
        point = point+vector(offset_m)
        norm = float(np.linalg.norm(derivative))
        tangent = derivative/norm if norm > 1e-12 else np.zeros(3)
        curvature = float(np.linalg.norm(np.cross(derivative,second))/norm**3) if norm>1e-12 else None
        if reverse:
            tangent = -tangent
        if self._document['space'] == 'parent':
            if parent_world is None:
                raise ValueError('camera3d parent-space path needs parent_world')
            from camera3d_math import _matrix
            matrix = _matrix(parent_world, rigid=True)
            point = matrix[:3,:3]@point+matrix[:3,3]
            tangent = matrix[:3,:3]@tangent
        elif parent_world is not None:
            raise ValueError('camera3d world path cannot apply parent_world')
        if not np.isfinite(point).all() or np.max(np.abs(point))>1e9:
            raise ValueError('camera3d evaluated path exceeds finite coordinate bounds')
        return {'position_m':point.tolist(), 'tangent':tangent.tolist(),
                'curvature_per_m':curvature, 'parameter':parameter,
                'progress':progress, 'length_m':self.length_m,
                'table_samples':self.table_samples,
                'length_refinement_delta_m':self.length_refinement_delta_m,
                'speed_policy':self._document['speed'], 'terminal':progress==1}

    def kinematics(self, progress, *, parent_world=None):
        """Numerical m/s and m/s² at exact progress, with one-sided endpoints.

        The finite-difference spacing is fixed in normalized path time. At a
        linear-path corner the reported acceleration records that discontinuity;
        the sampler does not claim to smooth an authored sharp turn.
        """
        import numpy as np
        progress=_number(progress,'path progress')
        rate=self._document['fps']
        duration=self._document['duration_frames']*rate['denominator']/rate['numerator']
        h=1e-4
        center=min(max(progress,h),1-h)
        times=[center-h,center,center+h]
        points=[np.asarray(self.sample(t,parent_world=parent_world)['position_m']) for t in times]
        dt=h*duration
        velocity=(points[2]-points[0])/(2*dt)
        acceleration=(points[2]-2*points[1]+points[0])/(dt*dt)
        return {'velocity_m_per_s':velocity.tolist(),'speed_m_per_s':float(np.linalg.norm(velocity)),
                'acceleration_m_per_s2':acceleration.tolist(),
                'acceleration_m_per_s2_magnitude':float(np.linalg.norm(acceleration)),
                'sample_progress':center,'difference_step_s':dt,
                'boundary_policy':'one-sided stencil at endpoints'}
