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

    def nested_catalog(self, *, property='translation'):
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        outer = deepcopy(self.bundle['documents']['example-prefab.json'])
        outer.update(id='outer-source', nodes=outer['nodes'][:1], dependencies=[], material_slots=[], sockets=[])
        outer['provenance'].update(origin='authored', fixture=False, inputs=[])
        outer_ref = library.register(outer)
        inner = deepcopy(self.bundle['documents']['example-prefab.json'])
        inner['id'] = 'inner-source'
        inner['parameters'] = {'amount': 1. if property == 'scale' else 0.}
        inner['parameter_bindings'] = [dict(parameter='amount', node='prefab-mesh', property=property, component=0)]
        inner_ref = library.register(inner)
        return library, outer_ref, inner_ref, outer, inner

    def test_nested_effective_override_admission_snapshot_and_reopen_fail_closed(self):
        for index, (property, parameters) in enumerate([
                ('translation', {'unknown': 1}), ('translation', {'amount': 'bad'}),
                ('scale', {'amount': 0.}), ('rotation', {'amount': .5})]):
            # Each case has a separate immutable source catalog.
            library, outer, inner, outer_doc, inner_doc = self.nested_catalog(property=property)
            child = node('optional-instance', 'prefab_instance', parent='prefab-root')
            child['prefab'] = inner
            child['prefab_overrides'] = dict(parameters=parameters, materials={}, nodes=[])
            overrides = dict(parameters={}, materials={}, nodes=[], children=[child])
            graph = self.graph()
            before = graph.document()
            with self.subTest(property=property, parameters=parameters), self.assertRaises(RigError):
                library.instantiate(graph, outer, 'outer-instance', overrides=overrides)
            self.assertEqual(before, graph.document())
            # Bypass the service to exercise actual staged reopening of a foreign document.
            foreign = graph.document()
            instance = node('outer-instance', 'prefab_instance', parent='root')
            instance.update(prefab=outer, prefab_overrides=overrides)
            foreign['nodes'].append(instance)
            malformed = SceneGraph(foreign, external_references={})
            destination = self.root/('invalid-nested-'+str(index))
            with self.assertRaises(RigError):
                snapshot_project(self.root/'source', destination, malformed, scene_path='example-scene.json',
                    derived_documents={outer['path']:outer_doc, inner['path']:inner_doc})
            self.assertFalse(destination.exists())

    def test_valid_nested_effective_controls_propagate_fixture_and_match_expansion_counts(self):
        from scene3d_contracts import validate_prefab_instances
        library, outer, inner, outer_doc, inner_doc = self.nested_catalog()
        child = node('optional-instance', 'prefab_instance', parent='prefab-root')
        child['prefab'] = inner
        child['prefab_overrides'] = dict(parameters={'amount': 2.}, materials={}, nodes=[])
        scene = self.graph().document()
        scene['provenance'].update(origin='authored', fixture=False)
        graph = SceneGraph(scene, external_references={})
        library.instantiate(graph, outer, 'outer-instance', overrides=dict(parameters={}, materials={}, nodes=[], children=[child]))
        self.assertTrue(graph.document()['provenance']['fixture'])
        expanded = library.expand(graph.document())
        evidence = validate_prefab_instances(graph.document(), library.resolve)
        self.assertEqual(evidence['expanded_nodes'], len(expanded['scene']['nodes']))
        with self.assertRaises(RigError):
            validate_prefab_instances(graph.document(), library.resolve, max_nodes=evidence['expanded_nodes']-1)
        destination = self.root/'valid-nested'
        snapshot = snapshot_project(self.root/'source', destination, graph, scene_path='example-scene.json',
            derived_documents={outer['path']:outer_doc, inner['path']:inner_doc})
        reopened = load_project(destination)
        self.assertEqual(reopened['status'], 'hash-and-contract-verified')
        self.assertTrue(reopened['documents'][snapshot['active_scene']['path']]['provenance']['fixture'])

    def test_effective_prefab_depth_counts_actual_transform_ancestors(self):
        library = PrefabLibrary()
        base = deepcopy(self.bundle['documents']['example-prefab.json'])
        base.update(id='deep-inner', nodes=[node('inner-'+str(i), parent=None if i == 0 else 'inner-'+str(i-1))
                                        for i in range(70)], roots=['inner-0'], material_slots=[], sockets=[], dependencies=[])
        inner = library.register(base)
        outer = deepcopy(base)
        outer.update(id='deep-outer', nodes=[node('outer-'+str(i), parent=None if i == 0 else 'outer-'+str(i-1))
                                          for i in range(70)], roots=['outer-0'])
        outer['nodes'][-1].update(type='prefab_instance', prefab=inner)
        reference = library.register(outer)
        graph = self.graph()
        before = graph.document()
        with self.assertRaises(RigError):
            library.instantiate(graph, reference, 'too-deep')
        self.assertEqual(before, graph.document())

    def test_effective_closure_memo_keeps_logical_budgets_and_scoped_id_collisions(self):
        from scene3d_contracts import prefab_scoped_id, validate_prefab_instances
        library, outer, inner, outer_doc, inner_doc = self.nested_catalog()
        scene = self.graph().document()
        scene['metadata'] = dict(kind='scene3d-prefab', path='missing.json', id='missing', version='1.0.0', sha256='f'*64)
        for index in range(8):
            instance = node('instance-'+str(index), 'prefab_instance', parent='root')
            instance['prefab'] = inner
            scene['nodes'].append(instance)
        with patch.object(library, 'resolve', wraps=library.resolve) as resolve:
            evidence = validate_prefab_instances(scene, resolve)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(evidence['expanded_nodes'], len(scene['nodes'])+8*len(inner_doc['nodes']))
        with self.assertRaises(RigError):
            validate_prefab_instances(scene, library.resolve, max_nodes=evidence['expanded_nodes']-1)
        graph = self.graph()
        collision = prefab_scoped_id('/collision-instance', 'prefab-root')
        graph.create(node(collision, parent='root'))
        before = graph.document()
        with self.assertRaises(RigError):
            library.instantiate(graph, inner, 'collision-instance')
        self.assertEqual(before, graph.document())

    def test_review_disabled_look_at_is_noop_even_when_coincident(self):
        scene = deepcopy(self.scene)
        mesh = next(n for n in scene['nodes'] if n['id'] == 'mesh')
        target = node('coincident')
        target['transform'] = deepcopy(mesh['transform'])
        scene['nodes'].append(target)
        scene['roots'].append('coincident')
        mesh['constraints'] = [dict(id='disabled',type='look-at',target='coincident',influence=0.)]
        graph = self.graph(scene)
        actual = graph.evaluate_base('mesh')['world_matrix']
        mesh['constraints'] = []
        self.assertEqual(actual, self.graph(scene).evaluate_base('mesh')['world_matrix'])

    def test_review_explicit_falsey_optional_values_are_not_defaults(self):
        library = PrefabLibrary.from_bundle(self.bundle)
        reference = self.bundle['project']['prefabs'][0]
        for key, values in [('overrides',[[],False,'']),('transform',[[],False,''])]:
            for value in values:
                graph = self.graph()
                before = graph.document()
                with self.subTest(key=key,value=value),self.assertRaises(RigError):
                    library.instantiate(graph,reference,'invalid',**{key:value})
                self.assertEqual(before,graph.document())
        for values in (dict(parameters=[]),dict(parameters=False),dict(parameter_bindings={}),dict(parameter_bindings=False)):
            before = library.payloads()
            with self.subTest(values=values),self.assertRaises(RigError):
                library.create(self.graph(),'mesh','invalid','1.0.0',**values)
            self.assertEqual(before,library.payloads())
        for values in (dict(asset_bounds=False),dict(animation_documents=False),dict(animation_payloads=False)):
            with self.subTest(values=values),self.assertRaises(RigError):
                SceneGraph(self.scene,**values)

    def test_review_animation_context_binds_exact_noncanonical_bytes(self):
        source = deepcopy(self.bundle['documents']['example-animation.json'])
        payload = (json.dumps(source,indent=4)+'  \n').encode()
        reference = deepcopy(self.bundle['project']['animations'][0])
        reference['sha256'] = hashlib.sha256(payload).hexdigest()
        scene = deepcopy(self.scene)
        scene['nodes'][1]['animation_bindings'] = [dict(target='mesh',clip=reference,start_frame=0,end_frame=24,speed=1.,loop=False)]
        tampered = deepcopy(source)
        tampered['tracks'][0]['keys'][0]['value'] = [99,99,99]
        with self.assertRaises(RigError):
            SceneGraph(scene,animation_documents={reference['path']:tampered},animation_payloads={reference['path']:payload})
        with self.assertRaises(RigError):
            SceneGraph(scene,animation_documents={reference['path']:tampered})
        graph = SceneGraph(scene,external_references={},animation_payloads={reference['path']:payload})
        derived = graph.duplicate('mesh','copy')['derived_documents']
        self.assertEqual(next(iter(derived.values()))['provenance']['inputs'][-1]['sha256'], reference['sha256'])
        self.assertEqual(graph._animation_payloads[reference['path']], payload)
        # A real noncanonical source closure reopens without rewriting its bytes.
        (self.root/'source'/reference['path']).write_bytes(payload)
        project = deepcopy(self.bundle['project'])
        for field in ('animations','dependency_lock'):
            for item in project[field]:
                if item['path'] == reference['path']:
                    item['sha256'] = reference['sha256']
        (self.root/'source/project.json').write_bytes(canonical_bytes(project))
        bundle = load_project(self.root/'source')
        bundle['documents']['example-scene.json'] = scene
        exact = SceneGraph.from_bundle(bundle,'example-scene.json',source_root=self.root/'source')
        exact.duplicate('mesh','portable-copy')
        snapshot_project(self.root/'source',self.root/'noncanonical-copy',exact,scene_path='example-scene.json')
        self.assertEqual((self.root/'noncanonical-copy'/reference['path']).read_bytes(),payload)
        load_project(self.root/'noncanonical-copy')

    def test_review_combined_source_and_derived_animation_budgets_are_atomic(self):
        clip = deepcopy(self.bundle['documents']['example-animation.json'])
        raw = canonical_bytes(clip)
        reference = deepcopy(self.bundle['project']['animations'][0])
        scene = deepcopy(self.scene)
        scene['nodes'][1]['animation_bindings'] = [dict(target='mesh',clip=reference,start_frame=0,end_frame=24,speed=1.,loop=False)]
        context = {reference['path']:raw}
        baseline = SceneGraph(scene,animation_payloads=context)
        derived = baseline.duplicate('mesh','copy')['derived_documents']
        combined = len(raw)+sum(len(canonical_bytes(d)) for d in derived.values())
        for setting, limit in [('MAX_FILES',1),('MAX_TOTAL_BYTES',combined-1)]:
            graph = SceneGraph(scene,animation_payloads=context)
            before = graph.document()
            with patch('scene3d_graph.'+setting,limit),self.assertRaises(RigError):
                graph.duplicate('mesh','copy')
            self.assertEqual(before,graph.document())
            self.assertEqual({},graph.derived_documents())
            self.assertEqual(context,graph._animation_payloads)
        with patch('scene3d_graph.MAX_FILES',2),patch('scene3d_graph.MAX_TOTAL_BYTES',combined):
            graph = SceneGraph(scene,animation_payloads=context)
            graph.duplicate('mesh','copy')
            self.assertEqual(len(graph._animation_payloads),2)

    def test_review_duplicate_clip_dag_is_memoized(self):
        payloads, reference = {}, None
        for index in range(30):
            clip = deepcopy(self.bundle['documents']['example-animation.json'])
            clip['id'] = 'dag-'+str(index)
            clip['bindings'] = [] if reference is None else [dict(target='mesh',clip=deepcopy(reference),start_frame=0,end_frame=24,speed=1.,loop=False) for _ in range(2)]
            clip['dependencies'] = [] if reference is None else [reference]
            payload = canonical_bytes(clip)
            path = 'dag/'+str(index)+'.json'
            payloads[path] = payload
            reference = dict(id=clip['id'],version=clip['version'],kind=clip['kind'],path=path,sha256=hashlib.sha256(payload).hexdigest())
        scene = deepcopy(self.scene)
        scene['nodes'][1]['animation_bindings'] = [dict(target='mesh',clip=reference,start_frame=0,end_frame=24,speed=1.,loop=False)]
        graph = SceneGraph(scene,animation_payloads=payloads)
        with patch.object(graph,'_map_clip',wraps=graph._map_clip) as calls:
            result = graph.duplicate('mesh','copy')
        self.assertEqual(len(result['derived_documents']),30)
        self.assertLessEqual(calls.call_count,60)

    def test_review_remaining_derived_bindings_protect_targets_and_removed_bindings_release_them(self):
        clip = deepcopy(self.bundle['documents']['example-animation.json'])
        clip['tracks'][0]['target'] = 'child'
        payload = canonical_bytes(clip)
        reference = deepcopy(self.bundle['project']['animations'][0])
        reference['sha256'] = hashlib.sha256(payload).hexdigest()
        scene = deepcopy(self.scene)
        scene['nodes'].append(node('child',parent='mesh'))
        scene['nodes'][1]['animation_bindings'] = [dict(target='mesh',clip=reference,start_frame=0,end_frame=24,speed=1.,loop=False)]
        graph = SceneGraph(scene,external_references={},animation_payloads={reference['path']:payload})
        copied = graph.duplicate('mesh','copy')
        child = copied['mapping']['child']
        before = graph.document()
        for operation in (lambda:graph.delete(child),lambda:graph.reparent(child,None)):
            with self.assertRaises(RigError): operation()
            self.assertEqual(before,graph.document())
        changed = graph.document()
        next(n for n in changed['nodes'] if n['id']=='copy')['animation_bindings'] = []
        graph.replace_document(changed,expected_revision=graph.revision)
        graph.delete(child)
        self.assertNotIn(child,{n['id'] for n in graph.document()['nodes']})

    def test_review_upgrade_optional_child_source_resolution_removes_entire_subtree(self):
        library = PrefabLibrary()
        old = deepcopy(self.bundle['documents']['example-prefab.json'])
        reference = library.register(old)
        new = deepcopy(old)
        new['version'] = '2.0.0'
        new['nodes'] = new['nodes'][:1]
        upgraded = library.register(new)
        graph = self.graph()
        library.instantiate(graph,reference,'instance',overrides=dict(parameters={},materials={},nodes=[],
            children=[node('optional',parent='prefab-mesh'),node('grandchild',parent='optional')]))
        result = library.upgrade(graph,'instance',upgraded)
        self.assertEqual(result['conflicts'],['child:optional'])
        result = library.upgrade(graph,'instance',upgraded,resolutions={'child:optional':'source'})
        self.assertTrue(result['applied'])
        instance = next(n for n in graph.document()['nodes'] if n['id']=='instance')
        self.assertEqual(instance['prefab_overrides']['children'],[])
        library.expand(graph.document())

    def test_review_world_matrix_batch_preserves_supported_transform_interface(self):
        graph = self.graph()
        before = graph.evaluate_base('mesh')['world_matrix']
        desired = deepcopy(before)
        desired[0][3] += 3.
        apply_commands(graph, PrefabLibrary(), dict(schema_version=1, commands=[dict(operation='transform',
            parameters=dict(identifier='mesh',space='world',transform=desired))]))
        np.testing.assert_allclose(graph.evaluate_base('mesh')['world_matrix'], desired)
        original = graph.document()
        with self.assertRaises(RigError):
            apply_commands(graph, PrefabLibrary(), dict(schema_version=1, commands=[dict(operation='transform',
                parameters=dict(identifier='mesh',space='world',transform=[1,2,3]))]))
        self.assertEqual(original, graph.document())

    def test_review_command_types_reject_before_any_mutation(self):
        invalid = [('transform', dict(identifier=[], transform=TRANSFORM)),
                   ('duplicate', dict(identifier='mesh', new_id=[])),
                   ('reparent', dict(identifier='mesh', parent=[], preserve_world=True)),
                   ('reparent', dict(identifier='mesh', parent=None, preserve_world='false')),
                   ('collection', dict(identifier='new', name='New', layer='false'))]
        for operation, parameters in invalid:
            graph, catalog = self.graph(), PrefabLibrary()
            before = graph.document()
            with self.subTest(operation=operation), self.assertRaises(RigError):
                apply_commands(graph, catalog, dict(schema_version=1, commands=[dict(operation=operation, parameters=parameters)]))
            self.assertEqual(before, graph.document())
            self.assertEqual({}, catalog.payloads())

    def test_review_integral_float_component_is_schema_valid_and_effective(self):
        source = deepcopy(self.bundle['documents']['example-prefab.json'])
        source['parameters'] = {'offset': 2.0}
        source['parameter_bindings'] = [dict(parameter='offset',node='prefab-mesh',property='translation',component=0.0)]
        validate_document(source, 'prefab')
        library = PrefabLibrary()
        reference = library.register(source)
        graph = self.graph()
        library.instantiate(graph, reference, 'instance')
        expanded = library.expand(graph.document())
        target = expanded['mappings']['instance']['prefab-mesh']
        self.assertEqual(next(n for n in expanded['scene']['nodes'] if n['id'] == target)['transform']['translation'][0], 2.)
        self.assertEqual(library.resolve(reference)['parameter_bindings'][0]['component'], 0.0)

    def test_review_prefab_create_with_linked_context_is_atomic(self):
        library = PrefabLibrary.from_bundle(self.bundle)
        graph = self.graph()
        reference = self.bundle['project']['prefabs'][0]
        library.instantiate(graph, reference, 'instance')
        created = library.create(graph, 'mesh', 'extracted', '1.0.0')
        self.assertEqual(created['placement'], SceneGraph(library.expand(graph.document())['scene']).evaluate_base('mesh')['world_matrix'])
        before = library.payloads()
        with self.assertRaises(RigError):
            library.create(graph, 'missing', 'missing', '1.0.0')
        self.assertEqual(before, library.payloads())

    def test_review_duplicate_animated_subtree_derives_complete_clip_closure(self):
        bundle = deepcopy(self.bundle)
        scene = bundle['documents']['example-scene.json']
        scene['nodes'][1]['animation_bindings'] = [dict(target='mesh',clip=bundle['project']['animations'][0],start_frame=0,end_frame=24,speed=1.,loop=False)]
        graph = SceneGraph.from_bundle(bundle, 'example-scene.json', source_root=self.root/'source')
        original = deepcopy(bundle['documents']['example-animation.json'])
        copied = graph.duplicate('mesh', 'mesh-copy')
        self.assertTrue(copied['derived_documents'])
        clip = next(iter(copied['derived_documents'].values()))
        self.assertEqual(clip['tracks'][0]['target'], 'mesh-copy')
        self.assertEqual(original, bundle['documents']['example-animation.json'])
        snapshot_project(self.root/'source', self.root/'animated-copy', graph, scene_path='example-scene.json')
        reopened = load_project(self.root/'animated-copy')
        self.assertTrue(any(d['kind'] == 'scene3d-animation' and d['id'] == clip['id'] for d in reopened['documents'].values()))
        missing = self.graph(scene)
        before = missing.document()
        with self.assertRaises(RigError):
            missing.duplicate('mesh', 'requires-context')
        self.assertEqual(before, missing.document())

    def test_review_detach_keeps_external_constraints_and_other_linked_instances(self):
        library = PrefabLibrary.from_bundle(self.bundle)
        graph = self.graph()
        reference = self.bundle['project']['prefabs'][0]
        library.instantiate(graph, reference, 'instance')
        library.instantiate(graph, reference, 'other')
        scene = graph.document()
        instance = next(n for n in scene['nodes'] if n['id'] == 'instance')
        instance['constraints'] = [dict(id='follow',type='copy-transform',target='mesh',influence=1.)]
        graph.replace_document(scene, expected_revision=graph.revision)
        result = library.detach(graph, 'instance')
        self.assertIn('instance', result['mappings'])
        self.assertEqual(next(n for n in graph.document()['nodes'] if n['id'] == 'other')['type'], 'prefab_instance')
        self.assertEqual(next(n for n in graph.document()['nodes'] if n['id'] == 'instance')['constraints'][0]['target'], 'mesh')

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
        before = library.payloads()
        with self.assertRaises(RigError): library.register(p)
        self.assertEqual(library.payloads(), before)

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

    def test_typed_node_and_world_edits_preserve_identity_and_reopen(self):
        graph = self.graph()
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        before = graph.document()
        camera = deepcopy(next(n for n in before['nodes'] if n['id'] == 'camera')['camera'])
        camera['vertical_fov'] = 1.0
        edits = dict(schema_version=1, commands=[
            dict(operation='rename', parameters=dict(identifier='mesh', name='Renamed triangle')),
            dict(operation='configure-node', parameters=dict(identifier='camera', properties={'camera':camera})),
            dict(operation='configure-world', parameters=dict(properties={'exposure':1.5}))])
        apply_commands(graph, library, edits)
        self.assertEqual(graph.revision, before['revision']+1)
        self.assertEqual(graph.query(name='Renamed triangle'), ['mesh'])
        self.assertEqual(graph.transform('mesh'), next(n for n in before['nodes'] if n['id'] == 'mesh')['transform'])
        snapshot = snapshot_project(self.root/'source', self.root/'configured', graph, scene_path='example-scene.json')
        reopened = load_project(self.root/'configured')['documents'][snapshot['active_scene']['path']]
        self.assertEqual(reopened['world']['exposure'], 1.5)
        self.assertEqual(next(n for n in reopened['nodes'] if n['id'] == 'camera')['camera'], camera)

    def test_typed_property_updates_reject_invalid_stale_locked_and_atomic_batches(self):
        graph = self.graph()
        library = PrefabLibrary.from_files(self.root/'source', self.bundle)
        before = graph.document()
        for props in ({'id':'replacement'}, {'parent':None}, {'name':''}, {'light':{'intensity':2}}, {'metadata':{'x':{}}}):
            with self.subTest(props=props), self.assertRaises(RigError):
                graph.configure_node('mesh', props)
            self.assertEqual(graph.document(), before)
        with self.assertRaises(RigError): graph.rename('mesh','stale',expected_revision=99)
        for props in ({'coordinate_system':'other'}, {'exposure':float('inf')}, {'environment':'bad'}):
            with self.subTest(props=props), self.assertRaises(RigError): graph.configure_world(props)
            self.assertEqual(graph.document(), before)
        with self.assertRaises(RigError):
            apply_commands(graph, library, dict(schema_version=1,commands=[
                dict(operation='rename',parameters=dict(identifier='mesh',name='temporary')),
                dict(operation='configure-world',parameters=dict(properties={'exposure':'bad'}))]))
        self.assertEqual(graph.document(), before)
        graph.set_node_flags('mesh',locked=True)
        locked = graph.document()
        with self.assertRaises(RigError): graph.rename('mesh','locked')
        self.assertEqual(graph.document(), locked)

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

    def test_review_malformed_uri_and_mixed_camera_light_declarations(self):
        path = self.source('gltf')
        document = json.loads(path.read_text())
        document['buffers'][0]['uri'] = 'http://['
        path.write_text(json.dumps(document))
        with self.assertRaises(RigError):
            source_snapshot(path, self.root/'bad-uri')
        path = self.source('gltf')
        document = json.loads(path.read_text())
        document['cameras'] = [dict(type='perspective',perspective=dict(yfov=.8,znear=.1,zfar=100.))]
        document['extensionsUsed'] = ['KHR_lights_punctual']
        document['extensions'] = {'KHR_lights_punctual': {'lights':[dict(type='point')]}}
        document['nodes'][1]['camera'] = 0
        document['nodes'][1]['extensions'] = {'KHR_lights_punctual': {'light':0}}
        path.write_text(json.dumps(document))
        imported = self.do_import(path)
        self.assertTrue({'combined-node-camera','combined-node-light'} <= {loss['feature'] for loss in imported.report['losses']})
        for key, value in [('camera',99), ('camera',False)]:
            document['nodes'][1][key] = value
            path.write_text(json.dumps(document))
            with self.subTest(value=value), self.assertRaises(RigError):
                self.do_import(path, asset_id='invalid-camera')
        document['nodes'][1]['camera'] = 0
        document['cameras'][0]['perspective']['znear'] = -1
        path.write_text(json.dumps(document))
        with self.assertRaises(RigError):
            self.do_import(path, asset_id='invalid-config')

    def do_import(self, path, asset_id='imported', **kwargs):
        return import_gltf(path, self.library, asset_id=asset_id, attribution='Test author',
                           license_file=self.license, **kwargs)

    def test_native_evaluation_uses_private_verified_closure_after_source_replacement(self):
        imported = self.do_import(self.source('gltf'))
        materialize(self.library, imported.library_reference, self.root/'admitted-package')
        native = self.root/'admitted-package/imports/imported/1.0.0/native'
        graph = SceneGraph.from_bundle(build_example(self.root/'admitted-project'), 'example-scene.json')
        add_import(graph, imported, 'model')
        expected = evaluate_imported_vertices(graph, imported, native)
        from rig_models import load_rig
        def replace_source_then_load(private_root):
            self.assertNotEqual(Path(private_root), native)
            (native/'rig.json').write_bytes(b'changed after admission')
            return load_rig(private_root)
        with patch('rig_models.load_rig', side_effect=replace_source_then_load):
            self.assertEqual(evaluate_imported_vertices(graph, imported, native), expected)
        with self.assertRaises(RigError):
            evaluate_imported_vertices(graph, imported, native)

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'mkfifo'), 'Real POSIX FIFO admission regression.')
    def test_native_evaluation_rejects_fifo_manifest_and_dependency_without_hanging(self):
        import multiprocessing
        imported = self.do_import(self.source('gltf'))
        materialize(self.library, imported.library_reference, self.root/'fifo-package')
        native = self.root/'fifo-package/imports/imported/1.0.0/native'
        graph = SceneGraph.from_bundle(build_example(self.root/'fifo-project'), 'example-scene.json')
        add_import(graph, imported, 'model')
        context = multiprocessing.get_context('fork')
        for name in ('rig.json', 'identity.json'):
            with self.subTest(name=name):
                target = native/name
                original = target.read_bytes()
                target.unlink()
                os.mkfifo(target)
                def evaluate_fifo():
                    try:
                        evaluate_imported_vertices(graph, imported, native)
                    except RigError:
                        return
                    raise AssertionError('FIFO was accepted as a registered native file.')
                child = context.Process(target=evaluate_fifo)
                child.start()
                child.join(3)
                hung = child.is_alive()
                if hung:
                    child.terminate()
                    child.join(3)
                target.unlink()
                target.write_bytes(original)
                self.assertFalse(hung, 'Native evaluation blocked on a FIFO.')
                self.assertEqual(child.exitcode, 0)

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

    def native_evidence(self, label, function):
        def retain(report):
            destination = os.environ.get('HATCH_SCENE3D_EVIDENCE')
            if destination:
                output = Path(destination)/self.id()
                output.mkdir(parents=True, exist_ok=True)
                (output/(label+'.json')).write_text(json.dumps(report,sort_keys=True,indent=2,allow_nan=False)+'\n')
        try:
            result = function()
        except BaseException as exc:
            retain(dict(ok=False, exception=type(exc).__name__, error=getattr(exc,'report',str(exc))))
            raise
        retain(result)
        return result

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
        result = self.native_evidence('blender', lambda: export_blender(rig, self.root/'imported-editable.blend'))
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
        result = self.native_evidence('browser-rest', lambda: verify_browser(rig, self.root/'viewer'))
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
        result = self.native_evidence('browser-mapped', lambda: verify_browser(rig, self.root/'mapped-viewer'))
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
