"""Actual numerical camera movement regressions; optional native 3D gate."""
import copy
import json
import math
from pathlib import Path
import sys
import unittest
import tempfile
import shutil
import os
import subprocess
from contextlib import nullcontext

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from camera3d_paths import CameraPath
from camera3d_motion import sample_motion
from camera3d_math import world_transform, project, projection_matrix
from camera3d_animation import smooth_samples, limit_motion

ROOT=Path(__file__).resolve().parents[1]


def path_doc(points, mode='linear', speed='arc-length', closed=False):
    metadata=json.loads((ROOT/'examples/camera3d/main-camera.json').read_text())['metadata']
    return dict(schema_version=1,kind='camera3d-path',id='motion-path',version='1.0.0',
        space='world',interpolation=mode,closed=closed,control_points_m=points,
        duration_frames=24,fps={'numerator':24,'denominator':1},orientation='authored',
        target=None,speed=speed,metadata=metadata)


class CameraMotionTests(unittest.TestCase):
    def setUp(self):
        self.base={'translation':[0,0,10],'rotation':[0,0,0,1],'scale':[1,1,1]}
        self.optics=json.loads((ROOT/'examples/camera3d/optics.json').read_text())
        self.render=json.loads((ROOT/'examples/camera3d/render.json').read_text())

    def sample(self,motion,parameters=None,frame=12,**kwargs):
        return sample_motion(self.base,self.optics,{'frame':frame},24,motion,parameters,**kwargs)

    def test_axis_movements_and_nonmutating_inputs(self):
        original=copy.deepcopy((self.base,self.optics))
        for kind,expected in [('dolly',[0,0,8]),('truck',[2,0,10]),('pedestal',[0,2,10])]:
            np.testing.assert_allclose(self.sample(kind,{'distance_m':4})['transform']['translation'],expected)
        self.assertEqual((self.base,self.optics),original)

    def test_pan_tilt_rotations_are_real_and_normalized(self):
        pan=self.sample('pan',{'angle_rad':math.pi},frame=24)['transform']
        tilt=self.sample('tilt',{'angle_rad':math.pi},frame=24)['transform']
        np.testing.assert_allclose(world_transform(pan)[:3,:3]@np.array([0,0,-1]),[0,0,1],atol=1e-12)
        np.testing.assert_allclose(world_transform(tilt)[:3,:3]@np.array([0,0,-1]),[0,0,1],atol=1e-12)
        self.assertEqual(pan['translation'],self.base['translation'])

    def test_dolly_zoom_keeps_apparent_subject_size(self):
        before=projection_matrix(self.optics,self.render)
        w=world_transform(self.base)
        a=project([1,0,0],w,before,self.render['width'],self.render['height'])['pixel']
        for frame in [0,6,12,18,24]:
            result=self.sample('dolly-zoom',{'distance_m':6},frame,target_position=[0,0,0])
            b=project([1,0,0],world_transform(result['transform']),projection_matrix(result['optics'],self.render),self.render['width'],self.render['height'])['pixel']
            np.testing.assert_allclose(a,b,atol=1e-10)

    def test_dolly_zoom_rejects_crossing(self):
        with self.assertRaises(ValueError):
            self.sample('dolly-zoom',{'distance_m':20},24,target_position=[0,0,0])

    def test_orbit_elliptical_and_dynamic_target(self):
        result=self.sample('orbit',{'angle_rad':math.pi,'radius_x_m':5,'radius_z_m':10},target_position=[2,0,0])
        point=result['transform']['translation']; theta=math.atan2(-2/5,10/10)+math.pi/2
        np.testing.assert_allclose(point,[2+5*math.sin(theta),0,10*math.cos(theta)],atol=1e-12)
        self.assertEqual(self.sample('follow',{'offset_m':[0,2,4]},target_position=[3,0,2])['transform']['translation'],[3,2,6])

    def test_elliptical_orbit_preserves_on_ellipse_start_and_endpoint(self):
        self.base['translation']=[3,0,8]
        parameters=dict(angle_rad=math.tau,radius_x_m=5,radius_z_m=10)
        for frame in (0,24):
            np.testing.assert_allclose(self.sample('orbit',parameters,frame,
                target_position=[0,0,0])['transform']['translation'],[3,0,8],atol=1e-12)

    def test_crane_arc_and_easing(self):
        result=self.sample('crane',{'delta_m':[4,0,0],'arc_height_m':2})
        np.testing.assert_allclose(result['transform']['translation'],[2,2,10])
        result=self.sample('dolly',{'distance_m':8,'easing':'ease-in'},frame=6)
        self.assertEqual(result['transform']['translation'],[0,0,9.5])

    def test_random_seek_shake_reproducible(self):
        params={'amplitude_m':.1,'angle_rad':.05,'frequency_hz':2.5,'seed':42}
        sequence={i:self.sample('shake',params,i) for i in range(25)}
        for i in [24,1,14,0,12,6,24]: self.assertEqual(self.sample('shake',params,i),sequence[i])
        self.assertEqual(sequence[0]['transform']['translation'],self.base['translation'])
        self.assertNotEqual(sequence[12],self.sample('shake',dict(params,seed=43),12))

    def test_clock_and_parameter_admission(self):
        for frame in [-1,25,True]:
            with self.assertRaises(ValueError): self.sample('static',frame=frame)
        for params in [{'distance_m':float('inf')},{'unexpected':1},{'distance_m':True}]:
            with self.assertRaises(ValueError):self.sample('dolly',params)
        with self.assertRaises(ValueError):self.sample('orbit',target_position=None)
        with self.assertRaises(ValueError):self.sample('shake',{'seed':True})
        with self.assertRaises(ValueError):self.sample('follow',target_position=[1e20,0,0])
        with self.assertRaises(ValueError):self.sample('crane',{'delta_m':[1e20,0,0]})
        self.assertTrue(self.sample('static',frame=24)['terminal'])
        self.assertFalse(self.sample('static',frame=23)['terminal'])

    def test_path_motion_and_hash_invalidation(self):
        path=CameraPath(path_doc([[0,0,10],[4,0,10]]))
        sample=self.sample('path',path=path)
        self.assertEqual(sample['transform']['translation'],[2,0,10])
        changed=CameraPath(path_doc([[0,0,10],[6,0,10]]))
        self.assertNotEqual(sample['dependency_sha256'],self.sample('path',path=changed)['dependency_sha256'])
        with self.assertRaises(ValueError):self.sample('path',path=path,fps={'numerator':25,'denominator':1})
        changed=CameraPath(dict(path_doc([[0,0,10],[6,0,10]]),duration_frames=48))
        with self.assertRaises(ValueError):self.sample('path',path=changed)

    def test_real_mesh_changes_perspective_under_dolly(self):
        from rig_import import load_character_model
        model=load_character_model(ROOT/'examples/rig-3d/demo-robotic-input.glb')
        vertices=np.concatenate([mesh.positions for mesh in model.geometry.meshes])
        self.assertGreater(len(vertices),100)
        selected=vertices[::max(1,len(vertices)//12)]
        projection=projection_matrix(self.optics,self.render)
        def pixels(frame):
            state=self.sample('dolly',{'distance_m':3},frame)
            return np.asarray([project(point,world_transform(state['transform']),projection,
                self.render['width'],self.render['height'])['pixel'] for point in selected])
        before,after=pixels(0),pixels(24)
        self.assertGreater(float(np.linalg.norm(after-before)),1)
        np.testing.assert_array_equal(pixels(24),after)

    def test_smoothing_is_baked_deterministic_and_dependency_bound(self):
        samples=[self.sample('truck',{'distance_m':24},i) for i in range(25)]
        samples[12]['transform']['translation'][0]+=10
        first=smooth_samples(samples,radius=2)
        second=smooth_samples(samples,radius=2)
        self.assertEqual(first,second)
        self.assertLess(first[12]['transform']['translation'][0],samples[12]['transform']['translation'][0])
        self.assertEqual(first[12],second[12])
        self.assertNotEqual(first[12]['dependency_sha256'],samples[12]['dependency_sha256'])
        changed=smooth_samples(samples,radius=3)
        self.assertNotEqual(first[12]['dependency_sha256'],changed[12]['dependency_sha256'])
        for value in first:
            self.assertAlmostEqual(sum(x*x for x in value['transform']['rotation']),1)
        self.assertEqual(samples[12]['transform']['translation'][0],22)

    def test_smoothing_bounds(self):
        for radius in [-1,True,33]:
            with self.assertRaises(ValueError):smooth_samples([self.sample('static')],radius=radius)
        with self.assertRaises(ValueError):smooth_samples([])

    def test_baked_motion_limits_are_measured_and_seek_independent(self):
        samples=[self.sample('truck',{'distance_m':12},i) for i in range(25)]
        settings=dict(max_speed_m_per_s=100,max_acceleration_m_per_s2=5,max_jerk_m_per_s3=10)
        result=limit_motion(samples,{'numerator':24,'denominator':1},**settings)
        other=limit_motion(samples,{'numerator':24,'denominator':1},**settings)
        self.assertEqual(result,other)
        for sample in result:
            limits=sample['motion_limits']
            self.assertLessEqual(limits['speed_m_per_s'],100+1e-8)
            self.assertLessEqual(limits['acceleration_m_per_s2'],5+1e-8)
            self.assertLessEqual(limits['jerk_m_per_s3'],10+1e-8)
        self.assertGreater(result[-1]['motion_limits']['goal_error_m'],0)


class CameraTrackTests(unittest.TestCase):
    def track(self, prop, values, kind='float'):
        return dict(id='animate-'+prop, target=dict(kind='camera', camera_id='camera', property=prop),
            value_type=kind, default_value=values[0], enabled=True,
            extrapolation_before='hold', extrapolation_after='hold',
            keys=[dict(time=dict(frame=0),value=values[0]),dict(time=dict(frame=24),value=values[1])])

    def registry(self, directory):
        import hashlib
        from animation3d_contracts import KINDS, TARGET_PROPERTIES
        from animation3d_registry import TargetRegistry
        native=json.loads((directory/'registry.json').read_text())
        source=dict(id=native['id'],version=native['version'],path='registry.json',
            kind=native['kind'],sha256=hashlib.sha256((directory/'registry.json').read_bytes()).hexdigest())
        entries=[dict(target=dict(kind='camera',camera_id='camera',property=prop),
            owner='camera3d',source=source) for prop in TARGET_PROPERTIES['camera']]
        return TargetRegistry(directory,dict(schema_version=1,kind=KINDS['registry'],
            id='camera-targets',version='1.0.0',entries=entries))

    def test_actual_camera_registry_sampling_and_stale_optics_rejection(self):
        from camera3d_animation import sample_camera_tracks
        from animation3d_registry import TargetRegistry
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'camera';shutil.copytree(ROOT/'examples/camera3d',directory)
            registry=self.registry(directory)
            camera=json.loads((directory/'main-camera.json').read_text())
            optics=json.loads((directory/'optics.json').read_text())
            tracks=[self.track('translation',[[0,0,10],[4,0,6]],'vec3'),
                    self.track('rotation',[[0,0,0,1],[0,1,0,0]],'quaternion'),
                    self.track('focal_length',[50,100]),self.track('focus_distance',[10,6]),
                    self.track('aperture',[4,8])]
            def at(frame):
                return sample_camera_tracks('camera',camera['transform'],optics,tracks,
                    dict(frame=frame),registry,duration_frames=24)
            last=at(24);middle=at(12);self.assertEqual(last,at(24))
            self.assertEqual(middle['transform']['translation'],[2,0,8])
            self.assertAlmostEqual(sum(v*v for v in middle['transform']['rotation']),1)
            self.assertEqual(middle['optics']['projection']['focal_length_mm'],75)
            self.assertEqual(middle['optics']['depth_of_field']['focus_distance_m'],8)
            self.assertTrue(last['terminal'])
            with self.assertRaises(ValueError):at(25)
            with self.assertRaises(ValueError):
                sample_camera_tracks('camera',camera['transform'],optics,tracks+[tracks[0]],
                    dict(frame=12),registry,duration_frames=24)
            (directory/'optics.json').write_bytes((directory/'optics.json').read_bytes()+b' ')
            with self.assertRaises(ValueError):at(12)

    def test_world_tracks_convert_to_local_under_nonidentity_parent(self):
        from camera3d_animation import sample_camera_tracks
        from rig_math import axis_angle
        directory=ROOT/'examples/camera3d';registry=self.registry(directory)
        optics=json.loads((directory/'optics.json').read_text())
        base=dict(translation=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1])
        parent=world_transform(dict(translation=[10,2,-3],
            rotation=axis_angle([0,1,0],math.pi/2).tolist(),scale=[1,1,1]))
        tracks=[self.track('translation',[[12,4,-4],[12,4,-4]],'vec3'),
                self.track('rotation',[[0,0,0,1],[0,0,0,1]],'quaternion')]
        for track in tracks:track['coordinate_space']='world'
        def at(values,**kwargs):
            return sample_camera_tracks('camera',base,optics,values,dict(frame=12),
                registry,duration_frames=24,**kwargs)
        state=at(tracks,parent_world=parent)
        actual=world_transform(state['transform'],parent)
        np.testing.assert_allclose(actual[:3,3],[12,4,-4],atol=1e-12)
        np.testing.assert_allclose(actual[:3,:3],np.eye(3),atol=1e-12)
        local=copy.deepcopy(tracks)
        for track in local:track['coordinate_space']='local'
        self.assertEqual(at(local)['transform']['translation'],[12,4,-4])
        with self.assertRaises(ValueError):at(tracks)
        local[0]['coordinate_space']='parent'
        with self.assertRaises(ValueError):at(local,parent_world=parent)

    def test_reusable_track_overrides_keep_source_and_seek_determinism(self):
        from camera3d_animation import sample_camera_track_overrides
        directory = ROOT/'examples/camera3d'
        registry = self.registry(directory)
        optics = json.loads((directory/'optics.json').read_text())
        base = json.loads((directory/'main-camera.json').read_text())['transform']
        shared = [self.track('translation', [[0,0,10],[4,0,6]], 'vec3'),
                  self.track('focal_length', [50,100])]
        overrides = [self.track('translation', [[2,0,10],[6,0,6]], 'vec3')]
        original = copy.deepcopy((base, optics, shared, overrides, registry.document))
        def at(frame, values=overrides):
            return sample_camera_track_overrides('camera', base, optics, shared,
                values, dict(frame=frame), registry, duration_frames=24)
        middle = at(12)
        self.assertEqual(middle['transform']['translation'], [4,0,8])
        self.assertEqual(middle['optics']['projection']['focal_length_mm'],75)
        self.assertEqual(middle['override_channels'], ['translation'])
        for frame in (24,0,12,3,12):
            self.assertEqual(at(frame), at(frame))
        self.assertEqual(at(12), middle)
        other = copy.deepcopy(overrides); other[0]['keys'][1]['value'][0] = 8
        self.assertNotEqual(at(12,other)['dependency_sha256'],middle['dependency_sha256'])
        # Even an overridden shared channel remains part of the immutable key.
        shared[0]['keys'][1]['value'][0] = 5
        self.assertNotEqual(at(12)['dependency_sha256'], middle['dependency_sha256'])
        shared[0]['keys'][1]['value'][0] = 4
        self.assertEqual((base,optics,shared,overrides,registry.document), original)
        middle['transform']['translation'][0] = 123
        self.assertEqual(at(12)['transform']['translation'],[4,0,8])

    def test_track_override_layers_reject_foreign_duplicate_and_invalid_keys(self):
        from camera3d_animation import sample_camera_track_overrides
        from rig_errors import RigError
        directory=ROOT/'examples/camera3d'; registry=self.registry(directory)
        optics=json.loads((directory/'optics.json').read_text())
        base=json.loads((directory/'main-camera.json').read_text())['transform']
        shared=[self.track('focal_length',[50,100])]
        override=self.track('focal_length',[60,110])
        def evaluate(values, sources=shared, frame=12):
            return sample_camera_track_overrides('camera',base,optics,sources,
                values,dict(frame=frame),registry,duration_frames=24)
        for values in ([override,override],{},[dict(override,target=dict(kind='camera',camera_id='missing',property='focal_length'))]):
            with self.assertRaises((ValueError,RigError)): evaluate(values)
        bad=copy.deepcopy(shared);bad[0]['keys'][1]['value']=-1
        with self.assertRaises((ValueError,RigError)): evaluate([override],bad)
        with self.assertRaises(ValueError):evaluate([override],frame=25)
        with self.assertRaises(ValueError):evaluate([override]*4096)
        # Disabled override uses its own authored default through the canonical sampler.
        disabled=dict(override,enabled=False,default_value=70)
        self.assertEqual(evaluate([disabled])['optics']['projection']['focal_length_mm'],70)

    def test_track_override_rejects_changed_native_source_even_when_replaced(self):
        from camera3d_animation import sample_camera_track_overrides
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'camera';shutil.copytree(ROOT/'examples/camera3d',directory)
            registry=self.registry(directory)
            optics=json.loads((directory/'optics.json').read_text())
            base=json.loads((directory/'main-camera.json').read_text())['transform']
            tracks=[self.track('focal_length',[50,100])]
            (directory/'optics.json').write_bytes((directory/'optics.json').read_bytes()+b' ')
            with self.assertRaises(ValueError):
                sample_camera_track_overrides('camera',base,optics,tracks,tracks,
                    dict(frame=12),registry,duration_frames=24)

    def test_competing_fov_and_unknown_camera_are_rejected(self):
        from camera3d_animation import sample_camera_tracks
        from animation3d_registry import TargetRegistry
        directory=ROOT/'examples/camera3d'
        registry=self.registry(directory)
        optics=json.loads((directory/'optics.json').read_text())
        base=json.loads((directory/'main-camera.json').read_text())['transform']
        with self.assertRaises(ValueError):registry.resolve(dict(kind='camera',camera_id='missing',property='translation'))
        with self.assertRaises(ValueError):
            sample_camera_tracks('camera',base,optics,[self.track('focal_length',[50,100]),
                self.track('field_of_view',[.5,1])],dict(frame=12),registry,duration_frames=24)


class CameraNativeMotionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('HATCH_CAMERA_MOTION_BLENDER'),
                         'set HATCH_CAMERA_MOTION_BLENDER for actual native motion gate')
    def test_actual_blender_random_seek(self):
        evidence=os.environ.get('HATCH_CAMERA_MOTION_EVIDENCE')
        with (nullcontext(evidence) if evidence else tempfile.TemporaryDirectory()) as temporary:
            result=subprocess.run([os.environ['HATCH_CAMERA_MOTION_BLENDER'], '--background','--disable-autoexec',
                '--threads','1','--python-exit-code','1','--python',
                str(ROOT/'scripts/verify_camera3d_motion.py'),'--','--output',temporary],
                capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout[-3000:]+result.stderr[-3000:])
            report=json.loads((Path(temporary)/'report.json').read_text())
            print(json.dumps(report,sort_keys=True))
            self.assertEqual(set(report['png_sha256']),{'0','12','24'})
            self.assertGreater(len(set(report['png_sha256'].values())),1)


class CameraPathTests(unittest.TestCase):
    def test_uneven_segments_have_constant_distance_speed(self):
        path=CameraPath(path_doc([[0,0,0],[1,0,0],[10,0,0]]))
        points=np.array([path.sample(i/10)['position_m'] for i in range(11)])
        np.testing.assert_allclose(points[:,0],np.arange(11),atol=1e-12)
        self.assertAlmostEqual(path.length_m,10)
        self.assertEqual(path.sample(0,reverse=True)['position_m'],[10,0,0])
        self.assertAlmostEqual(path.kinematics(.25)['speed_m_per_s'],10,places=8)
        self.assertLess(path.kinematics(.25)['acceleration_m_per_s2_magnitude'],1e-6)

    def test_bezier_curve_arc_length_reduces_speed_error(self):
        doc=path_doc([[0,0,0],[0,5,0],[5,-5,0],[5,0,0]],'bezier')
        path=CameraPath(doc,samples_per_segment=256)
        points=np.array([path.sample(i/100)['position_m'] for i in range(101)])
        lengths=np.linalg.norm(np.diff(points,axis=0),axis=1)
        self.assertLess(float(lengths.max()/lengths.min()),1.025)
        self.assertGreater(path.length_m,5)
        self.assertGreater(path.length_refinement_delta_m,0)
        self.assertEqual(path.sample(0)['position_m'],[0,0,0])
        self.assertEqual(path.sample(1)['position_m'],[5,0,0])

    def test_catmull_passes_control_points_and_closed_boundary(self):
        doc=path_doc([[0,0,0],[1,1,0],[2,0,0]],'catmull-rom',speed='parameter')
        path=CameraPath(doc)
        np.testing.assert_allclose(path.sample(.5)['position_m'],[1,1,0])
        closed=CameraPath(dict(doc,closed=True))
        np.testing.assert_allclose(closed.sample(0)['position_m'],closed.sample(1)['position_m'])
        np.testing.assert_allclose(closed.sample(0)['tangent'],closed.sample(1)['tangent'])

    def test_parent_space_conversion_and_no_double_conversion(self):
        doc=path_doc([[0,0,0],[1,0,0]])
        parent=np.eye(4);parent[1,3]=4
        with self.assertRaises(ValueError):CameraPath(doc).sample(.5,parent_world=parent)
        doc['space']='parent'; path=CameraPath(doc)
        with self.assertRaises(ValueError):path.sample(.5)
        np.testing.assert_allclose(path.sample(.5,parent_world=parent)['position_m'],[.5,4,0])
        parent[0,0]=2
        with self.assertRaises(ValueError):path.sample(.5,parent_world=parent)

    def test_bounds_and_degenerate_paths_rejected(self):
        for points in [[[0,0,0],[0,0,0]],[[0,0,0],[float('nan'),0,0]]]:
            with self.assertRaises(ValueError):CameraPath(path_doc(points))
        with self.assertRaises(ValueError):CameraPath(path_doc([[0,0,0],[1,0,0]]),samples_per_segment=10000)
        path=CameraPath(path_doc([[0,0,0],[1,0,0]]))
        for value in [-.1,1.1,float('nan'),True]:
            with self.assertRaises(ValueError):path.sample(value)

    def test_analytic_curvature_and_path_orientation(self):
        # Cubic representation of x=t, y=t^2; curvature at .5 is 1/sqrt(2).
        doc=path_doc([[0,0,0],[1/3,0,0],[2/3,1/3,0],[1,1,0]],
                     'bezier',speed='parameter')
        doc['orientation']='tangent'
        path=CameraPath(doc)
        value=path.sample(.5)
        self.assertAlmostEqual(value['curvature_per_m'],1/math.sqrt(2),places=10)
        base=dict(translation=[0,0,10],rotation=[0,0,0,1],scale=[1,1,1])
        optics=json.loads((ROOT/'examples/camera3d/optics.json').read_text())
        sample=sample_motion(base,optics,dict(frame=12),24,'path',path=path)
        forward=world_transform(sample['transform'])[:3,:3]@np.array([0,0,-1])
        np.testing.assert_allclose(forward,value['tangent'],atol=1e-12)
        reversed=sample_motion(base,optics,dict(frame=12),24,'path',dict(reverse=True),path=path)
        reverse_forward=world_transform(reversed['transform'])[:3,:3]@np.array([0,0,-1])
        np.testing.assert_allclose(reverse_forward,-np.array(value['tangent']),atol=1e-12)

    def test_path_document_is_detached(self):
        doc=path_doc([[0,0,0],[1,0,0],[10,0,0]])
        path=CameraPath(doc);doc['control_points_m'][1][0]=8
        returned=path.document;returned['speed']='parameter'
        self.assertEqual(path.sample(.5)['position_m'],[5,0,0])


if __name__=='__main__':unittest.main()
