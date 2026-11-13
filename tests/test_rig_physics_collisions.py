"""Collision geometry admission and real-solver regression tests."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_collisions import CollisionError, validate_shape, validate_colliders
from physics_backend_example import _box


class CollisionProxyTests(unittest.TestCase):
    def cube(self):
        points, faces = _box([1, 1, 1])
        return dict(type='convex-hull', source='geometry', vertices=points, triangles=faces)

    def test_convex_proxy_proves_closed_oriented_volume(self):
        self.assertEqual(validate_shape(self.cube(), dynamic=True, source_ids=['geometry']), (8, 12, 1))
        for change in ('open', 'reverse-face', 'concave', 'unused', 'degenerate', 'flat'):
            shape = self.cube()
            shape['vertices'] = [list(x) for x in shape['vertices']]
            shape['triangles'] = [list(x) for x in shape['triangles']]
            if change == 'open': shape['triangles'].pop()
            if change == 'reverse-face': shape['triangles'][0].reverse()
            if change == 'concave': shape['vertices'][0] = [0, 0, 0]
            if change == 'unused': shape['vertices'].append([0, 0, 0])
            if change == 'degenerate': shape['triangles'][0] = [0, 0, 1]
            if change == 'flat':
                for point in shape['vertices']: point[2] = 0
            with self.subTest(change=change), self.assertRaises(CollisionError):
                validate_shape(shape, dynamic=True, source_ids=['geometry'])

    def test_static_triangle_mesh_may_be_open_but_not_dynamic_or_degenerate(self):
        shape=dict(type='triangle-mesh', source='geometry', vertices=[[0,0,0],[1,0,0],[0,0,1]], triangles=[[0,1,2]])
        self.assertEqual(validate_shape(shape,dynamic=False,source_ids=['geometry']), (3,1,1))
        with self.assertRaises(CollisionError):validate_shape(shape,dynamic=True,source_ids=['geometry'])
        with self.assertRaises(CollisionError):validate_shape(shape,dynamic=False,source_ids=[])

    def test_compound_depth_and_leaf_bounds(self):
        shape=dict(type='sphere',radius=.5)
        for _ in range(9):shape=dict(type='compound',children=[dict(shape=shape)])
        with self.assertRaises(CollisionError):validate_shape(shape,dynamic=True)
        shape=dict(type='compound',children=[dict(shape=dict(type='sphere',radius=.5)) for _ in range(65)])
        with self.assertRaises(CollisionError):validate_shape(shape,dynamic=True)

    def test_supported_primitive_dimensions_and_filter_limits(self):
        for shape in [dict(type='capsule',radius=.2,height=.8,axis='Y'),dict(type='cylinder',radius=.2,height=.8,axis='Z')]:
            self.assertEqual(validate_shape(shape,dynamic=True),(0,0,1))
            shape['radius']=float('inf')
            with self.assertRaises(CollisionError):validate_shape(shape,dynamic=True)


class CollisionAdmissionTests(unittest.TestCase):
    def test_explicit_profile_preserves_legacy_admission(self):
        from physics_backend_example import build_inputs
        from physics_backends import PyBulletBackend, BackendError
        world,_=build_inputs(backend='pybullet')
        backend=PyBulletBackend('/not-installed')
        world['bodies'][0]['colliders'][0]['mask']=1
        with self.assertRaises(BackendError):backend.validate_scene(world)
        world['collision_profile']='explicit-proxies-v1'
        self.assertEqual(backend.validate_scene(world)['status'],'configuration-valid')
        world['bodies'][0]['colliders'][0]['sensor']=True
        with self.assertRaises(BackendError):backend.validate_scene(world)

    def test_playground_configuration_has_no_solver_readiness_claim(self):
        from physics_collision_example import build_collision_inputs
        from physics_backends import PyBulletBackend
        world,_=build_collision_inputs()
        report=PyBulletBackend('/not-installed').validate_scene(world)
        self.assertFalse(report['solver_executed'])
        self.assertFalse(report['native_scene_verified'])
        self.assertEqual(len(world['bodies']),12)

    def test_exclusion_pairs_are_explicit_unique_existing_bodies(self):
        from physics_collision_example import build_collision_inputs
        from physics_contracts import validate_contract, PhysicsError
        world,_=build_collision_inputs()
        for pairs in [[['excluded','missing']],[['floor','floor']],[['floor','excluded'],['excluded','floor']]]:
            candidate=deepcopy(world);candidate['collision_exclusions']=pairs
            with self.subTest(pairs=pairs), self.assertRaises(PhysicsError):validate_contract(candidate)

    def test_native_child_observation_cannot_lie_about_geometry_or_frame(self):
        from physics_collisions import verify_native_children
        from physics_backend_example import build_inputs
        world,_=build_inputs(backend='pybullet')
        body=world['bodies'][0]
        controls=dict(inertial_rotation=[0,0,0,1],collision_children=[dict(type=2,dimensions=[.5,.5,.5],translation=[0,0,0],rotation=[0,0,0,1])])
        verify_native_children(body,controls)
        for mutate in [lambda c:c['collision_children'][0].update(type=3),
                       lambda c:c['collision_children'][0].update(dimensions=[1,1,1]),
                       lambda c:c['collision_children'][0].update(translation=[1,0,0]),
                       lambda c:c.update(collision_children=[])]:
            candidate=deepcopy(controls);mutate(candidate)
            with self.assertRaises(CollisionError):verify_native_children(body,candidate)


import os
import tempfile


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_BACKEND_TEST') == '1','Explicit real solver gate')
class CollisionNativeTests(unittest.TestCase):
    def setUp(self):
        from physics_backends import PyBulletBackend
        self.backend=PyBulletBackend(os.environ['HATCH_PYBULLET_PYTHON'])
        retained=os.environ.get('HATCH_COLLISION_EVIDENCE')
        if retained:
            self.root=Path(retained)/self._testMethodName
            self.root.mkdir(parents=True,exist_ok=False)
        else:
            self.temporary=tempfile.TemporaryDirectory(prefix='hatch-collision-test-')
            self.addCleanup(self.temporary.cleanup);self.root=Path(self.temporary.name)

    def run_world(self,world,payloads,name):
        import physics_backends as physics
        prepared=self.backend.prepare(world,payloads)
        try:
            result=self.backend.simulate(prepared)
            cache=self.root/name
            self.backend.bake(result,cache)
            self.assertEqual(physics.verify_cache(cache)['status'],'hash-and-contract-verified')
            from physics_backend_evidence import direct_report, EvidenceError
            report=physics.parse((cache/'worker-report.json').read_bytes())
            for observed in report['actual_bodies']:
                candidate=deepcopy(report)
                index=report['actual_bodies'].index(observed)
                candidate['actual_bodies'][index]['margin_semantics']='inherent sphere radius' if observed['margin_semantics']=='explicit proxy margin' else 'explicit proxy margin'
                with self.assertRaises(EvidenceError):direct_report(candidate,world)

            return cache,physics.parse((cache/'states.json').read_bytes()),physics.parse((cache/'contacts.json').read_bytes())
        finally:self.backend.cleanup(prepared)

    def base(self):
        from physics_backend_example import build_inputs
        world,payloads=build_inputs(backend='pybullet')
        world['collision_profile']='explicit-proxies-v1'
        return world,payloads

    def test_real_extended_shapes_land_and_cache_reopens(self):
        points,triangles=_box([1,1,1])
        shapes=[('capsule',dict(type='capsule',radius=.25,height=1.,axis='Y'),.75),
                ('cylinder',dict(type='cylinder',radius=.35,height=1.,axis='Y'),.5),
                ('convex',dict(type='convex-hull',source='ball-asset',vertices=[list(v) for v in points],triangles=[list(v) for v in triangles]),.5),
                ('compound',dict(type='compound',children=[dict(shape=dict(type='sphere',radius=.3),
                     transform=dict(translation=[x,0,0],rotation=[0,0,0,1])) for x in [-.5,.5]]),.3)]
        for name,shape,height in shapes:
            with self.subTest(name=name):
                world,payloads=self.base();world['bodies'][0]['colliders'][0]['shape']=shape
                _,states,contacts=self.run_world(world,payloads,name)
                observed=states['samples'][-1]['bodies'][0]['translation'][1]
                self.assertAlmostEqual(observed,height,delta=.06)
                self.assertTrue(any(frame['contacts'] for frame in contacts['samples']))

    def test_real_query_rejects_native_zero_margin_hull_gap(self):
        from physics_collision_query import raycast_cache
        world,payloads=self.base()
        points,triangles=_box([1,1,1])
        body=world['bodies'][0]
        body['colliders'][0]['shape']=dict(type='convex-hull',source='ball-asset',vertices=[list(v) for v in points],triangles=[list(v) for v in triangles])
        body['colliders'][0]['group']=2
        world['bodies'][1]['colliders'][0]['group']=1
        cache,_,_=self.run_world(world,payloads,'query-margin')
        from physics_backends import BackendError
        for mask in [2,0xffffffff]:
            with self.assertRaises(BackendError) as raised:
                raycast_cache(cache,0,[dict(start=[0,5,0],end=[0,1,0],mask=mask)],interpreter=os.environ['HATCH_PYBULLET_PYTHON'])
            self.assertEqual(raised.exception.code,'PHYSICS_QUERY_UNSUPPORTED')
        query=raycast_cache(cache,0,[dict(start=[0,5,0],end=[0,-1,0],mask=1)],interpreter=os.environ['HATCH_PYBULLET_PYTHON'])
        self.assertTrue(query['results'][0]['hit'])
        self.assertEqual(query['results'][0]['body'],'floor')

    def test_real_sphere_offsets_and_centers_preserve_native_margin(self):
        for offset,center in [(0,0),(.2,0),(0,.2),(.2,.2)]:
            with self.subTest(offset=offset,center=center):
                world,payloads=self.base()
                body=world['bodies'][0]
                body['colliders'][0]['transform']['translation']=[offset,0,0]
                body['mass']['center_of_mass']=[center,0,0]
                self.run_world(world,payloads,'offset-'+str(offset)+'-center-'+str(center))

    def test_real_masks_and_pair_exclusions_remove_response(self):
        heights={}
        for mode in ['colliding','mask','pair']:
            world,payloads=self.base()
            if mode=='mask':world['bodies'][0]['colliders'][0]['mask']=0
            if mode=='pair':world['collision_exclusions']=[['ball','floor']]
            _,states,contacts=self.run_world(world,payloads,mode)
            heights[mode]=states['samples'][-1]['bodies'][0]['translation'][1]
            if mode!='colliding':self.assertFalse(any(frame['contacts'] for frame in contacts['samples']))
        self.assertGreater(heights['colliding'],.45)
        self.assertLess(heights['mask'],-10)
        self.assertLess(heights['pair'],-10)

    def test_real_friction_and_restitution_change_dynamics(self):
        distances={};bounce_heights={}
        for coefficient in [0.,.9]:
            world,payloads=self.base()
            world['bodies'][0]['colliders'][0]['shape']=dict(type='box',size=[1,1,1])
            world['bodies'][0]['linear_velocity']=[4,0,0]
            # Preserve actual source translation; the box falls before sliding.
            world['materials'][0].update(static_friction=coefficient,dynamic_friction=coefficient)
            _,states,_=self.run_world(world,payloads,'friction-'+str(coefficient))
            distances[coefficient]=states['samples'][-1]['bodies'][0]['translation'][0]
            world,payloads=self.base();world['materials'][0]['restitution']=coefficient
            _,states,contacts=self.run_world(world,payloads,'bounce-'+str(coefficient))
            # Output contacts are sampled at24fps and may miss a brief impact.
            # The minimum followed by rebound is measured from native poses.
            first=min(range(1,min(25,len(states['samples']))),key=lambda i:states['samples'][i]['bodies'][0]['translation'][1])
            bounce_heights[coefficient]=max(frame['bodies'][0]['translation'][1] for frame in states['samples'][first:])
        self.assertGreater(distances[0.]-distances[.9],1.)
        self.assertGreater(bounce_heights[.9]-bounce_heights[0.],.5)

    def test_real_static_concave_gap_is_not_replaced_by_a_convex_hull(self):
        from physics_collision_query import raycast_cache
        world,payloads=self.base()
        world['bodies'][1]['colliders'][0]['shape']=dict(type='triangle-mesh',source='floor-asset',
            vertices=[[-5,.1,-5],[-.6,.1,-5],[-.6,.1,5],[-5,.1,5],[.6,.1,-5],[5,.1,-5],[5,.1,5],[.6,.1,5]],
            triangles=[[0,2,1],[0,3,2],[4,6,5],[4,7,6]])
        cache,states,contacts=self.run_world(world,payloads,'concave-gap')
        self.assertLess(states['samples'][-1]['bodies'][0]['translation'][1],-10)
        self.assertFalse(any(frame['contacts'] for frame in contacts['samples']))
        result=raycast_cache(cache,0,[dict(start=[x,2,2],end=[x,-1,2],mask=0xffffffff) for x in [0,2]],
                             interpreter=os.environ['HATCH_PYBULLET_PYTHON'])
        self.assertFalse(result['results'][0]['hit'])
        self.assertEqual(result['results'][1]['body'],'floor')

    def test_real_playground_contacts_queries_and_tunneling_record(self):
        import physics_backends as physics
        from physics_collision_example import build_collision_inputs
        from physics_collision_query import raycast_cache
        world,payloads=build_collision_inputs()
        cache,states,contacts=self.run_world(world,payloads,'playground')
        self.assertTrue(any({'left-ball','right-ball'}=={c['body_a'],c['body_b']} for f in contacts['samples'] for c in f['contacts']))
        self.assertFalse(any('excluded' in (c['body_a'],c['body_b']) for f in contacts['samples'] for c in f['contacts']))
        self.assertLess(next(b for b in states['samples'][-1]['bodies'] if b['id']=='excluded')['translation'][1],-1)
        distances=[c['distance'] for f in contacts['samples'] for c in f['contacts']]
        self.assertGreater(min(distances),-.2)
        query=raycast_cache(cache,0,[dict(start=[0,8,0],end=[0,-2,0],mask=1)],interpreter=os.environ['HATCH_PYBULLET_PYTHON'],preview=True)
        self.assertTrue(query['results'][0]['hit'])
        self.assertTrue(query.pop('preview_png').startswith(b'\x89PNG\r\n\x1a\n'))
        physics._write(self.root/'query.json',physics.canonical(query))


class CollisionQueryAdmissionTests(unittest.TestCase):
    def test_malformed_rays_fail_before_cache_or_solver_access(self):
        from unittest.mock import patch
        from physics_collision_query import raycast_cache
        import physics_backends as physics
        base=dict(start=[0,1,0],end=[0,0,0],mask=0xffffffff)
        cases=[[],[dict(base,mask=True)],[dict(base,start=[float('nan'),0,0])],
               [dict(base,end=base['start'])],[dict(base,unused=0)],[dict(base,mask=-1)]]
        for rays in cases:
            with self.subTest(rays=rays), patch.object(physics,'_read_cache') as read:
                with self.assertRaises(physics.BackendError):raycast_cache('/must-not-open',0,rays)
                read.assert_not_called()
