"""Trusted language-plan adapter for the existing EditorRepository lifecycle.

HTTP handlers must choose these fixed functions explicitly and retain the editor
server's own token/origin/Host checks. No callable or filesystem path is accepted
from an untrusted language request.
"""
from copy import deepcopy

from rig_errors import finite_json
from scene3d_contracts import load_project
from scene3d_graph import SceneGraph, require
from scene3d_language import compile_description, apply_plan
from scene3d_language_environment import compile_environment_description, apply_environment_plan
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project


def plan_repository_description(repository, text, *, environment=False):
    """Plan against one verified immutable native editor revision."""
    require(type(environment) is bool, 'Environment mode must be boolean.')
    snapshot = repository.snapshot()
    native = repository.native_snapshot(snapshot['revision'], snapshot['sha256'])
    root, scene_path = native['root'], native['scene_path']
    bundle = load_project(root)
    require(bundle['inventory']['project.json']['sha256'] == native['inventory']['project.json']['sha256'],
            'Source project differs from the expected exact revision.', 'SCENE3D_SOURCE_CHANGED')
    graph = SceneGraph.from_bundle(bundle, scene_path, source_root=root)
    planner = compile_environment_description if environment else compile_description
    return {'schema_version': 1, 'kind': 'scene3d-editor-language-plan',
            'expected_revision': snapshot['revision'], 'expected_sha256': snapshot['sha256'],
            'scene_plan': planner(graph, text, expected_project_sha256=native['inventory']['project.json']['sha256'])}


def apply_repository_plan(repository, plan, transaction_id):
    """Commit through native locking, revision/hash checks, history and recovery.

    The caller chooses an idempotent transaction ID. Repeating an identical request
    returns its original committed revision; reusing an ID for different content
    fails in EditorRepository. Scene and repository revision tokens are distinct.
    """
    finite_json(plan)
    require(isinstance(plan, dict) and set(plan) == {'schema_version', 'kind', 'expected_revision',
            'expected_sha256', 'scene_plan'} and type(plan['schema_version']) is int and
            plan['schema_version'] == 1 and plan['kind'] == 'scene3d-editor-language-plan' and
            type(plan['expected_revision']) is int and plan['expected_revision'] >= 0 and
            isinstance(plan['expected_sha256'], str) and isinstance(plan['scene_plan'], dict),
            'Invalid editor language plan.')
    # Freeze caller-owned containers before handing the request to native locking.
    plan = deepcopy(plan)
    require(plan['scene_plan'].get('status') == 'ready', 'Resolve the language plan before applying it.')
    def build(root, destination, scene_path):
        inner = plan['scene_plan']
        bundle = load_project(root)
        if inner.get('kind') == 'scene3d-environment-language-plan':
            result = apply_environment_plan(root, destination, scene_path, inner,
                expected_project_sha256=bundle['inventory']['project.json']['sha256'])
            return result['snapshot']['active_scene']['path']
        graph = SceneGraph.from_bundle(bundle, scene_path, source_root=root)
        result = apply_plan(graph, PrefabLibrary.from_files(root, bundle), inner,
            expected_project_sha256=bundle['inventory']['project.json']['sha256'])
        receipt = snapshot_project(root, destination, graph, scene_path=scene_path,
                                   derived_documents=result['derived_documents'],
                                   expected_project_sha256=bundle['inventory']['project.json']['sha256'])
        return receipt['active_scene']['path']
    return repository.transact_native(plan['expected_revision'], transaction_id, {'language_plan': plan}, build,
        expected_sha256=plan['expected_sha256'], label='Apply reviewed scene language plan')
