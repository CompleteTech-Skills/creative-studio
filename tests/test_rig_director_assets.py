"""Actual geometry/export/registration acceptance for Director #28."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from pygltflib import GLTF2

import director_assets as da
from asset_library import AssetLibrary
from rig_errors import RigError, sha256
from rig_models import load_rig
from rig_skinning import evaluate_deformation
from rig_skeleton import neutral_pose, rotate_bone
from rig_math import axis_angle


class DirectorAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.parent = Path(cls.temp.name)
        cls.root = cls.parent/'first'
        cls.report = da.build_assets(cls.root,seed=42)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_exact_persistent_cast_and_actual_editable_rigs(self):
        self.assertEqual(set(self.report['assets']), {'aster','moss','laboratory','access-key'})
        for name in da.ROBOTS:
            rig = load_rig(self.root/('rig-'+name))
            self.assertGreater(len(rig.skeleton['bones']),20)
            self.assertEqual(rig.manifest['id'],name)
            self.assertEqual(rig.manifest['source']['origin'],'procedural-fixture')
            self.assertGreater(len(rig.geometry.meshes),10)
            self.assertTrue(rig.weights)
            pose = rotate_bone(rig.skeleton, neutral_pose(rig.skeleton),'upper-arm.right',axis_angle([0,0,1],.3))
            moved = evaluate_deformation(rig,pose)
            self.assertGreater(max(np.linalg.norm(moved[m.id]['positions']-m.positions,axis=1).max()
                                   for m in rig.geometry.meshes),.01)
        a,b = (self.report['assets'][name]['identity'] for name in da.ROBOTS)
        self.assertNotEqual(a['source_sha256'],b['source_sha256'])
        self.assertNotEqual(a['geometry_sha256'],b['geometry_sha256'])

    def test_independent_gltf_reader_loads_every_actual_export(self):
        for path in self.root.glob('*.glb'):
            with self.subTest(path=path.name):
                gltf = GLTF2().load_binary(str(path))
                blob = gltf.binary_blob()
                self.assertTrue(gltf.meshes)
                positions=[]
                for mesh in gltf.meshes:
                    for primitive in mesh.primitives:
                        accessor=gltf.accessors[primitive.attributes.POSITION]
                        view=gltf.bufferViews[accessor.bufferView]
                        offset=(view.byteOffset or 0)+(accessor.byteOffset or 0)
                        vertex=np.frombuffer(blob,dtype='<f4',count=accessor.count*3,offset=offset).reshape(-1,3)
                        self.assertTrue(np.isfinite(vertex).all())
                        positions.append(vertex)
                        self.assertGreater(gltf.accessors[primitive.indices].count,0)
                extent=np.ptp(np.concatenate(positions),axis=0)
                self.assertTrue((extent>0).all())
                if path.stem.endswith('-rigged'): self.assertTrue(gltf.skins)

    def test_lab_component_pivots_survive_actual_glb(self):
        gltf=GLTF2().load_binary(str(self.root/'laboratory.glb'))
        nodes={node.name:node for node in gltf.nodes}
        for name,center,_,_ in da.LAB_PARTS:
            self.assertEqual(nodes[name].translation,center)
            self.assertIsNotNone(nodes[name].mesh)

    def test_geometry_normals_bounds_and_layout(self):
        for asset in self.report['assets'].values():
            self.assertTrue(all(size>0 for size in asset['geometry']['dimensions_m']))
            for mesh in asset['geometry']['meshes']:
                self.assertEqual(mesh['boundary_edges'],0)
                self.assertEqual(mesh['nonmanifold_edges'],0)
                self.assertEqual(mesh['degenerate_triangles'],0)
                self.assertGreater(mesh['signed_volume_m3'],0)
        self.assertTrue(self.report['layout']['collision_free'])
        self.assertEqual(self.report['layout']['route_samples'],204)
        self.assertIn('door-lintel',[part[0] for part in da.LAB_PARTS])
        self.assertIn('console-screen',[part[0] for part in da.LAB_PARTS])

    def test_repeat_generation_stable_normalized_geometry_and_identity(self):
        second=da.build_assets(self.parent/'second',seed=42)
        for name in self.report['assets']:
            first=self.report['assets'][name]
            self.assertEqual(first['identity'],second['assets'][name]['identity'])
            self.assertEqual(first['identity_sha256'],second['assets'][name]['identity_sha256'])
        self.assertFalse(second['production_approved'])
        self.assertFalse(second['provider_executed'])

    def test_complete_native_dependency_inventory(self):
        inventory=self.report['inventory']
        self.assertTrue(any(x['path']=='rig-aster/rig.json' for x in inventory))
        self.assertTrue(any(x['path']=='rig-moss/rig.json' for x in inventory))
        for item in inventory:
            path=self.root/item['path']
            self.assertEqual(path.stat().st_size,item['bytes'])
            self.assertEqual(sha256(path),item['sha256'])

    def test_registered_glbs_survive_library_reopen(self):
        library=AssetLibrary(self.root/'library')
        for name,asset in self.report['assets'].items():
            stored=library.read_file(asset['reference'],asset['source'])
            self.assertEqual(stored,(self.root/asset['source']).read_bytes())
            self.assertEqual(library.resolve(asset['reference'])['identity']['id'],name)

    def test_existing_and_racing_destinations_are_preserved(self):
        sentinel=self.root/'user-owned.txt';sentinel.write_text('keep')
        with self.assertRaises(RigError):da.build_assets(self.root)
        self.assertEqual(sentinel.read_text(),'keep')
        target=self.parent/'raced'
        actual=da._rename_no_replace
        def race(stage,destination):
            destination.mkdir();(destination/'user-owned.txt').write_text('race')
            return actual(stage,destination)
        with patch.object(da,'_rename_no_replace',side_effect=race),self.assertRaises(FileExistsError):
            da.build_assets(target)
        self.assertEqual((target/'user-owned.txt').read_text(),'race')
        self.assertEqual(sorted(p.name for p in target.iterdir()),['user-owned.txt'])

    def test_rejects_symlink_destination_parent_and_bad_seed(self):
        for seed in [True,-1,2**32,'1']:
            with self.subTest(seed=seed), self.assertRaises(RigError):da.build_assets(self.parent/'bad',seed=seed)
        linked=self.parent/'linked'
        try: linked.symlink_to(self.parent,target_is_directory=True)
        except (OSError,NotImplementedError):self.skipTest('symlinks unavailable')
        with self.assertRaises(RigError):da.build_assets(linked/'bad')

    def test_route_and_camera_blockers_are_detected(self):
        parts=deepcopy(da.LAB_PARTS)
        parts.append(('blocker',[-.7,1.,.4],[2.,2.,2.],0))
        with patch.object(da,'LAB_PARTS',parts),self.assertRaises(RigError):da.validate_layout()
        with patch.object(da,'CAMERAS',[[0.,1.5,-3.]]),self.assertRaises(RigError):da.validate_layout()
