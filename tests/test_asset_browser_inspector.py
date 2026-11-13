"""Actual offline inspector controls using the canonical browser model workflow."""
import io
import os
import unittest

import test_asset_library_browser as browser_fixture


@unittest.skipUnless(os.environ.get('HATCH_ASSET_BROWSER_TEST')=='1' or os.environ.get('HATCH_RIG_BROWSER_TEST')=='1',
                     'Enable actual Chromium inspector acceptance.')
class InspectorBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture=browser_fixture.BrowserTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.reference=self.fixture.animated_asset()

    def test_actual_visibility_camera_helpers_lighting_hierarchy_and_clear(self):
        from PIL import Image, ImageChops
        from playwright.sync_api import sync_playwright, expect
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=os.environ.get('HATCH_CHROMIUM'),
                args=['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':1100,'height':1000})
            page.goto(f'http://127.0.0.1:{self.fixture.server.server_port}/')
            page.locator('#token').fill(self.fixture.server.token)
            page.get_by_role('button',name='Connect',exact=True).click()
            page.get_by_role('button',name='Animated fixture · 1.0.0',exact=True).click()
            page.get_by_role('button',name='Load model',exact=True).click()
            expect(page.locator('#model-status')).to_contain_text('Actual verified GLB')
            checkbox=page.get_by_role('checkbox',name='Show mesh strip',exact=True)
            expect(checkbox).to_be_checked()
            canvas=page.locator('#viewport canvas')
            page.evaluate('assetBrowser.renderer.render(assetBrowser.scene,assetBrowser.camera)')
            before=Image.open(io.BytesIO(canvas.screenshot())).convert('RGB')
            checkbox.uncheck()
            self.assertFalse(page.evaluate("assetBrowser.model.getObjectByName('strip').visible"))
            page.evaluate('assetBrowser.renderer.render(assetBrowser.scene,assetBrowser.camera)')
            hidden=Image.open(io.BytesIO(canvas.screenshot())).convert('RGB')
            self.assertIsNotNone(ImageChops.difference(before,hidden).getbbox())
            checkbox.check()
            page.get_by_role('checkbox',name='Bounding box',exact=True).check()
            page.get_by_role('checkbox',name='Coordinate axes',exact=True).check()
            self.assertEqual(page.evaluate("assetBrowser.scene.children.filter(o=>o.userData.hatchInspectorOverlay&&o.visible).map(o=>o.userData.hatchInspectorOverlay).sort()"),['axes','bounds'])
            expect(page.get_by_label('Model bounds and coordinates',exact=True)).to_contain_text('+Y up')
            for preset in ('front','back','left','right','top','three-quarter'):
                page.get_by_label('Camera preset',exact=True).select_option(preset)
                projection=page.evaluate('''() => {
                    const camera=assetBrowser.camera;camera.updateMatrixWorld(true);
                    const values=assetBrowser.scene.children.find(o=>o.userData.hatchInspectorOverlay==='bounds').geometry.attributes.position.array;
                    const points=[];for(let i=0;i<values.length;i+=3)points.push(camera.position.clone().set(values[i],values[i+1],values[i+2]).project(camera).toArray());
                    return points;
                }''')
                self.assertTrue(all(abs(x)<1.001 and abs(y)<1.001 and -1<z<1 for x,y,z in projection),preset)
            page.locator('#viewport').evaluate("node=>{node.style.width='180px';node.style.height='320px'}")
            page.wait_for_timeout(100)
            self.assertLess(page.evaluate('assetBrowser.camera.aspect'),1)
            page.get_by_label('Camera preset',exact=True).select_option('front')
            projection=page.evaluate('''() => {
                const camera=assetBrowser.camera;camera.updateMatrixWorld(true);
                const values=assetBrowser.scene.children.find(o=>o.userData.hatchInspectorOverlay==='bounds').geometry.attributes.position.array;
                const points=[];for(let i=0;i<values.length;i+=3)points.push(camera.position.clone().set(values[i],values[i+1],values[i+2]).project(camera).toArray());return points;
            }''')
            self.assertTrue(all(abs(x)<1.001 and abs(y)<1.001 for x,y,z in projection))
            original=page.evaluate("assetBrowser.scene.children.filter(o=>o.isLight).map(o=>({type:o.type,intensity:o.intensity,color:o.color.toArray()}))")
            page.get_by_label('Inspection lighting',exact=True).select_option('studio')
            studio=page.evaluate("assetBrowser.scene.children.filter(o=>o.isLight).map(o=>({type:o.type,intensity:o.intensity,color:o.color.toArray()}))")
            self.assertNotEqual(original,studio)
            page.get_by_label('Inspection lighting',exact=True).select_option('flat')
            self.assertEqual(page.evaluate("assetBrowser.scene.children.find(o=>o.isDirectionalLight).intensity"),0)
            page.get_by_label('Inspection lighting',exact=True).select_option('neutral')
            self.assertEqual(page.evaluate("assetBrowser.scene.children.filter(o=>o.isLight).map(o=>({type:o.type,intensity:o.intensity,color:o.color.toArray()}))"),original)
            page.get_by_text('Node hierarchy',exact=True).click()
            expect(page.get_by_label('Loaded model hierarchy',exact=True)).to_contain_text('b1')
            page.get_by_text('Materials and skin bindings',exact=True).click()
            expect(page.get_by_label('Loaded model materials and skin bindings',exact=True)).to_contain_text('MeshStandardMaterial')
            expect(page.get_by_label('Loaded model materials and skin bindings',exact=True)).to_contain_text('"influences": 4')
            checkbox.uncheck()
            page.get_by_label('Inspection lighting',exact=True).select_option('studio')
            page.evaluate("window.inspectorOldMesh=assetBrowser.model.getObjectByName('strip')")
            page.get_by_role('button',name='Triangle · 1.0.0',exact=True).click()
            expect(page.locator('#name')).to_have_text('Triangle')
            self.assertTrue(page.evaluate('inspectorOldMesh.visible'))
            self.assertEqual(page.evaluate("assetBrowser.scene.children.filter(o=>o.userData.hatchInspectorOverlay).length"),0)
            self.assertEqual(page.evaluate("assetBrowser.scene.children.filter(o=>o.isLight).map(o=>({type:o.type,intensity:o.intensity,color:o.color.toArray()}))"),original)
            browser.close()
