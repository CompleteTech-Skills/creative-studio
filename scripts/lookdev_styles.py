"""Versioned 3D style recipes, semantic identity bindings and isolated look state.

The existing style registry owns artistic IDs and frozen source instructions.
This module maps selected IDs to executable material/geometry recipes; Blender
and animation execution stay with their authoritative adapters.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re

from lookdev_contracts import canonical_bytes, content_hash, default_surface, validate_resource, definitions
from rig_errors import require, finite_json, RigError
from style_registry import resolve_style, validate_snapshot

VERSION = '1.0.0'
_IDS = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')

# Values are actual linear-space shader parameters. Recipe geometry remains
# explicit: the executor must apply it or reject the mapping, never drop it.
_RECIPES = {
 '3d-toy': {'roughness': .24, 'clearcoat': {'weight': .22, 'roughness': .15}, 'geometry': 'smooth'},
 'low-poly-3d': {'roughness': .7, 'geometry': 'flat'},
 'stylized-3d': {'roughness': .48, 'clearcoat': {'weight': .08, 'roughness': .3}, 'geometry': 'smooth'},
 'clay': {'roughness': .92, 'pattern': 'clay', 'geometry': 'smooth'},
 'polymer-clay': {'roughness': .53, 'clearcoat': {'weight': .08, 'roughness': .35}, 'pattern': 'clay', 'geometry': 'smooth'},
 'vinyl-toy': {'roughness': .19, 'clearcoat': {'weight': .35, 'roughness': .12}, 'geometry': 'smooth'},
 'porcelain': {'roughness': .13, 'clearcoat': {'weight': .85, 'roughness': .09}, 'geometry': 'smooth'},
 'wooden-toy': {'roughness': .65, 'pattern': 'wood', 'geometry': 'smooth'},
 'stained-glass': {'roughness': .08, 'transmission': 1.0, 'ior': 1.52, 'geometry': 'framed-glass'},
 'frosted-glass': {'roughness': .42, 'transmission': 1.0, 'ior': 1.5, 'geometry': 'solid-glass'},
 'crystal': {'roughness': .015, 'transmission': 1.0, 'ior': 1.65, 'geometry': 'flat-solid-glass'},
 'metallic-foil': {'roughness': .22, 'metallic': 1.0, 'anisotropy': {'strength': .65, 'direction': [1., 0.], 'rotation_radians': 0.}, 'pattern': 'foil', 'geometry': 'folded'},
}
_PATTERN_TYPES = {'clay', 'wood', 'foil'}
_GEOMETRY_TYPES = {'smooth', 'flat', 'framed-glass', 'solid-glass', 'flat-solid-glass', 'folded'}


def _keys(value, expected, code='LOOKDEV_STYLE'):
    require(isinstance(value, dict) and set(value) == set(expected), code, 'Unexpected or missing fields.')


def _identifier(value):
    require(isinstance(value, str) and _IDS.fullmatch(value), 'LOOKDEV_STYLE_ID', 'Invalid stable identifier.')
    return value


def _digest(value):
    require(isinstance(value, str) and _HASH.fullmatch(value), 'LOOKDEV_STYLE_HASH', 'Expected SHA-256.')
    return value


def mapping_ids():
    return tuple(sorted(_RECIPES))


def resolve_mapping(style_id=None, *, frozen_style=None):
    """Freeze a recipe with the exact already-approved style, never re-resolve it."""
    require((style_id is None) != (frozen_style is None), 'LOOKDEV_STYLE_INPUT', 'Supply one style ID or frozen style.')
    source = resolve_style(style_id) if frozen_style is None else validate_snapshot(frozen_style)
    identifier = source['resolved_id']
    require(identifier in _RECIPES, 'LOOKDEV_STYLE_UNSUPPORTED', 'No executable 3D mapping for ' + identifier)
    recipe = deepcopy(_RECIPES[identifier])
    result = {'schema_version': 1, 'mapping_version': VERSION, 'style_id': identifier,
              'source_style': source, 'recipe': recipe, 'rendering_mode': 'pbr',
              'lighting_preset': 'neutral-studio',
              'limitations': ['Production visual approval is separate from mapping execution.']}
    if 'transmission' in recipe:
        result['limitations'].append('Optical transmission requires Cycles and closed thickness-bearing geometry.')
    if recipe['geometry'] == 'framed-glass':
        result['limitations'].append('An explicit frame mesh is required; no frame is inferred from a character mesh.')
    if recipe['geometry'] == 'folded':
        result['limitations'].append('An explicit folded mesh is required; bump texture is not geometric folding.')
    result['sha256'] = content_hash(result)
    return result


def validate_mapping(value):
    _keys(value, {'schema_version','mapping_version','style_id','source_style','recipe','rendering_mode','lighting_preset','limitations','sha256'})
    finite_json(value)
    require(value['schema_version'] == 1 and type(value['schema_version']) is int and value['mapping_version'] == VERSION,
            'LOOKDEV_STYLE_VERSION', 'Unsupported style mapping version.')
    source = validate_snapshot(value['source_style'])
    require(value['style_id'] in _RECIPES and value['style_id'] == source['resolved_id'] and value['rendering_mode'] == 'pbr',
            'LOOKDEV_STYLE', 'Style identity differs from its frozen source.')
    require(value['lighting_preset'] == 'neutral-studio' and isinstance(value['limitations'], list)
            and len(value['limitations']) <= 16 and all(isinstance(s, str) and len(s) <= 1024 for s in value['limitations']),
            'LOOKDEV_STYLE', 'Invalid frozen mapping metadata.')
    recipe = value['recipe']
    require(isinstance(recipe, dict) and 'roughness' in recipe and recipe.get('geometry') in _GEOMETRY_TYPES and
            set(recipe) <= {'roughness','metallic','clearcoat','transmission','ior','anisotropy','geometry','pattern'},
            'LOOKDEV_STYLE', 'Unsupported recipe operation.')
    require(recipe.get('pattern', 'clay') in _PATTERN_TYPES, 'LOOKDEV_STYLE', 'Unsupported pattern.')
    # Validate all physical parameters through the existing canonical owner.
    material_resource(value, _validate=False)
    require(content_hash({k:v for k,v in value.items() if k != 'sha256'}) == _digest(value['sha256']),
            'LOOKDEV_STYLE_HASH', 'Frozen mapping changed.')
    return deepcopy(value)


def material_resource(mapping, *, color=(.45,.45,.45,1.), _validate=True):
    """Return an executable canonical surface; pattern/geometry are separate gates."""
    if _validate:
        mapping = validate_mapping(mapping)
    require(isinstance(color, (list,tuple)) and len(color) == 4 and
            all(type(v) in (int,float) and math.isfinite(v) and 0 <= v <= 1 for v in color),
            'LOOKDEV_STYLE_COLOR', 'Expected a linear RGBA color.')
    surface = default_surface()
    surface.update({k:deepcopy(v) for k,v in mapping['recipe'].items() if k not in ('pattern','geometry')})
    surface['base_color'] = list(color)
    result = {'schema_version':1,'kind':'material3d','id':_identifier(mapping['style_id']), 'version': VERSION,
              'dependencies':[], 'provenance': {'origin':'derived','tool':'lookdev_styles/'+VERSION,
                  'created_at':'2026-11-13T00:00:00Z','source_hashes':[mapping['source_style']['sha256']],
                  'attribution':'Versioned executable mapping from frozen Hatch Pet style',
                  'license':{'identifier':'MIT','source':'repository'}},
              'validation_status':'unvalidated','capabilities':[],'workflow':'metallic-roughness',
              'surface':surface,'texture_slots':{},'variants':{},
              'render_policy':{'strict':True,'allow_approximations':False,'required_features':['metallic-roughness'],'fallback':'reject'}}
    return validate_resource(result)


def bind_identity(registry, *, character, identity_sha256, slots):
    """Bind stable semantic slot IDs to exact materials and protected palettes.

    ``character`` refers to the existing identity owner, not a new character
    schema. Mesh targets use immutable object IDs and named material slots.
    """
    _keys(character, {'id','version','sha256'}, 'LOOKDEV_IDENTITY')
    _identifier(character['id']); _digest(character['sha256']); _digest(identity_sha256)
    require(isinstance(character['version'],str) and re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', character['version']),
            'LOOKDEV_IDENTITY','Invalid character version.')
    require(isinstance(slots,dict) and 0 < len(slots) <= 256,'LOOKDEV_IDENTITY','Expected bounded semantic slots.')
    output = {}
    occupied = set()
    for semantic, slot in slots.items():
        _identifier(semantic)
        _keys(slot, {'material','targets','protected_fields'}, 'LOOKDEV_IDENTITY')
        material = registry.effective_material(slot['material'])
        require(isinstance(slot['targets'],list) and 0 < len(slot['targets']) <= 4096,
                'LOOKDEV_IDENTITY','Expected bounded explicit mesh targets.')
        for target in slot['targets']:
            _keys(target, {'object_id','slot_id'}, 'LOOKDEV_IDENTITY')
            key = (_identifier(target['object_id']),_identifier(target['slot_id']))
            require(key not in occupied,'LOOKDEV_IDENTITY','A mesh slot has more than one semantic owner.')
            occupied.add(key)
        fields=slot['protected_fields']
        require(isinstance(fields,list) and all(isinstance(f,str) for f in fields) and len(fields)==len(set(fields)) and
                set(fields) <= set(material['surface']), 'LOOKDEV_IDENTITY','Unknown or duplicate protected fields.')
        output[semantic]={'material':deepcopy(slot['material']),'targets':deepcopy(slot['targets']),
                          'protected':{f:deepcopy(material['surface'][f]) for f in fields},
                          'effective_sha256':material['sha256']}
    result={'schema_version':1,'character':deepcopy(character),'identity_sha256':identity_sha256,'slots':output}
    result['sha256']=content_hash(result)
    return result


def validate_binding(binding):
    _keys(binding, {'schema_version','character','identity_sha256','slots','sha256'},'LOOKDEV_IDENTITY')
    require(binding['schema_version']==1 and type(binding['schema_version']) is int,
            'LOOKDEV_IDENTITY','Unsupported binding version.')
    _keys(binding['character'], {'id','version','sha256'}, 'LOOKDEV_IDENTITY')
    _identifier(binding['character']['id']);_digest(binding['character']['sha256']);_digest(binding['identity_sha256'])
    require(isinstance(binding['slots'],dict) and 0 < len(binding['slots']) <= 256,'LOOKDEV_IDENTITY','Invalid slot collection.')
    occupied=set()
    for name,slot in binding['slots'].items():
        _identifier(name)
        _keys(slot, {'material','targets','protected','effective_sha256'}, 'LOOKDEV_IDENTITY')
        _keys(slot['material'], {'id','version','sha256','kind','path'}, 'LOOKDEV_IDENTITY')
        _identifier(slot['material']['id']);_digest(slot['material']['sha256']);_digest(slot['effective_sha256'])
        require(isinstance(slot['protected'],dict) and len(slot['protected'])<=32 and set(slot['protected'])<=set(definitions()['Surface']['properties']),'LOOKDEV_IDENTITY','Invalid protected fields.')
        require(isinstance(slot['targets'],list) and 0<len(slot['targets'])<=4096,'LOOKDEV_IDENTITY','Invalid targets.')
        for target in slot['targets']:
            _keys(target, {'object_id','slot_id'}, 'LOOKDEV_IDENTITY')
            key=(_identifier(target['object_id']),_identifier(target['slot_id']))
            require(key not in occupied,'LOOKDEV_IDENTITY','Duplicate target owner.')
            occupied.add(key)
    require(content_hash({k:v for k,v in binding.items() if k!='sha256'})==_digest(binding['sha256']),
            'LOOKDEV_IDENTITY','Identity binding changed.')
    return deepcopy(binding)


def validate_identity_material(binding, semantic, registry, reference):
    binding=validate_binding(binding)
    require(semantic in binding['slots'],'LOOKDEV_IDENTITY','Unknown semantic slot.')
    effective=registry.effective_material(reference)
    slot=binding['slots'][semantic]
    original=registry.effective_material(slot['material'])
    require(original['sha256']==slot['effective_sha256'],'LOOKDEV_IDENTITY','Original material changed.')
    protected=slot['protected']
    require(all(original['surface'].get(k)==v for k,v in protected.items()),'LOOKDEV_IDENTITY','Protection differs from original material.')
    for key,value in protected.items():
        require(effective['surface'].get(key)==value,'LOOKDEV_IDENTITY_PROTECTED',
                'Unauthorized material change to protected '+semantic+'.'+key)
    return effective


def look_state(*, materials, lighting, environment, color):
    """Create an isolated authored state used as the base of every shot."""
    require(isinstance(materials,dict) and len(materials)<=256,'LOOKDEV_STATE','Invalid materials.')
    for name in materials:_identifier(name)
    result={'schema_version':1,'materials':deepcopy(materials),'lighting':deepcopy(lighting),
            'environment':deepcopy(environment),'color':deepcopy(color)}
    finite_json(result)
    result['sha256']=content_hash(result)
    return result


def shot_state(base, overrides, *, locks=()):
    """Return a fresh shot state; overrides can never leak to another shot."""
    require(isinstance(base,dict) and content_hash({k:v for k,v in base.items() if k!='sha256'})==base.get('sha256'),
            'LOOKDEV_STATE','Base state changed.')
    require(isinstance(overrides,dict) and set(overrides)<= {'materials','lighting','environment','color'},
            'LOOKDEV_STATE','Unknown override namespace.')
    require(isinstance(locks,(list,tuple)) and len(locks)<=1024 and all(isinstance(k,str) for k in locks),
            'LOOKDEV_STATE','Invalid continuity locks.')
    result=deepcopy(base)
    result.pop('sha256')
    for namespace, changes in overrides.items():
        require(isinstance(changes,dict) and isinstance(result[namespace],dict),'LOOKDEV_STATE','Expected whole-property overrides.')
        for key,value in changes.items():
            _identifier(key)
            require(key in result[namespace],'LOOKDEV_STATE','Unknown native property.')
            path=namespace+'.'+key
            require(path not in locks or result[namespace].get(key)==value,'LOOKDEV_CONTINUITY_LOCK', 'Override violates '+path)
            result[namespace][key]=deepcopy(value)
    finite_json(result)
    result['sha256']=content_hash(result)
    return result


def execute_mapping(obj, mapping, *, material_builder, color=(.45,.45,.45,1.), frame_object=None):
    """Apply the frozen recipe to actual Blender geometry using the PBR owner.

    ``material_builder`` accepts a canonical material3d document and returns a
    real bpy Material. Pattern nodes affect BSDF roughness and normals; geometry
    prerequisites are checked instead of flattening an unsuitable source mesh.
    """
    mapping=validate_mapping(mapping)
    try:
        import bpy
    except ImportError as exc:
        raise RigError('LOOKDEV_BLENDER_REQUIRED','Run recipe execution inside Blender.') from exc
    from lookdev_blender import assign_material
    require(obj.type=='MESH' and len(obj.data.polygons)>0,'LOOKDEV_STYLE_GEOMETRY','Expected real mesh geometry.')
    recipe=mapping['recipe'];geometry=recipe['geometry']
    if geometry in ('framed-glass','solid-glass','flat-solid-glass'):
        edge_uses={tuple(sorted(edge.vertices)):0 for edge in obj.data.edges}
        for face in obj.data.polygons:
            vertices=list(face.vertices)
            for a,b in zip(vertices,vertices[1:]+vertices[:1]):edge_uses[tuple(sorted((a,b)))]+=1
        require(edge_uses and all(count==2 for count in edge_uses.values()),
                'LOOKDEV_STYLE_GEOMETRY','Optical glass requires a closed two-manifold mesh.')
    if geometry=='framed-glass':
        require(frame_object is not None and frame_object!=obj and frame_object.type=='MESH' and len(frame_object.data.polygons)>0,
                'LOOKDEV_STYLE_GEOMETRY','Stained glass requires explicit separate frame geometry.')
    if geometry=='folded':
        adjacent={tuple(sorted(e.vertices)):[] for e in obj.data.edges}
        for face in obj.data.polygons:
            vertices=list(face.vertices)
            for a,b in zip(vertices,vertices[1:]+vertices[:1]):adjacent[tuple(sorted((a,b)))].append(face.normal)
        require(any(len(normals)==2 and normals[0].angle(normals[1],0.0)>.4 for normals in adjacent.values()),
                'LOOKDEV_STYLE_GEOMETRY','Metallic foil requires actual folds, not a smooth sphere with a label.')
    material=material_builder(material_resource(mapping,color=color))
    require(isinstance(material,bpy.types.Material) and material.use_nodes,
            'LOOKDEV_STYLE_MATERIAL','Material owner must return a real node material.')
    nodes=material.node_tree.nodes;links=material.node_tree.links
    principled=next((node for node in nodes if node.bl_idname=='ShaderNodeBsdfPrincipled'),None)
    require(principled is not None,'LOOKDEV_STYLE_MATERIAL','PBR owner did not provide a Principled BSDF.')
    pattern=recipe.get('pattern')
    if pattern:
        coordinate=nodes.new('ShaderNodeTexCoord')
        texture=nodes.new('ShaderNodeTexWave' if pattern=='wood' else 'ShaderNodeTexNoise')
        if pattern=='wood':
            texture.wave_type='BANDS';texture.bands_direction='X';texture.wave_profile='SIN'
            texture.inputs['Scale'].default_value=8.;texture.inputs['Distortion'].default_value=5.
            texture.inputs['Detail'].default_value=3.
        else:
            texture.inputs['Scale'].default_value=75. if pattern=='clay' else 24.
            texture.inputs['Detail'].default_value=2.
            texture.inputs['Roughness'].default_value=.7
        links.new(coordinate.outputs['Generated'],texture.inputs['Vector'])
        remap=nodes.new('ShaderNodeMapRange')
        remap.inputs['From Min'].default_value=0.;remap.inputs['From Max'].default_value=1.
        remap.inputs['To Min'].default_value=max(0.,recipe['roughness']-.12)
        remap.inputs['To Max'].default_value=min(1.,recipe['roughness']+.08)
        links.new(texture.outputs['Fac'],remap.inputs['Value']);links.new(remap.outputs['Result'],principled.inputs['Roughness'])
        bump=nodes.new('ShaderNodeBump');bump.inputs['Strength'].default_value=.18
        bump.inputs['Distance'].default_value=.002 if pattern=='clay' else .006
        links.new(texture.outputs['Fac'],bump.inputs['Height']);links.new(bump.outputs['Normal'],principled.inputs['Normal'])
    # Isolate shared mesh datablocks: a style choice cannot silently restyle all
    # other instances of the same source object.
    new_mesh=obj.data.copy()
    for face in new_mesh.polygons:face.use_smooth=geometry not in ('flat','flat-solid-glass','folded')
    new_mesh.materials.clear();new_mesh.materials.append(material)
    for face in new_mesh.polygons:face.material_index=0
    obj.data=new_mesh
    assign_material(obj,material,slots=[0])
    if frame_object is not None and geometry=='framed-glass':
        frame_mapping=deepcopy(mapping)
        frame_mapping['recipe']={'geometry':'flat','roughness':.38,'metallic':1.}
        frame_mapping['sha256']=content_hash({k:v for k,v in frame_mapping.items() if k!='sha256'})
        frame_material=material_builder(material_resource(frame_mapping,color=(.08,.08,.08,1.)))
        frame_mesh=frame_object.data.copy();frame_mesh.materials.clear();frame_mesh.materials.append(frame_material)
        for face in frame_mesh.polygons:face.material_index=0
        frame_object.data=frame_mesh
        assign_material(frame_object,frame_material,slots=[0])
    obj['hatch_style_mapping_sha256']=mapping['sha256']
    return {'style_id':mapping['style_id'],'mapping_sha256':mapping['sha256'],
            'material':material.name,'geometry':geometry,'pattern':pattern,
            'executed':True,'visual_approval':False}
