"""Bounded, explicit topology inspection and copy-based native mesh operations.

No operation changes the supplied Mesh. Exact vertex welding includes every
attribute and morph value, so UV seams, hard edges and authored deformation are
not merged merely because positions coincide. Holes and separate meshes are
never automatically joined. Self intersections and remeshing are not inferred.
"""
from __future__ import annotations

import copy
import math
from collections import deque
from fractions import Fraction
import numpy as np

from rig_contracts import DEFAULT_LIMITS
from rig_errors import require
from rig_models import Geometry, Mesh, validate_geometry


def _limits(limits):
    result = dict(DEFAULT_LIMITS)
    if limits is not None:
        require(isinstance(limits, dict) and set(limits) <= set(result),
                'MODEL3D_TOPOLOGY_LIMIT', 'Unknown topology limit.')
        for key, value in limits.items():
            require(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and 0 < value <= 2**63-1 and value == int(value),
                    'MODEL3D_TOPOLOGY_LIMIT', 'Limits must be positive finite integers.')
            result[key] = int(value)
    return result


def _check(mesh, limits=None, *, vertices=None, triangles=None):
    limits = _limits(limits)
    require(isinstance(mesh, Mesh), 'MODEL3D_TOPOLOGY', 'Expected a native Mesh.')
    v, t = mesh.positions, mesh.triangles
    require(isinstance(v, np.ndarray) and isinstance(t, np.ndarray)
            and v.ndim == t.ndim == 2 and v.shape[1:] == t.shape[1:] == (3,)
            and v.dtype.kind in 'fiu' and t.dtype.kind in 'iu',
            'MODEL3D_TOPOLOGY', 'Expected numeric native positions and integer triangle arrays.')
    nv, nt = len(v) if vertices is None else vertices, len(t) if triangles is None else triangles
    require(nv <= limits['vertices'] and nt <= limits['triangles'],
            'MODEL3D_TOPOLOGY_LIMIT', 'Topology output exceeds vertex/triangle budget.')
    # Conservative allowance for directed-edge dictionaries, adjacency, Python
    # integer/tuple records, sort keys and copied arrays, before constructing any.
    require(isinstance(mesh.attributes, dict) and isinstance(mesh.morphs, dict)
            and all(isinstance(k, str) and isinstance(a, np.ndarray) and a.dtype.kind in 'fiu'
                    for k, a in mesh.attributes.items())
            and all(isinstance(name, str) and isinstance(target, dict)
                    and all(isinstance(k, str) and isinstance(a, np.ndarray) and a.dtype.kind in 'fiu'
                            for k, a in target.items()) for name, target in mesh.morphs.items())
            and isinstance(mesh.morph_defaults, dict)
            and all(isinstance(k, str) and isinstance(x, (int, float)) and not isinstance(x, bool)
                    and -2 <= x <= 2 for k, x in mesh.morph_defaults.items()),
            'MODEL3D_TOPOLOGY', 'Expected native attribute and morph arrays.')
    extra = sum(a.nbytes for a in mesh.attributes.values())
    extra += sum(a.nbytes for target in mesh.morphs.values() for a in target.values())
    require(nv * 256 + nt * 2048 + extra * 8 <= limits['memory_bytes'],
            'MODEL3D_TOPOLOGY_LIMIT', 'Topology working set exceeds memory budget.')
    require(isinstance(mesh.material, int) and not isinstance(mesh.material, bool)
            and 0 <= mesh.material < 4096, 'MODEL3D_TOPOLOGY', 'Invalid material slot.')
    validate_geometry(Geometry([mesh], [{} for _ in range(mesh.material + 1)]), limits)
    return limits


def _edges(triangles):
    result = {}
    for face, tri in enumerate(triangles):
        for a, b in zip(tri, np.roll(tri, -1)):
            a, b = int(a), int(b)
            key = (min(a, b), max(a, b))
            result.setdefault(key, []).append((face, a < b))
    return result


def _components(count, edges):
    neighbors = [[] for _ in range(count)]
    for refs in edges.values():
        faces = sorted(set(face for face, _ in refs))
        # A star avoids quadratic adjacency on non-manifold edges.
        for face in faces[1:]:
            neighbors[faces[0]].append(face)
            neighbors[face].append(faces[0])
    visited, components = set(), []
    for face in range(count):
        if face in visited:
            continue
        queue, group = deque([face]), []
        visited.add(face)
        while queue:
            current = queue.popleft(); group.append(current)
            for other in neighbors[current]:
                if other not in visited:
                    visited.add(other); queue.append(other)
        components.append(np.asarray(sorted(group), dtype=np.int64))
    return components


def face_components(mesh: Mesh, limits=None) -> list[np.ndarray]:
    """Return stable face-index groups connected by edges (not mere contact)."""
    _check(mesh, limits)
    return _components(len(mesh.triangles), _edges(mesh.triangles))


def _scaled_positions(positions):
    p = positions.astype(np.float64)
    with np.errstate(over='ignore', invalid='ignore'):
        local = p - p[0]
    if not np.isfinite(local).all():
        magnitude = float(np.max(np.abs(p)))
        local = p / magnitude - p[0] / magnitude
    magnitude = float(np.max(np.abs(local)))
    return local / magnitude if magnitude else local


def _face_vectors(mesh):
    """Per-triangle scaled cross products and logarithmic area factors.

    Each edge is scaled independently, so neither a disconnected component's
    size nor a distant origin can classify another component as degenerate.
    Logarithms retain relative incident-face areas without squaring tiny units.
    """
    p = mesh.positions.astype(np.float64)[mesh.triangles]
    with np.errstate(over='ignore', invalid='ignore'):
        a, b = p[:, 1]-p[:, 0], p[:, 2]-p[:, 0]
    overflow = ~np.isfinite(a).all(axis=1) | ~np.isfinite(b).all(axis=1)
    adjustment = np.zeros(len(p))
    if overflow.any():
        scale = np.max(np.abs(p[overflow]), axis=(1, 2))
        local = p[overflow] / scale[:, None, None]
        a[overflow], b[overflow] = local[:, 1]-local[:, 0], local[:, 2]-local[:, 0]
        adjustment[overflow] = 2*np.log(scale)
    sa, sb = np.max(np.abs(a), axis=1), np.max(np.abs(b), axis=1)
    a = a / np.where(sa, sa, 1)[:, None]
    b = b / np.where(sb, sb, 1)[:, None]
    with np.errstate(divide='ignore'):
        logs = np.log(sa) + np.log(sb) + adjustment
    return np.cross(a, b), logs


def _exact_collinear(points):
    # Slow path only for numerically near-collinear faces. Exact binary-rational
    # arithmetic proves zero area rather than deleting a skinny valid triangle.
    p = [[Fraction(int(x)) if points.dtype.kind in 'iu' else Fraction(float(x))
          for x in row] for row in points]
    a = [p[1][i]-p[0][i] for i in range(3)]
    b = [p[2][i]-p[0][i] for i in range(3)]
    return all(a[i]*b[j] == a[j]*b[i] for i, j in ((0,1),(0,2),(1,2)))


def _degenerate(mesh):
    cross, _ = _face_vectors(mesh)
    uncertain = np.max(np.abs(cross), axis=1) <= np.finfo(np.float64).eps * 64
    if mesh.positions.dtype.kind in 'iu':
        uncertain[:] = True  # float64 can lose adjacent large integer coordinates.
    result = np.zeros(len(mesh.triangles), dtype=bool)
    for face in np.flatnonzero(uncertain):
        result[face] = _exact_collinear(mesh.positions[mesh.triangles[face]])
    return result


def _normals(mesh):
    cross, logs = _face_vectors(mesh)
    t = mesh.triangles
    maximum = np.full(len(mesh.positions), -np.inf)
    for i in range(3):
        np.maximum.at(maximum, t[:, i], logs)
    normals = np.zeros((len(mesh.positions), 3), dtype=np.float64)
    for i in range(3):
        weight = np.zeros(len(t))
        valid = np.isfinite(logs) & np.isfinite(maximum[t[:, i]])
        weight[valid] = np.exp(logs[valid]-maximum[t[valid, i]])
        np.add.at(normals, t[:, i], cross * weight[:, None])
    # Normalize each vertex independently: tiny disconnected components keep
    # their own valid normals instead of inheriting a whole-mesh area cutoff.
    scale = np.max(np.abs(normals), axis=1)
    require(np.all(scale > 0), 'MODEL3D_TOPOLOGY_NORMAL',
            'Normals are undefined at isolated, degenerate or exactly cancelling vertices.')
    normals /= scale[:, None]
    length = np.linalg.norm(normals, axis=1)
    return normals / length[:, None]


def topology_report(mesh: Mesh, limits=None) -> dict:
    """Measure indexed topology; coincident positions are not automatically welded."""
    _check(mesh, limits)
    edges = _edges(mesh.triangles)
    components = _components(len(mesh.triangles), edges)
    _, counts = np.unique(mesh.positions, axis=0, return_counts=True)
    _, face_counts = np.unique(np.sort(mesh.triangles, axis=1), axis=0, return_counts=True)
    used = np.unique(mesh.triangles)
    return dict(schema_version=1, kind='model3d-topology', mesh=mesh.id,
                vertices=len(mesh.positions), triangles=len(mesh.triangles),
                duplicate_positions=int(np.sum(counts - 1)),
                duplicate_faces=int(np.sum(face_counts - 1)),
                degenerate_faces=np.flatnonzero(_degenerate(mesh)).tolist(),
                isolated_vertices=np.setdiff1d(np.arange(len(mesh.positions)), used).tolist(),
                boundary_edges=[list(edge) for edge, refs in sorted(edges.items()) if len(refs) == 1],
                nonmanifold_edges=[list(edge) for edge, refs in sorted(edges.items()) if len(refs) > 2],
                inconsistent_edges=[list(edge) for edge, refs in sorted(edges.items())
                                    if len(refs) == 2 and refs[0][1] == refs[1][1]],
                face_components=[c.tolist() for c in components],
                self_intersections='unavailable: no robust surface-intersection solver in this module')


def _select(mesh, vertex_ids, triangles):
    result = copy.deepcopy(mesh)
    result.positions = mesh.positions[vertex_ids].copy()
    result.triangles = np.asarray(triangles, dtype=np.int64).copy()
    result.attributes = {key: values[vertex_ids].copy() for key, values in mesh.attributes.items()}
    result.morphs = {name: {key: values[vertex_ids].copy() for key, values in target.items()}
                     for name, target in mesh.morphs.items()}
    return result


def clean_mesh(mesh: Mesh, *, remove_degenerate=False, remove_duplicate_faces=False,
               remove_unused_vertices=False, weld_exact_vertices=False, limits=None) -> Mesh:
    """Apply only requested cleanup, retaining seams and all per-vertex values.

    Duplicate faces are index-equivalent in either winding. Explicit removal
    retains the first occurrence. Empty outputs fail rather than deleting the
    semantic mesh. No radius welding, hole filling or component removal occurs.
    """
    limits = _check(mesh, limits)
    require(all(isinstance(x, bool) for x in (remove_degenerate, remove_duplicate_faces,
                remove_unused_vertices, weld_exact_vertices)), 'MODEL3D_TOPOLOGY', 'Cleanup flags must be booleans.')
    vertices = np.arange(len(mesh.positions))
    triangles = mesh.triangles.copy()
    if weld_exact_vertices:
        arrays = [mesh.positions] + [mesh.attributes[k] for k in sorted(mesh.attributes)]
        arrays += [mesh.morphs[n][k] for n in sorted(mesh.morphs) for k in sorted(mesh.morphs[n])]
        # tuples compare numeric values, so -0 and +0 are the same exact value.
        seen, selected, inverse = {}, [], np.empty(len(vertices), dtype=np.int64)
        for i in vertices:
            key = tuple(x for values in arrays for x in values[i])
            if key not in seen:
                seen[key] = len(selected); selected.append(i)
            inverse[i] = seen[key]
        vertices = np.asarray(selected, dtype=np.int64)
        triangles = inverse[triangles]
    provisional = _select(mesh, vertices, triangles)
    keep = np.ones(len(triangles), dtype=bool)
    if remove_degenerate:
        keep &= ~_degenerate(provisional)
    if remove_duplicate_faces:
        seen = set()
        for i, face in enumerate(triangles):
            if not keep[i]:
                continue
            key = tuple(sorted(int(x) for x in face))
            if key in seen:
                keep[i] = False
            else:
                seen.add(key)
    triangles = triangles[keep]
    require(len(triangles) > 0, 'MODEL3D_TOPOLOGY_EMPTY', 'Cleanup would remove the entire semantic mesh.')
    if remove_unused_vertices:
        used, inverse = np.unique(triangles, return_inverse=True)
        vertices = vertices[used]; triangles = inverse.reshape(-1, 3)
    result = _select(mesh, vertices, triangles)
    _check(result, limits)
    return result


def orient_mesh(mesh: Mesh, *, outward=False, limits=None) -> Mesh:
    """Make edge-connected orientable manifold components consistent.

    Outward orientation is only meaningful for closed components; requested
    outward on any open surface fails. No hole is filled. Authored tangent
    frames and directional morphs are rejected if a face must change winding.
    """
    limits = _check(mesh, limits)
    require(isinstance(outward, bool), 'MODEL3D_TOPOLOGY', 'outward must be boolean.')
    edges = _edges(mesh.triangles)
    require(not np.any(_degenerate(mesh)) and all(len(refs) <= 2 for refs in edges.values()),
            'MODEL3D_TOPOLOGY_ORIENTATION', 'Orientation requires nondegenerate manifold edges.')
    neighbors = [[] for _ in mesh.triangles]
    for refs in edges.values():
        if len(refs) == 2:
            (a, da), (b, db) = refs
            require(a != b, 'MODEL3D_TOPOLOGY_ORIENTATION', 'Repeated edge within a face.')
            neighbors[a].append((b, da == db)); neighbors[b].append((a, da == db))
    flips = np.full(len(mesh.triangles), -1, dtype=np.int8)
    components = _components(len(mesh.triangles), edges)
    closed_faces = np.ones(len(mesh.triangles), dtype=bool)
    for refs in edges.values():
        if len(refs) == 1:
            closed_faces[refs[0][0]] = False
    for group in components:
        first = int(group[0]); flips[first] = 0; queue = deque([first])
        while queue:
            face = queue.popleft()
            for other, different in neighbors[face]:
                expected = int(flips[face]) ^ different
                require(flips[other] in (-1, expected), 'MODEL3D_TOPOLOGY_ORIENTATION',
                        'Surface is not consistently orientable.')
                if flips[other] == -1:
                    flips[other] = expected; queue.append(other)
        if outward:
            require(bool(np.all(closed_faces[group])),
                    'MODEL3D_TOPOLOGY_ORIENTATION', 'Outward orientation requires closed components.')
            tri = mesh.triangles[group].copy()
            mask = flips[group].astype(bool)
            tri[mask] = tri[mask][:, [0, 2, 1]]
            vertex_ids, inverse = np.unique(tri, return_inverse=True)
            tri = inverse.reshape(-1, 3)
            p = _scaled_positions(mesh.positions[vertex_ids])
            # Only this component is scanned; work no longer grows with the
            # number of disconnected components times the whole mesh size.
            p = p - p.mean(axis=0)
            volume6 = float(np.einsum('ij,ij->', p[tri[:, 0]], np.cross(p[tri[:, 1]], p[tri[:, 2]])))
            require(volume6 != 0 and math.isfinite(volume6), 'MODEL3D_TOPOLOGY_ORIENTATION',
                    'Closed component has no measurable signed volume.')
            if volume6 < 0:
                flips[group] ^= 1
    result = copy.deepcopy(mesh)
    if flips.any():
        require('TANGENT' not in mesh.attributes and not any(
                set(target) - {'POSITION'} for target in mesh.morphs.values()),
                'MODEL3D_TOPOLOGY_ATTRIBUTE', 'Reorientation of authored tangents or directional morphs requires a frame rebuild.')
        result.triangles[flips.astype(bool)] = result.triangles[flips.astype(bool)][:, [0, 2, 1]]
        if 'NORMAL' in result.attributes:
            result.attributes['NORMAL'] = _normals(result)
    _check(result, limits)
    return result


def subdivide_mesh(mesh: Mesh, *, levels=1, limits=None) -> Mesh:
    """Linear midpoint subdivision (four triangles per face), not smoothing/remeshing.

    UV/color values interpolate on native indexed edges, retaining split seams.
    Morph and tangent interpolation require an authored frame policy and fail
    explicitly. The source and all its arrays remain untouched.
    """
    limits = _check(mesh, limits)
    require(isinstance(levels, (int, float)) and not isinstance(levels, bool)
            and 0 <= levels <= 8 and levels == int(levels),
            'MODEL3D_TOPOLOGY_LIMIT', 'Subdivision levels must be an integer from 0 to 8.')
    require(levels == 0 or (not mesh.morphs and 'TANGENT' not in mesh.attributes),
            'MODEL3D_TOPOLOGY_ATTRIBUTE', 'Subdivision of morph/tangent data requires an explicit interpolation policy.')
    result = copy.deepcopy(mesh)
    for _ in range(int(levels)):
        # Upper bound before constructing edge records or any output allocation.
        edges = sorted(_edges(result.triangles))
        _check(result, limits, vertices=len(result.positions) + len(edges),
               triangles=len(result.triangles)*4)
        edge_array = np.asarray(edges, dtype=np.int64)
        midpoint = {edge: len(result.positions)+i for i, edge in enumerate(edges)}
        triangles = []
        for a, b, c in result.triangles:
            ab = midpoint[tuple(sorted((int(a), int(b))))]
            bc = midpoint[tuple(sorted((int(b), int(c))))]
            ca = midpoint[tuple(sorted((int(c), int(a))))]
            triangles.extend(((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)))
        result.positions = np.concatenate((result.positions, result.positions[edge_array[:, 0]] / 2 + result.positions[edge_array[:, 1]] / 2))
        for name, values in result.attributes.items():
            result.attributes[name] = np.concatenate((values, values[edge_array[:, 0]] / 2 + values[edge_array[:, 1]] / 2))
        result.triangles = np.asarray(triangles, dtype=np.int64)
        if 'NORMAL' in result.attributes:
            result.attributes['NORMAL'] = _normals(result)
        _check(result, limits)
    return result


def shade_mesh(mesh: Mesh, *, mode='smooth', limits=None) -> Mesh:
    """Smooth within current indexed seams, or split every face for flat shading.

    Existing split UV/hard-edge vertices never merge. Flat shading preserves all
    indexed per-vertex data by duplication. Directional morphs and tangents need
    a separate frame rebuild and are rejected instead of being silently changed.
    """
    limits = _check(mesh, limits)
    require(mode in ('smooth', 'flat'), 'MODEL3D_TOPOLOGY', 'Shading mode must be smooth or flat.')
    require('TANGENT' not in mesh.attributes and not any(
            set(target) - {'POSITION'} for target in mesh.morphs.values()),
            'MODEL3D_TOPOLOGY_ATTRIBUTE', 'Shading authored tangent/directional morph frames is unsupported.')
    if mode == 'flat':
        _check(mesh, limits, vertices=len(mesh.triangles)*3)
        result = _select(mesh, mesh.triangles.reshape(-1),
                         np.arange(len(mesh.triangles)*3).reshape(-1, 3))
    else:
        result = copy.deepcopy(mesh)
    result.attributes['NORMAL'] = _normals(result)
    _check(result, limits)
    return result


def fill_planar_hole(mesh: Mesh, boundary, *, limits=None) -> Mesh:
    """Explicitly cap one convex supporting-plane boundary of 3..256 vertices.

    This conservative policy requires every other vertex strictly on one side
    of the boundary plane. Thus the new cap cannot cross existing off-plane
    geometry. Concave/nonplanar/ambiguous/internal holes require a stronger
    repair backend. No loop is selected or closed automatically.
    """
    limits = _check(mesh, limits)
    require(isinstance(boundary, (list, tuple, np.ndarray)) and 3 <= len(boundary) <= 256,
            'MODEL3D_TOPOLOGY_HOLE', 'Select 3..256 ordered boundary vertices.')
    require(all(isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, (bool, np.bool_))
                and 0 <= x < len(mesh.positions) and x == int(x) for x in boundary),
            'MODEL3D_TOPOLOGY_HOLE', 'Invalid boundary vertex.')
    boundary = [int(x) for x in boundary]
    require(len(set(boundary)) == len(boundary), 'MODEL3D_TOPOLOGY_HOLE', 'Boundary vertices must be distinct.')
    require(not mesh.morphs and 'TANGENT' not in mesh.attributes,
            'MODEL3D_TOPOLOGY_ATTRIBUTE', 'Hole filling morph/tangent data requires an explicit repair policy.')
    _check(mesh, limits, triangles=len(mesh.triangles)+len(boundary)-2)
    edges = _edges(mesh.triangles)
    directions = []
    boundary_degree = {}
    for (a, b), refs in edges.items():
        require(len(refs) <= 2, 'MODEL3D_TOPOLOGY_HOLE', 'Nonmanifold mesh cannot be safely capped.')
        if len(refs) == 1:
            boundary_degree[a] = boundary_degree.get(a, 0)+1
            boundary_degree[b] = boundary_degree.get(b, 0)+1
    for a, b in zip(boundary, boundary[1:]+boundary[:1]):
        refs = edges.get((min(a,b), max(a,b)), [])
        require(len(refs) == 1 and boundary_degree[a] == 2,
                'MODEL3D_TOPOLOGY_HOLE', 'Selection must trace a simple existing boundary loop.')
        directions.append(refs[0][1] == (a < b))
    require(all(directions) or not any(directions), 'MODEL3D_TOPOLOGY_HOLE', 'Boundary winding is inconsistent.')
    if all(directions):
        boundary.reverse()
    p = _scaled_positions(mesh.positions)
    polygon = p[boundary]
    normal = np.cross(polygon[1]-polygon[0], polygon[2]-polygon[0])
    length = float(np.linalg.norm(normal))
    require(length > 1e-12, 'MODEL3D_TOPOLOGY_HOLE', 'Boundary has collinear starting vertices.')
    normal /= length
    distance = (p-polygon[0]) @ normal
    require(np.max(np.abs(distance[boundary])) <= 1e-10,
            'MODEL3D_TOPOLOGY_HOLE', 'Boundary must be planar.')
    require(not np.any(np.all(np.abs(distance[mesh.triangles]) <= 1e-10, axis=1)),
            'MODEL3D_TOPOLOGY_HOLE', 'Existing faces already occupy the proposed cap plane.')
    # Non-boundary chords lying on the cap plane would become nonmanifold after
    # adding the fan, even when their third face vertex is safely off-plane.
    loop_edges = {tuple(sorted((a,b))) for a,b in zip(boundary, boundary[1:]+boundary[:1])}
    for edge in edges:
        require(not (edge not in loop_edges and all(abs(distance[i]) <= 1e-10 for i in edge)),
                'MODEL3D_TOPOLOGY_HOLE', 'Existing interior chords occupy the proposed cap.')
    other = np.delete(distance, boundary)
    require(len(other) > 0 and (np.all(other > 1e-10) or np.all(other < -1e-10)),
            'MODEL3D_TOPOLOGY_HOLE', 'Cap must be a strict supporting plane of the remaining mesh.')
    # Convexity alone is not enough for a star polygon: every vertex must be
    # strictly on the interior side of each directed edge, rejecting crossings.
    for i, a in enumerate(polygon):
        b = polygon[(i+1) % len(polygon)]
        side = np.cross(b-a, polygon-a) @ normal
        others = [j for j in range(len(polygon)) if j not in (i, (i+1)%len(polygon))]
        require(np.all(side[others] > 1e-12), 'MODEL3D_TOPOLOGY_HOLE', 'Boundary must be strictly convex and simple.')
    cap = np.asarray([(boundary[0], boundary[i], boundary[i+1]) for i in range(1, len(boundary)-1)], dtype=np.int64)
    result = copy.deepcopy(mesh)
    result.triangles = np.concatenate((mesh.triangles, cap))
    if 'NORMAL' in result.attributes:
        result.attributes['NORMAL'] = _normals(result)
    _check(result, limits)
    return result
