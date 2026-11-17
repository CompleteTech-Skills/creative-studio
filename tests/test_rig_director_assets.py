"""Actual geometry/export/registration acceptance for Director #28."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
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
            manifest = library.resolve(asset['reference'])
            self.assertEqual(manifest['identity']['id'],name)
            for item in manifest['files']:
                payload = library.read_file(asset['reference'],item['path'])
                self.assertEqual(payload,(self.root/item['path']).read_bytes())
                self.assertEqual(sha256(self.root/item['path']),item['sha256'])

    def test_existing_and_racing_destinations_are_preserved(self):
        sentinel=self.root/'user-owned.txt';sentinel.write_text('keep')
        with self.assertRaises(RigError):da.build_assets(self.root)
        self.assertEqual(sentinel.read_text(),'keep')
        target=self.parent/'raced'
        actual=da.rename_no_replace_at
        def occupied(source_fd, source, destination_fd, destination):
            target.mkdir()
            (target/'user-owned.txt').write_text('race')
            return actual(source_fd, source, destination_fd, destination)
        with patch.object(da,'rename_no_replace_at',side_effect=occupied),self.assertRaises(FileExistsError):
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

    # One explicit check per Director #28 acceptance criterion.
    def test_criterion_exactly_two_robots_with_distinct_identities(self):
        library=AssetLibrary(self.root/'library')
        kinds={name:library.resolve(asset['reference'])['identity']['kind']
               for name,asset in self.report['assets'].items()}
        robots=sorted(name for name,kind in kinds.items() if kind=='character')
        self.assertEqual(robots,sorted(da.ROBOTS))
        self.assertEqual(len(robots),2)
        hashes={self.report['assets'][name]['identity_sha256'] for name in robots}
        self.assertEqual(len(hashes),2)
        self.assertEqual({self.report['assets'][n]['identity']['id'] for n in robots},set(robots))
        for name in robots:
            stored=json.loads((self.root/(name+'-identity.json')).read_text())
            self.assertEqual(stored,self.report['assets'][name]['identity'])

    def test_criterion_laboratory_structure_and_separate_movable_key(self):
        names=[part[0] for part in da.LAB_PARTS]
        self.assertIn('floor',names)
        self.assertGreaterEqual(sum('wall' in n for n in names),3)
        for equipment in ('workbench-top','console-base','equipment-cabinet'):
            self.assertIn(equipment,names)
        lab=GLTF2().load_binary(str(self.root/'laboratory.glb'))
        self.assertEqual({node.name for node in lab.nodes if node.mesh is not None},set(names))
        self.assertNotIn('access-key',{node.name for node in lab.nodes})
        key=GLTF2().load_binary(str(self.root/'access-key.glb'))
        self.assertEqual(len(key.meshes),1)
        library=AssetLibrary(self.root/'library')
        self.assertEqual(library.resolve(self.report['assets']['access-key']['reference'])['identity']['kind'],'prop')
        self.assertEqual(library.resolve(self.report['assets']['laboratory']['reference'])['identity']['kind'],'environment')
        recipe=json.loads((self.root/'access-key-recipe.json').read_text())
        self.assertEqual(recipe['generator'],'director_assets.py')
        for asset in self.report['assets'].values():
            self.assertEqual(asset['identity']['origin'],'procedural-fixture')

    def test_criterion_geometry_digest_is_normalized_and_sensitive(self):
        first,second=da.lab_geometry(),da.lab_geometry()
        self.assertEqual(da.geometry_digest(first),da.geometry_digest(second))
        second.meshes[0].positions=second.meshes[0].positions+1e-3
        self.assertNotEqual(da.geometry_digest(first),da.geometry_digest(second))
        for asset in self.report['assets'].values():
            loaded=da.load_character_model(self.root/asset['source'])
            self.assertEqual(da.geometry_digest(loaded.geometry),asset['identity']['geometry_sha256'])

    def test_criterion_routes_walk_on_floor_and_cameras_inside_room(self):
        floor=next(part for part in da.LAB_PARTS if part[0]=='floor')
        center,size=np.array(floor[1]),np.array(floor[2])
        lower,upper=center-size/2,center+size/2
        self.assertAlmostEqual(upper[1],0.)
        for route in da.ROUTES.values():
            self.assertGreaterEqual(len(route),2)
            for point in route:
                self.assertAlmostEqual(point[1],upper[1])
                self.assertTrue(lower[0]+.85<=point[0]<=upper[0]-.85)
                self.assertTrue(lower[2]+.35<=point[2]<=upper[2]-.35)
        walls_top=max(c[1]+s[1]/2 for n,c,s,_ in da.LAB_PARTS if 'wall' in n)
        for camera in da.CAMERAS:
            self.assertTrue(lower[0]<camera[0]<upper[0] and lower[2]<camera[2]<upper[2])
            self.assertTrue(0<camera[1]<walls_top)
        layout=self.report['layout']
        self.assertEqual(layout['routes'],da.ROUTES)
        self.assertEqual(layout['camera_positions'],da.CAMERAS)

    def test_layout_binds_decoded_bounds_and_rejects_oversized_robot(self):
        bounds = {name: deepcopy(self.report['assets'][name]['geometry']['bounds']) for name in da.ROBOTS}
        for name in da.ROBOTS:
            self.assertEqual(self.report['layout']['robot_envelopes'][name]['measured'], bounds[name])
        bounds['aster'][1][0] = 5.
        with self.assertRaises(RigError) as caught:
            da.validate_layout(bounds)
        self.assertEqual(caught.exception.report['code'], 'DIRECTOR_ASSET_ROUTE')

    def test_bundle_budget_refuses_before_publication(self):
        target = self.parent/'over-budget'

        def generate(root, seed):
            (root/'fixture.txt').write_bytes(b'bounded fixture')
            return {}

        with patch.object(da,'MAX_BUNDLE_BYTES',1), patch.object(da,'_generate_assets',side_effect=generate):
            with self.assertRaises(RigError) as caught:
                da.build_assets(target, seed=42)
        self.assertEqual(caught.exception.report['code'], 'DIRECTOR_ASSET_LIMIT')
        self.assertFalse(target.exists())

    def test_geometry_budget_refuses_before_analysis(self):
        with patch.object(da,'MAX_VERTICES',1), patch.object(da,'analyze_geometry') as analyze:
            with self.assertRaises(RigError) as caught:
                da._inspect(da.lab_geometry())
        self.assertEqual(caught.exception.report['code'], 'DIRECTOR_ASSET_LIMIT')
        analyze.assert_not_called()

    def test_unavailable_publication_adapter_returns_typed_refusal(self):
        target = self.parent/'unsupported'
        with patch.object(da,'private_stage_supported',return_value=False), patch.object(da,'_generate_assets') as generate:
            with self.assertRaises(RigError) as caught:
                da.build_assets(target, seed=42)
        self.assertEqual(caught.exception.report['code'], 'DIRECTOR_ASSET_PLATFORM')
        generate.assert_not_called()
        self.assertFalse(target.exists())

    def test_build_scratch_cleanup_failure_cannot_publish(self):
        target = self.parent/'scratch-cleanup-failed'
        original = da.tempfile.TemporaryDirectory

        @contextmanager
        def cleanup_fails(*args, **kwargs):
            with original(*args, **kwargs) as temporary:
                yield temporary
            raise OSError('synthetic scratch cleanup failure')

        def generate(root, seed):
            (root/'fixture.txt').write_text('fixture')
            return {'fixture': True}

        with patch.object(da.tempfile,'TemporaryDirectory',side_effect=cleanup_fails), \
                patch.object(da,'_generate_assets',side_effect=generate), \
                patch.object(da,'rename_no_replace_at') as promote:
            with self.assertRaisesRegex(OSError,'synthetic scratch cleanup failure'):
                da.build_assets(target, seed=42)
        promote.assert_not_called()
        self.assertFalse(target.exists())

    def test_publication_cleanup_failure_reports_completed_bundle(self):
        target = self.parent/'publication-cleanup-warning'
        original = da.private_stage

        @contextmanager
        def cleanup_fails(*args, **kwargs):
            with original(*args, **kwargs) as stage:
                yield stage
            raise OSError('synthetic publication cleanup failure')

        def generate(root, seed):
            (root/'fixture.txt').write_text('fixture')
            return {'fixture': True}

        with patch.object(da,'private_stage',side_effect=cleanup_fails), patch.object(da,'_generate_assets',side_effect=generate):
            report = da.build_assets(target, seed=42)
        self.assertEqual((target/'fixture.txt').read_text(), 'fixture')
        self.assertTrue(report['fixture'])
        self.assertEqual(report['publication_warning']['code'], 'DIRECTOR_ASSET_CLEANUP')
        self.assertNotIn('synthetic', json.dumps(report))

    def test_cli_io_failure_uses_path_free_diagnostic(self):
        output = io.StringIO()
        with patch.object(da,'build_assets',side_effect=OSError(13, 'Permission denied', str(self.parent/'private'))), \
                redirect_stdout(output):
            result = da._main(['--destination', str(self.parent/'unused'), '--seed', '42'])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())['diagnostic']['code'], 'DIRECTOR_ASSET_IO')
        self.assertNotIn(str(self.parent), output.getvalue())
