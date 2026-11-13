"""Physical light/world mapping for the canonical LookDev contracts.

Plans are pure data and contain source hashes. Execution is callable only inside
Blender 4.5 LTS; scene ownership, scheduling and publication remain with Render3D.
No optional dependency is imported merely by importing this module.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re

from lookdev_contracts import canonical_bytes, content_hash, portable_path, validate_resource
from rig_errors import RigError, finite_json, require

VERSION = '1.0.0'
ENGINES = ('CYCLES', 'BLENDER_EEVEE_NEXT')
# Explicit photometric reference convention. RGB rendering cannot reproduce a
# complete spectrum; this is a 555 nm equivalent, not lamp electrical wattage.
LUMINOUS_EFFICACY = 683.0
WEIGHTS = (0.2126, 0.7152, 0.0722)
LOCAL_FEATURES = frozenset(('light-point', 'light-spot', 'light-directional',
    'light-area', 'shadows', 'temperature-color'))


def _number(value, lo, hi, label):
    require(type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi,
            'LIGHT_VALUE', label + ' is outside its finite supported range.')
    return float(value)


def _rgb(value, label='color'):
    require(isinstance(value, (list, tuple)) and len(value) == 3,
            'LIGHT_COLOR', label + ' needs three linear components.')
    return [_number(v, 0, 1, label) for v in value]


def _linear(value):
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    return sum(a * b for a, b in zip(WEIGHTS, rgb))


def temperature_color(kelvin):
    """Planck spectrum integrated against smooth CIE 1931 matching functions.

    The compact analytic CIE approximations (Wyman et al., JCGT 2013) and gamut
    clipping make this explicitly a blackbody-inspired RGB approximation. Output
    is linear-sRGB normalized to maximum component 1, not gamma-encoded color.
    """
    kelvin = _number(kelvin, 1000, 40000, 'temperature kelvin')
    xyz = [0.0, 0.0, 0.0]
    def gaussian(wavelength, center, left, right):
        a = (wavelength - center) * (left if wavelength < center else right)
        return math.exp(-0.5 * a * a)
    for nm in range(380, 781, 5):
        meters = nm * 1e-9
        radiance = 1.0 / (meters**5 * math.expm1(0.01438776877 / (meters * kelvin)))
        match = (1.056 * gaussian(nm, 599.8, .0264, .0323)
                 + .362 * gaussian(nm, 442.0, .0624, .0374)
                 - .065 * gaussian(nm, 501.1, .0490, .0382),
                 .821 * gaussian(nm, 568.8, .0213, .0247)
                 + .286 * gaussian(nm, 530.9, .0613, .0322),
                 1.217 * gaussian(nm, 437.0, .0845, .0278)
                 + .681 * gaussian(nm, 459.0, .0385, .0725))
        for i in range(3):
            xyz[i] += radiance * match[i]
    x, y, z = (a / xyz[1] for a in xyz)
    rgb = [max(0.0, 3.2406*x - 1.5372*y - .4986*z),
           max(0.0, -.9689*x + 1.8758*y + .0415*z),
           max(0.0, .0557*x - .2040*y + 1.0570*z)]
    scale = max(rgb)
    return [a / scale for a in rgb]


def _options(options, kind):
    options = {} if options is None else deepcopy(options)
    require(isinstance(options, dict), 'LIGHT_OPTIONS', 'Options must be an object.')
    allowed = {'enabled', 'temperature_mode', 'tint', 'area_shape', 'sun_angle_radians',
               'diffuse', 'specular', 'volume', 'receivers', 'excluded_receivers',
               'camera_visible', 'exposure_stops'}
    require(not set(options) - allowed, 'LIGHT_OPTIONS', 'Unknown light execution options.')
    result = {'enabled': options.get('enabled', True),
              'temperature_mode': options.get('temperature_mode', 'multiply'),
              'tint': _rgb(options.get('tint', [1., 1., 1.]), 'tint'),
              'diffuse': options.get('diffuse', True), 'specular': options.get('specular', True),
              'volume': options.get('volume', True),
              'receivers': options.get('receivers', []),
              'excluded_receivers': options.get('excluded_receivers', []),
              'camera_visible': options.get('camera_visible', True),
              'exposure_stops': _number(options.get('exposure_stops', 0), -32, 32, 'exposure')}
    require(result['temperature_mode'] in ('multiply', 'replace', 'ignore'),
            'LIGHT_OPTIONS', 'Temperature policy must be multiply, replace or ignore.')
    for key in ('enabled', 'diffuse', 'specular', 'volume', 'camera_visible'):
        require(type(result[key]) is bool, 'LIGHT_OPTIONS', key + ' must be boolean.')
    for key in ('receivers', 'excluded_receivers'):
        names = result[key]
        require(isinstance(names, list) and len(names) <= 1024
                and all(isinstance(s, str) and 0 < len(s) <= 128 for s in names)
                and len(names) == len(set(names)), 'LIGHT_LINKING', 'Invalid bounded receiver identity list.')
    require(not set(result['receivers']) & set(result['excluded_receivers']),
            'LIGHT_LINKING', 'A receiver cannot be both included and excluded.')
    if 'area_shape' in options:
        require(kind == 'area' and options['area_shape'] in ('RECTANGLE', 'DISK', 'ELLIPSE'),
                'LIGHT_OPTIONS', 'Area shape requires an area light.')
        result['area_shape'] = options['area_shape']
    if 'sun_angle_radians' in options:
        require(kind == 'directional', 'LIGHT_OPTIONS', 'Angular size requires a sun.')
        result['sun_angle_radians'] = _number(options['sun_angle_radians'], 0, math.pi,
                                             'sun angular diameter')
    if kind != 'environment':
        require(result['camera_visible'] is True and result['exposure_stops'] == 0,
                'LIGHT_OPTIONS', 'Background visibility and exposure require an environment.')
    if kind == 'environment':
        require(not result['receivers'] and not result['excluded_receivers'],
                'LIGHT_LINKING', 'World light linking is not supported by this adapter.')
    return result


def _color(light, options):
    rgb = list(light['color']['value'])
    if light['color']['encoding'] == 'srgb':
        rgb = [_linear(v) for v in rgb]
    if 'temperature_kelvin' in light and options['temperature_mode'] != 'ignore':
        blackbody = temperature_color(light['temperature_kelvin'])
        rgb = blackbody if options['temperature_mode'] == 'replace' else [a*b for a,b in zip(rgb, blackbody)]
    rgb = [a*b for a,b in zip(rgb, options['tint'])]
    return rgb


def light_plan(registry, reference, *, engine='CYCLES', options=None):
    """Resolve an exact canonical light into a bounded, hash-bound build plan."""
    require(engine in ENGINES, 'LIGHT_ENGINE', 'Choose Cycles or EEVEE.')
    light = registry.get(reference)
    require(light['kind'] == 'light3d', 'LIGHT_KIND', 'Expected a canonical light reference.')
    validate_resource(light)
    kind = light['type']
    opts = _options(options, kind)
    from lookdev_contracts import check_compatibility
    features = LOCAL_FEATURES | ({'light-environment', 'indirect-lighting', 'light-linking'}
                                 if engine == 'CYCLES' else set())
    compatibility = check_compatibility(light, features)
    require(not compatibility['missing'], 'LIGHT_UNSUPPORTED',
            'Required light features are unsupported: ' + ', '.join(compatibility['missing']))
    require(engine == 'CYCLES' or not (opts['receivers'] or opts['excluded_receivers']),
            'LIGHT_UNSUPPORTED', 'Light linking is verified only in Cycles.')
    rgb = _color(light, opts)
    power = light['intensity']['value'] if opts['enabled'] else 0.0
    # Normalize chromaticity to Y=1 while converting cd/lux to the explicit
    # 555-nm equivalent radiant convention; black remains physically unlit.
    y = luminance(rgb)
    if light['intensity']['unit'] in ('candela', 'lux'):
        rgb = [a / y for a in rgb] if y > 0 else [0., 0., 0.]
        energy = power / LUMINOUS_EFFICACY * (4 * math.pi if kind in ('point', 'spot') else 1)
    else:
        energy = power
    require(math.isfinite(energy) and energy <= 1e9, 'LIGHT_POWER', 'Converted light energy exceeds budget.')
    plan = {'adapter_version': VERSION, 'engine': engine, 'reference': deepcopy(reference),
            'light': light, 'options': opts, 'linear_color': rgb, 'energy': energy,
            'units': '555nm-equivalent-radiant-watt' if kind in ('point', 'spot') else
                     '555nm-equivalent-watt-per-square-meter' if kind == 'directional' else
                     'radiant-watt' if kind == 'area' else 'reference-mean-planar-watt-per-square-meter'}
    if kind == 'environment':
        texture = registry.get(light['environment'])
        require(texture['role'] == 'environment' and texture['encoding'] == 'linear-srgb',
                'LIGHT_ENVIRONMENT', 'HDR environments require scene-linear RGB data.')
        require(texture['width'] == texture['height'] * 2,
                'LIGHT_ENVIRONMENT', 'The environment must be a 2:1 equirectangular image.')
        plan['texture'] = texture
    plan['sha256'] = content_hash(plan)
    return plan


def _validated(plan):
    require(isinstance(plan, dict) and set(plan) in (
        {'adapter_version','engine','reference','light','options','linear_color','energy','units','sha256'},
        {'adapter_version','engine','reference','light','options','linear_color','energy','units','sha256','texture'}),
        'LIGHT_PLAN', 'Invalid light plan fields.')
    finite_json(plan)
    require(plan['adapter_version'] == VERSION and plan['engine'] in ENGINES,
            'LIGHT_PLAN', 'Unsupported adapter version or engine.')
    require(content_hash({k:v for k,v in plan.items() if k != 'sha256'}) == plan['sha256'],
            'LIGHT_PLAN', 'Light plan has changed.')
    validate_resource(plan['light'])
    _options(plan['options'], plan['light']['type'])
    require(type(plan['energy']) in (int, float) and 0 <= plan['energy'] <= 1e9,
            'LIGHT_PLAN', 'Invalid converted energy.')
    require(len(plan['linear_color']) == 3 and all(type(v) in (int,float) and 0 <= v <= 20 for v in plan['linear_color']),
            'LIGHT_PLAN', 'Invalid linear chromaticity.')
    # A digest detects accidental edits, not semantic validity: callers can
    # recompute it. Rebuild all derived values from the admitted source records.
    class PlanSources:
        def get(self, reference):
            if reference == plan['reference']:
                value = plan['light']
            else:
                require(plan['light']['type'] == 'environment' and
                        reference == plan['light']['environment'] and 'texture' in plan,
                        'LIGHT_PLAN', 'Unexpected plan resource reference.')
                value = plan['texture']
            validate_resource(value)
            require(isinstance(reference, dict) and
                    set(reference) == {'id', 'version', 'kind', 'path', 'sha256'} and
                    all(reference.get(k) == value[k] for k in ('id', 'version', 'kind')) and
                    isinstance(reference['sha256'], str) and
                    re.fullmatch('[0-9a-f]{64}', reference['sha256']) is not None,
                    'LIGHT_PLAN', 'Plan reference must retain its exact source identity and digest.')
            portable_path(reference['path'])
            return deepcopy(value)
    expected = light_plan(PlanSources(), plan['reference'], engine=plan['engine'],
                          options=plan['options'])
    require(expected == plan, 'LIGHT_PLAN', 'Plan derived values disagree with source records.')
    return plan


def _bpy(engine):
    try:
        import bpy
    except ImportError as exc:
        raise RigError('LIGHT_BLENDER_REQUIRED', 'Execute in Blender 4.5 LTS.') from exc
    require(bpy.app.version[:2] == (4, 5), 'LIGHT_BLENDER_VERSION', 'Verified backend is Blender 4.5 LTS.')
    require(bpy.context.scene.render.engine == engine, 'LIGHT_ENGINE', 'Scene and plan engines differ.')
    return bpy


def probe():
    """Return live RNA capabilities; no successful render is implied."""
    bpy = _bpy('CYCLES')
    light = bpy.data.lights.new('_hatch_probe', 'POINT')
    obj = bpy.data.objects.new('_hatch_probe', light)
    try:
        return {'adapter': VERSION, 'blender': bpy.app.version_string,
                'point_normalize': hasattr(light, 'normalize'),
                'light_linking': hasattr(obj, 'light_linking'),
                'engines': list(ENGINES), 'executed': False,
                'unsupported': ['browser adapter', 'world light linking', 'arbitrary spectral photometry']}
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.lights.remove(light)


def build_light(plan, *, collection=None, receivers=None):
    """Build a real light without touching any pre-existing object or material.

    ``receivers`` maps stable scene IDs to already-owned bpy objects. Include and
    exclude lists are compiled into a private Cycles receiver collection.
    """
    plan = _validated(plan)
    light, opts = plan['light'], plan['options']
    require(light['type'] != 'environment', 'LIGHT_KIND', 'Use build_environment for world lights.')
    bpy = _bpy(plan['engine'])
    receivers = {} if receivers is None else receivers
    wanted = set(opts['receivers']) | set(opts['excluded_receivers'])
    require(wanted <= set(receivers), 'LIGHT_LINKING', 'A requested receiver identity is missing.')
    kind = {'point':'POINT', 'spot':'SPOT', 'directional':'SUN', 'area':'AREA'}[light['type']]
    data = bpy.data.lights.new('hatch:' + light['id'], kind)
    obj = bpy.data.objects.new('hatch:' + light['id'], data)
    linking = None
    try:
        (collection or bpy.context.scene.collection).objects.link(obj)
        # Lamps use local -Z as optical axis, like cameras: convert world basis
        # only, rather than conjugating the lamp-local coordinate convention.
        from rig_math import compose
        from camera3d_math import blender_camera_transform
        from mathutils import Matrix
        transform = light['transform']
        obj.matrix_world = Matrix(blender_camera_transform(compose(
            transform['translation_meters'], transform['rotation_xyzw'], [1.,1.,1.])).tolist())
        data.normalize = True
        data.energy = plan['energy']
        data.color = plan['linear_color']
        data.use_temperature = False
        data.use_shadow = light['casts_shadow']
        data.diffuse_factor = float(opts['diffuse'])
        data.specular_factor = float(opts['specular'])
        data.volume_factor = float(opts['volume'])
        if kind in ('POINT', 'SPOT'):
            data.shadow_soft_size = light['radius_meters']
        if kind == 'SPOT':
            data.spot_size = light['spot_outer_radians']
            outer_cos = math.cos(light['spot_outer_radians'] / 2)
            inner_cos = math.cos(light['spot_inner_radians'] / 2)
            data.spot_blend = (inner_cos - outer_cos) / (1 - outer_cos) if outer_cos < 1 else 0
        if kind == 'SUN':
            data.angle = opts.get('sun_angle_radians', 0.00918043)
        if kind == 'AREA':
            data.shape = opts.get('area_shape', 'RECTANGLE')
            data.size, data.size_y = light['area_size_meters']
            require(data.shape != 'DISK' or abs(data.size-data.size_y) < 1e-9,
                    'LIGHT_SHAPE', 'A disk needs equal diameters; use ellipse otherwise.')
        if wanted:
            linking = bpy.data.collections.new('hatch:receivers:' + light['id'])
            for identifier in opts['receivers']:
                linking.objects.link(receivers[identifier])
            for identifier in opts['excluded_receivers']:
                target = receivers[identifier]
                linking.objects.link(target)
                index = next(i for i, member in enumerate(linking.objects) if member == target)
                linking.collection_objects[index].light_linking.link_state = 'EXCLUDE'
            obj.light_linking.receiver_collection = linking
        obj['hatch_light_plan_sha256'] = plan['sha256']
        obj['hatch_light_source_sha256'] = plan['reference']['sha256']
        return obj
    except Exception:
        bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.lights.remove(data)
        if linking is not None:
            bpy.data.collections.remove(linking)
        raise


def environment_strength(pixels, irradiance, color=(1., 1., 1.)):
    """Normalize to mean planar irradiance over all surface orientations.

    E_ref = pi * spherical-mean radiance. Latitude rows have exact integrated
    solid-angle weights; rescaling the authored HDR cannot change the watts.
    RGB radiant power uses the declared luminance-weighted RGB convention.
    """
    import numpy as np
    pixels = np.asarray(pixels)
    require(pixels.ndim == 3 and pixels.shape[2] >= 3 and
            pixels.shape[0] > 0 and pixels.shape[1] == 2*pixels.shape[0] and
            np.isfinite(pixels).all() and (pixels >= 0).all(),
            'LIGHT_ENVIRONMENT', 'Expected finite nonnegative equirectangular pixels.')
    irradiance = _number(irradiance, 0, 1e9, 'environment irradiance')
    color = _rgb(color)
    edges = np.linspace(0., math.pi, pixels.shape[0]+1)
    weights = np.cos(edges[:-1]) - np.cos(edges[1:])
    rgb = pixels[:,:,:3].astype(np.float64)
    radiance = (rgb * np.asarray(color) * np.asarray(WEIGHTS)).sum(axis=2)
    reference = math.pi * float((radiance.mean(axis=1) * weights).sum()/2.)
    if irradiance == 0:
        return 0.
    require(reference > 0, 'LIGHT_ENVIRONMENT', 'A black HDR cannot provide nonzero irradiance.')
    strength = irradiance/reference
    require(math.isfinite(strength) and strength <= 1e12,
            'LIGHT_ENVIRONMENT', 'Environment normalization exceeds supported intensity range.')
    return strength


def build_environment(plan, *, payload):
    """Create a new packed world from exact bytes decoded by the texture owner.

    Returns the world without assigning scene.world. The caller can publish that
    change transactionally. No path, URL or Blender source file is accepted.
    """
    plan = _validated(plan)
    require(plan['light']['type'] == 'environment', 'LIGHT_KIND', 'Expected a world light plan.')
    bpy = _bpy(plan['engine'])
    require(isinstance(payload, bytes), 'LIGHT_ENVIRONMENT', 'Environment must be exact source bytes.')
    import hashlib
    file = plan['texture']['file']
    require(len(payload) == file['bytes'] and hashlib.sha256(payload).hexdigest() == file['sha256'],
            'LIGHT_ENVIRONMENT', 'Environment payload no longer matches its immutable source.')
    from lookdev_textures import decode_texture, create_blender_image
    decoded = decode_texture(payload, role='environment', encoding='linear-srgb')
    texture = plan['texture']
    require(decoded.width == texture['width'] and decoded.height == texture['height'],
            'LIGHT_ENVIRONMENT', 'Decoded environment dimensions disagree with the manifest.')
    require(decoded.source_sha256 == file['sha256'], 'LIGHT_ENVIRONMENT', 'Decoder source identity mismatch.')
    strength = environment_strength(decoded.pixels, plan['energy'], plan['linear_color'])
    image = create_blender_image(decoded, name='hatch:' + texture['id'])
    world = bpy.data.worlds.new('hatch:' + plan['light']['id'])
    try:
        world.use_nodes = True
        nodes, links = world.node_tree.nodes, world.node_tree.links
        nodes.clear()
        coordinates = nodes.new('ShaderNodeTexCoord')
        # A Mapping node can apply an arbitrary canonical orientation. Convert
        # the world quaternion with the existing authoritative basis adapter.
        from rig_math import compose
        from scene3d_math import to_blender
        from mathutils import Matrix
        matrix = Matrix(to_blender(compose([0.,0.,0.], plan['light']['transform']['rotation_xyzw'], [1.,1.,1.])).tolist())
        mapping = nodes.new('ShaderNodeMapping'); mapping.vector_type = 'VECTOR'
        mapping.inputs['Rotation'].default_value = matrix.inverted().to_euler()
        environment = nodes.new('ShaderNodeTexEnvironment'); environment.image = image
        environment.interpolation = 'Linear'; environment.projection = 'EQUIRECTANGULAR'
        multiply = nodes.new('ShaderNodeMixRGB'); multiply.blend_type = 'MULTIPLY'
        multiply.inputs[0].default_value = 1.; multiply.inputs[2].default_value = (*plan['linear_color'],1.)
        background = nodes.new('ShaderNodeBackground')
        background.inputs['Strength'].default_value = strength * 2**plan['options']['exposure_stops']
        output = nodes.new('ShaderNodeOutputWorld')
        links.new(coordinates.outputs['Generated'], mapping.inputs['Vector'])
        links.new(mapping.outputs['Vector'], environment.inputs['Vector'])
        links.new(environment.outputs['Color'], multiply.inputs[1])
        links.new(multiply.outputs['Color'], background.inputs['Color'])
        if plan['options']['camera_visible']:
            links.new(background.outputs[0], output.inputs[0])
        else:
            path = nodes.new('ShaderNodeLightPath')
            transparent = nodes.new('ShaderNodeBackground'); transparent.inputs['Strength'].default_value = 0.
            mix = nodes.new('ShaderNodeMixShader')
            links.new(path.outputs['Is Camera Ray'], mix.inputs[0])
            links.new(background.outputs[0], mix.inputs[1]); links.new(transparent.outputs[0], mix.inputs[2])
            links.new(mix.outputs[0], output.inputs[0])
        # Cycles evaluates independent path visibility, preserving world
        # illumination when the camera background is hidden.
        for field, option in (('diffuse','diffuse'),('glossy','specular'),('scatter','volume')):
            require(hasattr(world, 'cycles_visibility') and hasattr(world.cycles_visibility, field),
                    'LIGHT_WORLD_VISIBILITY', 'Backend lacks requested world path visibility.')
            setattr(world.cycles_visibility, field, plan['options'][option])
        world['hatch_light_plan_sha256'] = plan['sha256']
        world['hatch_environment_source_sha256'] = file['sha256']
        return world
    except Exception:
        bpy.data.worlds.remove(world)
        bpy.data.images.remove(image)
        raise


PRESET_VERSION = '1.0.0'
# Positions are meters in canonical Y-up space. Power is radiometric W for areas;
# directional and point powers retain the canonical lux/candela convention.
_PRESETS = {
    'neutral-studio': (('area',(-3,4,4),1000,(1,1,1),3), ('area',(3,2,2),350,(1,1,1),3)),
    'three-point-studio': (('area',(-3,4,3),1000,(1,.9,.8),2), ('area',(3,2,2),300,(.8,.9,1),3), ('area',(0,3,-3),900,(1,1,1),2)),
    'soft-product': (('area',(-3,4,2),1200,(1,1,1),5), ('area',(3,3,0),800,(1,1,1),4)),
    'hard-dramatic': (('area',(-3,4,2),1000,(1,1,1),.1),),
    'cinematic-warm': (('area',(-3,4,3),1100,(1,.6,.3),2), ('area',(3,2,1),150,(.4,.6,1),3)),
    'cool-moonlight': (('directional',(-3,5,-2),300,(.45,.65,1),0), ('area',(0,2,4),25,(.3,.4,1),4)),
    'golden-hour': (('directional',(-4,1,3),3000,(1,.55,.22),0), ('area',(0,4,0),100,(.4,.6,1),5)),
    'overcast-daylight': (('area',(0,7,0),3000,(.85,.92,1),12),),
    'interior-practical': (('point',(-1,2,1),600,(1,.65,.35),.04), ('point',(2,2,-1),350,(1,.65,.35),.04)),
    'neon-cyberpunk': (('area',(-2,2,1),700,(1,.01,.4),1), ('area',(2,2,-1),700,(.01,.6,1),1)),
    'fantasy-magical': (('point',(0,1.5,2),800,(.4,.1,1),.15), ('area',(2,3,-2),700,(.1,1,.6),2)),
    'low-key-noir': (('area',(-3,3,1),900,(1,1,1),.25), ('area',(2,2,-2),300,(1,1,1),.5)),
    'high-key-commercial': (('area',(-3,4,2),1500,(1,1,1),4), ('area',(3,4,2),1500,(1,1,1),4), ('area',(0,4,-2),1000,(1,1,1),3)),
    'toon-illustrative': (('directional',(-3,5,4),2000,(1,1,1),0),),
    'clay-render': (('area',(-3,4,3),1200,(1,.95,.9),4), ('area',(3,2,0),300,(.8,.9,1),3)),
    'product-turntable': (('area',(-3,4,2),1000,(1,1,1),3), ('area',(3,3,2),700,(1,1,1),3), ('area',(0,4,-3),700,(1,1,1),2)),
}


def lighting_preset(name):
    """Return actual canonical light documents, never material overrides.

    Toon and clay names describe the *illumination* only. Quantized toon shaders
    and clay materials are applied by their material/style owners, not fabricated
    by modifying a character's canonical palette here.
    """
    require(name in _PRESETS, 'LIGHT_PRESET', 'Unknown lighting preset.')
    from camera3d_math import look_at
    result = []
    for i, (kind, position, power, color, size) in enumerate(_PRESETS[name]):
        q = look_at(position, (0, 1, 0), up=(0,0,1) if position[0] == position[2] == 0 else (0,1,0)).tolist()
        light = dict(schema_version=1, id=name+'-'+str(i), version=PRESET_VERSION,
                     kind='light3d', dependencies=[], capabilities=[], validation_status='unvalidated',
                     provenance={'origin':'authored','tool':'lookdev_lighting '+VERSION,
                         'created_at':'2026-11-13T00:00:00Z','source_hashes':[],
                         'attribution':'Project-authored lighting preset',
                         'license':{'identifier':'project-license','source':'repository LICENSE.txt'}},
                     type=kind, transform={'translation_meters':list(position),'rotation_xyzw':q},
                     color={'encoding':'linear-srgb','value':list(color)},
                     intensity={'value':power,'unit':{'area':'watt','point':'candela','directional':'lux'}[kind]},
                     casts_shadow=True, render_policy={'strict':True,'allow_approximations':False,
                         'required_features':[],'fallback':'reject'})
        if kind == 'area': light['area_size_meters'] = [size,size]
        if kind == 'point': light['radius_meters'] = size
        result.append(validate_resource(light))
    return {'preset':name, 'version':PRESET_VERSION, 'lights':result,
            'sha256':content_hash(result), 'material_mutations':False}


def preset_names():
    return tuple(_PRESETS)


def rig_plan(registry, reference, *, engine='CYCLES', options=None, max_lights=64):
    """Preflight an entire canonical lighting rig before any scene mutation."""
    require(type(max_lights) is int and 1 <= max_lights <= 256, 'LIGHT_LIMIT', 'Invalid light count budget.')
    rig = registry.get(reference)
    require(rig['kind'] == 'lighting-rig3d', 'LIGHT_KIND', 'Expected a lighting rig.')
    require(len(rig['lights']) <= max_lights, 'LIGHT_LIMIT', 'Lighting rig exceeds active-light budget.')
    options = {} if options is None else options
    require(isinstance(options, dict) and not set(options) - {r['id'] for r in rig['lights']},
            'LIGHT_OPTIONS', 'Options refer to an unknown light.')
    plans = [light_plan(registry, ref, engine=engine, options=options.get(ref['id'])) for ref in rig['lights']]
    require(sum(p['light']['type'] == 'environment' for p in plans) <= 1,
            'LIGHT_ENVIRONMENT', 'Only one authoritative world environment can be applied.')
    result = {'reference':deepcopy(reference),'lights':plans,'engine':engine,'adapter_version':VERSION}
    result['sha256'] = content_hash(result)
    return result
