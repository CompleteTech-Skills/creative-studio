"""Actual canonical two-component import projected through the saved editor."""
import os
import json
from pathlib import Path
import threading
import unittest
import numpy as np

import test_rig_scene3d_service as scene_service_tests
from scene3d_example import build_example
from scene3d_graph import SceneGraph
from scene3d_import import add_import, evaluate_imported_vertices, materialize
from scene3d_snapshot import snapshot_project
from editor3d_repository import EditorRepository
from editor3d_viewer import EditorServer, preview_snapshot
from rig_math import transform_points


class EditorImportedComponentTests(unittest.TestCase):
    def setUp(self):
        case = scene_service_tests.RealSceneImportTests(); case.setUp()
        self.addCleanup(case.doCleanups)
        imported = case.do_import(case.source('gltf'))
        bundle = build_example(case.root/'project')
        graph = SceneGraph.from_bundle(bundle, 'example-scene.json')
        add_import(graph, imported, 'model')
        materialize(case.library, imported.library_reference, case.root/'materialized')
        self.expected = evaluate_imported_vertices(graph, imported,
            case.root/'materialized/imports/imported/1.0.0/native')['nodes']
        manifest = case.library.resolve(imported.library_reference)
        payloads = {record['path']:case.library.read_file(imported.library_reference, record['path'])
            for record in manifest['files'] if record['path'] != imported.asset_reference['path']}
        saved = snapshot_project(case.root/'project', case.root/'portable', graph,
            scene_path='example-scene.json', derived_documents={imported.asset_reference['path']:imported.asset}, extra_payloads=payloads)
        self.repository = EditorRepository.create(case.root/'portable', case.root/'repo', scene_path=saved['active_scene']['path'])

    def test_exact_native_mesh_membership_and_world_vertices_survive_projection(self):
        value = preview_snapshot(self.repository, self.repository.snapshot())
        graph = SceneGraph(value['scene'])
        from unittest.mock import patch
        with patch('editor3d_viewer.MAX_PREVIEW_VERTICES', 9):
            bounded = preview_snapshot(self.repository, self.repository.snapshot())
        self.assertTrue(all(bounded['viewport_nodes'][identifier]['format'] == 'geometry' for identifier in self.expected))
        for identifier, expected_meshes in self.expected.items():
            projection = value['viewport_nodes'][identifier]
            self.assertEqual(projection['format'], 'geometry')
            self.assertEqual({mesh['id'] for mesh in projection['meshes']}, set(expected_meshes))
            self.assertEqual(len(projection['meshes']), 1)
            world = graph.evaluate_base(identifier)['world_matrix']
            for mesh in projection['meshes']:
                actual = transform_points(np.asarray(world), mesh['positions'])
                np.testing.assert_allclose(actual, expected_meshes[mesh['id']], atol=1e-6)

    def test_shared_resource_payload_is_bounded_before_component_publication(self):
        from unittest.mock import patch
        value = preview_snapshot(self.repository, self.repository.snapshot())
        first = value['viewport_nodes'][next(iter(self.expected))]
        one_component = len(json.dumps(dict(materials=first['materials'], images=first['images']),
            separators=(',', ':'), allow_nan=False).encode('utf-8'))
        with patch('editor3d_viewer.MAX_COMPONENT_RESOURCE_BYTES', one_component):
            bounded = preview_snapshot(self.repository, self.repository.snapshot())
        projections = [bounded['viewport_nodes'][identifier] for identifier in self.expected]
        self.assertEqual(sum(value['format'] == 'geometry' for value in projections), 1)
        unavailable = [value for value in projections if value['format'] == 'unavailable']
        self.assertEqual(len(unavailable), 1)
        self.assertIn('payload budget', unavailable[0]['message'])

    def test_unknown_component_mapping_is_explicitly_unavailable(self):
        snapshot = self.repository.snapshot()
        identifier = next(iter(self.expected))
        node = next(node for node in snapshot['scene']['nodes'] if node['id'] == identifier)
        node['metadata']['source_component'] = 'missing-native-component'
        value = preview_snapshot(self.repository, snapshot)
        self.assertEqual(value['viewport_nodes'][identifier]['format'], 'unavailable')
        self.assertIn('component', value['viewport_nodes'][identifier]['message'].lower())

    @unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual component browser validation explicitly.')
    def test_actual_browser_matches_canonical_component_counts_and_all_world_vertices(self):
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        server = EditorServer(self.repository)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with sync_playwright() as runtime:
                browser = launch_chromium(runtime)
                try:
                    page = browser.new_page(viewport={'width':1400,'height':900})
                    page.goto(server.url); page.wait_for_function('() => window.editorReady === true')
                    actual = page.evaluate('''ids => Object.fromEntries(ids.map(id => {
                        const object = window.hatchEditor.objects.get(id); object.updateWorldMatrix(true,true);
                        const meshes=[]; object.traverse(mesh=>{if(mesh.isMesh){
                            const position=mesh.geometry.attributes.position, vertices=[];
                            for(let i=0;i<position.count;i++) vertices.push(mesh.localToWorld(mesh.position.clone().set(position.getX(i),position.getY(i),position.getZ(i))).toArray());
                            meshes.push({id:mesh.userData.nativeMeshId,vertices});
                        }}); return [id,meshes];}))''', list(self.expected))
                    for identifier, expected_meshes in self.expected.items():
                        self.assertEqual(len(actual[identifier]), 1)
                        for mesh in actual[identifier]:
                            np.testing.assert_allclose(mesh['vertices'], expected_meshes[mesh['id']], atol=1e-6)
                    self.assertEqual(page.evaluate('() => window.hatchEditor.errors'), [])
                    # The browser must not fall back to the full GLB on a missing per-node mapping.
                    server.cache.clear()
                    original = server.snapshot
                    def missing_projection():
                        value = original()
                        value['viewport_nodes'][next(iter(self.expected))] = dict(format='unavailable',message='No exact native component mapping.')
                        return value
                    server.snapshot = missing_projection
                    page.goto('about:blank'); page.goto(server.url)
                    page.wait_for_function('() => window.editorReady === true')
                    count = page.evaluate('''id => {let count=0;window.hatchEditor.objects.get(id).traverse(node=>{if(node.isMesh)count++;});return count;}''', next(iter(self.expected)))
                    self.assertEqual(count, 0)
                    self.assertTrue(any('component mapping' in error for error in page.evaluate('() => window.hatchEditor.errors')))
                finally: browser.close()
        finally: server.shutdown(); server.server_close(); thread.join(timeout=5)
