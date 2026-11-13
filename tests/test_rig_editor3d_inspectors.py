"""Native inspector commands and real Scene3D mutation checks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

from editor3d_inspectors import compile_inspector_commands, describe_inspector
from rig_errors import RigError
from scene3d_contracts import load_project, validate_document
from scene3d_example import build_example, node
from scene3d_graph import SceneGraph
from scene3d_edit import apply_commands
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project


class InspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        build_example(self.base/'source')
        self.bundle = load_project(self.base/'source')
        scene = deepcopy(self.bundle['documents']['example-scene.json'])
        light = node('key-light', 'light', 'root')
        light['light'] = dict(type='point', color=[1, 1, 1], intensity=10)
        scene['nodes'].append(light);scene['lights'].append(light['id'])
        self.scene = validate_document(scene)
        self.snapshot = dict(scene=self.scene)

    def tearDown(self):
        self.temp.cleanup()

    def test_camera_and_light_changes_use_native_validator_and_graph(self):
        before = deepcopy(self.snapshot)
        commands = compile_inspector_commands(self.snapshot,'camera',{'camera':{'near':0.2,'far':500,'vertical_fov':1.1}})
        commands += compile_inspector_commands(self.snapshot,'key-light',{'light':{'intensity':25,'color':[0.1,0.4,0.9]}})
        graph = SceneGraph(self.scene)
        apply_commands(graph,PrefabLibrary(),dict(schema_version=1,commands=commands))
        nodes={item['id']:item for item in graph.document()['nodes']}
        self.assertEqual(nodes['camera']['camera']['near'],0.2)
        self.assertEqual(nodes['camera']['camera']['far'],500)
        self.assertEqual(nodes['key-light']['light']['intensity'],25)
        self.assertEqual(self.snapshot,before)

    def test_world_commands_are_real_native_changes(self):
        commands=compile_inspector_commands(self.snapshot,None,{'world':{'background':[0.1,0.2,0.3],'exposure':2}})
        graph=SceneGraph(self.scene)
        apply_commands(graph,PrefabLibrary(),dict(schema_version=1,commands=commands))
        self.assertEqual(graph.document()['world']['exposure'],2)
        self.assertEqual(graph.document()['world']['background'],[0.1,0.2,0.3])

    def test_camera_change_survives_native_snapshot_reopen(self):
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.base/'source')
        commands=compile_inspector_commands({'scene':graph.document()},'camera',{'camera':{'projection':'orthographic','orthographic_height':8}})
        apply_commands(graph,PrefabLibrary.from_files(self.base/'source',self.bundle),dict(schema_version=1,commands=commands))
        result=snapshot_project(self.base/'source',self.base/'saved',graph,scene_path='example-scene.json')
        reopened=load_project(self.base/'saved')['documents'][result['active_scene']['path']]
        camera=next(item for item in reopened['nodes'] if item['id']=='camera')
        self.assertEqual(camera['camera']['projection'],'orthographic')
        self.assertEqual(camera['camera']['orthographic_height'],8)
        self.assertEqual((self.base/'source/example-scene.json').read_bytes(),(self.base/'saved/example-scene.json').read_bytes())

    def test_unknown_types_invalid_range_nonfinite_and_locked_fail(self):
        for target,changes in [('camera',{'camera':{'near':200,'far':1}}),('camera',{'camera':{'unsupported':1}}),
                               ('mesh',{'camera':{'near':1}}),('key-light',{'light':{'intensity':-1}}),
                               ('key-light',{'light':{'intensity':float('nan')}}),('camera',{'other':{}})]:
            with self.subTest(changes=changes),self.assertRaises(RigError):
                compile_inspector_commands(self.snapshot,target,changes)
        locked=deepcopy(self.snapshot)
        next(item for item in locked['scene']['nodes'] if item['id']=='camera')['locked']=True
        with self.assertRaises(RigError):compile_inspector_commands(locked,'camera',{'camera':{'near':1}})

    def test_descriptors_report_only_actual_native_fields(self):
        descriptor=describe_inspector(self.snapshot,'camera')
        self.assertEqual(descriptor['kind'],'camera')
        self.assertIn('near',descriptor['controls']['camera']['editable'])
        self.assertIn('depth-of-field',descriptor['unavailable'])
        self.assertFalse(describe_inspector(self.snapshot,'mesh')['controls'])
        self.assertEqual(describe_inspector(self.snapshot)['kind'],'world')
        with self.assertRaises(RigError):describe_inspector(self.snapshot,'missing')

    def test_material_inspection_of_non_geometry_nodes_is_empty(self):
        from editor3d_inspectors import describe_materials
        snapshot=dict(self.snapshot,documents=self.bundle['documents'])
        self.assertEqual(describe_materials(None,snapshot,'camera'),[])
        self.assertEqual(describe_materials(None,snapshot,'root'),[])

    def test_material_resource_requires_typed_native_json(self):
        import hashlib
        from editor3d_inspectors import _load_material_registry
        for raw in (b'[]',b'null',b'1',b'{"kind":"material3d"}'):
            class Source:
                def read_file(self,path,sha):return raw
            with self.subTest(raw=raw),self.assertRaises(RigError):
                _load_material_registry(Source(),{'sha256':'a'*64},dict(id='ceramic',version='1.0.0',path='material.json',sha256=hashlib.sha256(raw).hexdigest()))

    def test_bound_material_root_cannot_alias_a_dependency(self):
        from editor3d_inspectors import _load_material_registry
        import hashlib
        base=(Path(__file__).resolve().parents[1]/'examples/lookdev-contracts/blue-coat.json').read_bytes()
        class Source:
            def read_file(self,path,sha): return base
        with self.assertRaisesRegex(RigError,'exact material identity'):
            _load_material_registry(Source(),{'sha256':'a'*64},dict(id='ceramic',version='1.0.0',path='blue-coat.json',sha256=hashlib.sha256(base).hexdigest()))

    def test_inherited_texture_material_edit_keeps_direct_dependencies(self):
        import hashlib
        from PIL import Image
        from editor3d_repository import EditorRepository
        from editor3d_inspectors import apply_material_edit, describe_materials
        from lookdev_contracts import canonical_bytes
        example=Path(__file__).resolve().parents[1]/'examples/lookdev-contracts'
        base=json.loads((example/'ceramic.json').read_bytes())
        tex=json.loads((example/'albedo.json').read_bytes())
        image=self.base/'albedo.png'; Image.new('RGBA',(2,2),'blue').save(image)
        png=image.read_bytes();tex['file']['sha256']=hashlib.sha256(png).hexdigest();tex['file']['bytes']=len(png)
        tx=canonical_bytes(tex)
        tr=dict(id=tex['id'],version=tex['version'],kind=tex['kind'],path='albedo.json',sha256=hashlib.sha256(tx).hexdigest())
        base['texture_slots']={'base_color':dict(texture=tr,texcoord=0,channels='rgba',scale=[1,1,1],offset=[0,0,0],rotation_radians=0,strength=1)}
        base['dependencies']=[tr];br=canonical_bytes(base)
        ref=dict(id=base['id'],version=base['version'],kind=base['kind'],path='ceramic.json',sha256=hashlib.sha256(br).hexdigest())
        inst=json.loads((example/'blue-coat.json').read_bytes());inst['base_material']=ref;inst['dependencies']=[ref]
        ir=canonical_bytes(inst)
        generic=dict(id=inst['id'],version=inst['version'],kind='material',path='blue-coat.json',sha256=hashlib.sha256(ir).hexdigest())
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.base/'source')
        graph.configure_node('mesh',{'material_overrides':[dict(slot='coat',material=generic)]})
        payloads={'ceramic.json':br,'blue-coat.json':ir,'albedo.json':tx,'textures/albedo.png':png}
        copied=snapshot_project(self.base/'source',self.base/'textured',graph,scene_path='example-scene.json',extra_payloads=payloads)
        project=json.loads((self.base/'textured/project.json').read_text())
        for name,data in payloads.items():
            if name=='blue-coat.json':continue
            project['dependency_lock'].append(dict(kind='texture',id=name.replace('/','-').replace('.','-'),version='1.0.0',path=name,sha256=hashlib.sha256(data).hexdigest()))
        (self.base/'textured/project.json').write_bytes(canonical_bytes(project))
        repository=EditorRepository.create(self.base/'textured',self.base/'textured-repo',scene_path=copied['active_scene']['path'])
        snap=repository.snapshot()
        after=apply_material_edit(repository,dict(node_id='mesh',slot='coat',surface={'roughness':.4},expected_revision=snap['revision'],expected_sha256=snap['sha256'],transaction_id='inherited-edit'))
        effective=describe_materials(repository,after,'mesh')[0]['effective']
        self.assertEqual(effective['surface']['roughness'],.4)
        self.assertEqual(effective['texture_slots']['base_color']['texture'],tr)
        self.assertEqual(repository.read_file('textures/albedo.png',after['sha256']),png)

    def test_material_edit_publishes_new_native_version_and_preserves_source(self):
        import hashlib
        from editor3d_repository import EditorRepository
        from editor3d_inspectors import apply_material_edit, describe_materials
        material_path=Path(__file__).resolve().parents[1]/'examples/lookdev-contracts/ceramic.json'
        original=material_path.read_bytes()
        material=json.loads(original)
        reference=dict(id=material['id'],version=material['version'],kind='material',
                       path='ceramic.json',sha256=hashlib.sha256(original).hexdigest())
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.base/'source')
        graph.configure_node('mesh',{'material_overrides':[dict(slot='ceramic',material=reference)]})
        copied=snapshot_project(self.base/'source',self.base/'material-source',graph,scene_path='example-scene.json',extra_payloads={'ceramic.json':original})
        repository=EditorRepository.create(self.base/'material-source',self.base/'repository',scene_path=copied['active_scene']['path'])
        before=repository.snapshot()
        self.assertEqual(describe_materials(repository,before,'mesh')[0]['effective']['surface']['roughness'],.24)
        request=dict(node_id='mesh',slot='ceramic',surface=dict(roughness=.8,metallic=.6),expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='material-edit')
        after=apply_material_edit(repository,request)
        self.assertEqual(after['revision'],before['revision']+1)
        materials=describe_materials(repository,after,'mesh')
        self.assertEqual(materials[0]['effective']['surface']['roughness'],.8)
        self.assertEqual(materials[0]['effective']['surface']['metallic'],.6)
        self.assertEqual(materials[0]['reference']['version'],'1.0.1')
        self.assertEqual(repository.read_file('ceramic.json',after['sha256']),original)
        self.assertEqual(EditorRepository(self.base/'repository').snapshot()['sha256'],after['sha256'])
        stale=dict(request,transaction_id='stale-material-edit')
        with self.assertRaises(RigError):apply_material_edit(repository,stale)
        self.assertEqual(repository.snapshot()['sha256'],after['sha256'])
        undone=repository.undo(after['revision'],'undo-material',expected_sha256=after['sha256'])
        self.assertEqual(describe_materials(repository,undone,'mesh')[0]['effective']['surface']['roughness'],.24)
        redone=repository.redo(undone['revision'],'redo-material',expected_sha256=undone['sha256'])
        self.assertEqual(describe_materials(repository,redone,'mesh')[0]['effective']['surface']['roughness'],.8)


@unittest.skipUnless(os.environ.get('HATCH_EDITOR_BROWSER_TEST') == '1', 'Enable actual browser acceptance explicitly.')
class InspectorBrowserTests(unittest.TestCase):
    def test_camera_light_world_edits_persist_and_rejections_stay_visible(self):
        import threading
        from editor3d_repository import EditorRepository
        from editor3d_viewer import EditorServer
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            build_example(root/'source')
            graph=SceneGraph.from_bundle(load_project(root/'source'),'example-scene.json',source_root=root/'source')
            light=node('key-light','light','root');light['light']=dict(type='point',color=[1,1,1],intensity=10)
            graph.create(light)
            saved=snapshot_project(root/'source',root/'with-light',graph,scene_path='example-scene.json')
            repository=EditorRepository.create(root/'with-light',root/'repository',scene_path=saved['active_scene']['path'])
            server=EditorServer(repository)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as runtime:
                    browser=launch_chromium(runtime)
                    try:
                        page=browser.new_page(viewport={'width':1400,'height':1000})
                        page.goto(server.url)
                        page.wait_for_function('() => window.editorReady === true')
                        page.evaluate("window.hatchEditor.select('camera')")
                        panel=page.locator('#native-inspectors')
                        panel.get_by_label('Near plane (m)',exact=True).fill('0.2')
                        panel.get_by_label('Far plane (m)',exact=True).fill('500')
                        panel.get_by_role('button',name='Save camera optics',exact=True).click()
                        page.wait_for_function("() => window.hatchEditor.snapshot.scene.nodes.find(n=>n.id==='camera').camera.near === 0.2")
                        camera=next(n for n in repository.snapshot()['scene']['nodes'] if n['id']=='camera')
                        self.assertEqual(camera['camera']['near'],.2)
                        panel.get_by_role('button',name='Save world',exact=True).wait_for(state='visible')
                        page.wait_for_function("() => [...document.querySelectorAll('#native-inspectors button')].every(b=>!b.disabled)")
                        panel.get_by_label('Exposure (stops)',exact=True).fill('1.5')
                        panel.get_by_role('button',name='Save world',exact=True).click()
                        page.wait_for_function("() => window.hatchEditor.snapshot.scene.world.exposure === 1.5")
                        page.wait_for_function("() => [...document.querySelectorAll('#native-inspectors button')].every(b=>!b.disabled)")
                        previous=repository.snapshot()['sha256']
                        panel.get_by_label('Near plane (m)',exact=True).fill('600')
                        panel.get_by_role('button',name='Save camera optics',exact=True).click()
                        panel.get_by_role('status').filter(has_text='far').wait_for()
                        self.assertEqual(repository.snapshot()['sha256'],previous)
                        page.evaluate("window.hatchEditor.select('key-light')")
                        panel.get_by_label('Native intensity',exact=True).fill('25')
                        panel.get_by_role('button',name='Save light',exact=True).click()
                        page.wait_for_function("() => window.hatchEditor.snapshot.scene.nodes.find(n=>n.id==='key-light').light.intensity === 25")
                        page.goto('about:blank');page.goto(server.url)
                        page.wait_for_function('() => window.editorReady === true')
                        self.assertEqual(next(n for n in repository.snapshot()['scene']['nodes'] if n['id']=='key-light')['light']['intensity'],25)
                        self.assertEqual(EditorRepository(root/'repository').snapshot()['scene']['world']['exposure'],1.5)
                    finally:browser.close()
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)


if __name__ == '__main__':unittest.main()
