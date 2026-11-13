"""Exact editorial, physical-color compositing, catalog and camera-plan checks."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from PIL import Image
import numpy as np
from camera3d_contracts import canonical_bytes, validate
from camera3d_sequence import CameraSequence, composite_transition, transition_progress, write_transition_preview, read_rendered_png
from camera3d_catalog import update_catalog, revise_camera, resolve_camera, export_document
from camera3d_presets import CAMERA_PRESETS, MOVEMENT_PRESETS, SHOT_PRESETS, camera_preset, movement_preset, shot_preset, plan_storyboard_panel, native_camera_preset, import_preset, duplicate_preset
from camera3d_continuity import boundary_continuity
from test_rig_camera3d_contracts import fixtures, pointer, save_native_closure
from camera3d_math import world_transform, project_bounds, projection_matrix


def sequence_fixture(overlap=2, middle_duration=4):
    source = fixtures()
    transition = deepcopy(source['transition']); transition.update(id='dissolve', type='dissolve', duration_frames=overlap, overlap='tail-head')
    shots = []
    for identifier, start, duration, entry in [('one', 0, 4, pointer('transition')),
                                               ('two', 4-overlap, middle_duration, pointer('transition','dissolve'))]:
        shot = deepcopy(source['shot']); shot['id'] = identifier
        shot['shot'].update(start_frame=start,duration_frames=duration,transition=entry)
        shots.append(shot)
    board = deepcopy(source['storyboard']); board['shots']=[pointer('shot', 'one'), pointer('shot', 'two')]
    board['total_frames'] = 4-overlap+middle_duration
    documents = {(v['kind'],v['id'],v['version']):v for v in [source['transition'],transition,*shots]}
    return board, documents


class Editorial(unittest.TestCase):
    def test_overlap_maps_real_local_frames_and_no_terminal_output(self):
        board, documents = sequence_fixture()
        sequence = CameraSequence(board, documents)
        self.assertEqual(sequence.total_frames,6)
        self.assertEqual([(s.shot['id'],s.local_frame) for s in sequence.sample(1)['sources']], [('one',1)])
        self.assertEqual([(s.shot['id'],s.local_frame) for s in sequence.sample(2)['sources']], [('one',2),('two',0)])
        self.assertEqual([(s.shot['id'],s.local_frame) for s in sequence.sample(3)['sources']], [('one',3),('two',1)])
        self.assertEqual([(s.shot['id'],s.local_frame) for s in sequence.sample(4)['sources']], [('two',2)])
        for frame in [True,-1,6,1.5]:
            with self.subTest(frame=frame),self.assertRaises(ValueError):sequence.sample(frame)
        before = [sequence.sample(frame) for frame in range(6)]
        after = {frame:sequence.sample(frame) for frame in [5,2,0,3,1,4]}
        self.assertEqual(before,[after[frame] for frame in range(6)])

    def test_cut_never_interpolates_camera_or_duplicates_frame(self):
        board, documents=sequence_fixture(overlap=0)
        transition = documents[('camera3d-transition','dissolve','1.0.0')]
        transition.update(type='cut',overlap='none')
        sequence=CameraSequence(board,documents)
        observed=[]
        def provide(reference, frame):
            observed.append((reference['id'],frame))
            return Image.new('RGBA',(1,1),(frame,0,0,255))
        for frame in range(8):sequence.composite(frame,provide)
        self.assertEqual(observed,[(name,frame) for name in ['one','two'] for frame in range(4)])

    def test_disabled_camera_is_not_silently_replaced(self):
        board, documents=sequence_fixture();values=fixtures()
        documents[('camera3d-binding','binding','1.0.0')]=values['binding']
        catalog=values['catalog'];catalog.update(enabled=[],active=None)
        with self.assertRaisesRegex(ValueError,'disabled'):
            CameraSequence(board,documents,catalog=catalog)

    def test_ambiguous_triple_overlap_and_bad_timelines_fail(self):
        board, documents=sequence_fixture(overlap=3)
        third=deepcopy(documents[('camera3d-shot','two','1.0.0')]);third['id']='three'
        third['shot']['start_frame']=2
        documents[('camera3d-shot','three','1.0.0')]=third
        board['shots'].append(pointer('shot','three'));board['total_frames']=6
        with self.assertRaisesRegex(ValueError,'three-shot'):CameraSequence(board,documents)
        board,documents=sequence_fixture();board['total_frames']+=1
        with self.assertRaisesRegex(ValueError,'total'):CameraSequence(board,documents)

    def test_registry_loader_preserves_hash_bound_timing(self):
        values=fixtures();board,documents=sequence_fixture()
        all_documents=[values[k] for k in ('optics','camera','render','binding')]+list(documents.values())+[board]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);registry=save_native_closure(root,all_documents)
            reference=next(p for p in registry['resources'] if p['kind']=='camera3d-storyboard')
            sequence=CameraSequence.load(root,registry,reference)
            self.assertEqual(sequence.total_frames,6)
            (root/'one.json').write_text('{}')
            with self.assertRaises(ValueError):CameraSequence.load(root,registry,reference)


class Pixels(unittest.TestCase):
    def setUp(self):
        self.transition=deepcopy(fixtures()['transition']);self.transition.update(type='dissolve',duration_frames=1,overlap='tail-head')

    def test_dissolve_blends_linear_light_and_premultiplied_alpha(self):
        black=Image.new('RGBA',(2,2),(0,0,0,255));white=Image.new('RGBA',(2,2),(255,255,255,255))
        result=composite_transition(black,white,self.transition,0)
        self.assertEqual(result.getpixel((0,0)),(188,188,188,255))
        hidden=Image.new('RGBA',(2,2),(255,0,0,0))
        result=composite_transition(hidden,white,self.transition,0)
        self.assertEqual(result.getpixel((0,0)),(255,255,255,128))

    def test_fade_midpoint_is_configured_solid_with_real_alpha(self):
        self.transition.update(type='fade',color_rgba=[0,1,0,.5])
        result=composite_transition(Image.new('RGBA',(2,2),(255,0,0,255)),Image.new('RGBA',(2,2),(0,0,255,255)),self.transition,0)
        self.assertEqual(result.getpixel((0,0)),(0,255,0,128))

    def test_wipe_entering_edges_and_center_sampling(self):
        old=Image.new('RGBA',(4,4),(255,0,0,255));new=Image.new('RGBA',(4,4),(0,0,255,255))
        for direction,blue_point,red_point in [('left',(0,0),(3,0)),('right',(3,0),(0,0)),('up',(0,0),(0,3)),('down',(0,3),(0,0))]:
            self.transition.update(type='wipe',direction=direction)
            result=composite_transition(old,new,self.transition,0)
            self.assertEqual(result.getpixel(blue_point),(0,0,255,255))
            self.assertEqual(result.getpixel(red_point),(255,0,0,255))

    def test_exact_progress_and_rejection(self):
        self.transition['duration_frames']=4
        self.assertEqual(transition_progress(self.transition,0),Fraction(1,8))
        self.assertEqual(transition_progress(self.transition,3),Fraction(7,8))
        for frame in [True,4,-1,.5]:
            with self.assertRaises(ValueError):transition_progress(self.transition,frame)
        with self.assertRaises(ValueError):composite_transition(Image.new('RGB',(2,2)),Image.new('RGBA',(2,2)),self.transition,0)
        with self.assertRaises(ValueError):composite_transition(Image.new('RGBA',(2,2)),Image.new('RGBA',(2,2)),self.transition,0,max_pixels=3)

    def test_actual_retained_blender_pngs_publish_and_preserve_destinations(self):
        left=ROOT/'examples/rig-3d/humanoid/preview/blender/frame-0000.png'
        right=ROOT/'examples/rig-3d/quadruped/preview/blender/frame-0000.png'
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'preview'
            report=write_transition_preview(left,right,target,duration_frames=3)
            self.assertEqual(len(report['frames']),9)
            for frame in report['frames']:
                file=target/frame['path']
                self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(),frame['sha256'])
                with Image.open(file) as image:image.load();self.assertEqual(image.size,(256,256))
            self.assertEqual(report['source_sha256'],[hashlib.sha256(p.read_bytes()).hexdigest() for p in (left,right)])
            before=(target/'manifest.json').read_bytes()
            with self.assertRaises(ValueError):write_transition_preview(left,right,target,duration_frames=3)
            self.assertEqual(before,(target/'manifest.json').read_bytes())

    def test_corrupt_non_png_and_symlink_sources_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input.png';source.write_bytes(b'not a PNG')
            with self.assertRaises(ValueError):read_rendered_png(source)
            Image.new('RGBA',(2,2)).save(source)
            source.write_bytes(source.read_bytes()[:-12])
            with self.assertRaises(ValueError):read_rendered_png(source)
            link=root/'link.png'
            try:link.symlink_to(source)
            except (OSError,NotImplementedError):return
            with self.assertRaises(ValueError):read_rendered_png(link)


class ActualCameraPreviews(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('HATCH_CAMERA3D_PREVIEW_TEST') == '1',
                         'explicit actual Blender preset and storyboard preview gate')
    def test_actual_authored_cameras_render_their_projected_subject_bounds(self):
        from rig_blender import find_blender
        with tempfile.TemporaryDirectory() as temp:
            destination=Path(os.environ.get('HATCH_CAMERA_PREVIEW_EVIDENCE', str(Path(temp)/'evidence')))
            env=dict(os.environ)
            env['HATCH_CAMERA_TEST_SITE']=os.environ.get('HATCH_CAMERA_TEST_SITE') or str(Path(__import__('jsonschema').__file__).parent.parent)
            command=[find_blender(),'--background','--factory-startup','--disable-autoexec',
                     '--python-exit-code','1','--python',str(ROOT/'scripts/verify_camera3d_previews.py'),
                     '--','--destination',str(destination)]
            result=subprocess.run(command,capture_output=True,text=True,timeout=240,env=env)
            self.assertEqual(result.returncode,0,(result.stdout+result.stderr)[-12000:])
            report=json.loads((destination/'acceptance.json').read_text())
            self.assertTrue(report['ok'])
            self.assertEqual(len(report['previews']),26)
            for preview in report['previews']:
                path=destination/preview['path']
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),preview['sha256'])
                with Image.open(path) as image:
                    image.load();self.assertEqual(image.size,(192,128))
                self.assertGreater(preview['alpha_pixels'],3)
                self.assertLessEqual(max(abs(a-b) for a,b in zip(preview['actual_bounds'],preview['predicted_bounds'])),3)


class CatalogAndPresets(unittest.TestCase):
    def test_catalog_versioning_activation_and_safe_removal(self):
        catalog=fixtures()['catalog'];before=deepcopy(catalog);camera=pointer('camera')
        inactive=update_catalog(catalog,'1.0.1',enabled=[],active=None)
        self.assertEqual(catalog,before);self.assertIsNone(inactive['active'])
        with self.assertRaises(ValueError):update_catalog(catalog,'1.0.1',enabled=[])
        with self.assertRaises(ValueError):update_catalog(catalog,'1.0.1',remove=camera,referenced=[camera])
        with self.assertRaises(ValueError):update_catalog(catalog,'1.0.1',remove=camera)
        removed=update_catalog(catalog,'1.0.1',remove=camera,referenced=[])
        self.assertEqual(removed['cameras'],[])
        for version in ['1.0.0','0.9.9','1.0',True]:
            with self.assertRaises(ValueError):update_catalog(catalog,version)

    def test_camera_edits_and_overrides_are_independent(self):
        values=fixtures();camera=values['camera'];before=deepcopy(camera)
        duplicate=revise_camera(camera,'1.0.0',new_id='other',name='Other')
        duplicate['transform']['translation'][0]=8
        self.assertEqual(camera,before)
        renamed=revise_camera(camera,'1.0.1',name='Renamed')
        self.assertEqual(renamed['metadata']['name'],'Renamed')
        replacement=deepcopy(values['optics']);replacement['id']='wide';replacement['projection']['focal_length_mm']=24
        result=resolve_camera(camera,values['optics'],overrides={'optics':pointer('optics','wide')},override_optics=replacement)
        self.assertEqual(result['optics']['projection']['focal_length_mm'],24)
        self.assertEqual(camera,before)

    def test_immutable_export_roundtrip_and_all_presets_are_concrete(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'camera.json';document=fixtures()['camera']
            digest=export_document(document,target)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),digest)
            self.assertEqual(json.loads(target.read_text()),document)
            with self.assertRaises(ValueError):export_document(document,target)
            bundle=native_camera_preset('wide-24mm',pointer('camera'))
            preset_path=Path(directory)/'wide.json';export_document(bundle['preset'],preset_path)
            self.assertEqual(import_preset(preset_path),bundle['preset'])
            copy=duplicate_preset(bundle['preset'],'custom-wide')
            self.assertEqual(copy['camera'],bundle['preset']['camera'])
            self.assertEqual(bundle['preset']['overrides']['optics']['sha256'],
                             hashlib.sha256(canonical_bytes(bundle['optics'])).hexdigest())
        for name in CAMERA_PRESETS:
            result=camera_preset(name);validate(result['optics'],'optics')
            self.assertEqual(len(result['transform']['rotation']),4)
        for name in MOVEMENT_PRESETS:self.assertIn('motion',movement_preset(name))
        for name in SHOT_PRESETS:self.assertGreater(shot_preset(name)['subject_frame_fraction'],0)
        with self.assertRaises(ValueError):movement_preset('handheld',overrides={'amplitude_m':-1})
        with self.assertRaises(ValueError):movement_preset('slow-dolly-in',overrides={'unrecognized':1})

    def test_storyboard_plan_projects_real_supplied_bounds_and_discloses_uncertainty(self):
        panel=dict(id='establish',subject_ids=['actor'],shot_preset='establishing-shot',duration_frames=24,notes='show the room')
        bounds={'actor':dict(minimum=[-1,0,-1],maximum=[1,2,1])}
        result=plan_storyboard_panel(panel,subject_bounds=bounds)
        self.assertTrue(result['requires_render_review']);self.assertGreater(len(result['uncertainty']),1)
        self.assertEqual(panel,result['source_panel'])
        render=fixtures()['render'];render.update(width=640,height=360,max_pixels=230400)
        rectangle=project_bounds(bounds['actor']['minimum'],bounds['actor']['maximum'],world_transform(result['transform']),projection_matrix(result['optics'],render),640,360)
        self.assertIsNotNone(rectangle)
        self.assertTrue(0<=rectangle[0]<rectangle[2]<=640)
        self.assertTrue(0<=rectangle[1]<rectangle[3]<=360)
        with self.assertRaises(ValueError):plan_storyboard_panel(panel,subject_bounds={})

    def test_continuity_preserves_diagnostics_for_camera_plane_subjects(self):
        values=fixtures()
        state=dict(world_matrix=np.eye(4).tolist(),optics=values['optics'],render=values['render'])
        bounds={'camera-plane':dict(minimum=[-1,-1,-1],maximum=[1,1,1]),
                'visible':dict(minimum=[-1,-1,-6],maximum=[1,1,-4])}
        moved=deepcopy(bounds);moved['visible']['maximum'][0]=2
        result=boundary_continuity(state,state,bounds,moved,
            props_before={'key':'alice'},props_after={'key':'bob'})
        codes={warning['code'] for warning in result['warnings']}
        self.assertIn('subject-center-unprojectable',codes)
        self.assertIn('subject-position',codes)
        self.assertIn('prop-state-change',codes)

    def test_continuity_uses_actual_camera_projections_and_world_state(self):
        values=fixtures();transform=deepcopy(values['camera']['transform']);transform['translation']=[0,1,5]
        before=dict(world_matrix=world_transform(transform).tolist(),optics=values['optics'],render=values['render'])
        after=deepcopy(before);after['world_matrix'][0][3]=-5;after['world_matrix'][1][3]=2
        bounds={'actor':dict(minimum=[-1,0,-1],maximum=[1,2,1])}
        result=boundary_continuity(before,after,bounds,bounds,axis=[[-1,0,0],[1,0,0]],props_before={'key':'alice'},props_after={'key':'bob'})
        codes={w['code'] for w in result['warnings']}
        self.assertIn('camera-height',codes);self.assertIn('prop-state-change',codes)
        self.assertFalse(result['automatic_approval'])
        result=boundary_continuity(before,after,bounds,bounds,
            gaze_before={'actor':dict(origin=[0,1,0],direction=[1,0,0])},
            gaze_after={'actor':dict(origin=[0,1,0],direction=[-1,0,0])})
        self.assertIn('eyeline-screen-reversal',{item['code'] for item in result['warnings']})


if __name__=='__main__':unittest.main()
