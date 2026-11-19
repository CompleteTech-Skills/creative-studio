"""Local OBJ/MTL geometry importer. OBJ has no skeleton/animation preservation."""
from __future__ import annotations

import math
import hashlib
from pathlib import Path
import shlex
from time import monotonic
import numpy as np
from rig_contracts import DEFAULT_LIMITS
from rig_errors import require, safe_path, slug, normalize_root
from rig_geometry import analyze_geometry
from rig_models import Geometry, Mesh, TextureImage, validate_geometry


def import_obj(path: Path, *, scale=1., limits=None, source_reader=None, geometry_only=False):
    from rig_import import ImportedModel, _decode_texture
    limits = limits or DEFAULT_LIMITS
    path = Path(path)
    path = path.absolute() if source_reader is not None else normalize_root(path.parent)/path.name
    source_limit = limits['source_bytes']
    require(type(source_limit) in (int, float) and 0 < source_limit < 2**63
            and int(source_limit) == source_limit,
            'RIG_LIMIT_SOURCE', 'OBJ source byte budget must be a positive whole number.')
    source_limit = int(source_limit)
    deadline = monotonic() + limits['timeout_seconds'] if geometry_only else None
    payload_sizes = {}
    def read_source(source, maximum, *, code='RIG_LIMIT_SOURCE',
                    message='OBJ source exceeds its byte budget.', allow_empty=False):
        if source_reader is None:
            size = source.stat().st_size
            require(size <= maximum, code, message)
            if geometry_only:
                require(32 * size <= limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
                        'OBJ text exceeds its parsing workspace.')
            with source.open('rb') as stream:
                data = stream.read(size + 1)
            require(len(data) <= maximum, code, message)
            require(len(data) == size, 'RIG_SOURCE_CHANGED', 'OBJ source size changed during reading.')
        else:
            data = source_reader(source, maximum, code=code, message=message, allow_empty=allow_empty)
            require(len(data) <= maximum, code, message)
        payload_sizes[source] = len(data)
        require(sum(payload_sizes.values()) <= limits['dependency_bytes'],
                'RIG_LIMIT_DEPENDENCIES', 'OBJ dependencies exceed their byte budget.')
        return data
    entry_bytes = read_source(path, source_limit)
    # Reserve decoded lines, tokenizer records and face expansion before allocating them.
    text_workspace = 32 * len(entry_bytes)
    if geometry_only:
        require(text_workspace <= limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
                'OBJ text exceeds its parsing workspace.')
    triangle_count = 0
    expanded_vertices = 0
    require(math.isfinite(scale) and 0 < scale <= 10000, 'RIG_SCALE', 'OBJ scale must be positive.')
    vertices, uv, normals, groups, dependencies = [], [], [], {}, [path]
    materials = [dict(name='Default material')];material_ids = {'default': 0};images = [];textures = []
    total_texture_pixels = 0
    current_group, current_material = ('' if geometry_only else 'mesh'), 0
    current_object = None
    object_index = 0
    group_members = {}

    def check_deadline():
        require(monotonic() < deadline, 'RIG_OBJ_TIMEOUT',
                'OBJ geometry parsing exceeded its time budget.')

    def numbers(values, minimum):
        require(len(values) >= minimum, 'RIG_OBJ_NUMBER', 'OBJ numeric record is incomplete.')
        data = list(map(float, values[:minimum]));require(all(math.isfinite(v) for v in data), 'RIG_NONFINITE', 'OBJ data must be finite.')
        return data

    def material_file(name):
        nonlocal total_texture_pixels
        p = safe_path(path.parent, name, exists=True);dependencies.append(p)
        material = None
        data = read_source(p, min(1024**2, source_limit), code='RIG_LIMIT_MTL',
                           message='MTL exceeds its encoded-byte budget.', allow_empty=True)
        for line in data.decode('utf-8').splitlines():
            fields = shlex.split(line, comments=True)
            if not fields:continue
            key, values = fields[0], fields[1:]
            if key == 'newmtl':
                name = ' '.join(values);require(name not in material_ids, 'RIG_MTL', 'Material name is duplicated.')
                material_ids[name] = len(materials);material = dict(name=name, pbrMetallicRoughness=dict(metallicFactor=0., roughnessFactor=.65));materials.append(material)
            elif material is not None and key == 'Kd':
                color = numbers(values, 3);require(all(0 <= c <= 1 for c in color), 'RIG_MTL', 'Material colors must be in [0,1].')
                material['pbrMetallicRoughness']['baseColorFactor'] = color+[1.]
            elif material is not None and key in ('d', 'Tr'):
                opacity = numbers(values, 1)[0];opacity = 1-opacity if key == 'Tr' else opacity
                require(0 <= opacity <= 1, 'RIG_MTL', 'Opacity must be in [0,1].')
                material['pbrMetallicRoughness'].setdefault('baseColorFactor', [1., 1., 1., 1.])[3] = opacity
                material['alphaMode'] = 'BLEND'
            elif material is not None and key == 'map_Kd':
                require(values and not values[0].startswith('-'), 'RIG_MTL', 'Bake MTL texture options before import.')
                texture = safe_path(p.parent, ' '.join(values), exists=True)
                data = read_source(texture, source_limit, code='RIG_LIMIT_TEXTURE',
                                   message='MTL texture exceeds its encoded-byte budget.')
                mime, pixels = _decode_texture(data, limits, total_texture_pixels)
                total_texture_pixels += pixels
                dependencies.append(texture);images.append(TextureImage(texture.name, mime, data))
                textures.append(dict(source=len(images)-1))
                material['pbrMetallicRoughness']['baseColorTexture'] = dict(index=len(textures)-1)

    for line in entry_bytes.decode('utf-8').splitlines():
        if geometry_only:
            check_deadline()
            fields = line.partition('#')[0].split()
        else:
            fields = shlex.split(line, comments=True)
        if not fields:continue
        key, values = fields[0], fields[1:]
        if geometry_only:
            require(key not in ('mtllib', 'usemtl'), 'RIG_OBJ_MATERIAL',
                    'Geometry-only OBJ import does not accept material declarations.')
            require(key in ('v', 'vt', 'vn', 'g', 'o', 's', 'f'), 'RIG_OBJ_RECORD',
                    'Unsupported geometry-only OBJ record.')
            width = {'v': 3, 'vt': 2, 'vn': 3}.get(key)
            require(width is None or len(values) == width, 'RIG_OBJ_NUMBER',
                    'Unsupported geometry-only OBJ coordinate width.')
            if key == 's':
                require(values in (['off'], ['0']), 'RIG_OBJ_RECORD',
                        'Provide explicit normals and disable smoothing groups.')
        if key == 'v':
            vertices.append(numbers(values, 3));require(len(vertices) <= limits['vertices'], 'RIG_LIMIT_VERTICES', 'OBJ vertex budget exceeded.')
        elif key == 'vt':uv.append(numbers(values, 2))
        elif key == 'vn':normals.append(numbers(values, 3))
        elif key == 'mtllib':
            for name in values:material_file(name)
        elif key == 'usemtl':
            name = ' '.join(values);require(name in material_ids, 'RIG_MTL', 'Referenced material is undefined.');current_material = material_ids[name]
        elif key in ('g', 'o'):
            label = ' '.join(values)
            if not geometry_only:
                current_group = label or 'mesh'
            elif key == 'o':
                object_index += 1
                # Every declaration opens its own scope, including an unnamed one.
                current_object = (object_index, label)
            else:
                current_group = label
                group_members[label] = values
        elif key == 'f':
            require(3 <= len(values) <= 256, 'RIG_OBJ_FACE', 'OBJ faces need 3–256 vertices.')
            triangle_count += len(values) - 2
            require(triangle_count <= limits['triangles'], 'RIG_LIMIT_TRIANGLES', 'OBJ triangle budget exceeded.')
            if geometry_only:
                require(text_workspace + 2048 * triangle_count <= limits['memory_bytes'],
                        'RIG_LIMIT_MEMORY', 'OBJ triangle expansion exceeds its parsing workspace.')
            face = []
            for value in values:
                parts = value.split('/');require(len(parts) <= 3, 'RIG_OBJ_FACE', 'Invalid OBJ face tuple.')
                indices = []
                for part, length in zip(parts+['']*(3-len(parts)), [len(vertices), len(uv), len(normals)]):
                    if not part:indices.append(None);continue
                    index = int(part)
                    require(index != 0, 'RIG_OBJ_INDEX', 'OBJ indices start at one.')
                    index = index-1 if index > 0 else length+index
                    require(0 <= index < length, 'RIG_OBJ_INDEX', 'OBJ face references an invalid vertex/UV/normal.');indices.append(index)
                require(indices[0] is not None, 'RIG_OBJ_INDEX', 'OBJ face requires vertex positions.');face.append(tuple(indices))
            bucket = groups.setdefault((current_group, current_material, current_object), [])
            triangles = triangulate_polygon(np.asarray([vertices[v[0]] for v in face], float))
            if geometry_only:
                check_deadline()
            for triangle in triangles:
                bucket.append([face[i] for i in triangle])
            if geometry_only:
                require(len(groups) <= 10000, 'RIG_LIMIT_NODES', 'OBJ groups exceed the native node limit.')
    meshes = []
    obj_partitions = []
    for i, ((group, material, object_scope), faces) in enumerate(groups.items()):
        name = (object_scope[1] if object_scope is not None else '') or group or 'mesh'
        if geometry_only:
            partition = {}
            if object_scope is not None:
                partition['object_index'] = object_scope[0]
                if object_scope[1]:
                    partition['object'] = object_scope[1]
            if group:
                partition['groups'] = group_members[group]
            obj_partitions.append((len(group), partition))
        lookup = {};position_array = [];uv_array = [];normal_array = [];triangles = []
        has_uv = all(v[1] is not None for f in faces for v in f)
        has_normals = all(v[2] is not None for f in faces for v in f)
        require(has_uv or all(v[1] is None for f in faces for v in f),'RIG_OBJ_UV','Complete missing UV corners or separate the untextured faces before import.')
        require(has_normals or all(v[2] is None for f in faces for v in f),'RIG_OBJ_NORMAL','Complete or remove partial OBJ normals before import.')
        for face in faces:
            if geometry_only:
                check_deadline()
            triangle = []
            for vertex in face:
                if vertex not in lookup:
                    expanded_vertices += 1
                    require(expanded_vertices <= limits['vertices'], 'RIG_LIMIT_VERTICES',
                            'OBJ expanded vertex budget exceeded.')
                    lookup[vertex] = len(position_array);position_array.append(vertices[vertex[0]])
                    if has_uv:uv_array.append(uv[vertex[1]])
                    if has_normals:normal_array.append(normals[vertex[2]])
                triangle.append(lookup[vertex])
            triangles.append(triangle)
        attributes = {}
        if has_uv:
            attributes['TEXCOORD_0'] = np.asarray(uv_array, dtype=float)
            attributes['TEXCOORD_0'][:,1]=1-attributes['TEXCOORD_0'][:,1]
        if has_normals:
            n = np.asarray(normal_array, dtype=float)
            if geometry_only:
                magnitude = np.max(np.abs(n), axis=1)
                require(np.all(magnitude > 0), 'RIG_NORMAL', 'OBJ normals cannot be zero.')
                n /= magnitude[:, None]
            lengths = np.linalg.norm(n, axis=1)
            require(np.all(lengths > 1e-8), 'RIG_NORMAL', 'OBJ normals cannot be zero.')
            attributes['NORMAL'] = n / lengths[:, None]
        meshes.append(Mesh(f'{slug(name)}-{i}', np.asarray(position_array, dtype=float)*scale,
                           np.asarray(triangles, dtype=np.int64), material, attributes, name=name))
    geometry = Geometry(meshes, materials, images, textures)
    validate_geometry(geometry, limits)
    if geometry_only:
        check_deadline()
        return ImportedModel(geometry, dependencies, report={'obj_partitions': obj_partitions})
    report = analyze_geometry(geometry, limits)
    report.update(import_format='.obj', scale_conversion=scale, preserved_existing_rig=False, preserved_animation_clips=0,
                  source_sha256=hashlib.sha256(entry_bytes).hexdigest(), warnings=['OBJ imports geometry/materials only; skeletons and animations are not present in this format.',
                    'Planar simple polygons are ear-clipped; OBJ bottom-left UVs are converted to glTF top-left UVs.'])
    return ImportedModel(geometry, list(dict.fromkeys(dependencies)), report=report)


def triangulate_polygon(points):
    """Deterministic ear clipping for planar, simple concave OBJ polygons."""
    if len(points)==3:return [[0,1,2]]
    normal=np.sum(np.cross(points,np.roll(points,-1,axis=0)),axis=0);length=np.linalg.norm(normal)
    require(length>1e-12,'RIG_OBJ_POLYGON','Degenerate polygon cannot be triangulated.')
    normal/=length;extent=max(float(np.ptp(points,axis=0).max()),1e-6)
    require(np.max(np.abs((points-points[0])@normal))<=extent*1e-5,'RIG_OBJ_POLYGON','Triangulate nonplanar polygons in the source editor.')
    planar=np.delete(points,int(np.argmax(np.abs(normal))),axis=1)
    def cross(a,b,c):return float((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
    area=sum(planar[i,0]*planar[(i+1)%len(points),1]-planar[(i+1)%len(points),0]*planar[i,1] for i in range(len(points)))
    orientation=1 if area>0 else -1;remaining=list(range(len(points)));triangles=[];epsilon=extent*extent*1e-12
    for i in range(len(points)):
        for j in range(i+1,len(points)):
            if j==i+1 or (i==0 and j==len(points)-1):continue
            a,b=planar[i],planar[(i+1)%len(points)];c,d=planar[j],planar[(j+1)%len(points)]
            require(not (cross(a,b,c)*cross(a,b,d)<-epsilon and cross(c,d,a)*cross(c,d,b)<-epsilon),
                    'RIG_OBJ_POLYGON','Self-crossing polygons cannot be imported.')
    while len(remaining)>3:
        found=False
        for i,current in enumerate(remaining):
            previous=remaining[i-1];following=remaining[(i+1)%len(remaining)];a,b,c=planar[[previous,current,following]]
            if cross(a,b,c)*orientation<=epsilon:continue
            inside=False
            for index in remaining:
                if index in (previous,current,following):continue
                p=planar[index]
                if min(cross(a,b,p)*orientation,cross(b,c,p)*orientation,cross(c,a,p)*orientation)>=-epsilon:inside=True;break
            if inside:continue
            triangles.append([previous,current,following]);remaining.pop(i);found=True;break
        require(found,'RIG_OBJ_POLYGON','Polygon is degenerate or cannot be triangulated safely.')
    triangles.append(remaining);return triangles
