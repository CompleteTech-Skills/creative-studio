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


def _preflight_project(graph, recipes, *, destination=None, expected_project_sha256=None):
    """Use the actual generator and snapshot owners before claiming readiness."""
    from scene3d_contracts import load_project, validate_prefab_instances
    from scene3d_graph import SceneGraph
    from scene3d_environment import _prepare_recipe
    from scene3d_snapshot import snapshot_project
    from pathlib import Path
    from uuid import uuid4
    context = graph.planning_source()
    require(context is not None,
            'Environment planning requires a verified project source context.',
            'SCENE3D_LANGUAGE_CONTEXT')
    source, scene_path = context
    bundle = load_project(source)
    require(expected_project_sha256 is None or type(expected_project_sha256) is str and bundle['inventory']['project.json']['sha256'] == expected_project_sha256,
            'Source project differs from the expected exact revision.', 'SCENE3D_SOURCE_CHANGED')
    selected = SceneGraph.from_bundle(bundle, scene_path, source_root=source)
    require(selected.digest == graph.digest,
            'Scene changed before environment resource planning.', 'SCENE3D_LANGUAGE_STALE')
    scene = graph.document()
    context = dict(bundle, documents=dict(bundle['documents']))
    documents, payloads = {}, {}
    for recipe in recipes:
        prepared = _prepare_recipe(context, scene, recipe)
        scene = prepared['scene']
        documents.update(prepared['documents'])
        context['documents'].update(prepared['documents'])
        for name, raw in prepared['payloads'].items():
            require(name not in payloads or payloads[name] == raw, 'Generated payload collision.')
            payloads[name] = raw
    validate_prefab_instances(scene, lambda reference: context['documents'][reference['path']],
                              max_nodes=bundle['project']['budgets']['max_nodes'])
    scene['revision'] += 1
    candidate = SceneGraph(scene)
    # The destination is never created in dry-run mode. Its trusted parent is
    # the already selected project parent; no user-controlled output is opened.
    output = destination if destination is not None else Path(source).absolute().parent / ('.scene3d-language-preflight-' + uuid4().hex)
    return snapshot_project(source, output,
                     candidate, scene_path=scene_path, derived_documents=documents,
                     extra_payloads=payloads, dry_run=True,
                     expected_project_sha256=bundle['inventory']['project.json']['sha256'])


def _preflight_edit(graph, candidate, *, expected_project_sha256=None):
    """Check a persisted ordinary edit with the same complete snapshot engine."""
    from scene3d_contracts import load_project
    from scene3d_graph import SceneGraph
    from scene3d_snapshot import snapshot_project
    from pathlib import Path
    from uuid import uuid4
    context = graph.planning_source()
    if context is None:
        require(expected_project_sha256 is None, 'Project-bound planning requires a verified source context.',
                'SCENE3D_LANGUAGE_CONTEXT')
        # A standalone graph has no project limits or closure to authenticate.
        # Its plan remains an explicitly scene-only in-memory operation.
        return 'scene-only'
    source, scene_path = context
    bundle = load_project(source)
    require(expected_project_sha256 is None or type(expected_project_sha256) is str and bundle['inventory']['project.json']['sha256'] == expected_project_sha256,
            'Source project differs from the expected exact revision.', 'SCENE3D_SOURCE_CHANGED')
    selected = SceneGraph.from_bundle(bundle, scene_path, source_root=source)
    require(selected.digest == graph.digest, 'Scene changed before language planning.', 'SCENE3D_LANGUAGE_STALE')
    document = candidate.document()
    document['revision'] = graph.revision + 1  # The actual batch commits once.
    prepared = SceneGraph(document, external_references=candidate._references,
                          asset_bounds=candidate._asset_bounds,
                          animation_documents=candidate._animation_documents,
                          animation_payloads=candidate._animation_payloads)
    snapshot_project(source, Path(source).absolute().parent / ('.scene3d-language-preflight-' + uuid4().hex),
                     prepared, scene_path=scene_path, derived_documents=candidate.derived_documents(), dry_run=True,
                     expected_project_sha256=bundle['inventory']['project.json']['sha256'])
    return 'project-closure'


def compile_environment_description(graph, text, *, expected_project_sha256=None):
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
    if not diagnostics:
        try:
            _preflight_project(graph, recipes, expected_project_sha256=expected_project_sha256)
        except RigError as exc:
            diagnostics.append({'statement': len(statements) - 1, 'text': statements[-1],
                                'code': exc.report['code'], 'message': exc.report['message']})
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


def apply_environment_plan(source, destination, scene_path, plan, *, dry_run=False, expected_project_sha256=None):
    from scene3d_contracts import load_project
    from scene3d_graph import SceneGraph
    from scene3d_environment import apply_recipes
    require(isinstance(plan, dict) and isinstance(plan.get('original_text'), str), 'Invalid environment plan.')
    bundle = load_project(source)
    require(expected_project_sha256 is None or type(expected_project_sha256) is str and bundle['inventory']['project.json']['sha256'] == expected_project_sha256,
            'Source project differs from the expected exact revision.', 'SCENE3D_SOURCE_CHANGED')
    graph = SceneGraph.from_bundle(bundle, scene_path, source_root=source)
    require(type(plan.get('base_revision')) is int and plan['base_revision'] == graph.revision and
            plan.get('base_sha256') == graph.digest, 'Scene changed after environment planning.',
            'SCENE3D_LANGUAGE_STALE')
    expected = compile_environment_description(graph, plan['original_text'],
        expected_project_sha256=bundle['inventory']['project.json']['sha256'])
    require(expected == plan and expected['status'] == 'ready' and not plan['substitutions'],
            'Environment plan is unresolved or differs from its deterministic original description.',
            'SCENE3D_LANGUAGE_HASH')
    if dry_run:
        report = _preflight_project(graph, plan['recipes'], destination=destination,
            expected_project_sha256=bundle['inventory']['project.json']['sha256'])
        return dict(report, plan=plan, destination=str(destination))
    result = apply_recipes(source, destination, scene_path, plan['recipes'],
                           expected_project_sha256=bundle['inventory']['project.json']['sha256'])
    result['language_plan'] = plan
    return result
