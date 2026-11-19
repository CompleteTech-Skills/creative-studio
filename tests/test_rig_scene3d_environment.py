"""Real generated geometry, spatial limits and immutable save/reopen regressions."""
from collections import Counter
from copy import deepcopy
import json
import hashlib
import math
import os
from pathlib import Path
import tempfile
import unittest
import numpy as np
from rig_errors import RigError
from scene3d_geometry import Mesh, primitive, wall, extrude, curve, polygon
from scene3d_terrain import Heightfield, scatter
from scene3d_environment import plan, create_environment, create_environments, apply_recipe, apply_recipes, load_meshes, fingerprint
from scene3d_contracts import load_project, canonical_bytes
from scene3d_graph import SceneGraph
from scene3d_snapshot import snapshot_project


def recipe(generator='rooms',**parameters):
    return dict(id='test',version=1,seed=7,generator=generator,parameters=parameters,budget=100)


class GeometryTests(unittest.TestCase):
    def test_supported_primitives_have_real_nondegenerate_meshes(self):
        for kind in ('box','plane','cylinder','cone','sphere','capsule','torus','ramp','stairs','pipe','beam','platform','door','window'):
            with self.subTest(kind=kind):
                mesh=primitive(kind);mesh.validate()
                self.assertGreater(len(mesh.obj()),100)
                self.assertIn(b'\nf ',mesh.obj())
                self.assertIn(b'\nvn ',mesh.obj())
        extrude([[0,0],[1,0],[1,1],[0,1]]).validate()
        curve([[0,0,0],[1,1,0],[2,1,1]]).validate()

    def test_concave_custom_polygon_and_closed_extrusion(self):
        points=[[0,0],[3,0],[3,1],[1,1],[1,3],[0,3]]
        mesh=polygon(points);v=np.array(mesh.vertices);f=np.array(mesh.triangles)
        self.assertAlmostEqual(sum(np.linalg.norm(np.cross(v[b]-v[a],v[c]-v[a]))/2 for a,b,c in f),5)
        for profile in (points,list(reversed(points))):
            solid=extrude(profile,height=2);v=np.array(solid.vertices);f=np.array(solid.triangles)
            volume=np.einsum('ij,ij->i',v[f[:,0]],np.cross(v[f[:,1]],v[f[:,2]])).sum()/6
            self.assertAlmostEqual(volume,10)
        with self.assertRaises(RigError):polygon([[0,0],[2,2],[0,2],[2,0]])

    def test_polygon_holes_cut_through_concave_caps_and_form_closed_inner_walls(self):
        outer = [[0,0],[12,0],[12,8],[8,8],[8,5],[0,5]]
        elbow = [[1,1],[4,1],[4,2],[2,2],[2,4],[1,4]]
        window = [[9,4],[11,4],[11,7],[9,7]]
        offset = np.array([9500,-9000])
        for rings in ((outer, elbow, window), (outer[::-1], window[::-1], elbow),
                      (outer, elbow[::-1], window)):
            points, *holes = [(ring + offset).tolist() for ring in rings]
            planar = primitive('polygon', points=points, holes=holes)
            solid = primitive('extrude', points=points, holes=holes, height=2)
            vertices = np.array(solid.vertices) - [offset[0],0,offset[1]]
            faces = np.array(solid.triangles)
            tris = vertices[faces]
            normals = np.cross(tris[:,1]-tris[:,0], tris[:,2]-tris[:,0])
            for y, sign in ((0,-1), (2,1)):
                cap = normals[(tris[:,:,1] == y).all(axis=1),1]
                self.assertTrue((cap*sign > 0).all())
                self.assertAlmostEqual(cap.sum()/2, sign*(72-5-6))
            volume = np.einsum('ij,ij->i',tris[:,0],normals).sum()/6
            self.assertAlmostEqual(volume, 122)
            edges = Counter((a,b) for tri in faces for a,b in zip(tri, np.roll(tri,-1)))
            for (a,b), uses in edges.items():
                self.assertEqual((uses,edges[b,a]), (1,1))

            # Shoot vertical rays through the cutouts, the notch, and solid cap.
            cap = (np.array(planar.vertices)[:,[0,2]] - offset)[planar.triangles]
            for pos, covered in (([1.5,3],False), ([3,1.5],False), ([10,6],False),
                                 ([5,7],False), ([3,3],True), ([9,2],True)):
                side = np.roll(cap,-1,axis=1) - cap
                delta = pos-cap
                crosses = side[:,:,0]*delta[:,:,1] - side[:,:,1]*delta[:,:,0]
                self.assertEqual(bool((crosses <= 0).all(axis=1).any()), covered)

    def test_micrometer_through_hole_preserves_cap_area_and_closed_volume(self):
        outer = [[0,0], [40e-6,0], [40e-6,40e-6], [0,40e-6]]
        hole = [[2e-6,2e-6], [38e-6,2e-6], [38e-6,38e-6], [2e-6,38e-6]]
        area = 3.04e-10
        height = 20e-6
        for kind in ('polygon', 'extrude'):
            with self.subTest(kind=kind):
                dimensions = {'height': height} if kind == 'extrude' else {}
                mesh = primitive(kind, points=outer, holes=[hole], **dimensions)
                triangles = np.asarray(mesh.vertices)[mesh.triangles]
                normals = np.cross(triangles[:,1]-triangles[:,0], triangles[:,2]-triangles[:,0])
                caps = [(0,1)] if kind == 'polygon' else [(0,-1), (height,1)]
                for y, sign in caps:
                    cap = normals[(triangles[:,:,1] == y).all(axis=1),1]
                    self.assertEqual(len(cap), 8)
                    self.assertTrue((sign*cap > 0).all())
                    self.assertTrue(math.isclose(sign*sum(cap)/2, area, rel_tol=1e-12))
                if kind == 'extrude':
                    self.assertEqual(len(triangles), 32)
                    edges = Counter((a,b) for face in mesh.triangles
                                    for a,b in zip(face, face[1:]+face[:1]))
                    for (a,b), uses in edges.items():
                        self.assertEqual((uses, edges[b,a]), (1,1))
                    volume = np.einsum('ij,ij->i', triangles[:,0], normals).sum()/6
                    self.assertTrue(math.isclose(volume, area*height, rel_tol=1e-12))

    def test_float_scaled_holes_keep_nondegenerate_caps(self):
        rings = ([[0,0],[5,0],[5,4],[0,4]], [[2,3],[4,3],[4,2]], [[3,2],[2,1],[3,1]])
        # At 0.1 an exact sliver falls below the mesh area floor. At 35.6
        # and at translated scale 133.3, float cross products cancel to zero
        # for ears whose exact areas can exceed the floor.
        for scale, offset in ((0.1,0), (0.37,0), (0.01,0), (35.6,0), (133.3,0.1)):
            points, *holes = [(np.asarray(ring)*scale + [offset,-offset]).tolist() for ring in rings]
            for kind in ('polygon', 'extrude'):
                with self.subTest(kind=kind, scale=scale, offset=offset):
                    mesh = primitive(kind, points=points, holes=holes)
                    triangles = np.asarray(mesh.vertices)[mesh.triangles]
                    normals = np.cross(triangles[:,1]-triangles[:,0], triangles[:,2]-triangles[:,0])
                    caps = [(0,1)] if kind == 'polygon' else [(0,-1), (1,1)]
                    for y, sign in caps:
                        oriented = sign*normals[(triangles[:,:,1] == y).all(axis=1),1]
                        self.assertEqual(len(oriented), 12)
                        self.assertTrue((oriented > 1e-12).all())
                        self.assertTrue(math.isclose(sum(oriented)/2, 18.5*scale**2, rel_tol=1e-12))
                    self.assertEqual(mesh.obj().count(b'\nf '), len(mesh.triangles))
                    if kind == 'extrude':
                        edges = Counter((a,b) for face in mesh.triangles for a,b in zip(face, face[1:]+face[:1]))
                        self.assertTrue(all(n == edges[b,a] == 1 for (a,b),n in edges.items()))

    def test_triangulation_distinguishes_turns_lost_to_roundoff(self):
        from scene3d_geometry import _oriented_area2
        # The two cross-product terms round to the same float, although their
        # exact difference is one square grid unit.
        n, step = 2**27, 2.0**-18
        a = (0., 0.)
        b = ((n+1)*step, n*step)
        c = (n*step, (n-1)*step)
        self.assertEqual(b[0]*c[1], b[1]*c[0])
        self.assertEqual(_oriented_area2(a,b,c), -step**2)
        self.assertEqual(_oriented_area2(a,c,b), step**2)
        self.assertEqual(_oriented_area2(a,b,(2*b[0],2*b[1])), 0)

    def test_polygon_hole_validation_and_shared_vertex_budget(self):
        outer = [[0,0],[8,0],[8,8],[0,8]]
        hole = [[2,2],[5,2],[5,5],[2,5]]
        invalid = {
            'list required': None, 'malformed corner': [[[1,1],[3],[1,3]]],
            'nonfinite': [[[1,1],[float('inf'),1],[2,3]]],
            'non-numeric': [[[1,1],['bad',1],[2,3]]],
            'numeric string': [[[1,1],['3',1],[2,3]]],
            'boolean': [[[True,1],[3,1],[2,3]]],
            'large integer': [[[1,1],[10**400,1],[2,3]]],
            'coordinate bound': [[[1,1],[10001,1],[2,3]]],
            'self-crossing': [[[1,1],[4,4],[1,4],[4,1]]],
            'collinear corner': [[[1,1],[2,1],[3,1],[2,3]]],
            'repeated corner': [[[1,1],[2,1],[2,1],[2,3]]],
            'outside': [[[9,1],[10,1],[10,3]]],
            'outline vertex contact': [[[0,0],[2,1],[1,2]]],
            'outline edge contact': [[[0,2],[2,2],[2,3]]],
            'outline crossing': [[[1,1],[1,3],[-1,2]]],
            'nested': [hole, [[3,3],[4,3],[4,4]]],
            'enclosing existing hole': [[[3,3],[4,3],[4,4]], hole],
            'overlapping holes': [hole, [[3,3],[6,3],[6,6]]],
            'touching holes': [hole, [[5,5],[6,5],[6,6]]],
            'coincident holes': [hole, hole],
        }
        # Each corner of this triangle is inside the concave outline, but an
        # edge crosses its notch.
        notch = [[0,0],[8,0],[8,8],[5,8],[5,3],[3,3],[3,8],[0,8]]
        for kind in ('polygon','extrude'):
            for case, holes in invalid.items():
                with self.subTest(kind=kind, case=case), self.assertRaises(RigError):
                    plan(recipe('primitive', kind=kind, dimensions=dict(points=outer, holes=holes)))
            with self.assertRaises(RigError):
                primitive(kind, points=notch, holes=[[[1,1],[7,6],[1,6]]])
        for n in (253,254):
            outline = [[10*math.cos(2*math.pi*i/n),10*math.sin(2*math.pi*i/n)] for i in range(n)]
            if n == 253:
                self.assertEqual(len(polygon(outline, holes=[[[0,0],[1,0],[0,1]]]).triangles), 256)
            else:
                with self.assertRaises(RigError):
                    polygon(outline, holes=[[[0,0],[1,0],[0,1]]])

    def test_empty_polygon_holes_preserve_legacy_obj_bytes(self):
        outline = [[0,0],[3,0],[3,1],[1,1],[1,3],[0,3]]
        cases = ((outline, '4bfc6eb6b7df86a2360f9117fdab5188c6b47630ab9e049be9d86cb901806b94'),
                 (outline[::-1], 'e284f395e25c9b1cb4eb0b6ba7f1557feb357b0ea2b6f72a92c8cd37db1dc048'))
        for points, expected in cases:
            self.assertEqual(hashlib.sha256(extrude(points,2,holes=[]).obj()).hexdigest(), expected)
            self.assertEqual(polygon(points,holes=[]).obj(), polygon(points).obj())

    def test_integer_json_vertices_emit_finite_unit_normals(self):
        mesh=Mesh([[0,0,0],[1,0,0],[0,1,0]],[[0,1,2]])
        mesh.validate();payload=mesh.obj()
        self.assertIn(b'f 1/1/1 2/2/1 3/3/1',payload)
        normals=[line for line in payload.decode().splitlines() if line.startswith('vn ')]
        self.assertEqual(len(normals),1)
        self.assertAlmostEqual(np.linalg.norm([float(v) for v in normals[0].split()[1:]]),1)
        self.assertEqual(mesh.vertices,[[0,0,0],[1,0,0],[0,1,0]])
        numeric_strings=Mesh([['0','0','0'],['1','0','0'],['0','1','0']],[[0,1,2]])
        self.assertEqual(numeric_strings.bounds,dict(minimum=[0.,0.,0.],maximum=[1.,1.,0.]))
        self.assertEqual(numeric_strings.obj(),payload)

    def test_custom_coordinate_overflow_is_rejected_before_normals(self):
        for coordinate in (1e100,1e155):
            with self.assertRaises(RigError):polygon([[0,0],[coordinate,0],[coordinate,coordinate],[0,coordinate]]).obj()
        mesh=polygon([[0,0],[10000,0],[10000,10000],[0,10000]])
        normals=[line for line in mesh.obj().decode().splitlines() if line.startswith('vn ')]
        self.assertTrue(normals)
        for line in normals:
            self.assertAlmostEqual(np.linalg.norm([float(v) for v in line.split()[1:]]),1)

    def test_large_json_integer_parameters_fail_typed_before_geometry_or_publication(self):
        huge=10**400
        terrain=Heightfield(width=2,depth=2,resolution=2,elevation=1,heights=[[0,0],[0,0]])
        before=terrain.mesh().obj()
        for value in (huge,-huge):
            cases=[lambda:primitive('box',width=value),
                   lambda:wall(2,2,.2,[dict(x=value,y=0,width=1,height=1)]),
                   lambda:Heightfield(elevation=value),
                   lambda:terrain.sample(value,0),
                   lambda:terrain.edit([dict(x=0,z=0,height=value)]),
                   lambda:terrain.mask([[0,0],[0,1]],target=value),
                   lambda:terrain.path([[-1,0],[1,0]],1,elevation=value),
                   lambda:scatter(terrain,1,exclusions=[[-abs(value),-abs(value),abs(value),abs(value)]]),
                   lambda:scatter(terrain,1,clearance=value)]
            for operation in cases:
                with self.subTest(value_sign=value>0,operation=cases.index(operation)):
                    with self.assertRaises(RigError):operation()
                    self.assertEqual(terrain.mesh().obj(),before)
            with tempfile.TemporaryDirectory() as temporary:
                destination=Path(temporary)/'invalid'
                with self.assertRaises(RigError):
                    create_environment(destination,recipe('primitive',kind='box',dimensions=dict(width=value)))
                self.assertFalse(destination.exists())

    def test_closed_shapes_outward_winding(self):
        for kind in ('box','sphere','cylinder','cone','torus','pipe','capsule'):
            mesh=primitive(kind);v=np.array(mesh.vertices);f=np.array(mesh.triangles)
            volume=np.einsum('ij,ij->i',v[f[:,0]],np.cross(v[f[:,1]],v[f[:,2]])).sum()/6
            self.assertGreater(volume,0,kind)

    def test_opening_has_no_crossing_triangles(self):
        mesh=wall(5,3,.2,[dict(x=2,y=0,width=1,height=2)])
        for tri in np.array(mesh.vertices)[mesh.triangles]:
            center=tri.mean(axis=0)
            self.assertFalse(-.5<center[0]<.5 and 0<center[1]<2)

    def test_invalid_dimensions_and_profiles_rejected(self):
        for kwargs in (dict(width=0),dict(width=float('nan')),dict(no_such_parameter=1)):
            with self.assertRaises(RigError):primitive('box',**kwargs)
        with self.assertRaises(RigError):primitive('pipe',radius=1,inner_radius=2)
        with self.assertRaises(RigError):wall(2,2,.1,[dict(x=1,y=0,width=2,height=1)])
        with self.assertRaises(RigError):extrude([[0,0],[1,1],[0,1],[1,0]])


class TerrainTests(unittest.TestCase):
    def test_seed_slope_and_triangle_sampling(self):
        a=Heightfield(seed=8,max_slope=10);b=Heightfield(seed=8,max_slope=10)
        self.assertEqual(a.mesh().obj(),b.mesh().obj())
        self.assertNotEqual(a.mesh().obj(),Heightfield(seed=9).mesh().obj())
        t=Heightfield(width=2,depth=2,resolution=2,elevation=4,heights=[[0,1],[2,4]])
        self.assertAlmostEqual(t.sample(-.5,-.5)['height'],.75)
        self.assertAlmostEqual(t.sample(.5,.5)['height'],2.75)
        with self.assertRaises(RigError):t.sample(2,0)

    def test_triangle_slope_bound_and_edited_slope_failure(self):
        heights=np.zeros((5,5));heights[2,2]=1
        terrain=Heightfield(width=4,depth=4,resolution=5,elevation=1,heights=heights,max_slope=10)
        self.assertLessEqual(terrain.sample(.25,.25)['slope'],10+1e-9)
        terrain.heights[2,2]=4
        with self.assertRaises(RigError):terrain.mesh()

    def test_elevation_edits_masks_and_lod_preserve_limits(self):
        terrain=Heightfield(width=4,depth=4,resolution=5,elevation=2,heights=np.zeros((5,5)))
        terrain.edit([dict(x=2,z=2,height=2)])
        self.assertEqual(terrain.sample(0,0)['height'],2)
        coarse=terrain.lod_mesh(2)
        self.assertEqual(len(coarse.triangles),8)
        self.assertEqual(coarse.vertices[4],[0,2,0])
        mask=np.zeros((5,5));mask[2,2]=.5
        terrain.mask(mask,target=0)
        self.assertEqual(terrain.sample(0,0)['height'],1)
        previous=terrain.heights.copy()
        for updates in ([dict(x=2,z=2,height=3)], [dict(x=2,z=2,height=1)]*2):
            with self.assertRaises(RigError):terrain.edit(updates)
            np.testing.assert_array_equal(terrain.heights,previous)
        with self.assertRaises(RigError):terrain.mask(np.ones((5,5))*2)
        np.testing.assert_array_equal(terrain.heights,previous)
        with self.assertRaises(RigError):terrain.lod_mesh(3)

    def test_slope_violating_mask_and_edit_are_atomic(self):
        terrain=Heightfield(width=4,depth=4,resolution=5,elevation=2,heights=np.zeros((5,5)),max_slope=5)
        previous=terrain.heights.copy()
        with self.assertRaises(RigError):terrain.edit([dict(x=2,z=2,height=1)])
        np.testing.assert_array_equal(terrain.heights,previous)
        mask=np.zeros((5,5));mask[2,2]=1
        with self.assertRaises(RigError):terrain.mask(mask,target=1)
        np.testing.assert_array_equal(terrain.heights,previous)
        with self.assertRaises(RigError):terrain.path([[-2,0],[2,0]],width=1,elevation=1)
        np.testing.assert_array_equal(terrain.heights,previous)
        with self.assertRaises(RigError):terrain.path([[-2,0],[2,0]],width=1,elevation=3)
        np.testing.assert_array_equal(terrain.heights,previous)

    def test_flatten_path_and_spatial_scatter(self):
        a=Heightfield(seed=8).path([[-10,0],[10,0]],width=2,elevation=0)
        self.assertEqual(a.sample(0,0)['height'],0)
        points=scatter(a,20,spacing=2,seed=8,max_slope=90,exclusions=[[-2,-2,2,2]])
        self.assertEqual(points,scatter(a,20,spacing=2,seed=8,max_slope=90,exclusions=[[-2,-2,2,2]]))
        for i,p in enumerate(points):
            self.assertFalse(-2<=p[0]<=2 and -2<=p[2]<=2)
            for q in points[i+1:]:self.assertGreaterEqual(math.hypot(p[0]-q[0],p[2]-q[2]),2)
        with self.assertRaises(RigError):scatter(a,10,spacing=100,retries=1)


class EnvironmentTests(unittest.TestCase):
    def test_small_terrain_without_trees_has_no_prop_clearance_requirement(self):
        self.assertEqual(len(plan(dict(recipe('terrain',width=1,depth=1,scatter_count=0),budget=1))),1)

    def test_tree_geometry_fits_bounds_and_does_not_overlap(self):
        objects=plan(dict(recipe('terrain',elevation=0,scatter_count=100),budget=300))
        crowns=[o for o in objects if 'crown' in o['id']]
        for i,a in enumerate(crowns):
            self.assertLessEqual(abs(a['position'][0])+.65,10)
            self.assertLessEqual(abs(a['position'][2])+.65,10)
            for b in crowns[i+1:]:
                self.assertGreaterEqual(math.hypot(a['position'][0]-b['position'][0],a['position'][2]-b['position'][2]),1.3)

    def test_malformed_parameters_are_structured_errors(self):
        for p in ({'kind':[],'dimensions':{}},{'kind':'box','dimensions':[]}):
            with self.assertRaises(RigError):plan(recipe('primitive',**p))
        for data in ('not a heightmap',[[object()]]):
            with self.assertRaises(RigError):Heightfield(heights=data)
        with self.assertRaises(RigError):extrude([['a','b']])

    def test_external_house_door_and_internal_partition_have_real_ray_clearance(self):
        from rig_math import qmatrix
        def hits(item,origin):
            vertices=np.array(item['mesh'].vertices)@qmatrix(item['rotation']).T+item['position']
            direction=np.array([1.,0.,0.]);origin=np.array(origin);found=0
            for a,b,c in vertices[item['mesh'].triangles]:
                edge1=b-a;edge2=c-a;cross=np.cross(direction,edge2);det=float(np.dot(edge1,cross))
                if abs(det)<1e-10:continue
                offset=origin-a;u=float(np.dot(offset,cross)/det)
                if not 0<=u<=1:continue
                q=np.cross(offset,edge1);v=float(np.dot(direction,q)/det)
                if v<0 or u+v>1:continue
                distance=float(np.dot(edge2,q)/det)
                if 1e-8<distance<2:found+=1
            return found
        closed=next(o for o in plan(recipe(rooms=1)) if o['id']=='test-partition-0')
        opened=next(o for o in plan(recipe(rooms=1,external_door=True)) if o['id']=='test-partition-0')
        self.assertGreater(hits(closed,[-3.5,1.1,.1]),0)
        self.assertEqual(hits(opened,[-3.5,1.1,.1]),0)
        self.assertGreater(hits(opened,[-3.5,2.8,.1]),0)
        internal=next(o for o in plan(recipe(rooms=2,external_door=True)) if o['id']=='test-partition-1')
        self.assertEqual(hits(internal,[1.5,1.1,.1]),0)
        with self.assertRaises(RigError):plan(recipe(rooms=1,external_door='yes'))
        with self.assertRaises(RigError):plan(recipe(rooms=1,external_door=True,depth=1))

    def test_rooms_are_connected_and_bounded(self):
        items=plan(recipe(rooms=3))
        self.assertEqual(len([x for x in items if 'partition' in x['id']]),4)
        with self.assertRaises(RigError):plan(recipe(width=1,door_width=2))
        with self.assertRaises(RigError):plan(dict(recipe(),budget=2))
        with self.assertRaises(RigError):plan(recipe('caves'))

    def test_saved_extrusion_retains_hole_recipe_and_reopened_mesh(self):
        dimensions = dict(points=[[10,10],[18,10],[18,18],[10,18]],
                          holes=[[[12,12],[16,12],[16,16],[12,16]]], height=2)
        r = recipe('primitive', kind='extrude', dimensions=dimensions)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'saved'
            create_environment(root, r)
            bundle = load_project(root)
            path = bundle['project']['scenes'][-1]['path']
            scene = bundle['documents'][path]
            stored = root / scene['metadata']['generator_recipe_test']
            self.assertEqual(json.loads(stored.read_bytes()), r)
            mesh = load_meshes(root, path)['test-mesh']
            triangles = np.asarray(mesh['vertices'])[mesh['triangles']]
            normals = np.cross(triangles[:,1]-triangles[:,0], triangles[:,2]-triangles[:,0])
            self.assertAlmostEqual(np.einsum('ij,ij->i', triangles[:,0], normals).sum()/6, 96)
            invalid = deepcopy(r)
            invalid['parameters']['dimensions']['holes'][0][0] = [10,10]
            destination = Path(temp) / 'invalid'
            with self.assertRaises(RigError):
                apply_recipe(root, destination, path, invalid)
            self.assertFalse(destination.exists())
            self.assertEqual(load_project(root)['inventory'], bundle['inventory'])

    def test_publication_requires_recipe_ids_that_fit_scene_metadata_and_nodes(self):
        r=recipe('primitive',kind='box',dimensions={})
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source'
            create_environment(source,dict(r,id='a'*46))
            bundle=load_project(source);path=bundle['project']['scenes'][-1]['path']
            for identifier in ('Upper','1test','-test','a'*47):
                with self.subTest(identifier=identifier):
                    output=root/'output'
                    with self.assertRaises(RigError) as failure:
                        apply_recipe(source,output,path,dict(r,id=identifier))
                    self.assertEqual(failure.exception.report['code'],'SCENE3D_GENERATOR_ID')
                    self.assertFalse(output.exists())

    def test_schema_valid_malformed_history_is_a_typed_atomic_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source'
            r=recipe('primitive',kind='box',dimensions={});create_environment(source,r)
            b=load_project(source);path=b['project']['scenes'][-1]['path']
            for i,(key,value) in enumerate((('generator_nodes_test','{'),('generator_deleted_test','{'),('generator_nodes_test',2))):
                scene=deepcopy(b['documents'][path]);scene['metadata'][key]=value
                staged=root/f'staged-{i}';snapshot_project(source,staged,SceneGraph(scene),scene_path=path,
                    expected_project_sha256=b['inventory']['project.json']['sha256'])
                staged_bundle=load_project(staged);selected=staged_bundle['project']['scenes'][-1]['path']
                with self.assertRaises(RigError):apply_recipe(staged,root/f'output-{i}',selected,r)
                self.assertFalse((root/f'output-{i}').exists())

    def test_hash_verified_malformed_obj_numbers_are_typed_failures(self):
        from scene3d_materials import obj_material_bindings
        payloads=[b'\xff',b'v bogus 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n',b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf bogus 2 3\n']
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';create_environment(source,recipe('primitive',kind='box',dimensions={}))
            b=load_project(source);path=b['project']['scenes'][-1]['path']
            original_node=next(n for n in b['documents'][path]['nodes'] if n['id']=='test-mesh')
            for i,raw in enumerate(payloads):
                asset=deepcopy(b['documents'][original_node['asset']['path']]);asset['id']=f'malformed-{i}'
                file=dict(path=f'generated/malformed-{i}.obj',sha256=hashlib.sha256(raw).hexdigest())
                asset.update(content=file,source=file,meshes=[file],material_bindings=obj_material_bindings(asset['id']))
                asset['resources']['bytes']=len(raw);asset_path=f'generated/malformed-{i}.json'
                ref=dict(id=asset['id'],kind='scene3d-asset',version=asset['version'],path=asset_path,sha256=hashlib.sha256(canonical_bytes(asset)).hexdigest())
                scene=deepcopy(b['documents'][path]);n=deepcopy(original_node);n.update(id=f'malformed-node-{i}',asset=ref)
                scene['nodes'].append(n);scene['roots'].append(n['id']);scene['dependencies'].append(ref)
                staged=root/f'malformed-{i}'
                snapshot_project(source,staged,SceneGraph(scene),scene_path=path,derived_documents={asset_path:asset},
                    extra_payloads={file['path']:raw},expected_project_sha256=b['inventory']['project.json']['sha256'])
                verified=load_project(staged);selected=verified['project']['scenes'][-1]['path']
                with self.assertRaises(RigError) as failure:load_meshes(staged,selected)
                self.assertIsNotNone(failure.exception.__cause__)

    def test_saved_planar_terrain_uvs_match_lods_and_native_obj_import(self):
        from rig_obj import import_obj
        from rig_gltf import export_geometry
        from rig_import import load_character_model

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arguments = dict(width=12, depth=6, resolution=13, elevation=2, lod_strides=[2, 3])
            original = root / 'original'
            create_environment(original, recipe('terrain', **arguments))
            planar = root / 'planar'
            create_environment(planar, recipe('terrain', planar_uv=True, **arguments))

            def assets(project):
                bundle = load_project(project)
                path = bundle['project']['scenes'][-1]['path']
                scene = bundle['documents'][path]
                stored = json.loads((project / scene['metadata']['generator_recipe_test']).read_text())
                self.assertEqual(stored['parameters'].get('planar_uv', False), project == planar)
                ground = next(node for node in scene['nodes'] if node['id'] == 'test-ground')
                asset = bundle['documents'][ground['asset']['path']]
                return path, [asset, *(bundle['documents'][r['path']] for r in asset['lods'])]

            legacy_path, legacy_assets = assets(original)
            planar_path, planar_assets = assets(planar)
            explicit_false = root / 'explicit-false'
            create_environment(explicit_false, recipe('terrain', planar_uv=False, **arguments))
            self.assertEqual(assets(explicit_false)[1], legacy_assets)
            self.assertEqual(load_meshes(original, legacy_path), load_meshes(planar, planar_path))
            for stride, legacy_asset, asset in zip((1, 2, 3), legacy_assets, planar_assets, strict=True):
                with self.subTest(stride=stride):
                    path = planar / asset['content']['path']
                    lines = path.read_text().splitlines()
                    old = (original / legacy_asset['content']['path']).read_text().splitlines()
                    self.assertEqual([line for line in lines if line.startswith(('v ', 'vn '))],
                                     [line for line in old if line.startswith(('v ', 'vn '))])
                    uv = np.array([list(map(float, line.split()[1:])) for line in lines if line.startswith('vt ')])
                    n = 12 // stride + 1
                    self.assertEqual(uv.shape, (n*n, 2))
                    np.testing.assert_array_equal(uv[[0, n-1, -n, -1]], [[0, 1], [1, 1], [0, 0], [1, 0]])
                    if stride == 1:
                        full_uv = uv.reshape(n, n, 2)
                    else:
                        np.testing.assert_array_equal(uv, full_uv[::stride, ::stride].reshape(-1, 2))
                    imported = import_obj(path)
                    mesh = imported.geometry.meshes[0]
                    native_uv = np.column_stack((mesh.positions[:, 0] / 12 + .5, mesh.positions[:, 2] / 6 + .5))
                    np.testing.assert_allclose(mesh.attributes['TEXCOORD_0'], native_uv, rtol=0, atol=1e-15)
                    for extension in ('glb', 'gltf'):
                        output = root / f'lod-{stride}.{extension}'
                        export_geometry(imported.geometry, output, provenance={'fixture': True})
                        reopened = load_character_model(output).geometry.meshes[0]
                        np.testing.assert_array_equal(reopened.triangles, mesh.triangles)
                        np.testing.assert_allclose(reopened.positions, mesh.positions, rtol=0, atol=1e-6)
                        np.testing.assert_allclose(reopened.attributes['TEXCOORD_0'], native_uv, rtol=0, atol=1e-7)
            for index, value in enumerate((0, 1, None, 'true', [], {})):
                with self.subTest(planar_uv=value):
                    invalid = root / f'invalid-{index}'
                    with self.assertRaises(RigError):
                        create_environment(invalid, recipe('terrain', planar_uv=value))
                    self.assertFalse(invalid.exists())

    @unittest.skipUnless(os.environ.get('HATCH_RIG_INTEROP_TEST') == '1',
                         'Enable HATCH_RIG_INTEROP_TEST=1 for independent optional validators.')
    def test_khronos_accepts_saved_planar_ground_and_lod_transports(self):
        from rig_obj import import_obj
        from rig_gltf import export_geometry
        from rig_interop import validate_gltf

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / 'project'
            create_environment(project, recipe('terrain', width=12, depth=6, resolution=13,
                                              elevation=2, lod_strides=[2, 3], planar_uv=True))
            bundle = load_project(project)
            scene = bundle['documents'][bundle['project']['scenes'][-1]['path']]
            ground = next(node for node in scene['nodes'] if node['id'] == 'test-ground')
            asset = bundle['documents'][ground['asset']['path']]
            assets = [asset, *(bundle['documents'][ref['path']] for ref in asset['lods'])]
            for stride, asset in zip((1, 2, 3), assets, strict=True):
                geometry = import_obj(project / asset['content']['path']).geometry
                for extension in ('glb', 'gltf'):
                    with self.subTest(stride=stride, extension=extension):
                        output = root / f'lod-{stride}.{extension}'
                        exported = export_geometry(geometry, output, provenance={'fixture': True})
                        report = validate_gltf(output)
                        self.assertTrue(report['ok'], report)
                        self.assertEqual(report['issues']['numErrors'], 0)
                        self.assertIs(report['issues']['truncated'], False)
                        self.assertEqual(report['asset_sha256'], exported['sha256'])

    def test_actual_terrain_lod_dependency_closure_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'terrain'
            create_environment(root,recipe('terrain',resolution=9,elevation=2,lod_strides=[2,4]))
            b=load_project(root);scene=b['documents'][b['project']['scenes'][-1]['path']]
            ground=next(n for n in scene['nodes'] if n['id']=='test-ground')
            asset=b['documents'][ground['asset']['path']]
            self.assertEqual(len(asset['lods']),2)
            self.assertEqual(asset['resources']['triangles'],128)
            from scene3d_materials import project_material_slots
            projection=project_material_slots(asset)
            self.assertEqual(projection[0]['slot'],'surface')
            self.assertIsNone(projection[0]['material_index'])
            self.assertEqual(projection[0]['mesh'],asset['id'])
            lods=[b['documents'][r['path']] for r in asset['lods']]
            self.assertEqual([a['resources']['triangles'] for a in lods],[32,8])
            for lod in lods:
                self.assertTrue((root/lod['content']['path']).is_file())
                self.assertEqual(project_material_slots(lod)[0]['mesh'],lod['id'])

    def test_multi_recipe_snapshot_is_atomic_and_records_local_placement(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);first=root/'first';create_environment(first,recipe(rooms=1))
            b=load_project(first);path=b['project']['scenes'][-1]['path'];original=b['documents'][path]
            left=dict(recipe('primitive',kind='box',dimensions={}),id='left')
            right=dict(recipe('primitive',kind='sphere',dimensions={}),id='right')
            result=apply_recipes(first,root/'batch',path,[left,right],offsets={'left':[3,0,0],'right':[0,2,0]},parents={'right':'left-mesh'})
            final=load_project(root/'batch');scene=final['documents'][final['project']['scenes'][-1]['path']]
            self.assertEqual(scene['revision'],original['revision']+1)
            self.assertEqual(len(final['project']['scenes']),len(b['project']['scenes'])+1)
            graph=SceneGraph(scene)
            np.testing.assert_allclose(np.asarray(graph.evaluate_base('right-mesh')['world_matrix'])[:3,3],[3,2,0])
            stored=[json.loads((root/'batch'/r['path']).read_text()) for r in scene['recipes']]
            self.assertTrue(any(r.get('placement',{}).get('parent')=='left-mesh' for r in stored))
            invalid=dict(right,parameters={'kind':'box','dimensions':{'width':0}})
            with self.assertRaises(RigError):apply_recipes(first,root/'failed',path,[left,invalid])
            self.assertFalse((root/'failed').exists())
            self.assertEqual(load_project(first)['documents'][path],original)
            with self.assertRaises(RigError):apply_recipes(first,root/'duplicate',path,[left,left])

    def test_regeneration_preserves_each_omitted_placement_and_accepts_explicit_reset(self):
        left=dict(recipe('primitive',kind='box',dimensions={}),id='left')
        right=dict(recipe('primitive',kind='sphere',dimensions={}),id='right')
        changed=dict(right,parameters={'kind':'sphere','dimensions':{'radius':2}})
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source=root/'initial'
            create_environments(source,[left,right],offsets={'left':(3,0,0),'right':(0,2,0)},
                                parents={'right':'left-mesh'})
            path=load_project(source)['project']['scenes'][-1]['path']
            steps=[
                ('omitted',{},'left-mesh',[0,2,0],[3,2,0]),
                ('new-offset',{'offsets':{'right':[0,4,0]}},'left-mesh',[0,4,0],[3,4,0]),
                ('new-parent',{'parents':{'right':'root'}},'root',[0,4,0],[0,4,0]),
                ('reset-parent',{'parents':{'right':None}},None,[0,4,0],[0,4,0]),
                ('reset-offset',{'offsets':{'right':None}},None,[0,0,0],[0,0,0]),
                ('reopen-defaults',{},None,[0,0,0],[0,0,0]),
            ]
            for name,kwargs,parent,local,world in steps:
                with self.subTest(name=name):
                    target=root/name
                    result=apply_recipes(source,target,path,[left,changed],**kwargs)
                    self.assertEqual(result['conflicts'],[])
                    self.assertEqual(result['generated'],2)
                    bundle=load_project(target)
                    path=bundle['project']['scenes'][-1]['path']
                    scene=bundle['documents'][path]
                    child=next(n for n in scene['nodes'] if n['id']=='right-mesh')
                    self.assertEqual(child['parent'],parent)
                    self.assertEqual(child['transform']['translation'],local)
                    matrix=SceneGraph(scene).evaluate_base('right-mesh')['world_matrix']
                    np.testing.assert_allclose(np.asarray(matrix)[:3,3],world)
                    stored=json.loads((target/scene['metadata']['generator_recipe_right']).read_text())
                    expected=changed if parent is None and local==[0,0,0] else {
                        'recipe':changed,'placement':{'offset':local,'parent':parent}
                    }
                    self.assertEqual(stored,expected)
                    source=target
            placed=root/'placed'
            apply_recipe(source,placed,path,changed,offset=(1,2,3),parent='left-mesh')
            path=load_project(placed)['project']['scenes'][-1]['path']
            reopened=root/'singular-reopen'
            apply_recipe(placed,reopened,path,changed)
            bundle=load_project(reopened);path=bundle['project']['scenes'][-1]['path']
            scene=bundle['documents'][path]
            child=next(n for n in scene['nodes'] if n['id']=='right-mesh')
            self.assertEqual(child['parent'],'left-mesh')
            self.assertEqual(child['transform']['translation'],[1,2,3])
            reset=root/'singular-reset'
            apply_recipe(reopened,reset,path,changed,offset=None,parent=None)
            bundle=load_project(reset)
            scene=bundle['documents'][bundle['project']['scenes'][-1]['path']]
            child=next(n for n in scene['nodes'] if n['id']=='right-mesh')
            self.assertIsNone(child['parent'])
            self.assertEqual(child['transform']['translation'],[0,0,0])

    def test_hash_verified_invalid_stored_placement_fails_atomically(self):
        r=recipe('primitive',kind='box',dimensions={})
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'initial'
            create_environment(source,r)
            bundle=load_project(source);path=bundle['project']['scenes'][-1]['path']
            scene=deepcopy(bundle['documents'][path])
            raw=json.dumps({'recipe':r,'placement':{'offset':[0,False,0],'parent':None}}).encode()
            sha=hashlib.sha256(raw).hexdigest()
            stored=f'generated/recipe-{sha}.json'
            scene['metadata']['generator_recipe_test']=stored
            scene['recipes']=[dict(path=stored,sha256=sha)]
            staged=root/'staged'
            snapshot_project(source,staged,SceneGraph(scene),scene_path=path,extra_payloads={stored:raw},
                             expected_project_sha256=bundle['inventory']['project.json']['sha256'])
            reopened=load_project(staged);path=reopened['project']['scenes'][-1]['path']
            output=root/'output'
            with self.assertRaises(RigError) as failure:
                apply_recipe(staged,output,path,r)
            self.assertEqual(failure.exception.report['code'],'SCENE3D_GENERATOR_HISTORY')
            self.assertFalse(output.exists())
            self.assertEqual(load_project(staged)['inventory'],reopened['inventory'])

    def test_deleted_nodes_stay_deleted_across_generator_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);first=root/'first'
            original=recipe('primitive',kind='box',dimensions={})
            create_environment(first,original)
            b=load_project(first);path=b['project']['scenes'][-1]['path']
            graph=SceneGraph.from_bundle(b,path,source_root=first);graph.delete('test-mesh')
            snapshot_project(first,root/'deleted',graph,scene_path=path,
                             expected_project_sha256=b['inventory']['project.json']['sha256'])
            b=load_project(root/'deleted');path=b['project']['scenes'][-1]['path']
            apply_recipe(root/'deleted',root/'rooms',path,recipe(rooms=1))
            b=load_project(root/'rooms');path=b['project']['scenes'][-1]['path']
            result=apply_recipe(root/'rooms',root/'back',path,original)
            b=load_project(root/'back');scene=b['documents'][b['project']['scenes'][-1]['path']]
            self.assertIn('test-mesh',result['conflicts'])
            self.assertNotIn('test-mesh',[n['id'] for n in scene['nodes']])

    def test_deleting_a_preserved_obsolete_edit_leaves_a_tombstone(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);first=root/'first'
            original=recipe('primitive',kind='box',dimensions={})
            create_environment(first,original)
            bundle=load_project(first);path=bundle['project']['scenes'][-1]['path']
            graph=SceneGraph.from_bundle(bundle,path,source_root=first)
            generated=next(n for n in graph.document()['nodes'] if n['id']=='test-mesh')
            transform=deepcopy(generated['transform']);transform['translation']=[1,2,3]
            graph.set_transform('test-mesh',transform)
            edited=root/'edited'
            snapshot_project(first,edited,graph,scene_path=path,
                             expected_project_sha256=bundle['inventory']['project.json']['sha256'])
            path=load_project(edited)['project']['scenes'][-1]['path']

            switched=root/'switched'
            result=apply_recipe(edited,switched,path,recipe(rooms=1))
            self.assertEqual(result['conflicts'],['test-mesh'])
            bundle=load_project(switched);path=bundle['project']['scenes'][-1]['path']
            graph=SceneGraph.from_bundle(bundle,path,source_root=switched)
            obsolete=next(n for n in graph.document()['nodes'] if n['id']=='test-mesh')
            self.assertEqual(obsolete['transform'],transform)
            self.assertEqual(obsolete['metadata']['generated_fingerprint'],
                             generated['metadata']['generated_fingerprint'])
            graph.delete('test-mesh')
            deleted=root/'deleted'
            snapshot_project(switched,deleted,graph,scene_path=path,
                             expected_project_sha256=bundle['inventory']['project.json']['sha256'])
            path=load_project(deleted)['project']['scenes'][-1]['path']

            restored=root/'restored'
            result=apply_recipe(deleted,restored,path,original)
            self.assertEqual(result['conflicts'],['test-mesh'])
            bundle=load_project(restored)
            scene=bundle['documents'][bundle['project']['scenes'][-1]['path']]
            self.assertNotIn('test-mesh',[n['id'] for n in scene['nodes']])
            self.assertEqual(json.loads(scene['metadata']['generator_deleted_test']),['test-mesh'])

    def test_preprocessing_collections_are_bounded(self):
        with self.assertRaises(RigError):plan(recipe('terrain',paths=[{}]*9))
        with self.assertRaises(RigError):scatter(Heightfield(),1,exclusions=[[0,0,1,1]]*129)

    def test_real_save_reopen_and_manual_edit_survive_regeneration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);first=root/'first'
            result=create_environment(first,recipe(rooms=2))
            bundle=load_project(first)
            scene_ref=bundle['project']['scenes'][-1]
            scene=bundle['documents'][scene_ref['path']]
            generated=[n for n in scene['nodes'] if n['metadata'].get('generator_id')=='test']
            self.assertEqual(len(generated),11)
            for node in generated:
                asset=bundle['documents'][node['asset']['path']]
                self.assertIn(b'\nf ',(first/asset['content']['path']).read_bytes())
            graph=SceneGraph(scene)
            selected=generated[0]['id']
            transform=deepcopy(generated[0]['transform']);transform['translation'][1]=12
            graph.set_transform(selected,transform)
            edited=root/'edited';saved=snapshot_project(first,edited,graph,scene_path=scene_ref['path'],
                expected_project_sha256=bundle['inventory']['project.json']['sha256'])
            b=load_project(edited);active=b['project']['scenes'][-1]['path']
            regenerated=root/'regenerated'
            result=apply_recipe(edited,regenerated,active,recipe(rooms=3))
            self.assertEqual(result['conflicts'],[selected])
            final=load_project(regenerated);nodes=final['documents'][final['project']['scenes'][-1]['path']]['nodes']
            self.assertEqual(next(n for n in nodes if n['id']==selected)['transform']['translation'][1],12)
            with self.assertRaises(RigError):apply_recipe(edited,root/'rejected',active,recipe(),policy='reject')
            self.assertFalse((root/'rejected').exists())

if __name__=='__main__':unittest.main()
