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

def plan(source, parent, scene=None, shot=None):
    b = load_project(source)
    sr = scene or b['project']['scenes'][-1]
    sh = shot or b['project']['shots'][-1]
    create_snapshot(source, parent / 'snapshot', Scene3DAdapter())
    payload = (parent / 'snapshot/snapshot.json').read_bytes()
    r = request()
    r.update(project={k: b['project'][k] for k in ('id', 'version')}, scene={k: sr[k] for k in ('id', 'version')}, shot={k: sh[k] for k in ('id', 'version')}, fps=dict(numerator=24, denominator=1), frame_range=dict(start=0, end=1), source_start=dict(numerator=0, denominator=1), playback_rate=dict(numerator=1, denominator=1), snapshot=dict(path='snapshot.json', bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
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
