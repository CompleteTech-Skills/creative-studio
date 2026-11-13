"""Exact native-state camera comparison and actual optional WebGL exercise."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
import unittest

import numpy as np
from camera3d_math import projection_matrix, scene_camera_optics
from editor3d_camera_workbench import evaluate_comparison
from editor3d_viewer import EditorServer, preview_snapshot
from rig_errors import RigError
from test_rig_editor3d_viewer import NativeReadRepository


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.repository = NativeReadRepository()
        self.repository.value = deepcopy(self.repository.value)
        self.repository.value['scene']['nodes'][0]['transform']['translation'] = [3, 0, 0]
        self.snapshot = preview_snapshot(self.repository, self.repository.snapshot())
        self.server = type('ReadServer', (), {'snapshot': lambda server: self.snapshot})()
        self.request = dict(expected_revision=self.snapshot['revision'], expected_sha256=self.snapshot['sha256'],
            camera_ids=['camera'], width=256, height=144)

    def test_projection_reuses_native_optics_and_evaluated_parent_pose(self):
        before = deepcopy(self.repository.value)
        result = evaluate_comparison(self.server, self.request)
        state = result['cameras'][0]
        self.assertEqual(state['world_matrix'][0][3], 3)
        render = dict(schema_version=1, kind='camera3d-render', id='editor-comparison-render', version='1.0.0',
            width=256, height=144, pixel_aspect=dict(numerator=1, denominator=1), overscan=1,
            gate_fit='crop', output='png', color_space='srgb', transparent=False, max_pixels=640*640)
        optics = scene_camera_optics(self.snapshot['scene']['nodes'][2]['camera'])
        np.testing.assert_array_equal(state['projection_matrix'], projection_matrix(optics, render))
        np.testing.assert_allclose(np.asarray(state['view_matrix']) @ state['world_matrix'], np.eye(4))
        self.assertFalse(result['offline_render'])
        self.assertFalse(result['animation_evaluated'])
        self.assertEqual(self.repository.value, before)

    def test_bad_selection_size_stale_and_scaled_pose_fail_closed(self):
        variants = [dict(camera_ids=['mesh']), dict(camera_ids=['camera', 'camera']),
            dict(camera_ids=[{}]), dict(width=True), dict(height=10**400), dict(expected_revision=True),
            dict(expected_sha256='0'*64), dict(unexpected='field')]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(RigError):
                evaluate_comparison(self.server, dict(self.request, **changes))
        self.snapshot['viewport_state']['nodes']['camera']['world_matrix'][0][0] = 2
        with self.assertRaises(RigError): evaluate_comparison(self.server, self.request)


@unittest.skipUnless(os.environ.get('HATCH_CAMERA_EDITOR_BROWSER_TEST') == '1', 'Enable actual camera workbench WebGL explicitly.')
class ComparisonBrowserTests(unittest.TestCase):
    def test_actual_two_cameras_preserve_navigation_and_saved_source(self):
        from browser_runtime import launch_chromium
        from playwright.sync_api import sync_playwright
        repository = NativeReadRepository()
        repository.value = deepcopy(repository.value)
        scene = repository.value['scene']
        first = next(node for node in scene['nodes'] if node['type'] == 'camera')
        first['transform']['translation'] = [2, 0.5, 4]
        second = deepcopy(first)
        second['id'] = second['name'] = 'ortho-camera'
        second['camera']['projection'] = 'orthographic'
        second['transform']['translation'] = [2, 0.5, 3]
        scene['nodes'].append(second); scene['cameras'].append(second['id'])
        before = hashlib.sha256(json.dumps(repository.value['scene'],sort_keys=True).encode()).hexdigest()
        server = EditorServer(repository)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            with sync_playwright() as runtime:
                try:
                    browser = launch_chromium(runtime)
                except unittest.SkipTest as error:
                    self.fail('Explicit native browser gate requires Chromium: ' + str(error))
                try:
                    page = browser.new_page(viewport={'width':1600,'height':1000})
                    page.goto(server.url)
                    page.wait_for_function('() => window.editorReady === true')
                    navigation = page.evaluate('() => window.hatchEditor.camera.matrixWorld.toArray()')
                    page.get_by_role('button', name='Compare saved cameras').click()
                    page.wait_for_function("() => document.querySelectorAll('#camera-workbench canvas[data-camera-id]').length === 2")
                    records = page.evaluate("""() => [...document.querySelectorAll('#camera-workbench canvas')].map(canvas => {
                        const gl=canvas.getContext('webgl2'); const bytes=new Uint8Array(256*144*4);
                        gl.readPixels(0,0,256,144,gl.RGBA,gl.UNSIGNED_BYTE,bytes);
                        return {id:canvas.dataset.cameraId,sha:canvas.dataset.snapshotSha256, distinct:new Set(bytes).size};
                    })""")
                    self.assertEqual([item['id'] for item in records], ['camera','ortho-camera'])
                    self.assertTrue(all(item['distinct'] > 8 for item in records), records)
                    self.assertTrue(all(item['sha'] == repository.value['sha256'] for item in records))
                    self.assertEqual(page.evaluate('() => window.hatchEditor.camera.matrixWorld.toArray()'), navigation)
                    page.get_by_label('Show comparison safe area and thirds').uncheck()
                    self.assertEqual(page.locator('#camera-workbench figure div[aria-hidden=true]:visible').count(), 0)
                    self.assertEqual(page.evaluate('() => window.hatchEditor.errors'), [])
                finally: browser.close()
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=5)
        self.assertEqual(hashlib.sha256(json.dumps(repository.value['scene'],sort_keys=True).encode()).hexdigest(),before)
