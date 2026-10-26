"""Local OBJ/MTL geometry importer. OBJ has no skeleton/animation preservation."""
from __future__ import annotations

import math
from pathlib import Path
import shlex
import numpy as np
from rig_contracts import DEFAULT_LIMITS
from rig_errors import require, safe_path, sha256, slug
from rig_geometry import analyze_geometry
from rig_models import Geometry, Mesh, TextureImage, validate_geometry


def import_obj(path: Path, *, scale=1., limits=None):
    from rig_import import ImportedModel, _decode_texture
    limits = limits or DEFAULT_LIMITS;path = Path(path).resolve()
    require(path.stat().st_size <= limits['source_bytes'], 'RIG_LIMIT_SOURCE', 'OBJ exceeds the encoded source budget.')
    require(math.isfinite(scale) and 0 < scale <= 10000, 'RIG_SCALE', 'OBJ scale must be positive.')
    vertices, uv, normals, groups, dependencies = [], [], [], {}, [path]
    materials = [dict(name='Default material')];material_ids = {'default': 0};images = [];textures = []
    total_texture_pixels = 0
    current_group, current_material = 'mesh', 0

    def numbers(values, minimum):
        require(len(values) >= minimum, 'RIG_OBJ_NUMBER', 'OBJ numeric record is incomplete.')
        data = list(map(float, values[:minimum]));require(all(math.isfinite(v) for v in data), 'RIG_NONFINITE', 'OBJ data must be finite.')
        return data

    def material_file(name):
        nonlocal total_texture_pixels
        p = safe_path(path.parent, name, exists=True);dependencies.append(p)
        require(p.stat().st_size <= 1024**2, 'RIG_LIMIT_MTL', 'MTL exceeds 1 MiB.')
        material = None
        for line in p.read_text('utf-8').splitlines():
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
                require(texture.stat().st_size <= limits['source_bytes'], 'RIG_LIMIT_TEXTURE', 'MTL texture exceeds its budget.')
                with texture.open('rb') as stream:
                    data = stream.read(limits['source_bytes'] + 1)
                mime, pixels = _decode_texture(data, limits, total_texture_pixels)
                total_texture_pixels += pixels
                dependencies.append(texture);images.append(TextureImage(texture.name, mime, data))
                textures.append(dict(source=len(images)-1))
                material['pbrMetallicRoughness']['baseColorTexture'] = dict(index=len(textures)-1)

    for line in path.read_text('utf-8').splitlines():
        fields = shlex.split(line, comments=True)
        if not fields:continue
        key, values = fields[0], fields[1:]
        if key == 'v':
            vertices.append(numbers(values, 3));require(len(vertices) <= limits['vertices'], 'RIG_LIMIT_VERTICES', 'OBJ vertex budget exceeded.')
        elif key == 'vt':uv.append(numbers(values, 2))
        elif key == 'vn':normals.append(numbers(values, 3))
        elif key == 'mtllib':
            for name in values:material_file(name)
        elif key == 'usemtl':
            name = ' '.join(values);require(name in material_ids, 'RIG_MTL', 'Referenced material is undefined.');current_material = material_ids[name]
        elif key in ('g', 'o'):current_group = ' '.join(values) or 'mesh'
        elif key == 'f':
            require(3 <= len(values) <= 256, 'RIG_OBJ_FACE', 'OBJ faces need 3–256 vertices.')
            face = []
            for value in values:
                parts = value.split('/');require(len(parts) <= 3, 'RIG_OBJ_FACE', 'Invalid OBJ face tuple.')
                indices = []
                for part, length in zip(parts+['']*(3-len(parts)), [len(vertices), len(uv), len(normals)]):
                    if not part:indices.append(None);continue
                    index = int(part);index = index-1 if index > 0 else length+index
                    require(0 <= index < length, 'RIG_OBJ_INDEX', 'OBJ face references an invalid vertex/UV/normal.');indices.append(index)
                require(indices[0] is not None, 'RIG_OBJ_INDEX', 'OBJ face requires vertex positions.');face.append(tuple(indices))
            bucket = groups.setdefault((current_group, current_material), [])
            for triangle in triangulate_polygon(np.asarray([vertices[v[0]] for v in face],float)):
                bucket.append([face[i] for i in triangle])
            require(sum(len(g) for g in groups.values()) <= limits['triangles'], 'RIG_LIMIT_TRIANGLES', 'OBJ triangle budget exceeded.')
    meshes = []
    for i, ((name, material), faces) in enumerate(groups.items()):
        lookup = {};position_array = [];uv_array = [];normal_array = [];triangles = []
        has_uv = all(v[1] is not None for f in faces for v in f)
        has_normals = all(v[2] is not None for f in faces for v in f)
        require(has_uv or all(v[1] is None for f in faces for v in f),'RIG_OBJ_UV','Complete missing UV corners or separate the untextured faces before import.')
        require(has_normals or all(v[2] is None for f in faces for v in f),'RIG_OBJ_NORMAL','Complete or remove partial OBJ normals before import.')
        for face in faces:
            triangle = []
            for vertex in face:
                if vertex not in lookup:
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
            n = np.asarray(normal_array, dtype=float);lengths = np.linalg.norm(n, axis=1)
            require(np.all(lengths > 1e-8), 'RIG_NORMAL', 'OBJ normals cannot be zero.');attributes['NORMAL'] = n/lengths[:, None]
        meshes.append(Mesh(f'{slug(name)}-{i}', np.asarray(position_array, dtype=float)*scale,
                           np.asarray(triangles, dtype=np.int64), material, attributes, name=name))
    geometry = Geometry(meshes, materials, images, textures)
    validate_geometry(geometry, limits)
    require(sum(p.stat().st_size for p in set(dependencies)) <= limits['dependency_bytes'], 'RIG_LIMIT_DEPENDENCIES', 'OBJ dependencies exceed their budget.')
    report = analyze_geometry(geometry, limits)
    report.update(import_format='.obj', scale_conversion=scale, preserved_existing_rig=False, preserved_animation_clips=0,
                  source_sha256=sha256(path), warnings=['OBJ imports geometry/materials only; skeletons and animations are not present in this format.',
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
