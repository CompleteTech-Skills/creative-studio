"""Deterministic supported environment prose delegated to canonical geometry recipes."""
from __future__ import annotations

import re

from rig_errors import RigError
from scene3d_graph import require
from scene3d_language import NUMBER, _number, _text, _digest

ID = r'(?P<id>[a-z][a-z0-9-]{0,31})'
DIMENSIONS = (r'width (?P<width>' + NUMBER + r') meters?, depth (?P<depth>' + NUMBER +
              r') meters?, height (?P<height>' + NUMBER + r') meters?, seed (?P<seed>\d{1,10})')


def _recipe(statement):
    architecture = re.fullmatch(r'create (?:a |an )?(?P<kind>workshop|fantasy|science-fiction|rooms) named ' +
        ID + r' with (?P<rooms>' + NUMBER + r') rooms?, ' + DIMENSIONS, statement, re.I)
    primitive = re.fullmatch(r'create (?:a |an )?(?P<kind>box|plane) named ' + ID + r' with ' +
        DIMENSIONS, statement, re.I)
    terrain = re.fullmatch(r'create (?:a )?terrain named ' + ID + r' with width (?P<width>' + NUMBER +
        r') meters?, depth (?P<depth>' + NUMBER + r') meters?, elevation (?P<elevation>' + NUMBER +
        r') meters?, resolution (?P<resolution>\d{1,3}), seed (?P<seed>\d{1,10})', statement, re.I)
    match = architecture or primitive or terrain
    require(match is not None, 'Unsupported environment description; no words were ignored.',
            'SCENE3D_LANGUAGE_UNSUPPORTED')
    seed = int(match['seed'])
    require(0 <= seed < 2**32, 'Seed must be an unsigned 32-bit integer.')
    parameters = {key: _number(match[key], positive=True) for key in ('width', 'depth')}
    if terrain:
        elevation = _number(match['elevation'])
        require(elevation >= 0, 'Terrain elevation cannot be negative.')
        parameters.update(elevation=elevation, resolution=int(match['resolution']))
        generator = 'terrain'
    elif architecture:
        rooms = _number(match['rooms'], positive=True)
        require(rooms.is_integer(), 'Room count must be a whole number.')
        parameters.update(height=_number(match['height'], positive=True), rooms=int(rooms))
        generator = match['kind'].lower()
    else:
        dimensions = dict(parameters)
        if match['kind'].lower() == 'box':
            dimensions['height'] = _number(match['height'], positive=True)
        else:
            # Plane requests still state a height; a nonzero thickness cannot be
            # represented by a plane and must not be silently ignored.
            require(_number(match['height']) == 0, 'Plane height must be zero; use a box for thickness.')
        parameters = {'kind': match['kind'].lower(), 'dimensions': dimensions}
        generator = 'primitive'
    return {'id': match['id'].lower(), 'version': 1, 'seed': seed, 'generator': generator,
            'parameters': parameters, 'budget': 256}


def compile_environment_description(graph, text):
    from scene3d_environment import plan as geometry_plan
    statements = _text(text)
    require(len(statements) <= 16, 'Environment prose supports one to sixteen template statements.')
    recipes, diagnostics, counts = [], [], []
    for index, statement in enumerate(statements):
        try:
            recipe = _recipe(statement)
            require(all(existing['id'] != recipe['id'] for existing in recipes), 'Recipe IDs must be unique.')
            objects = geometry_plan(recipe)
            triangles = sum(len(item['mesh'].triangles) for item in objects)
            require(sum(item['triangles'] for item in counts) + triangles <= 200000,
                    'Aggregate generator batch triangle budget exceeded.', 'SCENE3D_LAYOUT')
            recipes.append(recipe)
            counts.append({'id': recipe['id'], 'nodes': len(objects),
                           'triangles': sum(len(item['mesh'].triangles) for item in objects)})
        except RigError as exc:
            diagnostics.append({'statement': index, 'text': statement, 'code': exc.report['code'],
                                'message': exc.report['message']})
            break
    value = {'schema_version': 1, 'kind': 'scene3d-environment-language-plan',
             'planner': 'offline-environment-grammar-v1', 'original_text': text,
             'base_revision': graph.revision, 'base_sha256': graph.digest,
             'recipes': recipes, 'geometry': counts, 'diagnostics': diagnostics, 'substitutions': [],
             'intent': {'themes': [r['generator'] for r in recipes], 'units': 'meter',
                        'spatial_zones': [], 'requested_objects': counts, 'relationships': [],
                        'required_assets': [], 'character_assignments': [], 'lighting_intentions': [],
                        'camera_intentions': [], 'layout_constraints': [],
                        'generation_seeds': [r['seed'] for r in recipes],
                        'uncertainties': diagnostics, 'user_constraints': {'node_budget_per_recipe': 256}},
             'status': 'ready' if not diagnostics else 'unresolved'}
    value['sha256'] = _digest(value)
    return value


def apply_environment_plan(source, destination, scene_path, plan, *, dry_run=False):
    from scene3d_contracts import load_project
    from scene3d_graph import SceneGraph
    from scene3d_environment import apply_recipes
    require(isinstance(plan, dict) and isinstance(plan.get('original_text'), str), 'Invalid environment plan.')
    graph = SceneGraph.from_bundle(load_project(source), scene_path, source_root=source)
    require(type(plan.get('base_revision')) is int and plan['base_revision'] == graph.revision and
            plan.get('base_sha256') == graph.digest, 'Scene changed after environment planning.',
            'SCENE3D_LANGUAGE_STALE')
    expected = compile_environment_description(graph, plan['original_text'])
    require(expected == plan and expected['status'] == 'ready' and not plan['substitutions'],
            'Environment plan is unresolved or differs from its deterministic original description.',
            'SCENE3D_LANGUAGE_HASH')
    if dry_run:
        return {'dry_run': True, 'plan': plan, 'destination': str(destination)}
    result = apply_recipes(source, destination, scene_path, plan['recipes'])
    result['language_plan'] = plan
    return result
