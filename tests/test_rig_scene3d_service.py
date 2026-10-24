"""Operational hierarchy/prefab/import acceptance using real native files."""
from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from asset_library import AssetLibrary
from rig_errors import RigError
from rig_gltf import GltfBuilder, export_gltf
from rig_math import axis_angle
from rig_test_helpers import fixture as rig_fixture
from scene3d_contracts import canonical_bytes, load_project, schemas, validate_document
from scene3d_example import build_example, node, TRANSFORM
from scene3d_graph import SceneGraph, transform_for_matrix
from scene3d_import import (add_import, evaluate_imported_vertices, import_gltf,
                            materialize, source_snapshot, resolve_import, edit_mesh_vertices)
from scene3d_edit import apply_commands
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project


class SceneServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bundle = build_example(self.root/'source')
        self.scene = deepcopy(self.bundle['documents']['example-scene.json'])

    def graph(self, scene=None, *, references=None):
        return SceneGraph(scene or self.scene, external_references={} if references is None else references)

    def test_create_reorder_duplicate_serialize_and_reconstruct(self):
        graph = self.graph()
        child = node('child', parent='mesh')
        graph.create(child)
        before = deepcopy(child)
        duplicate = graph.duplicate('mesh', 'mesh-copy')
        self.assertEqual(child, before)
        self.assertEqual(len(duplicate['mapping']), 2)
        copy_child = duplicate['mapping']['child']
        self.assertEqual(next(n for n in graph.document()['nodes'] if n['id'] == copy_child)['parent'], 'mesh-copy')
        graph.reorder('camera', 0)
        siblings = [n['id'] for n in graph.document()['nodes'] if n['parent'] == 'root']
        self.assertEqual(siblings[0], 'camera')
        restored = self.graph(json.loads(canonical_bytes(graph.document())))
        self.assertEqual(restored.document(), graph.document())
        self.assertEqual(restored.evaluate_base(), graph.evaluate_base())

    def test_preserve_world_reparent_with_pivot_and_nonuniform_parent(self):
        graph = self.graph()
        target = node('destination')
        target['transform'] = dict(translation=[-3, 2, 1], rotation=axis_angle([0, 1, 0], .7).tolist(), scale=[2, 2, 2])
        graph.create(target)
        transform = deepcopy(TRANSFORM)
        transform.update(translation=[2, 0, 1], pivot=[.25, .1, .7], rotation=axis_angle([1, 0, 0], .2).tolist())
        graph.set_transform('mesh', transform)
        before = graph.transform('mesh', space='world')
        graph.reparent('mesh', 'destination')
        np.testing.assert_allclose(graph.transform('mesh', space='world'), before, atol=1e-7)
        self.assertEqual(graph.transform('mesh')['pivot'], transform['pivot'])
        graph.reparent('mesh', None)
        np.testing.assert_allclose(graph.transform('mesh', space='world'), before, atol=1e-7)

    def test_unrepresentable_reparent_is_transactional(self):
        graph = self.graph()
        parent = node('scaled')
        parent['transform']['scale'] = [2, 1, 1]
        graph.create(parent)
        t = deepcopy(TRANSFORM)
        t['rotation'] = axis_angle([0, 0, 1], .6).tolist()
        graph.set_transform('mesh', t)
        before = graph.document()
        with self.assertRaises(RigError) as error:
            graph.reparent('mesh', 'scaled')
        self.assertEqual(error.exception.report['code'], 'SCENE3D_UNREPRESENTABLE')
        self.assertEqual(graph.document(), before)
        with self.assertRaises(RigError): graph.reparent('root', 'mesh')
        self.assertEqual(graph.document(), before)

    def test_promote_children_preserves_world_and_subtree_requires_explicit_policy(self):
        graph = self.graph()
        child = node('child', parent='mesh')
        child['transform']['translation'] = [1, 2, 3]
        graph.create(child)
        before = graph.transform('child', space='world')
        with self.assertRaises(RigError): graph.delete('mesh')
        graph.delete('mesh', policy='promote')
        np.testing.assert_allclose(graph.transform('child', space='world'), before)
        self.assertEqual(next(n for n in graph.document()['nodes'] if n['id'] == 'child')['parent'], 'root')
        graph.delete('child', policy='subtree')
        self.assertNotIn('child', graph.query())

    def test_external_shot_references_and_unknown_context_fail_closed(self):
        graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json')
        with self.assertRaises(RigError) as error: graph.delete('camera')
        self.assertEqual(error.exception.report['code'], 'SCENE3D_REFERENCED')
        graph = SceneGraph(self.scene)
        with self.assertRaises(RigError) as error: graph.delete('mesh')
        self.assertEqual(error.exception.report['code'], 'SCENE3D_REFERENCE_CONTEXT')

    def test_revision_conflict_and_invalid_create_do_not_change_authored_state(self):
        graph = self.graph()
        before = graph.document()
        with self.assertRaises(RigError): graph.create(node('root'))
        with self.assertRaises(RigError): graph.create(node('orphan', parent='missing'))
        with self.assertRaises(RigError): graph.create(node('new'), expected_revision=9)
        self.assertEqual(before, graph.document())

    def test_selective_cache_invalidation_and_constraint_dependencies(self):
        graph = self.graph()
        graph.create(node('independent'))
        follower = node('follow')
        follower['constraints'] = [dict(id='copy', type='copy-transform', target='mesh', influence=1.)]
        graph.create(follower)
        graph.evaluate_base()
        initial = graph.evaluation_counts()
        graph.evaluate_base()
        self.assertEqual(initial, graph.evaluation_counts())
        transform = deepcopy(TRANSFORM)
        transform['translation'] = [4, 1, 2]
        dirty = graph.set_transform('mesh', transform)['dirty']
        self.assertEqual(dirty, ['follow', 'mesh'])
        graph.evaluate_base()
        self.assertEqual(graph.evaluation_counts()['independent'], initial['independent'])
        np.testing.assert_allclose(graph.transform('follow', space='world'), graph.transform('mesh', space='world'))

    def test_constraints_aim_socket_and_attachment_use_real_target_matrices(self):
        graph = self.graph()
        socket = node('socket', 'socket', 'mesh')
        socket['transform']['translation'] = [0, 2, 0]
        graph.create(socket)
        carried = node('carried')
        carried['attachment'] = dict(owner='mesh', socket='socket', offset=deepcopy(TRANSFORM))
        graph.create(carried)
        np.testing.assert_allclose(np.asarray(graph.transform('carried', space='world'))[:3, 3], [2, 2, 0])
        aim = node('aim')
        aim['constraints'] = [dict(id='look', type='look-at', target='mesh', influence=1.)]
        graph.create(aim)
        np.testing.assert_allclose(np.asarray(graph.transform('aim', space='world'))[:3, 2], [1, 0, 0], atol=1e-12)
        invalid = node('bad')
        invalid['constraints'] = [dict(id='sock', type='socket', target='mesh', influence=1.)]
        before = graph.document()
        with self.assertRaises(RigError): graph.create(invalid)
        self.assertEqual(graph.document(), before)
        with self.assertRaises(RigError): graph.reparent('carried', 'camera')

    def test_collections_visibility_isolation_and_locks_are_distinct(self):
        graph = self.graph()
        graph.collection('props', name='Props', editor_visible=False, render_visible=True)
        graph.set_memberships('mesh', collections=['props'])
        v = graph.evaluate_base('mesh')
        self.assertFalse(v['editor_visible'])
        self.assertTrue(v['render_visible'])
        graph.collection('props', editor_visible=True, render_visible=False)
        self.assertTrue(graph.evaluate_base('mesh')['editor_visible'])
        self.assertFalse(graph.evaluate_base('mesh')['render_visible'])
        graph.collection('props', render_visible=True, locked=True)
        with self.assertRaises(RigError): graph.set_transform('mesh', deepcopy(TRANSFORM))
        graph.collection('props', locked=False)
        graph.isolate(['mesh'])
        self.assertFalse(graph.evaluate_base('camera')['editor_visible'])
        self.assertTrue(graph.evaluate_base('camera')['render_visible'])
        graph.isolate()
        graph.set_node_flags('mesh', camera_visible=False, cast_shadow=False)
        self.assertFalse(graph.evaluate_base('mesh')['camera_visible'])
        self.assertFalse(graph.evaluate_base('mesh')['cast_shadow'])

    def test_world_bounds_queries_and_multiple_tags(self):
        graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json')
        self.assertEqual(graph.bounds(['mesh']), dict(minimum=[2., 0., 0.], maximum=[3., 0., 1.]))
        graph.set_tags('mesh', ['prop', 'triangle'])
        self.assertEqual(graph.query(tag='prop'), ['mesh'])
        self.assertEqual(graph.query(tag='triangle', node_type='mesh_instance'), ['mesh'])
        self.assertEqual(graph.query(node_type='mesh_instance', region=dict(minimum=[2.1, -.1, -.1], maximum=[2.2, .1, .1])), ['mesh'])
        self.assertEqual(graph.query(name='camera'), ['camera'])

    def test_immutable_prefab_parameters_instances_children_and_source_preservation(self):
        library = PrefabLibrary()
        prefab = deepcopy(self.bundle['documents']['example-prefab.json'])
        prefab['parameters'] = dict(distance=1., show=True)
        prefab['parameter_bindings'] = [dict(parameter='distance', node='prefab-mesh', property='translation', component=0),
                                       dict(parameter='show', node='prefab-mesh', property='visible')]
        reference = library.register(prefab)
        payloads = library.payloads()
        graph = self.graph()
        library.instantiate(graph, reference, 'first')
        library.instantiate(graph, reference, 'second', overrides=dict(parameters={'distance': 3.}, materials={}, nodes=[]))
        expanded = library.expand(graph.document())
        evaluated = self.graph(expanded['scene']).evaluate_base()
        first = expanded['mappings']['first']['prefab-mesh']
        second = expanded['mappings']['second']['prefab-mesh']
        self.assertEqual(evaluated[first]['world_matrix'][0][3], 1.)
        self.assertEqual(evaluated[second]['world_matrix'][0][3], 3.)
        child = node('optional', parent='prefab-root')
        library.set_overrides(graph, 'first', dict(parameters={'show': False}, materials={}, nodes=[], children=[child]))
        expanded = library.expand(graph.document())
        evaluated = self.graph(expanded['scene']).evaluate_base()
        self.assertFalse(evaluated[expanded['mappings']['first']['prefab-mesh']]['editor_visible'])
        self.assertIn('optional', expanded['mappings']['first'])
        self.assertEqual(payloads, library.payloads())
        library.reset(graph, 'first')
        self.assertNotIn('optional', library.expand(graph.document())['mappings']['first'])

    def test_prefab_invalid_parameters_and_optional_children_reject_atomically(self):
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        reference = self.bundle['project']['prefabs'][0]
        graph = self.graph()
        library.instantiate(graph, reference, 'instance')
        before = graph.document()
        for override in [dict(parameters={'unknown': 2}, materials={}, nodes=[]),
                         dict(parameters={}, materials={}, nodes=[dict(node='absent', visible=False)]),
                         dict(parameters={}, materials={}, nodes=[], children=[node('prefab-root')]),
                         dict(parameters={}, materials={}, nodes=[], unexpected=True)]:
            with self.subTest(override=override), self.assertRaises(RigError):
                library.set_overrides(graph, 'instance', override)
            self.assertEqual(graph.document(), before)

    def test_simultaneous_quaternion_parameters_are_not_silently_normalized(self):
        library = PrefabLibrary()
        p = deepcopy(self.bundle['documents']['example-prefab.json'])
        p['parameters'] = {'w': .5}
        p['parameter_bindings'] = [dict(parameter='w', node='prefab-root', property='rotation', component=3)]
        r = library.register(p)
        graph = self.graph()
        before = graph.document()
        with self.assertRaises(RigError): library.instantiate(graph, r, 'invalid')
        self.assertEqual(graph.document(), before)

    def test_prefab_creation_extracts_subtree_and_material_slot_override(self):
        graph = self.graph()
        material = dict(id='mat', version='1.0.0', kind='material', path='mat.json', sha256='a'*64)
        updated = graph.document()
        updated['nodes'][1]['material_overrides'] = [dict(slot='surface', material=material)]
        graph.replace_document(updated, expected_revision=graph.revision)
        library = PrefabLibrary()
        extracted = library.create(graph, 'mesh', 'extracted', '1.0.0')
        self.assertEqual(extracted['prefab']['nodes'][0]['transform'], TRANSFORM)
        self.assertEqual(extracted['placement'][0][3], 2.)
        changed = {**material, 'id': 'other', 'sha256': 'b'*64}
        library.instantiate(graph, extracted['reference'], 'instance',
                            overrides=dict(parameters={}, materials={'surface': changed}, nodes=[]))
        expanded = library.expand(graph.document())
        i = expanded['mappings']['instance']['mesh']
        self.assertEqual(next(n for n in expanded['scene']['nodes'] if n['id'] == i)['material_overrides'][0]['material'], changed)
        self.assertEqual(library.resolve(extracted['reference']), extracted['prefab'])

    def test_upgrade_reports_conflicts_and_keep_source_revert_are_explicit(self):
        library = PrefabLibrary()
        p = deepcopy(self.bundle['documents']['example-prefab.json'])
        r1 = library.register(p)
        p2 = deepcopy(p)
        p2['version'] = '2.0.0'
        p2['nodes'][1]['transform']['translation'] = [5, 0, 0]
        r2 = library.register(p2)
        graph = self.graph()
        override = deepcopy(TRANSFORM)
        override['translation'] = [2, 0, 0]
        library.instantiate(graph, r1, 'instance', overrides=dict(parameters={}, materials={},
                            nodes=[dict(node='prefab-mesh', transform=override)]))
        before = graph.document()
        conflict = library.upgrade(graph, 'instance', r2)
        self.assertFalse(conflict['applied'])
        self.assertEqual(conflict['conflicts'], ['node:prefab-mesh:transform'])
        self.assertEqual(graph.document(), before)
        self.assertTrue(library.upgrade(graph, 'instance', r2, resolutions={'node:prefab-mesh:transform': 'keep'})['applied'])
        self.assertTrue(library.upgrade(graph, 'instance', r1, resolutions={'node:prefab-mesh:transform': 'source'})['applied'])
        self.assertEqual(library.resolve(r2)['nodes'][1]['transform']['translation'], [5, 0, 0])

    def test_nested_prefabs_detach_and_derived_animation_preserve_source_bytes(self):
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        original = deepcopy(self.bundle['documents']['example-prefab.json'])
        original['id'] = 'animated-prefab'
        original['nodes'][1]['animation_bindings'] = [dict(target='prefab-mesh', clip=self.bundle['project']['animations'][0],
            start_frame=0, end_frame=24, speed=1., loop=False)]
        # Bind a real source-local clip with the correct immutable target identity.
        clip = deepcopy(self.bundle['documents']['example-animation.json'])
        clip['id'] = 'prefab-animation'
        clip['tracks'][0]['target'] = 'prefab-mesh'
        clipref = library.register(clip)
        original['nodes'][1]['animation_bindings'][0]['clip'] = clipref
        original['dependencies'].append(clipref)
        child_ref = library.register(original)
        outer = deepcopy(self.bundle['documents']['example-prefab.json'])
        outer['id'] = 'nested'
        n = node('inner', 'prefab_instance', 'prefab-root')
        n['prefab'] = child_ref
        outer['nodes'] = [outer['nodes'][0], n]
        outer['dependencies'] = [child_ref]
        r = library.register(outer)
        graph = self.graph()
        library.instantiate(graph, r, 'instance')
        library.instantiate(graph, r, 'untouched')
        source = library.payloads()
        result = library.detach(graph, 'instance')
        self.assertTrue(any(n.get('prefab') == r for n in graph.document()['nodes']))
        self.assertGreater(len(result['derived_documents']), 0)
        for document in result['derived_documents'].values():
            self.assertNotEqual(document['tracks'][0]['target'], 'prefab-mesh')
        self.assertEqual(source, library.payloads())
        self.assertEqual(library.expand(graph.document())['scene']['kind'], 'scene3d-scene')

    def test_hash_or_version_alias_and_missing_nested_prefab_fail(self):
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        r = self.bundle['project']['prefabs'][0]
        with self.assertRaises(RigError): library.resolve({**r, 'sha256': '0'*64})
        with self.assertRaises(RigError): library.register(library.resolve(r))
        graph = self.graph()
        broken = deepcopy(library.resolve(r))
        broken['id'] = 'broken'
        nested = node('nested', 'prefab_instance', 'prefab-root')
        nested['prefab'] = {**r, 'id': 'missing'}
        broken['nodes'].append(nested)
        new = library.register(broken)
        with self.assertRaises(RigError): library.instantiate(graph, new, 'instance')

    def test_fixture_provenance_propagates_through_nested_instances_and_snapshot(self):
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        reference = self.bundle['project']['prefabs'][0]
        outer = deepcopy(self.bundle['documents']['example-prefab.json'])
        outer['id'] = 'authored-wrapper'
        outer['provenance']['origin'], outer['provenance']['fixture'] = 'authored', False
        n = node('nested', 'prefab_instance', 'prefab-root')
        n['prefab'] = reference
        outer['nodes'] = [outer['nodes'][0], n]
        outer['dependencies'] = [reference]
        outer_ref = library.register(outer)
        scene = deepcopy(self.scene)
        scene['provenance']['origin'], scene['provenance']['fixture'] = 'authored', False
        graph = self.graph(scene)
        library.instantiate(graph, outer_ref, 'instance')
        self.assertTrue(graph.document()['provenance']['fixture'])
        false_version = deepcopy(self.bundle['documents']['example-prefab.json'])
        false_version['version'] = '2.0.0'
        false_version['provenance']['origin'], false_version['provenance']['fixture'] = 'authored', False
        with self.assertRaises(RigError): library.register(false_version)

    def test_complete_snapshot_preserves_originals_and_new_versions_reopen(self):
        graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json')
        t = deepcopy(TRANSFORM)
        t['translation'] = [8, 0, 0]
        graph.set_transform('mesh', t)
        output = snapshot_project(self.root/'source', self.root/'portable', graph, scene_path='example-scene.json')
        reopened = load_project(self.root/'portable')
        active = output['active_scene']
        self.assertEqual(reopened['documents'][active['path']]['nodes'][1]['transform']['translation'], [8, 0, 0])
        self.assertEqual((self.root/'source'/'example-scene.json').read_bytes(),
                         (self.root/'portable'/'example-scene.json').read_bytes())
        self.assertEqual(len(reopened['project']['scenes']), 2)
        self.assertEqual(len(reopened['project']['shots']), 2)
        for path, info in output['inventory'].items():
            self.assertEqual(hashlib.sha256((self.root/'portable'/path).read_bytes()).hexdigest(), info['sha256'])

    def test_snapshot_racing_destination_and_stale_source_are_preserved(self):
        import scene3d_snapshot
        original = scene3d_snapshot._rename_no_replace
        destination = self.root/'race'
        inode = []
        def race(stage, target):
            target.mkdir()
            inode.append(target.stat().st_ino)
            return original(stage, target)
        with patch.object(scene3d_snapshot, '_rename_no_replace', race), self.assertRaises(OSError):
            snapshot_project(self.root/'source', destination)
        self.assertEqual(destination.stat().st_ino, inode[0])
        self.assertEqual(list(destination.iterdir()), [])
        old = scene3d_snapshot.read_source_bytes
        def stale(path, limit):
            if Path(path).name == 'triangle.obj':
                Path(path).write_bytes(b'corrupted original by external test writer')
            return old(path, limit)
        with patch.object(scene3d_snapshot, 'read_source_bytes', stale), self.assertRaises(RigError):
            snapshot_project(self.root/'source', self.root/'stale')
        self.assertFalse((self.root/'stale').exists())

    def test_command_batch_and_prefab_catalog_roll_back_all_changes_on_failure(self):
        graph = self.graph()
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        before, payloads = graph.document(), library.payloads()
        commands = dict(schema_version=1, commands=[
            dict(operation='create', parameters=dict(node=node('new'))),
            dict(operation='prefab-create', parameters=dict(root_id='mesh', identifier='new-prefab', version='1.0.0')),
            dict(operation='reparent', parameters=dict(identifier='root', parent='mesh'))])
        with self.assertRaises(RigError): apply_commands(graph, library, commands)
        self.assertEqual(graph.document(), before)
        self.assertEqual(library.payloads(), payloads)
        success = dict(schema_version=1, commands=[dict(operation='create', parameters=dict(node=node('new'))),
                    dict(operation='tags', parameters=dict(identifier='new', tags=['authored']))])
        result = apply_commands(graph, library, success)
        self.assertEqual(graph.revision, before['revision']+1)
        self.assertEqual(result['revision'], graph.revision)
        self.assertEqual(graph.query(tag='authored'), ['new'])

    def test_noncanonical_foreign_prefab_bytes_are_preserved_in_portable_snapshot(self):
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        value = deepcopy(self.bundle['documents']['example-prefab.json'])
        value['id'] = 'foreign-prefab'
        payload = (json.dumps(value, indent=3)+'\r\n').encode()
        path = 'foreign/source-prefab.json'
        reference = library.register_bytes(payload, path)
        graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json')
        library.instantiate(graph, reference, 'foreign-instance')
        output = snapshot_project(self.root/'source', self.root/'foreign-copy', graph, scene_path='example-scene.json',
                                  derived_documents={path: payload})
        self.assertEqual((self.root/'foreign-copy'/path).read_bytes(), payload)
        self.assertEqual(output['inventory'][path]['sha256'], hashlib.sha256(payload).hexdigest())
        load_project(self.root/'foreign-copy')

    def test_hybrid_snapshot_delegates_complete_native_legacy_closure(self):
        from production_fixture import build_fixture_project
        from production_contracts import load_project as load_legacy_project
        legacy_root = self.root/'source/legacy'
        build_fixture_project(legacy_root)
        legacy = load_legacy_project(legacy_root)
        legacy_payload = (legacy_root/'project.json').read_bytes()
        project = deepcopy(self.bundle['project'])
        reference = dict(id=legacy['project']['id'], version=legacy['project']['version'], kind='project',
            path='legacy/project.json', sha256=hashlib.sha256(legacy_payload).hexdigest())
        project['linked_2d_project'] = reference
        project['dependency_lock'].append(reference)
        project['budgets'].update(max_files=512, max_bytes=64*1024*1024)
        (self.root/'source/project.json').write_bytes(canonical_bytes(project))
        before = {p.relative_to(legacy_root).as_posix(): p.read_bytes() for p in legacy_root.rglob('*') if p.is_file()}
        output = snapshot_project(self.root/'source', self.root/'hybrid')
        restored = load_legacy_project(self.root/'hybrid/legacy')
        self.assertEqual(legacy['project'], restored['project'])
        for path, payload in before.items():
            self.assertEqual((self.root/'hybrid/legacy'/path).read_bytes(), payload)
        self.assertGreater(len(output['inventory']), len(self.bundle['inventory'])+len(legacy['inventory']))

    def test_locked_promoted_child_and_invalid_contexts_are_structured(self):
        graph = self.graph()
        graph.create(node('child', parent='mesh'))
        graph.set_node_flags('child', locked=True)
        before = graph.document()
        with self.assertRaises(RigError): graph.delete('mesh', policy='promote')
        self.assertEqual(graph.document(), before)
        with self.assertRaises(RigError): SceneGraph(self.scene, asset_bounds={'asset': {'minimum': ['bad']*3, 'maximum': [1]*3}})
        with self.assertRaises(RigError): graph.query(region=dict(minimum=['bad']*3, maximum=[1]*3))
        with self.assertRaises(RigError): graph.query(tag=4)

    def test_bounds_ancestor_and_cached_visibility_updates_are_not_stale(self):
        graph = SceneGraph.from_bundle(self.bundle, 'example-scene.json')
        initial = graph.evaluate_base('root')['world_bounds']
        self.assertEqual(initial, graph.evaluate_base('mesh')['world_bounds'])
        counts = graph.evaluation_counts()
        transform = graph.transform('mesh')
        transform['translation'] = [6, 0, 0]
        graph.set_transform('mesh', transform)
        self.assertEqual(graph.evaluate_base('root')['world_bounds']['minimum'][0], 6)
        self.assertEqual(graph.evaluation_counts()['root'], counts['root'])
        graph.set_node_flags('mesh', selectable=False)
        self.assertFalse(graph.evaluate_base('mesh')['selectable'])
        graph.set_node_flags('root', camera_visible=False)
        self.assertFalse(graph.evaluate_base('mesh')['camera_visible'])
        graph.collection('locked', name='Locked', locked=True)
        graph.set_memberships('root', collections=['locked'])
        with self.assertRaises(RigError): graph.set_transform('mesh', transform)

    def test_actual_cli_edit_reopen_and_failed_batch_leave_original_and_destination_intact(self):
        original = {p.name: p.read_bytes() for p in (self.root/'source').iterdir() if p.is_file()}
        commands = self.root/'commands.json'
        commands.write_text(json.dumps(dict(schema_version=1, commands=[dict(operation='transform', parameters=
            dict(identifier='mesh', transform=dict(translation=[7, 0, 0], rotation=[0, 0, 0, 1], scale=[1, 1, 1])))])))
        script = Path(__file__).resolve().parents[1]/'scripts/scene3d_edit.py'
        result = subprocess.run([sys.executable, str(script), '--project', str(self.root/'source'), '--scene',
            'example-scene.json', '--commands', str(commands), '--destination', str(self.root/'cli-output')],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        reopened = load_project(self.root/'cli-output')
        self.assertEqual(reopened['documents'][receipt['active_scene']['path']]['nodes'][1]['transform']['translation'], [7, 0, 0])
        commands.write_text(json.dumps(dict(schema_version=1, commands=[dict(operation='delete', parameters=dict(identifier='camera'))])))
        failed = subprocess.run([sys.executable, str(script), '--project', str(self.root/'source'), '--scene',
            'example-scene.json', '--commands', str(commands), '--destination', str(self.root/'must-not-exist')],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(json.loads(failed.stderr)['error']['code'], 'SCENE3D_REFERENCED')
        self.assertNotIn('Traceback', failed.stderr)
        self.assertFalse((self.root/'must-not-exist').exists())
        self.assertEqual(original, {p.name: p.read_bytes() for p in (self.root/'source').iterdir() if p.is_file()})


class RealSceneImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.license = self.root/'license.txt'
        self.license.write_text('CC0-1.0. Procedural engineering fixture authored for this test.\n')
        self.library = AssetLibrary(self.root/'library', create=True)

    def source(self, suffix='gltf'):
        builder = GltfBuilder()
        builder.json['asset']['extras'] = {'hatchSource': {'fixture': True}}
        position = builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3', bounds=True)
        indices = builder.accessor([0, 1, 2], 'SCALAR', 5123)
        builder.json['meshes'] = [dict(primitives=[dict(attributes={'POSITION': position}, indices=indices)])]
        builder.json['nodes'] = [dict(name='Parent', translation=[1, 0, 0], children=[1, 2]),
                                 dict(name='Shared A', mesh=0, translation=[0, 2, 0]),
                                 dict(name='Shared B', mesh=0, translation=[0, 4, 0])]
        builder.json['scenes'] = [dict(nodes=[0])]
        path = self.root/('source.'+suffix)
        builder.write(path)
        if suffix == 'gltf':
            document = json.loads(path.read_text())
            encoded = document['buffers'][0]['uri'].split(',', 1)[1]
            (self.root/'source-buffer.bin').write_bytes(base64.b64decode(encoded))
            document['buffers'][0]['uri'] = 'source-buffer.bin'
            path.write_text(json.dumps(document))
        return path

    def do_import(self, path, asset_id='imported', **kwargs):
        return import_gltf(path, self.library, asset_id=asset_id, attribution='Test author',
                           license_file=self.license, **kwargs)

    def test_real_hierarchical_external_gltf_registration_and_component_edits(self):
        path = self.source('gltf')
        before = {p.name: p.read_bytes() for p in self.root.glob('source.*')}
        imported = self.do_import(path)
        self.assertEqual(imported.report['geometry']['meshes'], 2)
        self.assertEqual(imported.report['independent_validator']['errors'], 0)
        self.assertEqual([h['semantic_path'] for h in imported.hierarchy], ['Parent', 'Parent/Shared A', 'Parent/Shared B'])
        out = self.root/'materialized'
        materialize(self.library, imported.library_reference, out)
        native = out/'imports/imported/1.0.0/native'
        b = build_example(self.root/'project')
        graph = SceneGraph.from_bundle(b, 'example-scene.json')
        mapping = add_import(graph, imported, 'model')['mapping']
        vertices = evaluate_imported_vertices(graph, imported, native)['nodes']
        np.testing.assert_allclose(vertices[mapping['node-1']][next(iter(vertices[mapping['node-1']]))][0], [1, 2, 0])
        old_b = deepcopy(vertices[mapping['node-2']])
        moved = graph.transform(mapping['node-1'])
        moved['translation'][0] = 3
        graph.set_transform(mapping['node-1'], moved)
        after = evaluate_imported_vertices(graph, imported, native)['nodes']
        np.testing.assert_allclose(after[mapping['node-1']][next(iter(after[mapping['node-1']]))][0], [4, 2, 0])
        self.assertEqual(after[mapping['node-2']], old_b)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob('source.*')})
        self.assertTrue((out/'imports/imported/1.0.0/preview.png').is_file())

    def test_real_skinned_animated_glb_native_deformation_and_independent_export(self):
        rig = rig_fixture(self.root, name='original')
        source = self.root/'animated.glb'
        export_gltf(rig, source)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        imported = self.do_import(source)
        self.assertEqual(imported.report['geometry']['skins'], 1)
        self.assertEqual(imported.report['geometry']['clips'], 2)
        self.assertEqual(imported.report['geometry']['morph_targets'], 1)
        materialize(self.library, imported.library_reference, self.root/'reopened')
        b = build_example(self.root/'project')
        graph = SceneGraph.from_bundle(b, 'example-scene.json')
        mapping = add_import(graph, imported, 'character')['mapping']
        native = self.root/'reopened/imports/imported/1.0.0/native'
        first = evaluate_imported_vertices(graph, imported, native, clip='bend', time=0)['nodes']
        middle = evaluate_imported_vertices(graph, imported, native, clip='bend', time=.5)['nodes']
        total_difference = 0
        for node_id in first:
            for mesh in first[node_id]:
                total_difference += np.linalg.norm(np.array(first[node_id][mesh])-middle[node_id][mesh])
        self.assertGreater(total_difference, .01)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
        self.assertFalse(imported.report['production_approved'])
        self.assertTrue(imported.report['fixture'])

    def test_missing_unsafe_corrupt_and_required_extension_sources_fail_without_registration(self):
        path = self.source()
        raw = json.loads(path.read_text())
        for index, mutate in enumerate((lambda d: d['buffers'][0].update(uri='../escape.bin'),
                                        lambda d: d['buffers'][0].update(uri='https://invalid.example/buffer'),
                                        lambda d: d.update(extensionsRequired=['UNSUPPORTED_REQUIRED']),
                                        lambda d: d['meshes'][0]['primitives'][0]['attributes'].update(POSITION=99999))):
            value = deepcopy(raw)
            mutate(value)
            path.write_text(json.dumps(value))
            with self.subTest(index=index), self.assertRaises(RigError): self.do_import(path, 'invalid-'+str(index))
        path.write_bytes(b'not-json')
        with self.assertRaises(RigError): self.do_import(path, 'corrupt')
        path.write_text(json.dumps(raw))
        (self.root/raw['buffers'][0]['uri']).unlink()
        with self.assertRaises(RigError): self.do_import(path, 'missing')

    def test_source_symlinks_and_aliases_are_rejected_before_native_execution(self):
        path = self.source()
        raw = json.loads(path.read_text())
        buffer = self.root/raw['buffers'][0]['uri']
        original = buffer.read_bytes()
        target = self.root/'unrelated.bin'
        target.write_bytes(original)
        buffer.unlink()
        try: buffer.symlink_to(target)
        except OSError as exc:
            if os.name == 'nt' and getattr(exc, 'winerror', None) == 1314:
                self.skipTest('Windows symlink creation privilege is unavailable.')
            raise
        with self.assertRaises(RigError): self.do_import(path)
        self.assertEqual(target.read_bytes(), original)

    def test_library_versions_keep_creation_identity_and_original_bytes(self):
        path = self.source('glb')
        first = self.do_import(path)
        second = self.do_import(path, version='1.0.1', prior_reference=first.library_reference)
        a, b = self.library.resolve(first.library_reference), self.library.resolve(second.library_reference)
        self.assertEqual(a['identity']['created_at'], b['identity']['created_at'])
        self.assertNotEqual(first.asset['skeleton'], second.asset['skeleton']) if first.asset['skeleton'] else None
        self.assertEqual(first.report['source_sha256'], second.report['source_sha256'])
        with self.assertRaises(RigError): self.do_import(path)

    def test_registered_import_reopens_and_simple_mesh_edit_is_actual_new_geometry(self):
        source = self.source('glb')
        first = self.do_import(source)
        reopened = resolve_import(self.library, first.library_reference)
        self.assertEqual(reopened.asset, first.asset)
        mesh = next(iter(first.asset['components'].values()))[0]
        changed = edit_mesh_vertices(self.library, first.library_reference, '1.0.1',
                                    [dict(mesh=mesh, vertex=0, position=[9, 2, 0])], attribution='Test author')
        self.assertEqual(changed.report['geometry']['vertices'], first.report['geometry']['vertices'])
        self.assertIn('derivation', changed.report)
        self.assertEqual(changed.report['derivation']['parent'], first.library_reference)
        materialize(self.library, changed.library_reference, self.root/'edited')
        from rig_models import load_rig
        rig = load_rig(self.root/'edited/imports/imported/1.0.1/native')
        np.testing.assert_allclose(rig.geometry.by_id()[mesh].positions[0], [9, 2, 0])
        self.assertEqual(self.library.resolve(first.library_reference)['version'], '1.0.0')
        self.assertEqual(source.read_bytes(), self.library.read_file(first.library_reference,
                         'imports/imported/1.0.0/native/source/model.glb'))

    def test_imported_asset_and_native_dependency_closure_survive_project_snapshot(self):
        imported = self.do_import(self.source('gltf'))
        b = build_example(self.root/'project')
        graph = SceneGraph.from_bundle(b, 'example-scene.json')
        add_import(graph, imported, 'model')
        manifest = self.library.resolve(imported.library_reference)
        payloads = {r['path']: self.library.read_file(imported.library_reference, r['path']) for r in manifest['files']
                    if r['path'] != imported.asset_reference['path']}
        result = snapshot_project(self.root/'project', self.root/'portable', graph, scene_path='example-scene.json',
            derived_documents={imported.asset_reference['path']: imported.asset}, extra_payloads=payloads)
        reopened = load_project(self.root/'portable')
        self.assertIn(imported.asset_reference['path'], reopened['documents'])
        self.assertIn(imported.asset_reference, reopened['project']['assets'])
        self.assertEqual(reopened['status'], 'hash-and-contract-verified')
        self.assertIn('imports/imported/1.0.0/native/source/source-buffer.bin', result['inventory'])

    def test_source_budget_and_preview_are_real(self):
        from PIL import Image
        path = self.source('glb')
        import scene3d_import
        with patch.object(scene3d_import, 'MAX_SOURCE_BYTES', 8), self.assertRaises(RigError):
            self.do_import(path)
        imported = self.do_import(path)
        picture = self.library.read_file(imported.library_reference, 'imports/imported/1.0.0/preview.png')
        import io
        image = Image.open(io.BytesIO(picture)).convert('RGB')
        self.assertEqual(image.size, (256, 256))
        colors = image.getcolors(256*256)
        self.assertGreater(len(colors), 1)
        self.assertGreater(sum(count for count, rgb in colors if rgb != (20, 28, 40)), 50)

    def _imported_rig(self):
        original = rig_fixture(self.root, name='original')
        source = self.root/'animated.glb'
        export_gltf(original, source)
        imported = self.do_import(source)
        materialize(self.library, imported.library_reference, self.root/'external')
        from rig_models import load_rig
        return load_rig(self.root/'external/imports/imported/1.0.0/native')

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BLENDER_TEST') == '1',
                         'Enable pinned Blender to independently reopen imported service assets.')
    def test_actual_imported_service_asset_blender_reopens_and_deforms(self):
        from rig_blender import export_blender
        rig = self._imported_rig()
        result = export_blender(rig, self.root/'imported-editable.blend')
        self.assertTrue(result['ok'])
        self.assertTrue(result['reopened'])
        self.assertGreater(result['compared_vertices'], 0)
        self.assertLess(result['max_vertex_error_m'], .01)

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BROWSER_TEST') == '1',
                         'Enable actual Chromium to verify imported service GLB playback.')
    def test_actual_imported_service_asset_threejs_vertex_comparison(self):
        from rig_browser import verify_browser
        from rig_viewer import create_review_ui
        rig = self._imported_rig()
        create_review_ui(rig, self.root/'viewer')
        result = verify_browser(rig, self.root/'viewer')
        self.assertTrue(result['ok'])
        self.assertTrue(result['ui_playback'])

    @unittest.skipUnless(os.environ.get('HATCH_RIG_BROWSER_TEST') == '1',
                         'Enable actual Chromium to verify version-scoped clip label mappings.')
    def test_actual_imported_scene_clip_aliases_drive_reduced_motion(self):
        from rig_browser import verify_browser
        from rig_viewer import create_review_ui
        rig = self._imported_rig()
        neutral = next(name for name, clip in rig.clips.items() if 'neutral' in clip['provenance']['operation'])
        create_review_ui(rig, self.root/'mapped-viewer', clip_aliases={'neutral': neutral})
        configuration = json.loads((self.root/'mapped-viewer/review.json').read_text())
        self.assertEqual(configuration['reduced_motion_clip'], neutral)
        result = verify_browser(rig, self.root/'mapped-viewer')
        self.assertTrue(result['ok'])
        self.assertTrue(result['ui_reduced_motion'])

    def test_viewer_clip_aliases_reject_wrong_targets_versions_and_shadowing_before_output(self):
        from rig_viewer import create_review_ui
        rig = self._imported_rig()
        names = list(rig.clips)
        for index, aliases in enumerate(({'neutral': 'missing'}, {'neutral': True}, {names[0]: names[1]}, [], {'': names[0]})):
            destination = self.root/('invalid-viewer-'+str(index))
            with self.subTest(aliases=aliases), self.assertRaises(RigError):
                create_review_ui(rig, destination, clip_aliases=aliases)
            self.assertFalse(destination.exists())
        stale = deepcopy(rig)
        stale.clips[names[0]]['version'] = '99.0.0'
        with self.assertRaises(RigError):
            create_review_ui(stale, self.root/'stale-viewer', clip_aliases={'neutral': names[0]})
        self.assertFalse((self.root/'stale-viewer').exists())


if __name__ == '__main__':
    unittest.main()
