"""Typed property admission through Scene3D, Camera3D and LookDev owners.

These adapters resolve real persisted objects. They do not add a second native
scene evaluator or imply that all declared target domains have an executor.
"""
from copy import deepcopy
from animation3d_assets import verify_native
from rig_errors import require


def resolve_property(root, entry):
    target, source = entry['target'], entry['source']
    kind, native = verify_native(root, source)
    domain, prop = target['kind'], target['property']
    owner = entry['owner']
    if owner == 'scene3d':
        require(kind == 'scene3d-project' and domain == 'node', 'ANIMATION_OWNER',
                'Scene3D adapter requires a native project and node target.')
        matches = [node for doc in native['documents'].values() if doc['kind']=='scene3d-scene'
                   for node in doc['nodes'] if node['id']==target['node_id']]
        require(len(matches)==1, 'ANIMATION_TARGET', 'Scene node is absent or ambiguous across scenes.')
        node = matches[0]
        return deepcopy(node['visible'] if prop=='visibility' else node['transform'][prop])
    if owner == 'camera3d':
        require(kind == 'camera3d-registry' and domain == 'camera', 'ANIMATION_OWNER',
                'Camera3D adapter requires its registry and camera target.')
        matches = [d for d in native.values() if d['kind']=='camera3d-camera' and d['id']==target['camera_id']]
        require(len(matches)==1, 'ANIMATION_TARGET', 'Camera is absent or ambiguous.')
        camera = matches[0]
        if prop in ('translation','rotation'): return deepcopy(camera['transform'][prop])
        optics = native[(camera['optics']['kind'],camera['optics']['id'],camera['optics']['version'])]
        if prop in ('focus_distance','aperture'):
            return optics['depth_of_field'][{'focus_distance':'focus_distance_m','aperture':'f_stop'}[prop]]
        fields = {'focal_length':'focal_length_mm','orthographic_size':'orthographic_height_m'}
        require(prop in fields and fields[prop] in optics['projection'], 'ANIMATION_PROPERTY',
                'Camera property has no native representation for this projection.')
        return optics['projection'][fields[prop]]
    if owner == 'lookdev':
        reference = native.reference(source['id'], source['version'])
        if domain == 'material':
            require(kind in ('material3d','material-instance3d','material-variant3d') and
                    target['material_id']==source['id'], 'ANIMATION_TARGET', 'Material target differs from native source.')
            surface = native.effective_material(reference)['surface']
            if prop in ('emissive_color','emissive_intensity'):
                require('emission' in surface, 'ANIMATION_PROPERTY', 'Material has no authored emission property.')
                return deepcopy(surface['emission'][{'emissive_color':'color','emissive_intensity':'strength_watts_per_square_meter'}[prop]])
            require(prop in surface, 'ANIMATION_PROPERTY', 'Material property is unavailable.')
            return deepcopy(surface[prop])
        require(domain=='light' and kind=='light3d' and target['light_id']==source['id'],
                'ANIMATION_TARGET', 'Light target differs from native source.')
        light = native.get(reference)
        fields = {'translation':('transform','translation_meters'), 'rotation':('transform','rotation_xyzw'),
                  'color':('color','value'), 'intensity':('intensity','value')}
        if prop=='cone_angle':
            require(light['type']=='spot', 'ANIMATION_PROPERTY', 'Cone angle requires a spot light.')
            return light['spot_outer_radians']
        require(prop in fields, 'ANIMATION_PROPERTY', 'Light property has no native representation.')
        outer, inner = fields[prop]
        return deepcopy(light[outer][inner])
    require(False, 'ANIMATION_OWNER', 'Unknown property owner: '+owner)
