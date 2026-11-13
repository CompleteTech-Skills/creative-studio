"""Canonical world-space color projection through the shared texture owners.

Returns shader sockets, never a second material or asset store. Tangent-space
normal maps deliberately reject: blending normals requires separate reorientation.
"""
import hashlib
import math
from pathlib import Path

from lookdev_contracts import content_hash
from lookdev_texture_sampling import sampler_plan, configure_blender_sampler
from lookdev_texture_blender import create_cached_blender_image
from lookdev_textures import COLOR_ROLES, verify_texture_document
from rig_errors import require

_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _number(value, low, high, label):
    # Bound Python integers before any conversion, including math.isfinite.
    require(type(value) in (int, float) and low <= value <= high,
            'TEXTURE_PROJECTION', 'Expected bounded ' + label + '.')
    require(math.isfinite(value), 'TEXTURE_PROJECTION', 'Expected finite ' + label + '.')
    return float(value)


def triplanar_plan(document, *, scale=1.0, sharpness=4.0):
    plan = sampler_plan(document)
    require(document['role'] in COLOR_ROLES, 'TEXTURE_PROJECTION_UNSUPPORTED',
            'Triplanar color projection accepts color textures only; tangent normals require reorientation.')
    plan.update(projection='canonical-world-triplanar',
                scale_per_meter=_number(scale, 1e-9, 1e9, 'projection scale'),
                sharpness=_number(sharpness, 1, 64, 'projection sharpness'),
                adapter_source_sha256=_SOURCE_SHA256)
    return plan


def create_triplanar_color(node_tree, document, payload, cache, *, name='Triplanar',
                           profile='browser', scale=1.0, sharpness=4.0):
    """Verify original bytes, install one cached image, return Color/Alpha/receipt.

    Coordinates are world meters in the canonical Y-up basis. Both position and
    world shading normals use the authoritative rig basis adapter. Scale changes
    projected repeat density; no implicit generated bounds or object transforms
    can change source identity. Caller owns material assignment and returned nodes.
    """
    plan = triplanar_plan(document, scale=scale, sharpness=sharpness)
    require(isinstance(name, str) and 0 < len(name) <= 100,
            'TEXTURE_NAME', 'Expected bounded projection name.')
    require(getattr(node_tree, 'bl_idname', None) == 'ShaderNodeTree',
            'TEXTURE_PROJECTION', 'Expected a native shader node tree.')
    texture = verify_texture_document(document, payload)
    # Optional native/numeric dependencies remain inside executed 3D operations.
    import bpy
    from rig_math import BLENDER_FROM_CANONICAL
    basis = BLENDER_FROM_CANONICAL[:3, :3].T
    nodes, links = node_tree.nodes, node_tree.links
    created = []
    image = None

    def node(kind):
        value = nodes.new(kind)
        value.label = name
        created.append(value)
        return value

    def connect(value, socket):
        if isinstance(value, (int, float)):
            socket.default_value = value
        else:
            links.new(value, socket)

    def math_node(operation, a, b=0):
        value = node('ShaderNodeMath')
        value.operation = operation
        connect(a, value.inputs[0]); connect(b, value.inputs[1])
        return value.outputs[0]

    def vector(values):
        value = node('ShaderNodeCombineXYZ')
        for item, socket in zip(values, value.inputs):
            connect(item, socket)
        return value.outputs[0]

    def components(socket):
        value = node('ShaderNodeSeparateXYZ')
        links.new(socket, value.inputs[0])
        return tuple(value.outputs)

    def canonical(socket):
        result = []
        for row in basis:
            value = node('ShaderNodeVectorMath'); value.operation = 'DOT_PRODUCT'
            links.new(socket, value.inputs[0]); value.inputs[1].default_value = tuple(row)
            result.append(value.outputs['Value'])
        return result

    try:
        image, derivation = create_cached_blender_image(cache, texture, name=name, profile=profile)
        geometry = node('ShaderNodeNewGeometry')
        position = canonical(geometry.outputs['Position'])
        normal = canonical(geometry.outputs['Normal'])
        # For sign(0), use +1 as the reference coordinate authority does.
        signs = [math_node('SUBTRACT', 1, math_node('MULTIPLY',
                 math_node('LESS_THAN', component, 0), 2)) for component in normal]
        uv = [(math_node('MULTIPLY', position[1], signs[0]), position[2]),
              (math_node('MULTIPLY', math_node('MULTIPLY', position[0], -1), signs[1]), position[2]),
              (math_node('MULTIPLY', position[0], signs[2]), position[1])]
        # Unit world normals have at least one component >= 1/sqrt(3), so the
        # bounded power cannot underflow all weights. Normalizing by max first
        # also matches the CPU authority and remains stable at sharpness 64.
        absolute = [math_node('ABSOLUTE', component) for component in normal]
        maximum = math_node('MAXIMUM', math_node('MAXIMUM', absolute[0], absolute[1]), absolute[2])
        powers = [math_node('POWER', math_node('DIVIDE', component, maximum), plan['sharpness'])
                  for component in absolute]
        total = math_node('ADD', math_node('ADD', powers[0], powers[1]), powers[2])
        weights = [math_node('DIVIDE', component, total) for component in powers]
        colors, alphas = [], []
        for coordinates in uv:
            sample = node('ShaderNodeTexImage'); sample.image = image
            configure_blender_sampler(sample, document)
            mapped = vector([math_node('MULTIPLY', item, plan['scale_per_meter'])
                             for item in coordinates] + [0])
            links.new(mapped, sample.inputs['Vector'])
            colors.append(components(sample.outputs['Color']))
            alphas.append(sample.outputs['Alpha'])
        blended = []
        for channel in range(3):
            terms = [math_node('MULTIPLY', colors[axis][channel], weights[axis]) for axis in range(3)]
            blended.append(math_node('ADD', math_node('ADD', terms[0], terms[1]), terms[2]))
        terms = [math_node('MULTIPLY', alphas[axis], weights[axis]) for axis in range(3)]
        alpha = math_node('ADD', math_node('ADD', terms[0], terms[1]), terms[2])
        color = vector(blended)
        receipt = dict(plan, document_sha256=content_hash(document), derivation=derivation,
                       canonical_from_blender=basis.tolist(), owned_nodes=len(created),
                       alpha='weighted-straight', tangent_normal_reorientation=False)
        return color, alpha, receipt
    except BaseException:
        for value in reversed(created):
            nodes.remove(value)
        if image is not None:
            bpy.data.images.remove(image)
        raise
