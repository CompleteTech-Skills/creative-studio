"""Camera foundation admission, actual file resolution, numerical and legacy checks."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import camera3d_contracts as contracts


def pointer(kind, name=None):
    name=name or kind
    return dict(id=name,version='1.0.0',kind='camera3d-'+kind,path=name+'.json',sha256='0'*64)


def fixtures():
    trs=dict(translation=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1])
    metadata=dict(name='Camera engineering fixture',description='Contract/math fixture, no rendered approval',
        provenance=dict(origin='procedural-fixture',fixture=True,attribution='Original test fixture',license='CC0-1.0',inputs=[]))
    scene=dict(id='scene',version='1.0.0',kind='scene3d-scene',path='scene.json',sha256='a'*64)
    def base(kind,**kw):
        return dict(schema_version=1,kind='camera3d-'+kind,id=kind,version='1.0.0',**kw)
    optics=base('optics',projection=dict(type='perspective',focal_length_mm=50,sensor_width_mm=36,
        sensor_height_mm=24,sensor_fit='horizontal',shift_x=0,shift_y=0,near_clip_m=.1,far_clip_m=100),
        depth_of_field=dict(enabled=False,focus_distance_m=5,f_stop=2.8,aperture_blades=7,
                            aperture_rotation_rad=0,anamorphic_ratio=1))
    values={
      'transform':base('transform',local=deepcopy(trs),parent=None),
      'optics':optics,
      'camera':base('camera',transform=deepcopy(trs),parent=None,optics=pointer('optics'),constraints=[],animation=None,metadata=metadata),
      'target':base('target',scene=scene,node='actor',attachment=None,offset_m=[0,1,0],space='node'),
      'constraint':base('constraint',type='look-at',targets=[pointer('target')],priority=1,enabled=True,
          weight=1,up=[0,1,0],safe_rect=[.1,.1,.9,.9],distance_range_m=[1,10],collision_radius_m=.1,
          failure='reject',metadata=metadata),
      'path':base('path',space='world',interpolation='linear',closed=False,control_points_m=[[0,0,0],[0,0,-10]],
          duration_frames=24,fps=dict(numerator=24,denominator=1),orientation='authored',target=None,speed='parameter',metadata=metadata),
      'animation':base('animation',duration_frames=24,fps=dict(numerator=24,denominator=1),timeline=None,path=pointer('path'),
          terminal_pose=dict(time=dict(frame=24,subframe_ticks=0),translation=[0,0,-10],rotation=[0,0,0,1]),metadata=metadata),
      'rig':base('rig',nodes=[dict(id='root',parent=None,transform=deepcopy(trs),camera=pointer('camera'))],metadata=metadata),
      'preset':base('preset',camera=pointer('camera'),overrides={},metadata=metadata),
      'render':base('render',width=360,height=240,pixel_aspect=dict(numerator=1,denominator=1),overscan=1,
          gate_fit='crop',output='png',color_space='srgb',transparent=False,max_pixels=86400),
      'binding':base('binding',scene=scene,camera=pointer('camera'),rig=None,animation=None,overrides={},
          targets=[],constraints=[],render=pointer('render'),metadata=metadata),
      'transition':base('transition',type='cut',duration_frames=0,overlap='none',color_rgba=[0,0,0,1],direction='left',curve='linear'),
      'shot':base('shot',shot=dict(render_mode='3d',scene=scene,binding=pointer('binding'),duration_frames=24,
          fps=dict(numerator=24,denominator=1),transition=pointer('transition'),start_frame=0),metadata=metadata),
      'storyboard':base('storyboard',shots=[pointer('shot')],total_frames=24,fps=dict(numerator=24,denominator=1),source=None,metadata=metadata),
      'registry':base('registry',resources=[pointer('camera')],metadata=metadata),
    }
    identity=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    # Independent closed-form 50 mm /36x24 mm projection, near .1, far100.
    projection=[[100/36,0,0,0],[0,100/24,0,0],[0,0,-100.1/99.9,-20/99.9],[0,0,-1,0]]
    values['evaluated']=base('evaluated',camera=pointer('camera'),scene=scene,shot=pointer('shot'),
        time=dict(frame=0,subframe_ticks=0),fps=dict(numerator=24,denominator=1),terminal=False,duration_frames=24,
        world_matrix=identity,view_matrix=deepcopy(identity),projection_matrix=projection,
        projection=deepcopy(optics['projection']),depth_of_field=deepcopy(optics['depth_of_field']),render=pointer('render'),dependencies=[])
    return values


def save_native_closure(root, documents):
    """Real immutable JSON fixture files; source digests are computed after refs resolve."""
    documents={(d['kind'],d['id'],d['version']):deepcopy(d) for d in documents}
    externals={}
    for name in ('scene','scene-other'):
        payload=contracts.canonical_bytes(dict(fixture='external-owner-admission-separate',id=name))
        externals[('scene3d-scene',name,'1.0.0')]=payload
    refs={};active=set()
    def store(key):
        if key in refs:return refs[key]
        if key in active:raise AssertionError('test fixture creates an impossible circular byte-hash dependency')
        active.add(key)
        def replace(value):
            if isinstance(value,dict):
                if set(value)=={'id','version','path','sha256','kind'}:
                    return deepcopy(store((value['kind'],value['id'],value['version'])))
                return {k:replace(v) for k,v in value.items()}
            if isinstance(value,list):return [replace(v) for v in value]
            return value
        payload=externals[key] if key in externals else contracts.canonical_bytes(replace(documents[key]))
        path=key[1]+'.json';(root/path).write_bytes(payload)
        refs[key]=dict(kind=key[0],id=key[1],version=key[2],path=path,sha256=hashlib.sha256(payload).hexdigest())
        active.remove(key)
        return refs[key]
    for key in documents:store(key)
    registry=fixtures()['registry'];registry['resources']=list(refs.values())
    return registry


class CameraContractTests(unittest.TestCase):
    def setUp(self):
        self.values=fixtures()

    def test_every_generated_schema_validates_real_contract_examples_and_is_current(self):
        import jsonschema
        for name,schema in contracts.schemas().items():
            kind=name[len('camera3d-'):-len('.schema.json')]
            with self.subTest(kind=kind):
                jsonschema.Draft202012Validator.check_schema(schema)
                self.assertEqual(schema,json.loads((ROOT/'profiles'/name).read_text()))
                contracts.validate(self.values[kind],kind)
                bad=deepcopy(self.values[kind]);bad['unknown']=1
                with self.assertRaises(ValueError):contracts.validate(bad,kind)
                bad=deepcopy(self.values[kind]);bad['schema_version']=2
                with self.assertRaises(ValueError):contracts.validate(bad,kind)

    def test_schema_and_runtime_reject_identifier_hash_and_path_newline_aliases(self):
        import jsonschema
        schema=contracts.schemas()['camera3d-camera.schema.json']
        validator=jsonschema.Draft202012Validator(schema)
        for edit in (lambda d:d.update(id='camera\n'),lambda d:d.update(version='1.0.0\n'),
                     lambda d:d['optics'].update(id='optics\n'),lambda d:d['optics'].update(kind='camera3d-optics\n'),
                     lambda d:d['optics'].update(sha256='0'*64+'\n'),
                     lambda d:d['optics'].update(path='../escape'),lambda d:d['optics'].update(path='a//b'),
                     lambda d:d['optics'].update(path='CON.txt'),lambda d:d['optics'].update(path='a/COM¹.txt'),
                     lambda d:d['optics'].update(path='CONIN$'),lambda d:d['optics'].update(path=' leading.json')):
            value=deepcopy(self.values['camera']);edit(value)
            self.assertFalse(validator.is_valid(value),value)
            with self.assertRaises(ValueError):contracts.validate(value)
        native=deepcopy(self.values['animation'])
        native['path']=None
        native['timeline']=dict(id='Native.Animation:42',version='1.0.0',path='native.json',
            sha256='a'*64,kind='animation_project_3d')
        contracts.validate(native)

    def test_json_schema_integral_float_numbers_remain_admitted_without_tracebacks(self):
        edits=(('path',lambda d:d['fps'].update(numerator=24.0)),
               ('render',lambda d:d['pixel_aspect'].update(numerator=1.0)),
               ('evaluated',lambda d:d['time'].update(subframe_ticks=0.0)))
        with tempfile.TemporaryDirectory() as directory:
            for kind,edit in edits:
                value=deepcopy(self.values[kind]);edit(value)
                contracts.validate(value)
                path=Path(directory)/(kind+'.json');path.write_bytes(contracts.canonical_bytes(value))
                completed=subprocess.run([sys.executable,str(ROOT/'scripts/camera3d_contracts.py'),'--validate',str(path)],capture_output=True,text=True)
                self.assertEqual(completed.returncode,0,completed.stderr)
                self.assertTrue(json.loads(completed.stdout)['ok'])
                self.assertNotIn('Traceback',completed.stderr)

    def test_invalid_optics_quaternion_rational_reference_and_nonfinite_rejected(self):
        cases=[('camera',lambda d:d['transform'].update(rotation=[0,0,0,0])),
               ('camera',lambda d:d['transform'].update(scale=[2,1,1])),
               ('optics',lambda d:d['projection'].update(near_clip_m=100)),
               ('optics',lambda d:d['projection'].update(focal_length_mm=0)),
               ('optics',lambda d:d['projection'].update(focal_length_mm=float('nan'))),
               ('camera',lambda d:d['optics'].update(path='../escape.json')),
               ('camera',lambda d:d['optics'].update(path='CON.json')),
               ('camera',lambda d:d['optics'].update(path='a\\b')),
               ('camera',lambda d:d['optics'].update(sha256='not-a-hash')),
               ('animation',lambda d:d.update(fps=dict(numerator=48,denominator=2))),
               ('path',lambda d:d.update(duration_frames=True))]
        for kind,edit in cases:
            value=deepcopy(self.values[kind]);edit(value)
            with self.subTest(kind=kind,value=value),self.assertRaises(ValueError):contracts.validate(value)

    def test_semantic_graph_path_constraint_and_boundary_rejections(self):
        cases=[('target',lambda d:d.update(space='bone')),
               ('constraint',lambda d:d.update(targets=[])),
               ('constraint',lambda d:d.update(up=[0,0,0])),
               ('constraint',lambda d:d.update(safe_rect=[.9,0,.1,1])),
               ('constraint',lambda d:d.update(targets=d['targets']*2)),
               ('path',lambda d:d.update(interpolation='bezier')),
               ('path',lambda d:d.update(orientation='target')),
               ('rig',lambda d:d['nodes'][0].update(parent='root')),
               ('rig',lambda d:d['nodes'][0].update(parent='missing')),
               ('rig',lambda d:d['nodes'].append(deepcopy(d['nodes'][0]))),
               ('animation',lambda d:d['terminal_pose']['time'].update(frame=23)),
               ('animation',lambda d:d.update(path=None)),
               ('transition',lambda d:d.update(duration_frames=1)),
               ('render',lambda d:d.update(max_pixels=86399)),
               ('evaluated',lambda d:d['time'].update(frame=24)),
               ('evaluated',lambda d:d['view_matrix'][0].__setitem__(3,1))]
        for kind,edit in cases:
            value=deepcopy(self.values[kind]);edit(value)
            with self.subTest(kind=kind),self.assertRaises(ValueError):contracts.validate(value)
        value=deepcopy(self.values['evaluated']);value['time']['frame']=24;value['terminal']=True
        contracts.validate(value)
        value['time']['subframe_ticks']=1
        with self.assertRaises(ValueError):contracts.validate(value)

    def test_real_exact_byte_registry_closure_and_negative_payload_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            optics=self.values['optics'];payload=contracts.canonical_bytes(optics)
            (root/'optics.json').write_bytes(payload)
            optics_ref=pointer('optics');optics_ref['sha256']=hashlib.sha256(payload).hexdigest()
            camera=deepcopy(self.values['camera']);camera['optics']=optics_ref
            payload=contracts.canonical_bytes(camera);(root/'camera.json').write_bytes(payload)
            camera_ref=pointer('camera');camera_ref['sha256']=hashlib.sha256(payload).hexdigest()
            registry=deepcopy(self.values['registry']);registry['resources']=[optics_ref,camera_ref]
            found=contracts.load_registry(root,registry)
            self.assertEqual(found[('camera3d-camera','camera','1.0.0')],camera)
            self.assertEqual(contracts.canonical_bytes(camera),contracts.canonical_bytes(json.loads(payload)))
            (root/'camera.json').write_bytes(payload+b' ')
            with self.assertRaisesRegex(ValueError,'hash'):contracts.load_registry(root,registry)
            (root/'camera.json').write_bytes(payload)
            unresolved=deepcopy(registry);unresolved['resources']=[camera_ref]
            with self.assertRaisesRegex(ValueError,'unresolved'):contracts.load_registry(root,unresolved)
            duplicate=deepcopy(registry);duplicate['resources'].append(deepcopy(camera_ref))
            with self.assertRaisesRegex(ValueError,'duplicates'):contracts.load_registry(root,duplicate)
            (root/'camera.json').unlink()
            with self.assertRaisesRegex(ValueError,'missing'):contracts.load_registry(root,registry)
            (root/'camera-real.json').write_bytes(payload)
            try:(root/'camera.json').symlink_to(root/'camera-real.json')
            except (OSError,NotImplementedError):return  # native platform need not grant link privilege
            with self.assertRaisesRegex(ValueError,'symlink'):contracts.load_registry(root,registry)

    def test_resolved_storyboard_overlap_and_evaluated_dependency_semantics(self):
        def write_registry(root,values):
            refs={}
            scene_payload=b'{"fixture":"external owner admission tested separately"}\n'
            (root/'scene.json').write_bytes(scene_payload)
            scene=dict(id='scene',version='1.0.0',kind='scene3d-scene',path='scene.json',sha256=hashlib.sha256(scene_payload).hexdigest())
            refs[('scene3d-scene','scene')]=scene
            def replace(value):
                if isinstance(value,dict):
                    if set(value)=={'id','version','path','sha256','kind'}:
                        return deepcopy(refs[(value['kind'],value['id'])])
                    return {key:replace(child) for key,child in value.items()}
                if isinstance(value,list):return [replace(child) for child in value]
                return value
            order=('optics','render','camera','binding','transition','transition-next','shot','shot-next','storyboard','evaluated')
            for name in order:
                if name not in values:continue
                document=replace(values[name]);payload=contracts.canonical_bytes(document)
                (root/(name+'.json')).write_bytes(payload)
                refs[(document['kind'],document['id'])]=dict(kind=document['kind'],id=document['id'],version=document['version'],path=name+'.json',sha256=hashlib.sha256(payload).hexdigest())
            registry=deepcopy(values['registry']);registry['resources']=list(refs.values())
            return registry
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            registry=write_registry(root,deepcopy(self.values))
            self.assertEqual(len(contracts.load_registry(root,registry)),8)
            overlapping=deepcopy(self.values)
            overlapping['transition-next']=deepcopy(overlapping['transition'])
            overlapping['transition-next'].update(id='transition-next',type='dissolve',duration_frames=2,overlap='tail-head')
            overlapping['shot-next']=deepcopy(overlapping['shot'])
            overlapping['shot-next'].update(id='shot-next')
            overlapping['shot-next']['shot'].update(start_frame=22,transition=pointer('transition','transition-next'))
            overlapping['storyboard']['shots'].append(pointer('shot','shot-next'))
            overlapping['storyboard']['total_frames']=46
            registry=write_registry(root,overlapping)
            self.assertEqual(len(contracts.load_registry(root,registry)),10)
            overlapping['shot-next']['shot']['start_frame']=23
            registry=write_registry(root,overlapping)
            with self.assertRaisesRegex(ValueError,'overlap accounting'):contracts.load_registry(root,registry)

            for kind,edit,message in (
                ('storyboard',lambda d:d.update(total_frames=25),'total frame'),
                ('storyboard',lambda d:d['shots'].append(deepcopy(d['shots'][0])),'duplicates shot'),
                ('transition',lambda d:d.update(type='dissolve',duration_frames=2,overlap='tail-head'),'adjacent'),
                ('evaluated',lambda d:d['projection'].update(focal_length_mm=35),'optical state')):
                values=deepcopy(self.values);edit(values[kind]);registry=write_registry(root,values)
                with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,message):contracts.load_registry(root,registry)

    def test_malformed_kind_json_values_paths_and_cli_errors_are_structured(self):
        for value in ({'kind':123}, [], {'kind':'camera3d-camera','bad':object()}):
            with self.subTest(value=str(value)),self.assertRaises(ValueError):contracts.validate(value)
        for path in ('.','a//b','a/./b','a/../b','/a','NUL','x.','a:e','e\u0301.json','a/COM¹.txt','LPT²','a/CONOUT$',' leading.json'):
            with self.subTest(path=path),self.assertRaises(ValueError):contracts.portable_path(path)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad.json';path.write_text('{"kind":123}')
            completed=subprocess.run([sys.executable,str(ROOT/'scripts/camera3d_contracts.py'),'--validate',str(path)],capture_output=True,text=True)
            self.assertEqual(completed.returncode,2)
            self.assertFalse(json.loads(completed.stdout)['ok']);self.assertNotIn('Traceback',completed.stderr)

    def test_real_owned_reference_links_enforce_native_timeline_type_scene_and_timing(self):
        values=deepcopy(self.values)
        values['camera'].update(animation=pointer('animation'),constraints=[pointer('constraint')])
        values['binding']['rig']=pointer('rig')
        names=('optics','render','path','animation','target','constraint','camera','rig','binding','transition','shot')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.assertEqual(len(contracts.load_registry(root,save_native_closure(root,[values[n] for n in names]))),11)
            for kind,edit,message in (
                ('path',lambda d:d.update(duration_frames=25),'animation/path timing'),
                ('path',lambda d:d.update(fps=dict(numerator=30,denominator=1)),'animation/path timing'),
                ('shot',lambda d:d['shot'].update(duration_frames=25),'shot/animation timing'),
                ('camera',lambda d:d.update(parent=dict(scene=dict(id='scene-other',kind='scene3d-scene',version='1.0.0',path='scene-other.json',sha256='0'*64),node='camera-parent')),'parent belongs'),
                ('target',lambda d:d.update(scene=dict(d['scene'],id='scene-other')),'target belongs'),
                ('rig',lambda d:d['nodes'][0].update(camera=None),'exactly once'),
                ('rig',lambda d:d['nodes'].append(dict(deepcopy(d['nodes'][0]),id='second-mount',transform=dict(translation=[1,0,0],rotation=[0,0,0,1],scale=[1,1,1]))),'exactly once')):
                edited=deepcopy(values);edit(edited[kind])
                registry=save_native_closure(root,[edited[n] for n in names])
                with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,message):contracts.load_registry(root,registry)
            path_target=deepcopy(values)
            path_target['camera']['constraints']=[]
            path_target['path'].update(orientation='target',target=pointer('target'))
            path_target['target']['scene']=dict(path_target['target']['scene'],id='scene-other')
            registry=save_native_closure(root,[path_target[n] for n in names])
            with self.assertRaisesRegex(ValueError,'target belongs'):contracts.load_registry(root,registry)
            bad=deepcopy(values['animation']);bad.update(path=None,timeline=pointer('optics'))
            registry=save_native_closure(root,[values['optics'],bad])
            with self.assertRaisesRegex(ValueError,'schema'):contracts.load_registry(root,registry)
        for attachment in ('Bone.001','Hand:R','RightHand'):
            target=deepcopy(values['target']);target.update(space='bone',attachment=attachment)
            contracts.validate(target)
        target['attachment']='Hand:R\n'
        with self.assertRaises(ValueError):contracts.validate(target)

    def test_shot_local_clock_reference_at_global_cuts_and_overlaps(self):
        # Two 24-frame shots overlap by two frames: starts0/22, end24/46.
        # This is a contract reference; scene/animation owners map global playback.
        for start,global_frame,local,terminal in ((0,22,22,False),(22,22,0,False),
                                                 (0,24,24,True),(22,24,2,False),
                                                 (22,46,24,True)):
            sample=deepcopy(self.values['evaluated'])
            sample['time']=dict(frame=local,subframe_ticks=0);sample['terminal']=terminal
            contracts.validate(sample)
            self.assertEqual(Fraction(start)+Fraction(sample['time']['frame'])+
                Fraction(sample['time']['subframe_ticks'],1000000),Fraction(global_frame))
        sample=deepcopy(self.values['evaluated']);sample['time']=dict(frame=23,subframe_ticks=500000)
        contracts.validate(sample)
        self.assertEqual(Fraction(22)+Fraction(47,2),Fraction(91,2))

    def test_actual_legacy_wrapper_uses_unchanged_v1_validator_and_duration(self):
        from test_production_render import fixture_project
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);legacy=fixture_project(root/'legacy-project')['shots']['first']
            payload=contracts.canonical_bytes(legacy);(root/'legacy.json').write_bytes(payload)
            legacy_ref=dict(id=legacy['id'],version='1.0.0',kind='hatch-production-shot',path='legacy.json',sha256=hashlib.sha256(payload).hexdigest())
            transition=deepcopy(self.values['transition']);payload=contracts.canonical_bytes(transition)
            (root/'transition.json').write_bytes(payload)
            transition_ref=pointer('transition');transition_ref['sha256']=hashlib.sha256(payload).hexdigest()
            wrapper=deepcopy(self.values['shot'])
            wrapper['shot']=dict(render_mode='2d',legacy_shot=legacy_ref,duration_frames=legacy['duration_frames'],
                fps=dict(numerator=24,denominator=1),transition=transition_ref,start_frame=0)
            def registry_for(wrapper):
                payload=contracts.canonical_bytes(wrapper);(root/'shot.json').write_bytes(payload)
                wrapper_ref=pointer('shot');wrapper_ref['sha256']=hashlib.sha256(payload).hexdigest()
                registry=deepcopy(self.values['registry']);registry['resources']=[legacy_ref,transition_ref,wrapper_ref]
                return registry
            self.assertEqual(len(contracts.load_registry(root,registry_for(wrapper))),2)
            wrapper['shot']['duration_frames']+=1
            with self.assertRaisesRegex(ValueError,'duration'):contracts.load_registry(root,registry_for(wrapper))

    def test_regular_file_admission_rejects_actual_fifo_without_blocking(self):
        if not hasattr(os,'mkfifo'):
            return  # Windows lacks FIFOs; descriptor regular-file check is universal.
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'document.pipe';os.mkfifo(path)
            completed=subprocess.run([sys.executable,str(ROOT/'scripts/camera3d_contracts.py'),'--validate',str(path)],
                capture_output=True,text=True,timeout=5)
            self.assertEqual(completed.returncode,2)
            self.assertIn('regular file',json.loads(completed.stdout)['error'])
            self.assertNotIn('Traceback',completed.stderr)

    def test_duplicate_json_keys_and_unknown_versions_fail_on_actual_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'camera.json';path.write_text('{"schema_version":1,"schema_version":2}')
            with self.assertRaisesRegex(ValueError,'Duplicate'):contracts.load(path)
        self.assertEqual(contracts.canonical_bytes({'z':1,'a':2}),b'{"a":2,"z":1}\n')

    def test_legacy_shot_and_track_contracts_delegate_without_changes(self):
        from production_performance import evaluate_track
        from production_contracts import schemas
        track=dict(interpolation='linear',keys=[dict(frame=0,value=[10,20]),dict(frame=4,value=[18,12])])
        self.assertEqual(evaluate_track(track,2),[14,16])
        self.assertEqual(evaluate_track(track,4),[18,12])
        self.assertEqual(evaluate_track(dict(track,interpolation='step'),3),[10,20])
        import jsonschema
        legacy=self.values['shot'];legacy['shot']=dict(render_mode='2d',legacy_shot=dict(id='legacy',version='1.0.0',
            kind='hatch-production-shot',path='legacy.json',sha256='b'*64),duration_frames=24,
            fps=dict(numerator=24,denominator=1),transition=pointer('transition'),start_frame=0)
        contracts.validate(legacy)
        for kind in ('project','scene','shot'):
            self.assertEqual(json.loads((ROOT/'profiles'/('production-'+kind+'.schema.json')).read_text()),schemas()[kind])


class CameraMathTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import camera3d_math as camera
        self.np,self.camera=np,camera
        self.values=fixtures()
        self.world=np.eye(4)
        self.projection=camera.projection_matrix(self.values['optics'],self.values['render'])

    def test_inherited_vector_quaternion_and_coordinate_reference_operations(self):
        camera,np=self.camera,self.np
        np.testing.assert_allclose(camera.add([1,2,3],[4,5,6]),[5,7,9])
        np.testing.assert_allclose(camera.subtract([1,2,3],[4,5,6]),[-3,-3,-3])
        self.assertEqual(camera.dot([1,2,3],[4,5,6]),32)
        np.testing.assert_allclose(camera.cross([1,0,0],[0,1,0]),[0,0,1])
        self.assertEqual(camera.distance([0,0,0],[3,4,0]),5)
        self.assertAlmostEqual(camera.angle([1,0,0],[0,1,0]),math.pi/2)
        np.testing.assert_allclose(camera.normalize([0,0,-2]),[0,0,-1])
        q=camera.euler_xyz([math.pi/2,0,math.pi/2])
        # Rx(90) sends +Y to +Z; Rz(90) leaves +Z unchanged.
        np.testing.assert_allclose(camera.quaternion_matrix(q)@[0,1,0],[0,0,1],atol=1e-12)
        identity=camera.quaternion_multiply(q,camera.quaternion_inverse(q))
        np.testing.assert_allclose(camera.quaternion_matrix(identity),np.eye(3),atol=1e-12)
        midpoint=camera.quaternion_slerp([0,0,0,1],camera.euler_xyz([0,math.pi,0]),.5)
        np.testing.assert_allclose(camera.quaternion_matrix(midpoint)@[0,0,-1],[-1,0,0],atol=1e-12)
        with self.assertRaises(ValueError):camera.quaternion_slerp(q,q,True)
        world=camera.world_transform(dict(translation=[1,2,3],rotation=[0,0,0,1],scale=[1,1,1]))
        np.testing.assert_allclose(camera.world_to_camera([1,2,1],world),[0,0,-2])
        np.testing.assert_allclose(camera.camera_to_world([0,0,-2],world),[1,2,1])
        np.testing.assert_allclose(camera.target_offset([1,2,3],[0,1,0]),[1,3,3])

    def test_actual_ray_plane_and_frustum_intersection_reference(self):
        camera,np=self.camera,self.np
        np.testing.assert_allclose(camera.ray_plane_intersection([0,2,0],[0,-1,0],[0,0,0],[0,1,0]),[0,0,0])
        self.assertIsNone(camera.ray_plane_intersection([0,2,0],[1,0,0],[0,0,0],[0,1,0]))
        self.assertIsNone(camera.ray_plane_intersection([0,2,0],[0,1,0],[0,0,0],[0,1,0]))
        corners=camera.frustum_corners(self.world,self.projection)
        np.testing.assert_allclose(corners[0],[-.036,.024,-.1],atol=1e-12)
        np.testing.assert_allclose(corners[6],[36,-24,-100],atol=1e-8)
        for point in ([0,0,-1],[-.036,.024,-.1],[36,-24,-100]):
            self.assertTrue(camera.frustum_contains(point,self.world,self.projection))
        for point in ([0,0,-.01],[0,0,-101],[100,0,-1],[0,0,1]):
            self.assertFalse(camera.frustum_contains(point,self.world,self.projection))
        np.testing.assert_allclose(camera.project_bounds([-.1,-.1,-2],[.1,.1,-1],self.world,self.projection,360,240),[130,70,230,170],atol=1e-8)
        np.testing.assert_allclose(camera.project_bounds([-.1,-.1,-.2],[.1,.1,0],self.world,self.projection,360,240),[0,0,360,240],atol=1e-8)
        self.assertIsNone(camera.project_bounds([100,100,-2],[101,101,-1],self.world,self.projection,360,240))
        with self.assertRaises(ValueError):camera.project_bounds([1,0,0],[0,1,1],self.world,self.projection,360,240)

    def test_integral_json_float_raster_sizes_match_schema_acceptance(self):
        render=deepcopy(self.values['render']);render.update(width=360.0,height=240.0)
        render['pixel_aspect'].update(numerator=1.0)
        projection=self.camera.projection_matrix(self.values['optics'],render)
        point=self.camera.project([0,0,-1],self.world,projection,render['width'],render['height'])
        self.assertEqual(point['pixel'],[180,120])
        self.np.testing.assert_allclose(self.camera.unproject(point['pixel'],point['ndc'][2],self.world,projection,360.0,240.0),[0,0,-1])
        for width in (True,360.5,float('inf'),0,16385):
            with self.subTest(width=width),self.assertRaises(ValueError):self.camera.project([0,0,-1],self.world,projection,width,240)

    def test_perspective_independent_lens_field_of_view_and_clipping_reference(self):
        camera,np=self.camera,self.np
        self.assertAlmostEqual(math.degrees(2*math.atan(36/100)),39.597752709049864,places=10)
        np.testing.assert_allclose(self.projection,self.values['evaluated']['projection_matrix'],atol=1e-12)
        for distance,depth in ((.1,-1),(100,1)):
            got=camera.project([0,0,-distance],self.world,self.projection,360,240)
            np.testing.assert_allclose(got['pixel'],[180,120],atol=1e-10)
            self.assertAlmostEqual(got['ndc'][2],depth,places=12)
        edge=camera.project([.36,.24,-1],self.world,self.projection,360,240)
        np.testing.assert_allclose(edge['pixel'],[360,0],atol=1e-10)
        far=camera.project([.36,.24,-2],self.world,self.projection,360,240)
        np.testing.assert_allclose(far['pixel'],[270,60],atol=1e-10)
        self.assertFalse(camera.project([0,0,1],self.world,self.projection,360,240)['inside'])
        with self.assertRaisesRegex(ValueError,'division'):camera.project([1,0,0],self.world,self.projection,360,240)

    def test_orthographic_depth_independence_shift_and_pixel_aspect(self):
        optics=deepcopy(self.values['optics']);optics['projection']=dict(type='orthographic',orthographic_height_m=4,
            shift_x=.1,shift_y=-.1,near_clip_m=.1,far_clip_m=100)
        p=self.camera.projection_matrix(optics,self.values['render'])
        for z in (-1,-50):
            got=self.camera.project([1,1,z],self.world,p,360,240)
            self.np.testing.assert_allclose(got['pixel'],[276,84],atol=1e-10)
        render=deepcopy(self.values['render']);render['pixel_aspect']=dict(numerator=2,denominator=1)
        p=self.camera.projection_matrix(optics,render)
        got=self.camera.project([1,1,-1],self.world,p,360,240)
        self.np.testing.assert_allclose(got['pixel'],[246,84],atol=1e-10)

    def test_roundtrip_unprojection_and_actual_center_rays(self):
        camera,np=self.camera,self.np
        for point in ([.1,.2,-.5],[-1,2,-20],[3,-4,-90]):
            result=camera.project(point,self.world,self.projection,360,240)
            np.testing.assert_allclose(camera.unproject(result['pixel'],result['ndc'][2],self.world,self.projection,360,240),point,rtol=1e-11,atol=1e-11)
        near,direction=camera.screen_ray([180,120],self.world,self.projection,360,240)
        np.testing.assert_allclose(near,[0,0,-.1],atol=1e-12)
        np.testing.assert_allclose(direction,[0,0,-1],atol=1e-12)

    def test_parent_order_look_at_and_blender_camera_basis_are_distinct_from_object_basis(self):
        camera,np=self.camera,self.np
        import rig_math
        parent=rig_math.compose([1,2,3],rig_math.axis_angle([0,1,0],math.pi/2))
        local=dict(translation=[0,0,-2],rotation=[0,0,0,1],scale=[1,1,1])
        world=camera.world_transform(local,parent)
        np.testing.assert_allclose(world[:3,3],[-1,2,3],atol=1e-12)
        q=camera.look_at([1,0,0],[0,0,0]);rotation=rig_math.qmatrix(q)
        np.testing.assert_allclose(rotation@[0,0,-1],[-1,0,0],atol=1e-12)
        blender=camera.blender_camera_transform(np.eye(4))
        np.testing.assert_allclose(blender@[0,0,-1,0],[0,1,0,0],atol=1e-12)
        np.testing.assert_allclose(camera.canonical_camera_transform(blender),np.eye(4),atol=1e-12)
        converted=camera.basis_transform(world)
        np.testing.assert_allclose(camera.basis_transform(converted,'canonical-from-blender'),world,atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(converted[:3,:3]),1)
        np.testing.assert_allclose(camera.basis_transform(world,'gltf'),world)
        with self.assertRaises(ValueError):camera.look_at([0,0,0],[0,1,0])
        with self.assertRaises(ValueError):camera.world_transform(local,np.diag([2,1,1,1]))

    def test_sensor_fit_gate_shift_overscan_and_singular_projection_rejection(self):
        optics=deepcopy(self.values['optics']);render=deepcopy(self.values['render'])
        render.update(width=480,height=240,max_pixels=115200)
        p=self.camera.projection_matrix(optics,render)
        self.assertAlmostEqual(p[0,0],100/36);self.assertAlmostEqual(p[1,1],200/36)
        optics['projection']['sensor_fit']='vertical'
        p=self.camera.projection_matrix(optics,render)
        self.assertAlmostEqual(p[0,0],100/48);self.assertAlmostEqual(p[1,1],100/24)
        optics['projection'].update(sensor_fit='auto',shift_x=.1,shift_y=-.1)
        render['gate_fit']='expand'
        p=self.camera.projection_matrix(optics,render)
        self.assertAlmostEqual(p[0,0],100/48)
        center=self.camera.project([0,0,-1],self.world,p,480,240)
        self.np.testing.assert_allclose(center['pixel'],[288,144],atol=1e-10)
        render.update(overscan=2,max_pixels=460800)
        expanded=self.camera.projection_matrix(optics,render)
        self.assertAlmostEqual(expanded[0,0],p[0,0]/2)
        with self.assertRaises(ValueError):self.camera.unproject([1,2],0,self.world,self.np.zeros((4,4)),480,240)


if __name__ == '__main__':
    unittest.main()
