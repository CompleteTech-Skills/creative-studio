"""Real saved-input guards against unconsumed overrides and duplicated imports."""
import unittest
from render3d_contracts import Render3DError
from pathlib import Path
from copy import deepcopy
import tempfile, hashlib, json
import numpy as np
from scene3d_example import build_example
from scene3d_contracts import canonical_bytes, load_project
from render3d_snapshot import create_snapshot, Scene3DAdapter
from render3d_blender import _prepare_scene
from test_render3d_contracts import request
from scene3d_evaluate import evaluate_scene
from test_lookdev_contracts import material
from lookdev_contracts import canonical_bytes as native_bytes
from asset_library import AssetLibrary
from scene3d_import import import_gltf, add_import, materialize
from scene3d_graph import SceneGraph
from scene3d_snapshot import snapshot_project
from rig_models import Geometry, Mesh
from rig_gltf import export_geometry, read_gltf

def refresh(docs):

    def walk(v):
        if isinstance(v, dict):
            if 'path' in v and 'sha256' in v and (v['path'] in docs):
                v['sha256'] = hashlib.sha256(canonical_bytes(docs[v['path']])).hexdigest()
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for _ in range(8):
        for doc in docs.values():
            walk(doc)

def plan(source, parent, scene=None, shot=None, frame_end=1):
    b = load_project(source)
    sr = scene or b['project']['scenes'][-1]
    sh = shot or b['project']['shots'][-1]
    create_snapshot(source, parent / 'snapshot', Scene3DAdapter())
    payload = (parent / 'snapshot/snapshot.json').read_bytes()
    r = request()
    r.update(camera=b['documents'][sh['path']]['camera'],project={k: b['project'][k] for k in ('id', 'version')}, scene={k: sr[k] for k in ('id', 'version')}, shot={k: sh[k] for k in ('id', 'version')}, fps=dict(numerator=24, denominator=1), frame_range=dict(start=0, end=frame_end), source_start=dict(numerator=0, denominator=1), playback_rate=dict(numerator=1, denominator=1), snapshot=dict(path='snapshot.json', bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
    w = parent / 'workspace'
    w.mkdir()
    return (_prepare_scene(r, parent / 'snapshot', w), w, evaluate_scene(source, sr['path'], sh['path'], 0))

class SceneAdapterGuardTests(unittest.TestCase):

    def test_actual_shot_material_override_cannot_be_silently_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            bundle = build_example(source)
            docs = deepcopy(bundle['documents'])
            paint = material()
            raw = native_bytes(paint)
            (source / 'paint.json').write_bytes(raw)
            pin = dict(kind='material', id=paint['id'], version=paint['version'], path='paint.json', sha256=hashlib.sha256(raw).hexdigest())
            docs['example-shot.json']['material_overrides'] = [dict(target='mesh', slot='default', material=pin)]
            docs['project.json']['dependency_lock'].append(pin)
            refresh(docs)
            for p, d in docs.items():
                (source / p).write_bytes(canonical_bytes(d))
            with self.assertRaises(Render3DError) as caught:
                plan(source, root)
            self.assertEqual(caught.exception.report['category'], 'unsupported-feature')

    def test_actual_two_mesh_import_cannot_duplicate_entire_asset_per_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            bundle = build_example(source)
            meshes = [Mesh('one', np.array([[0.0, 0, 0], [1, 0, 0], [0, 0, 1]]), np.array([[0, 1, 2]])), Mesh('two', np.array([[2.0, 0, 0], [3, 0, 0], [2, 0, 1]]), np.array([[0, 1, 2]]))]
            export_geometry(Geometry(meshes, [{'name': 'surface'}]), root / 'two.glb')
            (root / 'license.txt').write_text('CC0-1.0')
            lib = AssetLibrary(root / 'library', create=True)
            imp = import_gltf(root / 'two.glb', lib, asset_id='two', attribution='review', license_file=root / 'license.txt')
            g = SceneGraph.from_bundle(bundle, 'example-scene.json', source_root=source)
            add_import(g, imp, 'actor')
            record = lib.resolve(imp.library_reference)
            files = {r['path']: lib.read_file(imp.library_reference, r['path']) for r in record['files'] if r['path'] != imp.asset_reference['path']}
            docs = {imp.asset_reference['path']: imp.asset}
            saved = snapshot_project(source, root / 'saved', g, scene_path='example-scene.json', derived_documents=docs, extra_payloads=files)
            with self.assertRaises(Render3DError) as caught:
                plan(root / 'saved', root, scene=saved['active_scene'])
            self.assertEqual(caught.exception.report['category'], 'unsupported-feature')


    def test_inherited_camera_and_shadow_masks_fail_closed(self):
        for flag in ('camera_visible', 'cast_shadow'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=root/'source';bundle=build_example(source)
                docs=deepcopy(bundle['documents'])
                next(node for node in docs['example-scene.json']['nodes'] if node['id']=='root')['render'][flag]=False
                refresh(docs)
                for path,document in docs.items():(source/path).write_bytes(canonical_bytes(document))
                actual=evaluate_scene(source,'example-scene.json','example-shot.json',0)
                self.assertFalse(actual['nodes']['mesh'][flag])
                with self.assertRaises(Render3DError) as caught:plan(source,root)
                self.assertEqual(caught.exception.report['category'],'unsupported-feature')

    def test_supported_local_shadow_mask_and_inherited_hidden_nodes_are_preserved(self):
        for mode in ('local-shadow','hidden-parent'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=root/'source';bundle=build_example(source);docs=deepcopy(bundle['documents'])
                nodes={node['id']:node for node in docs['example-scene.json']['nodes']}
                if mode=='local-shadow':nodes['mesh']['render']['cast_shadow']=False
                else:nodes['root']['render']['visible']=False
                refresh(docs)
                for path,document in docs.items():(source/path).write_bytes(canonical_bytes(document))
                prepared,_,actual=plan(source,root)
                mesh=next(node for node in prepared['nodes'] if node['id']=='mesh')
                if mode=='local-shadow':self.assertFalse(mesh['cast_shadow'])
                else:self.assertFalse(mesh['frames'][0]['visible'])

    def test_late_material_binding_is_rejected_after_the_first_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';bundle=build_example(source);docs=deepcopy(bundle['documents'])
            paint=material();raw=native_bytes(paint);(source/'paint.json').write_bytes(raw)
            pin=dict(kind='material',id=paint['id'],version=paint['version'],path='paint.json',sha256=hashlib.sha256(raw).hexdigest())
            clip=docs['example-animation.json'];clip['dependencies']=[pin]
            clip['tracks']=[dict(target='mesh',property='material',interpolation='step',keys=[dict(frame=0,value=paint['id']+'@'+paint['version'])])]
            docs['example-shot.json']['bindings']=[dict(target='mesh',clip=deepcopy(docs['project.json']['animations'][0]),start_frame=1,end_frame=3,speed=1,loop=False)]
            docs['project.json']['dependency_lock'].append(pin);refresh(docs)
            for path,document in docs.items():(source/path).write_bytes(canonical_bytes(document))
            self.assertFalse(evaluate_scene(source,'example-scene.json','example-shot.json',0)['nodes']['mesh']['material_overrides'])
            self.assertTrue(evaluate_scene(source,'example-scene.json','example-shot.json',1)['nodes']['mesh']['material_overrides'])
            with self.assertRaises(Render3DError) as caught:plan(source,root,frame_end=2)
            self.assertEqual(caught.exception.report['category'],'unsupported-feature')

    def test_static_fallback_uses_graph_or_rejects_nondefault_flags(self):
        from unittest.mock import patch
        import builtins
        from render3d_blender import _static_state
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);bundle=build_example(root/'source');scene=deepcopy(bundle['documents']['example-scene.json'])
            next(node for node in scene['nodes'] if node['id']=='root')['render']['visible']=False
            shot=bundle['documents']['example-shot.json']
            self.assertFalse(_static_state(bundle,scene,shot)['nodes']['mesh']['visible'])
            original_import=builtins.__import__
            def missing_graph(name,*args,**kwargs):
                if name=='scene3d_graph':raise ImportError('authoritative Graph absent')
                return original_import(name,*args,**kwargs)
            with patch('builtins.__import__',side_effect=missing_graph),self.assertRaises(Render3DError) as caught:
                _static_state(bundle,scene,shot)
            self.assertEqual(caught.exception.report['category'],'unsupported-feature')
