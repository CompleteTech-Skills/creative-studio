"""Actual canonical prefab projection, immutable source and resource boundaries."""
from copy import deepcopy
import hashlib
import json
import math
import subprocess
from pathlib import Path
import tempfile
import unittest

import numpy as np

from editor3d_prefabs import prepare_prefab_projection, project_prefab_state
from editor3d_repository import EditorRepository
from rig_errors import RigError
from rig_import import load_character_model
from rig_math import transform_points
from scene3d_contracts import canonical_bytes, load_project
from scene3d_example import build_example, node
from scene3d_graph import SceneGraph
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project


class EditorPrefabProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, *, noncanonical=False, instances=2, nested_animated=False, upright=False):
        source = self.root/'source'; bundle = build_example(source)
        if noncanonical:
            reference = bundle['project']['prefabs'][0]
            payload = (json.dumps(bundle['documents'][reference['path']], indent=2) + '\n\n').encode()
            (source/reference['path']).write_bytes(payload)
            project = deepcopy(bundle['project'])
            for entry in project['prefabs'] + project['dependency_lock']:
                if entry['path'] == reference['path']:
                    entry['sha256'] = hashlib.sha256(payload).hexdigest()
            (source/'project.json').write_bytes(canonical_bytes(project))
            bundle = load_project(source)
        if nested_animated:
            library = PrefabLibrary.from_files(source, bundle)
            clip = deepcopy(bundle['documents']['example-animation.json'])
            clip['id'] = 'prefab-motion'; clip['tracks'][0]['target'] = 'prefab-mesh'
            clipref = library.register(clip)
            child = deepcopy(bundle['documents']['example-prefab.json']); child['id'] = 'animated-prefab'
            child['nodes'][1]['animation_bindings'] = [dict(target='prefab-mesh', clip=clipref,
                start_frame=0, end_frame=24, speed=1., loop=False)]
            child['dependencies'].append(clipref); childref = library.register(child)
            outer = deepcopy(bundle['documents']['example-prefab.json']); outer['id'] = 'nested-prefab'
            inner = node('inner', 'prefab_instance', 'prefab-root'); inner['prefab'] = childref
            outer['nodes'] = [outer['nodes'][0], inner]; outer['dependencies'] = [childref]
            outerref = library.register(outer)
            for name, payload in library.payloads().items():
                path = source/name
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
            project = deepcopy(bundle['project']); project['prefabs'] = [outerref]
            project['animations'].append(clipref); project['dependency_lock'].extend([clipref, childref, outerref])
            (source/'project.json').write_bytes(canonical_bytes(project)); bundle = load_project(source)
        graph = SceneGraph.from_bundle(bundle, 'example-scene.json', source_root=source)
        library = PrefabLibrary.from_files(source, bundle)
        for index in range(instances):
            library.instantiate(graph, bundle['project']['prefabs'][0], 'instance-' + str(index),
                transform=dict(translation=[index*3+1, 0, 0], rotation=[-math.sqrt(.5),0,0,math.sqrt(.5)] if upright else [0,0,0,1], scale=[1,1,1]))
        saved = snapshot_project(source, self.root/'portable', graph, scene_path='example-scene.json')
        repository = EditorRepository.create(self.root/'portable', self.root/'repo', scene_path=saved['active_scene']['path'])
        snapshot = repository.snapshot()
        captured = self.root/'captured'; captured.mkdir()
        for name in snapshot['inventory']:
            path = captured/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(repository.read_file(name, snapshot['sha256']))
        return repository, snapshot, captured

    def test_two_instances_use_canonical_scoping_native_vertices_and_preserve_saved_sources(self):
        repository, snapshot, captured = self.fixture()
        before = deepcopy(snapshot)
        sources = {name: repository.read_file(name, snapshot['sha256']) for name in snapshot['inventory']}
        value = prepare_prefab_projection(captured, snapshot)
        projection = value['projection']; runtime = value['runtime_snapshot']
        expected = PrefabLibrary.from_files(captured, snapshot).expand(snapshot['scene'])
        self.assertEqual(projection['scene'], expected['scene'])
        self.assertEqual(projection['mappings'], expected['mappings'])
        self.assertEqual(projection['original_prefab_references'], expected['original_prefab_references'])
        self.assertEqual(set(projection['readonly_nodes']), set(projection['edit_targets']))
        self.assertEqual(len(projection['readonly_nodes']), 4)
        state = project_prefab_state(value)
        vertices = load_character_model(captured/'triangle.obj').geometry.meshes[0].positions
        for index in range(2):
            anchor = 'instance-' + str(index)
            mesh = projection['mappings'][anchor]['prefab-mesh']
            self.assertEqual(projection['edit_targets'][mesh], anchor)
            actual = transform_points(np.asarray(state['nodes'][mesh]['world_matrix']), vertices)
            np.testing.assert_allclose(actual, vertices + [index*3+1,0,0], atol=1e-10, rtol=0)
            self.assertIn(mesh, state['transform_readonly'])
            self.assertNotIn(anchor, state['transform_readonly'])
            self.assertEqual(next(n for n in snapshot['scene']['nodes'] if n['id']==anchor)['type'], 'prefab_instance')
            self.assertEqual(next(n for n in runtime['scene']['nodes'] if n['id']==anchor)['type'], 'group')
        self.assertEqual(snapshot, before)
        self.assertEqual(repository.snapshot(), before)
        self.assertEqual({name: repository.read_file(name, snapshot['sha256']) for name in snapshot['inventory']}, sources)

    def test_only_authored_anchor_edits_persist_and_reopen_without_rewriting_prefabs(self):
        repository, snapshot, captured = self.fixture()
        projection = prepare_prefab_projection(captured, snapshot)['projection']
        derived = projection['readonly_nodes'][0]
        transform = dict(translation=[6,0,0], rotation=[0,0,0,1], scale=[1,1,1])
        commands = dict(schema_version=1, commands=[dict(operation='transform',
            parameters=dict(identifier=derived, transform=transform))])
        with self.assertRaises(RigError):
            repository.apply(snapshot['revision'], commands, 'reject-derived-edit', expected_sha256=snapshot['sha256'])
        self.assertEqual(repository.snapshot(), snapshot)
        commands['commands'][0]['parameters']['identifier'] = 'instance-0'
        saved = repository.apply(snapshot['revision'], commands, 'move-authored-instance', expected_sha256=snapshot['sha256'])
        reopened = EditorRepository(repository.root).snapshot()
        self.assertEqual(reopened, saved)
        anchor = next(node for node in reopened['scene']['nodes'] if node['id']=='instance-0')
        self.assertEqual(anchor['type'], 'prefab_instance')
        self.assertEqual(anchor['transform'], transform)
        self.assertEqual(reopened['project']['prefabs'], snapshot['project']['prefabs'])
        for reference in snapshot['project']['prefabs']:
            self.assertEqual(repository.read_file(reference['path'], reopened['sha256']),
                             repository.read_file(reference['path'], snapshot['sha256']))
        self.assertTrue(set(projection['readonly_nodes']).isdisjoint({node['id'] for node in reopened['scene']['nodes']}))

    def test_nested_prefabs_and_derived_animation_use_static_canonical_context(self):
        repository, snapshot, captured = self.fixture(nested_animated=True)
        before = deepcopy(snapshot)
        prepared = prepare_prefab_projection(captured, snapshot)
        projection = prepared['projection']; state = project_prefab_state(prepared)
        self.assertEqual(len(projection['readonly_nodes']), 8)
        self.assertEqual(set(projection['edit_targets'].values()), {'instance-0', 'instance-1'})
        self.assertEqual(len(projection['derived_documents']), 3)
        self.assertTrue(state['base_pose_only']); self.assertFalse(state['animation_evaluated'])
        for name in projection['derived_documents']:
            self.assertFalse((captured/name).exists())
        self.assertEqual(repository.snapshot(), before)
        self.assertEqual(snapshot, before)
        self.assertTrue(all(identifier in state['nodes'] for identifier in projection['readonly_nodes']))

    def test_javascript_maps_every_derived_selection_to_its_authored_anchor(self):
        _, snapshot, captured = self.fixture()
        snapshot['viewport_prefabs'] = prepare_prefab_projection(captured, snapshot)['projection']
        code = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
const {source,snapshot}=JSON.parse(fs.readFileSync(0,'utf8'));
const api=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const before=JSON.stringify(snapshot);
assert.equal(api.prefabRuntimeScene(snapshot).nodes.length,9);
for(const [id,anchor] of Object.entries(snapshot.viewport_prefabs.edit_targets)) {
  assert.equal(api.prefabEditTarget(snapshot,id),anchor);
  assert.equal(api.prefabReadonly(snapshot,id),true);
  assert.equal(api.prefabAuthoredNode(snapshot,id).type,'prefab_instance');
  assert.equal(api.prefabAuthoredNode(snapshot,id).id,anchor);
}
assert.equal(api.prefabEditTarget(snapshot,'instance-0'),'instance-0');
assert.equal(api.prefabReadonly(snapshot,'instance-0'),false);
assert.throws(()=>api.prefabEditTarget(snapshot,'unbound-node'));
const id=snapshot.viewport_prefabs.readonly_nodes[0];
for(const mutate of [s=>s.viewport_prefabs.sha256='0'.repeat(64),
 s=>s.viewport_prefabs.edit_targets[id]=s.viewport_prefabs.edit_targets[id]==='instance-0'?'instance-1':'instance-0',
 s=>s.viewport_prefabs.readonly_nodes.push(id),
 s=>s.viewport_prefabs.scene.nodes.find(n=>n.id===id).parent=id,
 s=>s.viewport_prefabs.scene.nodes.push(s.viewport_prefabs.scene.nodes[0])]) {
 const bad=structuredClone(snapshot);mutate(bad);
 assert.throws(()=>api.prefabRuntimeScene(bad));
}
const absent=structuredClone(snapshot);delete absent.viewport_prefabs;
assert.throws(()=>api.prefabRuntimeScene(absent));
assert.equal(JSON.stringify(snapshot),before);
console.log('PASS: real canonical runtime metadata; authored anchors; wrong-parent/identity/duplicate/cycle rejection');
"""
        root = Path(__file__).resolve().parents[1]
        request = json.dumps(dict(source=(root/'assets/editor3d/prefabs.js').read_text(), snapshot=snapshot))
        result = subprocess.run(['node','--input-type=module','-e',code], input=request,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_noncanonical_prefab_source_bytes_keep_their_original_reference(self):
        _, snapshot, captured = self.fixture(noncanonical=True)
        reference = snapshot['project']['prefabs'][0]
        original = (captured/reference['path']).read_bytes()
        self.assertNotEqual(original, canonical_bytes(snapshot['documents'][reference['path']]))
        prepared = prepare_prefab_projection(captured, snapshot)
        self.assertEqual(prepared['projection']['original_prefab_references'], [reference])
        self.assertEqual((captured/reference['path']).read_bytes(), original)
        self.assertEqual(hashlib.sha256(original).hexdigest(), reference['sha256'])

    def test_expansion_respects_interactive_nodes_files_bytes_and_depth(self):
        _, snapshot, captured = self.fixture()
        original = deepcopy(snapshot)
        total = sum(record['bytes'] for record in snapshot['inventory'].values())
        for limit in (dict(max_nodes=len(snapshot['scene']['nodes'])),
                      dict(max_files=len(snapshot['inventory'])), dict(max_bytes=total), dict(max_depth=1)):
            with self.subTest(limit=limit), self.assertRaises(RigError):
                prepare_prefab_projection(captured, snapshot, **limit)
        self.assertEqual(snapshot, original)

    def test_invalid_limit_values_are_typed_rejections(self):
        _, snapshot, captured = self.fixture()
        for value in (True, False, 0, -1, 1.5, math.inf, 10**400):
            with self.subTest(value=value), self.assertRaises(RigError):
                prepare_prefab_projection(captured, snapshot, max_nodes=value)

    def test_native_integral_json_project_budgets_are_used_without_mutating_documents(self):
        _, snapshot, captured = self.fixture()
        value = deepcopy(snapshot)
        for field in ('max_nodes','max_files','max_bytes'):
            value['project']['budgets'][field] = float(value['project']['budgets'][field])
        before = deepcopy(value)
        self.assertTrue(prepare_prefab_projection(captured, value)['projection']['readonly_nodes'])
        self.assertEqual(value, before)

    def test_changed_source_manifest_is_rejected_before_projection(self):
        _, snapshot, captured = self.fixture()
        path = captured/snapshot['scene_path']; path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(RigError): prepare_prefab_projection(captured, snapshot)

    def test_changed_prefab_bytes_and_symlink_sources_are_rejected(self):
        _, snapshot, captured = self.fixture()
        reference = snapshot['project']['prefabs'][0]; path = captured/reference['path']
        original = path.read_bytes(); path.write_bytes(original+b' ')
        with self.assertRaises(RigError): prepare_prefab_projection(captured, snapshot)
        path.unlink(); outside = self.root/'outside.json'; outside.write_bytes(original)
        try: path.symlink_to(outside)
        except OSError: self.skipTest('Filesystem does not support symlink creation.')
        with self.assertRaises((RigError, OSError)): prepare_prefab_projection(captured, snapshot)

    def test_unsafe_scene_path_and_virtual_namespace_conflicts_fail_closed(self):
        _, snapshot, captured = self.fixture()
        bad = deepcopy(snapshot); bad['scene_path'] = '../outside.json'
        with self.assertRaises(RigError): prepare_prefab_projection(captured, bad)
        bad = deepcopy(snapshot)
        name = 'editor-runtime-' + snapshot['sha256'] + '/scene.json'
        bad['inventory'][name] = dict(sha256='0'*64, bytes=1)
        with self.assertRaises(RigError): prepare_prefab_projection(captured, bad)

    def test_no_prefab_requires_no_derived_runtime_projection(self):
        _, snapshot, captured = self.fixture(instances=0)
        before = deepcopy(snapshot)
        self.assertIsNone(prepare_prefab_projection(captured, snapshot))
        self.assertEqual(snapshot, before)


if __name__ == '__main__': unittest.main()
