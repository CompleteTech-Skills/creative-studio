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


def mapped_repository(root):
    from editor3d_repository import EditorRepository
    from scene3d_contracts import all_refs
    from scene3d_materials import obj_material_bindings
    import hashlib
    build_example(root/'source')
    # The generic example winds its XZ triangle downward. This pixel fixture
    # authors the same triangle upward so the above-ground preview sees it.
    triangle=root/'source/triangle.obj'
    triangle.write_text(triangle.read_text().replace('f 1 2 3','f 1 3 2'))
    asset=json.loads((root/'source/triangle.json').read_text())
    asset['material_bindings']=obj_material_bindings(asset['id'])
    (root/'source/triangle.json').write_text(json.dumps(asset))
    for _ in range(6):
        for path in (root/'source').glob('*.json'):
            value=json.loads(path.read_text())
            for reference in all_refs(value):
                target=root/'source'/reference['path']
                if target.is_file():reference['sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
            path.write_text(json.dumps(value))
    return EditorRepository.create(root/'source',root/'repository',scene_path='example-scene.json')


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

    def test_authored_material_assignment_targets_native_slot_and_is_reversible(self):
        from editor3d_inspectors import assign_authored_material,projected_editor_materials,describe_materials
        repository=mapped_repository(self.base/'assignment')
        before=repository.snapshot()
        original=repository.read_file('triangle.obj',before['sha256'])
        request=dict(node_id='mesh',slot='surface',surface={'base_color':[1,0,0,1],'roughness':.3},
            expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='assign-red')
        after=assign_authored_material(repository,request)
        self.assertEqual(after['revision'],1)
        self.assertEqual(repository.read_file('triangle.obj',after['sha256']),original)
        projection=projected_editor_materials(repository,after)['mesh']
        self.assertEqual(len(projection),1);self.assertEqual(projection[0]['slot'],'surface')
        self.assertTrue(projection[0]['supported'])
        self.assertEqual(projection[0]['effective']['surface']['base_color'],[1,0,0,1])
        self.assertEqual(describe_materials(repository,after,'mesh')[0]['effective']['surface']['roughness'],.3)
        self.assertEqual(assign_authored_material(repository,request)['sha256'],after['sha256'])
        with self.assertRaises(RigError):assign_authored_material(repository,dict(request,slot='foreign-slot',transaction_id='bad-slot'))
        with self.assertRaises(RigError):assign_authored_material(repository,dict(request,transaction_id='stale-new'))
        undone=repository.undo(after['revision'],'undo-red',expected_sha256=after['sha256'])
        self.assertFalse(next(n for n in undone['scene']['nodes'] if n['id']=='mesh')['material_overrides'])
        redone=repository.redo(undone['revision'],'redo-red',expected_sha256=undone['sha256'])
        self.assertEqual(projected_editor_materials(repository,redone)['mesh'][0]['effective']['surface']['base_color'],[1,0,0,1])

    def test_shared_native_material_instances_receive_distinct_immutable_edit_versions(self):
        from editor3d_inspectors import assign_authored_material,apply_material_edit,describe_materials
        repository=mapped_repository(self.base/'shared')
        before=repository.snapshot()
        red=assign_authored_material(repository,dict(node_id='mesh',slot='surface',surface={'base_color':[1,0,0,1]},expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='shared-red'))
        duplicate=repository.apply(red['revision'],dict(schema_version=1,commands=[dict(operation='duplicate',parameters=dict(identifier='mesh',new_id='mesh2'))]),'duplicate-red',expected_sha256=red['sha256'])
        first=apply_material_edit(repository,dict(node_id='mesh',slot='surface',surface={'roughness':.4},expected_revision=duplicate['revision'],expected_sha256=duplicate['sha256'],transaction_id='first-edit'))
        second=apply_material_edit(repository,dict(node_id='mesh2',slot='surface',surface={'roughness':.7},expected_revision=first['revision'],expected_sha256=first['sha256'],transaction_id='second-edit'))
        first_material=describe_materials(repository,second,'mesh')[0]
        second_material=describe_materials(repository,second,'mesh2')[0]
        self.assertEqual(first_material['effective']['surface']['roughness'],.4)
        self.assertEqual(second_material['effective']['surface']['roughness'],.7)
        self.assertEqual(first_material['reference']['version'],'1.0.1')
        self.assertEqual(second_material['reference']['version'],'1.0.2')

    def test_editing_default_material_publishes_only_a_targeted_slot_override(self):
        from editor3d_inspectors import assign_authored_material,apply_material_edit,projected_editor_materials
        repository=mapped_repository(self.base/'default')
        before=repository.snapshot()
        red=assign_authored_material(repository,dict(node_id='mesh',slot='surface',surface={'base_color':[1,0,0,1],'roughness':.3},expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='default-red'))
        reference=next(n for n in red['scene']['nodes'] if n['id']=='mesh')['material_overrides'][0]['material']
        default=repository.apply(red['revision'],dict(schema_version=1,commands=[dict(operation='configure-node',parameters=dict(identifier='mesh',properties={'material_overrides':[dict(slot='default',material=reference)]}))]),'make-default',expected_sha256=red['sha256'])
        edited=apply_material_edit(repository,dict(node_id='mesh',slot='surface',surface={'roughness':.6},expected_revision=default['revision'],expected_sha256=default['sha256'],transaction_id='edit-default-target'))
        overrides=next(n for n in edited['scene']['nodes'] if n['id']=='mesh')['material_overrides']
        self.assertEqual(next(o['material'] for o in overrides if o['slot']=='default'),reference)
        self.assertEqual(projected_editor_materials(repository,edited)['mesh'][0]['effective']['surface']['roughness'],.6)

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
    def test_default_material_is_displayed_and_edits_only_selected_slot(self):
        import threading
        from editor3d_inspectors import assign_authored_material
        from editor3d_viewer import EditorServer
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with tempfile.TemporaryDirectory() as directory:
            repository=mapped_repository(Path(directory));before=repository.snapshot()
            red=assign_authored_material(repository,dict(node_id='mesh',slot='surface',surface={'base_color':[1,0,0,1],'roughness':.3},expected_revision=before['revision'],expected_sha256=before['sha256'],transaction_id='browser-default-red'))
            reference=next(n for n in red['scene']['nodes'] if n['id']=='mesh')['material_overrides'][0]['material']
            repository.apply(red['revision'],dict(schema_version=1,commands=[dict(operation='configure-node',parameters=dict(identifier='mesh',properties={'material_overrides':[dict(slot='default',material=reference)]}))]),'browser-make-default',expected_sha256=red['sha256'])
            server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as runtime:
                    browser=launch_chromium(runtime)
                    try:
                        page=browser.new_page(viewport={'width':1400,'height':1000})
                        page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                        page.evaluate("window.hatchEditor.select('mesh')")
                        field=page.locator('#native-inspectors fieldset').filter(has=page.get_by_text('surface',exact=True))
                        field.get_by_role('button',name='Save native material',exact=True).wait_for()
                        self.assertEqual(field.get_by_label('R',exact=True).input_value(),'1')
                        self.assertEqual(field.get_by_label('Roughness',exact=True).input_value(),'0.3')
                        field.get_by_label('Roughness',exact=True).fill('0.8')
                        field.get_by_role('button',name='Save native material',exact=True).click()
                        page.wait_for_function("() => window.hatchEditor.snapshot.viewport_materials.mesh[0].effective.surface.roughness===0.8")
                        overrides=next(n for n in repository.snapshot()['scene']['nodes'] if n['id']=='mesh')['material_overrides']
                        self.assertEqual(next(o['material'] for o in overrides if o['slot']=='default'),reference)
                        self.assertEqual(len(overrides),2)
                        self.assertEqual(page.evaluate('() => window.hatchEditor.errors'),[])
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(timeout=5)

    def test_native_glb_two_material_slots_change_only_targeted_primitive(self):
        import threading
        from asset_library import AssetLibrary
        from rig_gltf import GltfBuilder
        from scene3d_import import import_gltf,add_import
        from editor3d_repository import EditorRepository
        from editor3d_viewer import EditorServer
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);library=AssetLibrary(root/'library',create=True)
            license=root/'license.txt';license.write_text('CC0-1.0 engineering fixture')
            builder=GltfBuilder();builder.json['asset']['extras']={'hatchSource':{'fixture':True}}
            left=builder.accessor([[0,0,0],[1,0,0],[0,1,0]],'VEC3',bounds=True)
            right=builder.accessor([[1.5,0,0],[2.5,0,0],[1.5,1,0]],'VEC3',bounds=True)
            indices=builder.accessor([0,1,2],'SCALAR',5123)
            builder.json['materials']=[{'name':'blue','pbrMetallicRoughness':{'baseColorFactor':[0,0,1,1]}},
                                       {'name':'green','pbrMetallicRoughness':{'baseColorFactor':[0,1,0,1]}}]
            builder.json['meshes']=[{'primitives':[{'attributes':{'POSITION':left},'indices':indices,'material':0},
                                                   {'attributes':{'POSITION':right},'indices':indices,'material':1}]}]
            builder.json['nodes']=[{'mesh':0}];builder.json['scenes']=[{'nodes':[0]}]
            source=root/'two-surfaces.glb';builder.write(source);original=source.read_bytes()
            imported=import_gltf(source,library,asset_id='two-surfaces',attribution='Engineering fixture',license_file=license)
            bundle=build_example(root/'source');graph=SceneGraph.from_bundle(bundle,'example-scene.json')
            mapping=add_import(graph,imported,'model')['mapping'];node_id=mapping['node-0']
            manifest=library.resolve(imported.library_reference)
            payloads={r['path']:library.read_file(imported.library_reference,r['path']) for r in manifest['files'] if r['path']!=imported.asset_reference['path']}
            saved=snapshot_project(root/'source',root/'portable',graph,scene_path='example-scene.json',derived_documents={imported.asset_reference['path']:imported.asset},extra_payloads=payloads)
            repository=EditorRepository.create(root/'portable',root/'repository',scene_path=saved['active_scene']['path'])
            server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as runtime:
                    browser=launch_chromium(runtime)
                    try:
                        page=browser.new_page(viewport={'width':1400,'height':1000})
                        page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                        page.evaluate('(id)=>window.hatchEditor.select(id)',node_id)
                        field=page.locator('#native-inspectors fieldset').filter(has=page.get_by_text('material-0',exact=True))
                        field.get_by_role('button',name='Assign new authored material',exact=True).wait_for()
                        for label_name,value in [('R','1'),('G','0'),('B','0')]:field.get_by_label(label_name,exact=True).fill(value)
                        field.get_by_role('button',name='Assign new authored material',exact=True).click()
                        page.wait_for_function("""(id)=>{const colors=[];window.hatchEditor.objects.get(id).traverse(o=>{if(o.isMesh)colors.push({color:o.material.color.toArray(),native:!!o.userData.nativeMaterialReference})});return colors.filter(c=>c.native).length===1;}""",arg=node_id)
                        colors=page.evaluate("""(id)=>{const colors=[];window.hatchEditor.objects.get(id).traverse(o=>{if(o.isMesh)colors.push({color:o.material.color.toArray(),native:!!o.userData.nativeMaterialReference})});return colors;}""",node_id)
                        self.assertEqual(len(colors),2)
                        self.assertEqual(next(c['color'] for c in colors if c['native']),[1,0,0])
                        self.assertEqual(next(c['color'] for c in colors if not c['native']),[0,1,0])
                        self.assertEqual(source.read_bytes(),original)
                        self.assertEqual(page.evaluate('() => window.hatchEditor.errors'),[])
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(timeout=5)

    def test_assign_authored_material_changes_actual_mesh_and_survives_reopen(self):
        import threading
        from editor3d_repository import EditorRepository
        from editor3d_viewer import EditorServer
        from playwright.sync_api import sync_playwright
        from browser_runtime import launch_chromium
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repository=mapped_repository(root)
            server=EditorServer(repository);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as runtime:
                    browser=launch_chromium(runtime)
                    try:
                        page=browser.new_page(viewport={'width':1400,'height':1000})
                        page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                        page.evaluate("window.hatchEditor.select('mesh')")
                        field=page.locator('#native-inspectors fieldset').filter(has=page.get_by_text('surface',exact=True))
                        field.get_by_role('button',name='Assign new authored material',exact=True).wait_for()
                        before=page.locator('canvas').first.screenshot()
                        for label_name,value in [('R','1'),('G','0'),('B','0')]:field.get_by_label(label_name,exact=True).fill(value)
                        field.get_by_role('button',name='Assign new authored material',exact=True).click()
                        page.wait_for_function("""() => {let mesh;window.hatchEditor.objects.get('mesh').traverse(o=>{if(o.isMesh)mesh=o});return mesh?.userData.nativeMaterialReference && mesh.material.color.r===1 && mesh.material.color.g===0;}""")
                        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                        after=page.locator('canvas').first.screenshot()
                        evidence=os.environ.get('HATCH_EDITOR_EVIDENCE_DIR')
                        if evidence:
                            Path(evidence).mkdir(parents=True,exist_ok=True)
                            (Path(evidence)/'material-before.png').write_bytes(before);(Path(evidence)/'material-after.png').write_bytes(after)
                        import hashlib
                        self.assertNotEqual(hashlib.sha256(before).hexdigest(),hashlib.sha256(after).hexdigest(),'Visible material pixels did not change.')
                        page.goto('about:blank');page.goto(server.url);page.wait_for_function('() => window.editorReady === true')
                        state=EditorRepository(root/'repository').snapshot()
                        self.assertEqual(state['revision'],1)
                        self.assertEqual(page.evaluate("() => window.hatchEditor.errors"),[])
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(timeout=5)

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
