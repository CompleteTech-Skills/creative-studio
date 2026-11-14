from pathlib import Path
import tempfile,json,os
from asset_library import AssetLibrary
from asset_library_import import ImportPipeline
root=Path(tempfile.mkdtemp(prefix='hatch139-compositor-'));source=root/'source.blend';escaped=root/'outside-quarantine'
script=root/'create.py';script.write_text('''import bpy
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add()
scene=bpy.context.scene;scene.use_nodes=True
output=scene.node_tree.nodes.new('CompositorNodeOutputFile');output.base_path='''+repr(str(escaped))+'''
scene.node_tree.links.new(scene.node_tree.nodes.get('Render Layers').outputs['Image'],output.inputs[0])
bpy.ops.wm.save_as_mainfile(filepath='''+repr(str(source))+''')
''')
pipeline=ImportPipeline(AssetLibrary(root/'library',create=True),blender=os.environ['HATCH_BLENDER'])
workspace=root/'create';workspace.mkdir();pipeline.blender._run([str(pipeline.blender.executable),'--background','--factory-startup','--disable-autoexec','--python',str(script)],workspace)
base=json.loads(Path('examples/asset-library-foundation/manifest.json').read_text());license=root/'license.txt';license.write_text('CC0 engineering safety fixture')
result=pipeline.import_asset(source,identity=base['identity'],version='1.0.0',metadata=base['metadata'],license_file=license,fixture=True,trusted_packed_blend=True)
print(json.dumps(dict(root=str(root),status=result['status'],unrequested_files=[str(p.relative_to(root)) for p in escaped.rglob('*')] if escaped.exists() else [])))
