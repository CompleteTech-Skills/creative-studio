"""Bounded editable polygons and genuine native Mesh primitives, in canonical Y-up meters.

Source polygon loops remain immutable and separate from flat-shaded render vertices.
No acquisition/provider, storage, repair or visual approval is implied.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import numbers
import re
import numpy as np
from rig_contracts import DEFAULT_LIMITS
from rig_errors import require
from rig_models import Geometry, Mesh, validate_geometry
from rig_obj import triangulate_polygon


def _number(value, name, *, positive=True):
    require(isinstance(value, numbers.Real) and not isinstance(value, bool)
            and -1e100 <= value <= 1e100 and math.isfinite(value) and (value > 0 if positive else True),
            'MODEL3D_PARAMETER', f'{name} must be a finite positive number.' if positive else f'{name} must be finite.')
    return float(value)


def _integer(value, name, minimum=1, maximum=1_000_000):
    result = _number(value, name, positive=False)
    require(result.is_integer() and minimum <= result <= maximum,
            'MODEL3D_PARAMETER', f'{name} must be a whole number in [{minimum}, {maximum}].')
    return int(result)


def _limits(limits):
    result = dict(DEFAULT_LIMITS)
    if limits is not None:
        require(isinstance(limits, dict) and not set(limits)-set(result),
                'MODEL3D_PARAMETER', 'Unknown geometry limit.')
        for key, value in limits.items():
            result[key] = _integer(value, key, maximum=2**53-1)
    return result


def _budget(vertices, triangles, corners, normal_mode, limits):
    rendered = corners if normal_mode == 'flat' else vertices
    require(rendered <= limits['vertices'], 'RIG_LIMIT_VERTICES', 'Primitive vertex budget exceeded.')
    require(triangles <= limits['triangles'], 'RIG_LIMIT_TRIANGLES', 'Primitive triangle budget exceeded.')
    # Retained source, triangulation, normals and conservative temporary work arrays.
    require((vertices*24 + corners*16 + rendered*48 + triangles*32)*8 <= limits['memory_bytes'],
            'RIG_LIMIT_MEMORY', 'Primitive working memory budget exceeded before allocation.')


def _readonly(values, dtype):
    result = np.array(values, dtype=dtype, copy=True)
    result.setflags(write=False)
    return result


@dataclass(frozen=True)
class EditableMesh:
    """Native render mesh plus immutable original editable polygon topology."""
    mesh: Mesh
    source_positions: np.ndarray
    faces: tuple[tuple[int, ...], ...]
    triangle_faces: np.ndarray
    normal_mode: str

    def geometry(self):
        """Return the native Geometry view; no competing geometry/storage format."""
        return Geometry([self.mesh], [dict(name='primitive-diffuse', pbrMetallicRoughness=dict(
            baseColorFactor=[1., 1., 1., 1.], metallicFactor=0., roughnessFactor=.65))])


def editable_mesh(positions, faces, *, name='mesh', mesh_id=None, normal_mode='smooth', limits=None):
    """Validate simple planar polygon loops and triangulate using the native OBJ authority.

    The display name is preserved. Supply mesh_id when that name is not a portable
    Model3D identifier; IDs are never silently sanitized or guessed.
    Holes must be represented explicitly by other topology operations, never bridged
    or automatically filled. Flat shading preserves hard polygon boundaries.
    """
    limits = _limits(limits)
    require(isinstance(normal_mode,str) and normal_mode in ('flat', 'smooth'), 'MODEL3D_PARAMETER', 'normal_mode must be flat or smooth.')
    require(isinstance(name, str) and 0 < len(name) <= 128 and '\x00' not in name,
            'MODEL3D_PARAMETER', 'Mesh name must be nonempty and bounded.')
    identifier = name if mesh_id is None else mesh_id
    require(isinstance(identifier, str) and 1 <= len(identifier) <= 128
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*', identifier) is not None,
            'MODEL3D_PARAMETER', 'Mesh ID must be a portable Model3D identifier; provide mesh_id for display names.')
    require(isinstance(positions, (list, tuple, np.ndarray)) and (not isinstance(positions,np.ndarray) or positions.ndim > 0) and 3 <= len(positions) <= limits['vertices'],
            'RIG_LIMIT_VERTICES', 'Expected bounded nonempty positions.')
    require(isinstance(faces, (list, tuple)) and 0 < len(faces) <= limits['triangles'],
            'RIG_LIMIT_TRIANGLES', 'Expected bounded nonempty polygon faces.')
    require(all(isinstance(row, (list, tuple, np.ndarray)) and (not isinstance(row,np.ndarray) or row.ndim > 0) and len(row) == 3
                and all(isinstance(v, numbers.Real) and not isinstance(v, (bool, np.bool_))
                        and -1e100 <= v <= 1e100 and math.isfinite(v) for v in row) for row in positions),
            'MODEL3D_TOPOLOGY', 'Positions must be finite Nx3 numeric values.')
    loops = []; corner_count = 0; triangle_count = 0
    for face in faces:
        require(isinstance(face, (list, tuple, np.ndarray)) and (not isinstance(face,np.ndarray) or face.ndim > 0) and 3 <= len(face) <= 256,
                'MODEL3D_TOPOLOGY', 'Polygon loops need 3–256 indices.')
        corner_count += len(face); triangle_count += len(face)-2
        _budget(len(positions), triangle_count, corner_count, normal_mode, limits)
        loop = tuple(_integer(v, 'face index', 0, len(positions)-1) for v in face)
        require(len(set(loop)) == len(loop) and max(loop) < len(positions),
                'MODEL3D_TOPOLOGY', 'Polygon indices must be unique and reference source vertices.')
        loops.append(loop)
    triangles = sum(len(f)-2 for f in loops); corners = sum(map(len, loops))
    _budget(len(positions), triangles, corners, normal_mode, limits)
    try:
        source = np.asarray(positions, dtype=np.float64)
    except (ValueError, TypeError, OverflowError):
        require(False, 'MODEL3D_TOPOLOGY', 'Positions must be finite Nx3 numeric values.')
    require(source.shape == (len(positions), 3) and np.isfinite(source).all(),
            'MODEL3D_TOPOLOGY', 'Positions must be finite Nx3 values.')
    render, indices, owners, normals = [], [], [], []
    for fi, loop in enumerate(loops):
        polygon = source[list(loop)]
        extent = np.max(np.ptp(polygon, axis=0))
        require(np.isfinite(extent) and extent > 0, 'MODEL3D_TOPOLOGY', 'Polygon has no measurable extent.')
        # Native ear clipping uses canonical tolerances; normalizing makes these relative.
        local = (polygon-polygon[0])/extent
        local_triangles = triangulate_polygon(local)
        require(all(np.linalg.norm(np.cross(local[t[1]]-local[t[0]],local[t[2]]-local[t[0]])) > 1e-12
                    for t in local_triangles), 'MODEL3D_TOPOLOGY', 'Degenerate polygon triangle.')
        if normal_mode == 'flat':
            offset = len(render); render.extend(polygon)
            normal = np.cross(local[local_triangles[0][1]]-local[local_triangles[0][0]],
                              local[local_triangles[0][2]]-local[local_triangles[0][0]])
            normal /= np.linalg.norm(normal)
            normals.extend([normal]*len(loop))
            indices.extend([[offset+j for j in tri] for tri in local_triangles])
        else:
            indices.extend([[loop[j] for j in tri] for tri in local_triangles])
        owners.extend([fi]*len(local_triangles))
    positions_array = np.asarray(render if normal_mode == 'flat' else source, dtype=np.float64)
    indices_array = np.asarray(indices, dtype=np.int64)
    mesh = Mesh(identifier, _readonly(positions_array, float), _readonly(indices_array, np.int64),
                attributes={}, name=name)
    if normal_mode == 'smooth':
        # Reuse the topology owner's per-vertex area scaling. One whole-mesh
        # scale lets a larger disconnected surface erase a smaller one's normal.
        from model3d_topology import _normals
        normals = _normals(mesh)
    mesh.attributes['NORMAL'] = _readonly(normals, float)
    result = EditableMesh(mesh, _readonly(source, float), tuple(loops), _readonly(owners, np.int64), normal_mode)
    validate_geometry(result.geometry(), limits)
    return result


def primitive(kind, parameters=None, *, name=None, mesh_id=None, normal_mode='smooth', limits=None):
    """Build box, plane, cylinder, cone, UV sphere or torus with editable face loops.

    Dimensions are meters. Plane lies in XZ with +Y normal; rotational axes are +Y.
    Sphere poles are shared, avoiding degenerate cap triangles. Torus requires
    major_radius > minor_radius, preventing horn/self-intersecting geometry.
    """
    allowed = {'box': {'width','height','depth'}, 'plane': {'width','depth'},
               'cylinder': {'radius','height','segments'}, 'cone': {'radius','height','segments'},
               'sphere': {'radius','segments','rings'},
               'torus': {'major_radius','minor_radius','segments','rings'}}
    require(isinstance(kind,str) and kind in allowed, 'MODEL3D_PARAMETER', 'Unknown primitive kind.')
    p = {} if parameters is None else parameters
    require(isinstance(p, dict) and not set(p)-allowed[kind], 'MODEL3D_PARAMETER', 'Unknown primitive parameter.')
    l = _limits(limits)
    require(isinstance(normal_mode,str) and normal_mode in ('flat','smooth'), 'MODEL3D_PARAMETER', 'normal_mode must be flat or smooth.')
    def number(key, default): return _number(p.get(key, default), key)
    def integer(key, default, minimum): return _integer(p.get(key, default), key, minimum)
    if kind in ('box','plane'):
        w,d = number('width',1)/2,number('depth',1)/2
        if kind == 'plane':
            _budget(4,2,4,normal_mode,l)
            points=[[-w,0,-d],[-w,0,d],[w,0,d],[w,0,-d]]; faces=[[0,1,2,3]]
        else:
            h=number('height',1)/2; _budget(8,12,24,normal_mode,l)
            points=[[-w,-h,-d],[w,-h,-d],[w,-h,d],[-w,-h,d],[-w,h,-d],[w,h,-d],[w,h,d],[-w,h,d]]
            # Ear clipping joins loop positions 1 and 3 for each convex quad.
            # Checkerboard diagonals balance shared corner-normal contributions.
            faces=[[1,2,3,0],[4,7,6,5],[4,5,1,0],[1,5,6,2],[6,7,3,2],[3,7,4,0]]
    elif kind in ('cylinder','cone'):
        n=integer('segments',32,3); r=number('radius',.5); h=number('height',1)/2
        # Cap loops are bounded by the native polygon triangulator.
        require(n<=256, 'MODEL3D_PARAMETER', 'Capped primitive segments must be <=256.')
        _budget(2*n if kind=='cylinder' else n+1, 4*n-4 if kind=='cylinder' else 2*n-2,
                6*n if kind=='cylinder' else 4*n,normal_mode,l)
        points=[[r*math.cos(2*math.pi*i/n),-h,r*math.sin(2*math.pi*i/n)] for i in range(n)]
        faces=[list(range(n))]
        if kind=='cylinder':
            points += [[x,h,z] for x,_,z in points]
            faces += [list(range(2*n-1,n-1,-1))]
            faces += [[i,n+i,n+(i+1)%n,(i+1)%n] for i in range(n)]
        else:
            points.append([0,h,0]); faces += [[i,n,(i+1)%n] for i in range(n)]
    elif kind=='sphere':
        n=integer('segments',32,3); rings=integer('rings',16,2); r=number('radius',.5)
        _budget(2+n*(rings-1),2*n*(rings-1),n*6+max(0,rings-2)*n*4,normal_mode,l)
        points=[[0,r,0]]
        for j in range(1,rings):
            a=math.pi*j/rings
            points += [[r*math.sin(a)*math.cos(2*math.pi*i/n),r*math.cos(a),r*math.sin(a)*math.sin(2*math.pi*i/n)] for i in range(n)]
        bottom=len(points);points.append([0,-r,0])
        faces=[[0,1+(i+1)%n,1+i] for i in range(n)]
        for j in range(rings-2):
            a=1+j*n;b=a+n
            faces += [[a+i,a+(i+1)%n,b+(i+1)%n,b+i] for i in range(n)]
        a=1+(rings-2)*n; faces += [[a+i,a+(i+1)%n,bottom] for i in range(n)]
    else:
        n=integer('segments',32,3); rings=integer('rings',16,3)
        major=number('major_radius',1);minor=number('minor_radius',.25)
        require(major>minor,'MODEL3D_PARAMETER','Torus major radius must exceed minor radius.')
        _budget(n*rings,2*n*rings,4*n*rings,normal_mode,l)
        points=[];faces=[]
        for i in range(n):
            a=2*math.pi*i/n
            for j in range(rings):
                b=2*math.pi*j/rings;radius=major+minor*math.cos(b)
                points.append([radius*math.cos(a),minor*math.sin(b),radius*math.sin(a)])
        for i in range(n):
            for j in range(rings):
                faces.append([i*rings+j,i*rings+(j+1)%rings,((i+1)%n)*rings+(j+1)%rings,((i+1)%n)*rings+j])
    return editable_mesh(points, faces, name=kind if name is None else name, mesh_id=mesh_id, normal_mode=normal_mode, limits=l)
