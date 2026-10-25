"""Render3D foundation: real file snapshots; no renderer success is implied."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from render3d_contracts import (Render3DError, definitions, validate, schemas, load,
    frame_times, blender_frame, make_frame_spec, snapshot_hash)
from render3d_snapshot import NativeRigAdapter, create_snapshot, verify_snapshot


def file(path='snapshot.json', payload=b'fixture'):
    return dict(path=path, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())


def settings():
    return dict(engine='cycles', device_policy='cpu', device=None, allow_cpu_fallback=False,
        resolution=[128, 128], samples=8, seed=17, denoise=False,
        output=dict(format='png', bit_depth=8, passes=['combined'], alpha='straight', color_space='srgb'),
        exposure=0, view_transform='standard', resources=dict(memory_bytes=1024**3,
        storage_bytes=1024**3, timeout_seconds=60, workers=1, max_frames=1000))


def request():
    return dict(schema_version=1, kind='render3d-request', id='req',
        project=dict(id='project', version='1.0.0'), scene=dict(id='scene', version='1.0.0'),
        shot=dict(id='shot', version='1.0.0'), mode='3d', snapshot=file(),
        fps=dict(numerator=30000, denominator=1001), frame_range=dict(start=5,end=8),
        source_start=dict(numerator=1, denominator=2), playback_rate=dict(numerator=2, denominator=1),
        camera='camera', settings=settings(), priority=0, max_attempts=2, cache_policy='verify-reuse',
        destination=dict(path='render/output', owner_token='owner-unique'))


def backend():
    return dict(id='blender',adapter_version='1.0.0',application_version='fixture-not-executed',
        executable_sha256='0'*64,worker_sha256='1'*64,platform='fixture',engine='cycles',device='cpu')


class Contracts(unittest.TestCase):
    def test_schema_generation_and_request(self):
        from jsonschema import Draft202012Validator
        for name, schema in schemas().items():
            Draft202012Validator.check_schema(schema)
            self.assertEqual(json.loads((ROOT/'profiles'/name).read_text()),schema)
        value=request(); before=deepcopy(value)
        self.assertIs(validate(value),value)
        self.assertEqual(value,before)

    def test_unknown_fields_versions_modes_and_nonfinite(self):
        for update in (dict(schema_version=2),dict(mode='2d'),dict(hidden=True)):
            value=request();value.update(update)
            with self.assertRaises(Render3DError): validate(value)
        value=request();value['settings']['exposure']=float('nan')
        with self.assertRaises(Render3DError): validate(value)
        for value in ([],None,'request',12):
            with self.assertRaises(Render3DError):validate(value)

    def test_bad_paths_ranges_rates_and_policy(self):
        for path in ('../x','/absolute','C:/file','x\\y','x/CON','x//y','x/../y'):
            value=request();value['snapshot']['path']=path
            with self.subTest(path=path),self.assertRaises(Render3DError):validate(value)
        for change in (lambda v:v['fps'].update(numerator=60000,denominator=2002),
                       lambda v:v['frame_range'].update(end=5),
                       lambda v:v['settings'].update(device_policy='gpu-required',allow_cpu_fallback=True),
                       lambda v:v['settings']['output'].update(format='png',bit_depth=32),
                       lambda v:v['settings']['resources'].update(max_frames=2)):
            value=request();change(value)
            with self.assertRaises(Render3DError):validate(value)

    def test_duplicate_json_and_oversized_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.json'
            path.write_text('{"kind":"render3d-request","kind":"render3d-job"}')
            with self.assertRaises(Render3DError):load(path)
            path.write_bytes(b' '* (32*1024**2+1))
            with self.assertRaises(Render3DError):load(path)

    def test_exact_time_and_boundaries(self):
        value=request()
        self.assertEqual(frame_times(value,5),(Fraction(5005,30000),Fraction(1,2)))
        self.assertEqual(frame_times(value,7),(Fraction(7007,30000),Fraction(1,2)+Fraction(4004,30000)))
        self.assertEqual(blender_frame(Fraction(1,60),Fraction(24)),(1,Fraction(2,5)))
        for index in (4,8,True,5.5):
            with self.assertRaises(Render3DError):frame_times(value,index)

    def test_result_cannot_report_success_without_verifier_and_images(self):
        result=dict(schema_version=1,kind='render3d-result',job='job',request_sha256='0'*64,
            snapshot_sha256='1'*64,backend=backend(),status='complete',requested_frames=[0],
            successful=[0],failed=[],skipped=[],reused=[],images=[],diagnostics=[],
            usage=dict(elapsed_seconds=None,peak_memory_bytes=None,output_bytes=None,measurement='unavailable'),
            logs=[],manifest=None,verification=dict(status='not-run',verifier=None,evidence=None))
        with self.assertRaises(Render3DError):validate(result)
        result['status']='partial';validate(result)
        result['reused']=[0]
        with self.assertRaises(Render3DError):validate(result)

    def test_job_counts_and_complete_state(self):
        job=dict(schema_version=1,kind='render3d-job',id='job',request_sha256='0'*64,snapshot_sha256='1'*64,
            backend=None,destination_owner='owner',submitted_at='2026-10-25T00:00:00Z',updated_at='2026-10-25T00:00:00Z',
            worker=None,heartbeat_at=None,attempt=0,counts=dict(requested=3,queued=3,active=0,completed=0,failed=0),
            state='queued',cancellation_requested=False,manifest=None,error=None)
        validate(job);job['state']='complete'
        with self.assertRaises(Render3DError):validate(job)
        job['state']='queued';job['counts']['active']=1
        with self.assertRaises(Render3DError):validate(job)

    def test_discovery_is_not_execution(self):
        capability=dict(schema_version=1,kind='render3d-capabilities',adapter=dict(id='blender',version='1.0.0'),
            backend=None,discovery='available',initialization='not-run',execution='not-run',evidence=[],
            engines=['cycles'],features=[],diagnostics=[])
        validate(capability);capability['execution']='passed'
        with self.assertRaises(Render3DError):validate(capability)


class NativeSnapshot(unittest.TestCase):
    def test_real_rig_snapshot_survives_relocation_and_original_change(self):
        from rig_demo import create_demo
        from rig_models import load_rig
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'rig';destination=root/'snapshot'
            rig,_=create_demo(source,'robotic',animate=False)
            original=load_rig(source)
            report=create_snapshot(source,destination,NativeRigAdapter())
            self.assertEqual(report,verify_snapshot(destination,NativeRigAdapter()))
            self.assertEqual(original.geometry.vertex_count,load_rig(destination).geometry.vertex_count)
            source.joinpath('rig.json').write_text('{}')
            moved=root/'moved';destination.rename(moved)
            self.assertEqual(report,verify_snapshot(moved,NativeRigAdapter()))
            with self.assertRaises(Render3DError):create_snapshot(moved,moved,NativeRigAdapter())
            value=request();value['snapshot']=file('snapshot.json', (moved/'snapshot.json').read_bytes())
            frame=make_frame_spec(value,5,moved/'snapshot.json')
            self.assertEqual(frame['snapshot_sha256'],report['snapshot_sha256'])
            target=moved/report['files'][0]['path'];target.write_bytes(b'changed')
            with self.assertRaises(Render3DError):verify_snapshot(moved,NativeRigAdapter())

    def test_cli_has_no_render_or_success_alias(self):
        completed=subprocess.run([sys.executable,str(ROOT/'scripts/render3d.py'),'--help'],capture_output=True,text=True)
        self.assertEqual(completed.returncode,0)
        self.assertIn('snapshot-rig',completed.stdout)
        self.assertNotIn('render,',completed.stdout)


if __name__=='__main__':unittest.main()
