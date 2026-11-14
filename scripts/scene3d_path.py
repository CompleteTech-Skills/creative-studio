"""Optional-free path admission shared by contracts and the spatial sampler."""
import math
from rig_errors import require


def _finite(value):
    try:
        return type(value) in (int,float) and math.isfinite(value)
    except OverflowError:
        return False


def path_lengths(points):
    require(isinstance(points,(list,tuple)) and 2 <= len(points) <= 10000,
            'SCENE3D_SPATIAL','Path requires 2..10000 points.')
    require(all(isinstance(point,(list,tuple)) and len(point)==3 and
                all(_finite(value) for value in point)
                for point in points),'SCENE3D_SPATIAL','Path points require finite XYZ coordinates.')
    lengths = [math.dist(a,b) for a,b in zip(points,points[1:])]
    require(all(math.isfinite(length) for length in lengths),
            'SCENE3D_SPATIAL','Path length exceeds finite numerical bounds.')
    try:
        total = math.fsum(lengths)
    except OverflowError:
        total = math.inf
    require(math.isfinite(total) and total>1e-10,
            'SCENE3D_SPATIAL','Path has no measurable finite length.')
    return lengths,total
