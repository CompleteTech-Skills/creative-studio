"""Blender 4.5 surface adapter for validated canonical LookDev resources.

This is a callable material builder, not a render/job or storage authority. The
caller owns frozen inputs, scene construction, output publication and evaluation.
No bpy import occurs until the adapter executes inside a trusted Blender worker.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math

from lookdev_contracts import content_hash
from rig_errors import RigError, finite_json, require

VERSION = '1.0.0'
BASE_FEATURES = frozenset({'metallic-roughness', 'specular', 'ior', 'emission',
    'alpha-mask', 'alpha-blend', 'double-sided', 'transmission', 'clearcoat',
    'sheen', 'anisotropy', 'iridescence', 'subsurface'})
REQUIRED_SOCKETS = {'Base Color', 'Metallic', 'Roughness', 'IOR', 'Alpha',
    'Specular IOR Level', 'Specular Tint', 'Transmission Weight', 'Coat Weight',
    'Coat Roughness', 'Sheen Weight', 'Sheen Tint', 'Sheen Roughness',
    'Anisotropic', 'Anisotropic Rotation', 'Emission Color', 'Emission Strength',
    'Thin Film IOR', 'Thin Film Thickness', 'Subsurface Weight',
    'Subsurface Radius', 'Subsurface Scale'}


def _bpy():
    try:
        import bpy
    except ImportError as exc:
        raise RigError('LOOKDEV_BLENDER_REQUIRED', 'Execute this adapter inside Blender 4.5 LTS.') from exc
    require(bpy.app.version[:2] == (4, 5), 'LOOKDEV_BLENDER_VERSION',
            'This adapter is verified against Blender 4.5 LTS only.')
    return bpy


def _features(engine):
    require(engine in ('CYCLES', 'BLENDER_EEVEE_NEXT'), 'LOOKDEV_BLENDER_ENGINE',
            'Choose Cycles or EEVEE.')
    return BASE_FEATURES if engine == 'CYCLES' else BASE_FEATURES - {'subsurface', 'transmission', 'iridescence', 'anisotropy', 'sheen'}


def probe():
    """Probe actual RNA sockets without rendering or changing the active scene."""
    bpy = _bpy()
    material = bpy.data.materials.new('_hatch_probe')
    try:
        material.use_nodes = True
        node = material.node_tree.nodes.get('Principled BSDF')
        missing = REQUIRED_SOCKETS - {x.name for x in node.inputs}
        require(not missing, 'LOOKDEV_BLENDER_SOCKET', 'Missing Principled inputs: ' + ', '.join(sorted(missing)))
        return {'adapter': VERSION, 'blender': bpy.app.version_string,
                'features': sorted(_features(bpy.context.scene.render.engine)), 'engine': bpy.context.scene.render.engine,
                'scope': 'surface construction; rendering must be independently verified',
                'unsupported': ['volume-attenuation', 'textures-until-resolved', 'material-graph-until-compiled']}
    finally:
        bpy.data.materials.remove(material)


def material_plan(registry, reference, *, engine='CYCLES'):
    """Resolve/validate complete native inheritance before any Blender mutation.

    Texture/graph requests are rejected here until a verified corresponding owner
    has supplied an execution adapter. They never silently become flat materials.
    """
    require(engine in ('CYCLES', 'BLENDER_EEVEE_NEXT'), 'LOOKDEV_BLENDER_ENGINE', 'Choose Cycles or EEVEE.')
    effective = registry.effective_material(reference)
    features = _features(engine)
    compatibility = registry.check_material_compatibility(reference, features)
    require(not compatibility['missing'], 'LOOKDEV_BLENDER_UNSUPPORTED',
            'Material requires unimplemented mappings: ' + ', '.join(compatibility['missing']))
    require(not effective['texture_slots'] and not effective.get('graph'),
            'LOOKDEV_BLENDER_UNSUPPORTED', 'Texture and graph execution require their dedicated adapters.')
    surface = effective['surface']
    require(not surface.get('subsurface', {}).get('weight', 0) or (surface['metallic'] == 0 and surface.get('transmission', 0) == 0),
            'LOOKDEV_BLENDER_UNSUPPORTED', 'Subsurface scattering with metallic or transmission mixtures requires a separate verified closure adapter.')
    def contains_map(value):
        if isinstance(value, dict):
            return 'texture' in value or any(contains_map(v) for v in value.values())
        if isinstance(value, list):
            return any(contains_map(v) for v in value)
        return False
    require(not contains_map(surface), 'LOOKDEV_BLENDER_UNSUPPORTED',
            'Embedded surface maps require the texture adapter.')
    film = surface.get('iridescence', {})
    require(not film.get('weight', 0) or
            (film['weight'] == 1 and surface['metallic'] == 0 and
             film['thickness_min_nanometers'] == film['thickness_max_nanometers']),
            'LOOKDEV_BLENDER_UNSUPPORTED', 'Partial/variable-thickness iridescence needs a verified texture/mixture adapter.')
    require(not any(k in surface for k in ('normal', 'height', 'occlusion')) and
            not surface.get('displacement', {}).get('enabled'),
            'LOOKDEV_BLENDER_UNSUPPORTED', 'Embedded maps require the texture adapter.')
    require(surface.get('volume', {}).get('thickness_meters', 0) == 0,
            'LOOKDEV_BLENDER_UNSUPPORTED', 'Volume attenuation needs verified solid geometry and a volume adapter.')
    # Blender 4.5 has no user-selectable backface culling in Cycles; preserve a
    # one-sided canonical surface with an explicit Geometry/Transparent mix.
    plan = {'adapter_version': VERSION, 'engine': engine, 'reference': deepcopy(reference),
            'surface': deepcopy(surface), 'effective_sha256': effective['sha256']}
    plan['sha256'] = content_hash(plan)
    return plan


def _validate_plan(plan):
    """Fail closed on altered host plans before creating any Blender datablock."""
    require(isinstance(plan, dict) and set(plan) ==
            {'adapter_version', 'engine', 'reference', 'surface', 'effective_sha256', 'sha256'},
            'LOOKDEV_BLENDER_PLAN', 'Expected an exact host-resolved material plan.')
    finite_json(plan)
    require(plan['adapter_version'] == VERSION, 'LOOKDEV_BLENDER_PLAN', 'Unsupported material adapter version.')
    payload = {k: v for k, v in plan.items() if k != 'sha256'}
    require(content_hash(payload) == plan['sha256'], 'LOOKDEV_BLENDER_PLAN', 'Material plan was altered.')
    # Plans are internal values constructed by material_plan, not a replacement
    # external admission API. Recheck physical fields at the execution boundary.
    s = plan['surface']
    require(isinstance(s, dict), 'LOOKDEV_BLENDER_PLAN', 'Invalid material surface.')
    for key in ('metallic', 'roughness', 'opacity', 'alpha_cutoff'):
        require(type(s.get(key)) in (float, int) and 0 <= s[key] <= 1,
                'LOOKDEV_BLENDER_PLAN', 'Invalid unit scalar: ' + key)
    require(type(s.get('ior')) in (float, int) and 1 <= s['ior'] <= 4,
            'LOOKDEV_BLENDER_PLAN', 'Invalid IOR.')
    require(s.get('alpha_mode') in ('OPAQUE', 'MASK', 'BLEND') and
            s.get('base_color_space') == 'linear-srgb' and type(s.get('double_sided')) is bool,
            'LOOKDEV_BLENDER_PLAN', 'Invalid surface modes.')
    c = s.get('base_color')
    require(isinstance(c, list) and len(c) == 4 and all(type(x) in (int, float) and 0 <= x <= 1 for x in c),
            'LOOKDEV_BLENDER_PLAN', 'Invalid base color.')
    return s


def build_material(plan):
    """Construct real Principled/SSS shader nodes from a trusted resolved plan."""
    s = _validate_plan(plan)
    bpy = _bpy()
    require(bpy.context.scene.render.engine == plan['engine'], 'LOOKDEV_BLENDER_ENGINE', 'Plan/backend mismatch.')
    probe()
    material = bpy.data.materials.new('hatch:' + plan['reference']['id'])
    try:
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        links = material.node_tree.links
        output = nodes.new('ShaderNodeOutputMaterial')
        principled = nodes.new('ShaderNodeBsdfPrincipled')
        def value(name, setting):
            principled.inputs[name].default_value = setting
        color = tuple(s['base_color'])
        value('Base Color', color)
        value('Metallic', s['metallic'])
        value('Roughness', s['roughness'])
        value('IOR', s['ior'])
        alpha = s['opacity'] * color[3]
        value('Alpha', 1.0 if s['alpha_mode'] == 'OPAQUE' else
              (float(alpha >= s['alpha_cutoff']) if s['alpha_mode'] == 'MASK' else alpha))
        spec = s.get('specular', {'strength': 1.0, 'color': [1.0, 1.0, 1.0]})
        value('Specular IOR Level', spec['strength'] * 0.5)
        value('Specular Tint', (*spec['color'], 1.0))
        value('Transmission Weight', s.get('transmission', 0.0))
        coat = s.get('clearcoat', {})
        value('Coat Weight', coat.get('weight', 0.0))
        value('Coat Roughness', coat.get('roughness', 0.0))
        sheen = s.get('sheen', {})
        value('Sheen Weight', sheen.get('weight', 0.0))
        value('Sheen Roughness', sheen.get('roughness', 0.5))
        value('Sheen Tint', (*sheen.get('color', [0.0, 0.0, 0.0]), 1.0))
        anisotropy = s.get('anisotropy', {})
        value('Anisotropic', anisotropy.get('strength', 0.0))
        direction = anisotropy.get('direction', [1.0, 0.0])
        value('Anisotropic Rotation', (anisotropy.get('rotation_radians', 0.0) +
              math.atan2(direction[1], direction[0])) / (2.0 * math.pi) % 1.0)
        emit = s.get('emission', {})
        value('Emission Color', (*emit.get('color', [0.0, 0.0, 0.0]), 1.0))
        value('Emission Strength', emit.get('strength_watts_per_square_meter', 0.0))
        shader = principled.outputs['BSDF']
        film = s.get('iridescence', {})
        if film.get('weight', 0) > 0:
            require(film['thickness_min_nanometers'] == film['thickness_max_nanometers'],
                    'LOOKDEV_BLENDER_UNSUPPORTED', 'Varying iridescence thickness requires a texture mapping.')
            require(film['weight'] == 1, 'LOOKDEV_BLENDER_UNSUPPORTED',
                    'Partial iridescence weight requires a validated BSDF mixture.')
            value('Thin Film IOR', film['ior'])
            value('Thin Film Thickness', film['thickness_min_nanometers'])
        sss = s.get('subsurface', {})
        if sss.get('weight', 0) > 0:
            # Both closures keep identical coverage and surface lobes. Mixing a
            # bare BSSRDF would erase reflection/emission and make zero-alpha
            # surfaces opaque; only the diffuse response changes here.
            scatter = nodes.new('ShaderNodeBsdfPrincipled')
            require(len(scatter.inputs) == len(principled.inputs), 'LOOKDEV_BLENDER_SOCKET',
                    'Principled closure socket layouts differ.')
            for original_input, scatter_input in zip(principled.inputs, scatter.inputs):
                require(original_input.identifier == scatter_input.identifier, 'LOOKDEV_BLENDER_SOCKET',
                        'Principled closure socket identifiers differ.')
                if hasattr(original_input, 'default_value'):
                    default = original_input.default_value
                    try:
                        default = tuple(default)
                    except TypeError:
                        pass
                    scatter_input.default_value = default
            scatter.subsurface_method = 'RANDOM_WALK' if sss['method'] == 'random-walk' else 'BURLEY'
            scatter.inputs['Base Color'].default_value = (*sss['color'], color[3])
            scatter.inputs['Subsurface Weight'].default_value = 1.0
            scatter.inputs['Subsurface Scale'].default_value = 1.0
            scatter.inputs['Subsurface Radius'].default_value = tuple(sss['radius_meters'])
            # Blender 4.5 uses the copied Principled IOR for subsurface transport.
            mix = nodes.new('ShaderNodeMixShader')
            mix.inputs[0].default_value = sss['weight']
            links.new(shader, mix.inputs[1]); links.new(scatter.outputs['BSDF'], mix.inputs[2])
            shader = mix.outputs[0]
        if not s['double_sided']:
            geometry = nodes.new('ShaderNodeNewGeometry')
            transparent = nodes.new('ShaderNodeBsdfTransparent')
            mix = nodes.new('ShaderNodeMixShader')
            links.new(geometry.outputs['Backfacing'], mix.inputs[0])
            links.new(shader, mix.inputs[1]); links.new(transparent.outputs[0], mix.inputs[2])
            shader = mix.outputs[0]
        links.new(shader, output.inputs['Surface'])
        material.diffuse_color = color
        material.surface_render_method = 'DITHERED'
        material.use_transparent_shadow = True
        material['hatch_effective_sha256'] = plan['effective_sha256']
        material['hatch_plan_sha256'] = plan['sha256']
        material['hatch_adapter_version'] = VERSION
        return material
    except Exception:
        bpy.data.materials.remove(material)
        raise


def assign_material(obj, material, *, slots=None):
    """Assign explicit mesh slots without changing linked object instances."""
    require(obj.type == 'MESH', 'LOOKDEV_MESH', 'Material assignment requires a real mesh.')
    if slots is not None:
        require(isinstance(slots, (list, tuple)) and slots and len(set(slots)) == len(slots) and
                all(type(i) is int and 0 <= i < len(obj.data.materials) for i in slots),
                'LOOKDEV_SLOT', 'Select distinct existing material slot indices.')
    # Blender stores slots and polygon indices on shared mesh data. Isolate the
    # selected instance only after all slot admission checks have succeeded.
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    if slots is None:
        obj.data.materials.append(material)
        index = len(obj.data.materials) - 1
        for face in obj.data.polygons:
            face.material_index = index
    else:
        for i in slots:
            obj.data.materials[i] = material
    return {'mesh': obj.name, 'material': material.name,
            'plan_sha256': material['hatch_plan_sha256'], 'polygons': len(obj.data.polygons)}


def apply_displacement(obj, image, setting, *, max_vertices=1_000_000, max_extent_meters=10_000):
    """Bake a verified image into actual mesh geometry, transactionally.

    The texture owner must decode/hash-check ``image`` before calling. Geometry
    with existing modifiers is delegated to its owner rather than flattening a
    rig or silently destroying animation. UV0 and object-local meters are used.
    The subdivided copy is evaluated and bounded before replacing source data.
    """
    bpy = _bpy()
    require(obj.type == 'MESH' and not obj.modifiers and not obj.data.shape_keys, 'LOOKDEV_DISPLACEMENT_OWNER',
            'Displace static meshes before rigging; modifiers and shape keys remain under their owner.')
    require(isinstance(setting, dict) and set(setting) <=
            {'enabled', 'texture', 'scale_meters', 'midlevel', 'max_subdivision'},
            'LOOKDEV_DISPLACEMENT', 'Unexpected displacement fields.')
    scale, midlevel, levels = (setting.get('scale_meters'), setting.get('midlevel'), setting.get('max_subdivision'))
    require(setting.get('enabled') is True and type(scale) in (float, int) and math.isfinite(scale)
            and abs(scale) <= 100 and type(midlevel) in (float, int) and 0 <= midlevel <= 1
            and type(levels) is int and 0 <= levels <= 8,
            'LOOKDEV_DISPLACEMENT', 'Invalid displacement scale, midlevel or subdivision.')
    require(type(max_vertices) is int and 1 <= max_vertices <= 10_000_000 and
            type(max_extent_meters) in (float, int) and 0 < max_extent_meters <= 100_000,
            'LOOKDEV_DISPLACEMENT', 'Invalid displacement resource limits.')
    require(obj.data.uv_layers and len(obj.data.vertices) > 0,
            'LOOKDEV_DISPLACEMENT_UV', 'Actual UVs and vertices are required for displacement.')
    require(all(abs(s - 1.0) <= 1e-8 for s in obj.scale), 'LOOKDEV_DISPLACEMENT_UNITS',
            'Apply object scale before displacement so local distances are meters.')
    bpy.context.view_layer.update()
    basis = obj.matrix_world.to_3x3()
    columns = [basis.col[i] for i in range(3)]
    require(all(abs(column.length - 1.0) <= 1e-6 for column in columns) and
            all(abs(columns[i].dot(columns[j])) <= 1e-6 for i in range(3) for j in range(i)) and
            abs(basis.determinant() - 1.0) <= 1e-6,
            'LOOKDEV_DISPLACEMENT_UNITS', 'Apply effective parent scale/shear/reflection before displacement.')
    predicted = (len(obj.data.vertices) + len(obj.data.edges) + len(obj.data.polygons)) * 4**levels
    require(predicted <= max_vertices, 'LOOKDEV_DISPLACEMENT_LIMIT', 'Subdivision exceeds vertex budget.')
    require(image and image.has_data and 0 < image.size[0] * image.size[1] <= 16_777_216,
            'LOOKDEV_DISPLACEMENT_IMAGE', 'A bounded, decoded image is required.')
    for vertex in obj.data.vertices:
        require(all(math.isfinite(v) for v in vertex.co), 'LOOKDEV_DISPLACEMENT_GEOMETRY', 'Nonfinite source geometry.')
    temporary = obj.copy(); temporary.data = obj.data.copy()
    texture = bpy.data.textures.new('_hatch_displacement', type='IMAGE')
    new_mesh = None
    bpy.context.scene.collection.objects.link(temporary)
    try:
        texture.image = image
        texture.use_color_ramp = False
        texture.extension = 'REPEAT'
        if levels:
            subdivision = temporary.modifiers.new('_hatch_subdivision', 'SUBSURF')
            subdivision.subdivision_type = 'SIMPLE'
            subdivision.levels = levels; subdivision.render_levels = levels
        modifier = temporary.modifiers.new('_hatch_displacement', 'DISPLACE')
        modifier.texture = texture
        modifier.texture_coords = 'UV'
        modifier.uv_layer = temporary.data.uv_layers[0].name
        modifier.direction = 'NORMAL'
        modifier.mid_level = midlevel; modifier.strength = scale
        graph = bpy.context.evaluated_depsgraph_get()
        evaluated = temporary.evaluated_get(graph)
        new_mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=graph)
        require(0 < len(new_mesh.vertices) <= max_vertices, 'LOOKDEV_DISPLACEMENT_LIMIT', 'Evaluated geometry exceeds budget.')
        bounds = [[min(v.co[axis] for v in new_mesh.vertices), max(v.co[axis] for v in new_mesh.vertices)] for axis in range(3)]
        require(all(math.isfinite(n) and abs(n) <= max_extent_meters for axis in bounds for n in axis),
                'LOOKDEV_DISPLACEMENT_GEOMETRY', 'Displaced geometry exceeds finite spatial bounds.')
        require(all(p.area > 1e-15 for p in new_mesh.polygons),
                'LOOKDEV_DISPLACEMENT_GEOMETRY', 'Displacement produced degenerate polygons.')
        original = obj.data
        result = {'source_vertices': len(original.vertices), 'vertices': len(new_mesh.vertices),
                  'bounds_meters': bounds, 'scale_meters': scale, 'midlevel': midlevel,
                  'subdivision': levels, 'geometry_changed': (len(new_mesh.vertices) != len(original.vertices) or
                      any((a.co - b.co).length > 1e-9 for a, b in zip(new_mesh.vertices, original.vertices))),
                  'intersection_check': 'not-performed; solid intersection validation belongs to geometry owner'}
        obj.data = new_mesh
        new_mesh = None
        obj['hatch_displacement'] = content_hash(result)
        return result
    finally:
        source_copy = temporary.data
        bpy.data.objects.remove(temporary, do_unlink=True)
        if source_copy.users == 0:
            bpy.data.meshes.remove(source_copy)
        bpy.data.textures.remove(texture)
        if new_mesh is not None and new_mesh.users == 0:
            bpy.data.meshes.remove(new_mesh)


def gltf_material(plan):
    """Produce glTF 2.0 factor/extension data for the native geometry exporter.

    This does not replace the exporter or claim visual roundtrip verification.
    Geometry baking is required for true displacement and no SSS extension exists
    in this supported subset, so those requests fail instead of losing features.
    """
    s = _validate_plan(plan)
    require(not s.get('subsurface', {}).get('weight', 0), 'LOOKDEV_GLTF_UNSUPPORTED',
            'Subsurface scattering has no supported glTF mapping; retain the native material.')
    result = {'name': plan['reference']['id'], 'pbrMetallicRoughness': {
        'baseColorFactor': [*s['base_color'][:3], s['base_color'][3] * s['opacity']],
        'metallicFactor': s['metallic'], 'roughnessFactor': s['roughness']},
        'alphaMode': s['alpha_mode'], 'doubleSided': s['double_sided'],
        'extras': {'hatchLookdevReference': deepcopy(plan['reference']),
                   'hatchEffectiveSha256': plan['effective_sha256']}}
    if s['alpha_mode'] == 'MASK':
        result['alphaCutoff'] = s['alpha_cutoff']
    extensions = {}
    if s['ior'] != 1.5:
        extensions['KHR_materials_ior'] = {'ior': s['ior']}
    spec = s.get('specular', {})
    if spec:
        extensions['KHR_materials_specular'] = {'specularFactor': spec['strength'], 'specularColorFactor': spec['color']}
    if s.get('transmission', 0):
        extensions['KHR_materials_transmission'] = {'transmissionFactor': s['transmission']}
    coat = s.get('clearcoat', {})
    if coat.get('weight', 0):
        extensions['KHR_materials_clearcoat'] = {'clearcoatFactor': coat['weight'], 'clearcoatRoughnessFactor': coat['roughness']}
    sheen = s.get('sheen', {})
    if sheen.get('weight', 0):
        extensions['KHR_materials_sheen'] = {'sheenColorFactor': [x * sheen['weight'] for x in sheen['color']],
                                            'sheenRoughnessFactor': sheen['roughness']}
    anisotropy = s.get('anisotropy', {})
    if anisotropy.get('strength', 0):
        angle = anisotropy['rotation_radians'] + math.atan2(anisotropy['direction'][1], anisotropy['direction'][0])
        extensions['KHR_materials_anisotropy'] = {'anisotropyStrength': anisotropy['strength'], 'anisotropyRotation': angle}
    film = s.get('iridescence', {})
    if film.get('weight', 0):
        extensions['KHR_materials_iridescence'] = {'iridescenceFactor': film['weight'], 'iridescenceIor': film['ior'],
            'iridescenceThicknessMinimum': film['thickness_min_nanometers'],
            'iridescenceThicknessMaximum': film['thickness_max_nanometers']}
    emit = s.get('emission', {})
    if emit.get('strength_watts_per_square_meter', 0):
        result['emissiveFactor'] = emit['color']
        extensions['KHR_materials_emissive_strength'] = {'emissiveStrength': emit['strength_watts_per_square_meter']}
    if extensions:
        result['extensions'] = extensions
    return result
