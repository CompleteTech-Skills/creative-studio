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

    def test_integral_json_floats_are_typed_rejections(self):
        for field in ('numerator', 'denominator'):
            value=request();value['fps'][field]=float(value['fps'][field])
            with self.assertRaises(Render3DError):validate(value)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'request.json';value=request();value['fps']['numerator']=30000.0
            path.write_text(json.dumps(value))
            completed=subprocess.run([sys.executable,str(ROOT/'scripts/render3d.py'),'validate',str(path)],capture_output=True,text=True)
            self.assertEqual(completed.returncode,2)
            self.assertEqual(json.loads(completed.stdout)['code'],'RENDER3D_INVALID_INPUT')
            self.assertNotIn('Traceback',completed.stderr)

    def test_large_coordinate_values_are_typed_rejections(self):
        from render3d_contracts import canonical_to_blender
        for value in (10**309,-10**400):
            matrix=[[1,0,0,value],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
            with self.assertRaises(Render3DError):canonical_to_blender(matrix)

    def test_derived_time_can_represent_all_bounded_input_factors(self):
        from render3d_contracts import rational
        value=request();value['source_start']=dict(numerator=1,denominator=1000000007)
        presentation,source=frame_times(value,6)
        validate(rational(source),'Time')
        self.assertEqual(source,Fraction(1001000022007,15000000105000))
        value['source_start']=dict(numerator=2**31-1,denominator=2**31-2)
        value['fps']=dict(numerator=2**31-1,denominator=2**31-2)
        value['playback_rate']=dict(numerator=2**31-3,denominator=2**31-4)
        value['frame_range']=dict(start=0,end=2**31-1)
        value['settings']['resources']['max_frames']=100000
        value['frame_range']['start']=value['frame_range']['end']-100000
        _,source=frame_times(value,value['frame_range']['end']-1)
        validate(rational(source),'Time')

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

    def test_usage_bytes_cover_the_supported_resource_budget(self):
        from jsonschema import Draft202012Validator
        schema=schemas()['render3d-result.schema.json']
        value=dict(schema_version=1,kind='render3d-result',job='job',request_sha256='0'*64,
            snapshot_sha256='1'*64,backend=backend(),status='partial',requested_frames=[0],
            successful=[],failed=[],skipped=[0],reused=[],images=[],diagnostics=[],
            usage=dict(elapsed_seconds=1,peak_memory_bytes=2**31,output_bytes=2**40,measurement='measured'),
            logs=[],manifest=None,verification=dict(status='not-run',verifier=None,evidence=None))
        validate(value)
        Draft202012Validator(schema).validate(value)
        for field in ('peak_memory_bytes','output_bytes'):
            for measurement in (0,2**31-1,2**31,2**40,None):
                candidate=deepcopy(value);candidate['usage'][field]=measurement
                validate(candidate)
                Draft202012Validator(schema).validate(candidate)
            for measurement in (-1,2**40+1,True,1.5):
                candidate=deepcopy(value);candidate['usage'][field]=measurement
                with self.subTest(field=field,measurement=measurement),self.assertRaises(Render3DError):
                    validate(candidate)

    def test_exr_requests_require_the_combined_completion_pass(self):
        value=request()
        value['settings']['output'].update(format='exr',bit_depth=16,color_space='linear')
        for passes in (['depth'],['normal'],['albedo'],['depth','normal','albedo']):
            value['settings']['output']['passes']=passes
            with self.subTest(passes=passes),self.assertRaises(Render3DError):validate(value)
        for passes in (['combined'],['combined','depth'],['normal','combined','albedo']):
            value['settings']['output']['passes']=passes
            validate(value)

    def test_discovery_is_not_execution(self):
        capability=dict(schema_version=1,kind='render3d-capabilities',adapter=dict(id='blender',version='1.0.0'),
            backend=None,discovery='available',initialization='not-run',execution='not-run',evidence=[],
            engines=['cycles'],features=[],diagnostics=[])
        validate(capability);capability['execution']='passed'
        with self.assertRaises(Render3DError):validate(capability)


class NativeSnapshot(unittest.TestCase):
    def test_malformed_hash_consistent_native_archives_have_typed_cli_errors(self):
        from rig_test_helpers import fixture
        from rig_errors import atomic_json,sha256
        for kind in ('geometry','skin'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);rig=fixture(root,animated=False)
                frozen=root/'frozen'
                document=create_snapshot(rig.root,frozen,NativeRigAdapter())
                for source in (rig.root,frozen):
                    manifest=json.loads((source/'rig.json').read_text())
                    descriptor=manifest['geometry']['data'] if kind=='geometry' else manifest['skins'][0]['data']
                    archive=source/descriptor['path'];archive.write_bytes(b'not an NPZ archive')
                    descriptor['sha256']=sha256(archive)
                    atomic_json(source/'rig.json',manifest)
                destination=root/'rejected'
                with self.assertRaises(Render3DError) as raised:
                    create_snapshot(rig.root,destination,NativeRigAdapter())
                self.assertEqual(raised.exception.report['code'],'RENDER3D_INVALID_INPUT')
                self.assertFalse(destination.exists())
                for item in document['files']:
                    path=frozen/item['path'];item.update(sha256=sha256(path),bytes=path.stat().st_size)
                document['total_bytes']=sum(item['bytes'] for item in document['files'])
                document['snapshot_sha256']=snapshot_hash(document)
                atomic_json(frozen/'snapshot.json',document)
                with self.assertRaises(Render3DError) as raised:
                    verify_snapshot(frozen,NativeRigAdapter())
                self.assertEqual(raised.exception.report['code'],'RENDER3D_STALE_ASSET')
                for arguments,code in ((['snapshot-rig','--source',str(rig.root),'--destination',str(destination)],'RENDER3D_INVALID_INPUT'),
                                       (['verify-snapshot',str(frozen)],'RENDER3D_STALE_ASSET')):
                    completed=subprocess.run([sys.executable,str(ROOT/'scripts/render3d.py'),*arguments],capture_output=True,text=True)
                    self.assertEqual(completed.returncode,2,completed.stderr)
                    self.assertEqual(json.loads(completed.stdout)['code'],code)
                    self.assertNotIn('Traceback',completed.stderr)

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

    def test_present_native_controls_skins_and_clips_require_owner_semantics(self):
        from rig_demo import create_demo
        from rig_models import load_rig
        from rig_jobs import save_rig
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);baseline=root/'base'
            create_demo(baseline,'robotic')
            for kind in ('ik','cycle','skin','clip'):
                source=root/kind;shutil.copytree(baseline,source)
                rig=load_rig(source)
                if kind=='ik':rig.manifest['controllers']['ik'][0]['bones'][-1]='missing-bone'
                elif kind=='cycle':
                    bone=rig.manifest['skeleton']['bones'][1];bone['parent']=bone['id']
                elif kind=='skin':next(iter(rig.weights.values())).joints[0]='missing-bone'
                else:next(iter(rig.clips.values()))['tracks'][0]['target']='missing-bone'
                save_rig(rig,bump=False)
                # Storage is still hash-consistent; semantic validation must reject.
                load_rig(source)
                with self.subTest(kind=kind),self.assertRaises(Render3DError):
                    create_snapshot(source,root/(kind+'-snapshot'),NativeRigAdapter())
                self.assertFalse((root/(kind+'-snapshot')).exists())
            static=root/'static';shutil.copytree(baseline,static)
            rig=load_rig(static);rig.manifest['skeleton']=None
            rig.manifest['controllers']=dict(ik=[],constraints=[])
            rig.weights={};rig.clips={};save_rig(rig,bump=False)
            result=create_snapshot(static,root/'static-snapshot',NativeRigAdapter())
            self.assertEqual(result,verify_snapshot(root/'static-snapshot',NativeRigAdapter()))

    def test_ragged_native_animation_reverification_has_typed_failure(self):
        from rig_demo import create_demo
        from rig_models import load_rig
        from rig_jobs import save_rig
        from rig_errors import atomic_json, sha256
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';frozen=root/'frozen'
            create_demo(source,'robotic')
            document=create_snapshot(source,frozen,NativeRigAdapter())
            rig=load_rig(frozen)
            clip=next(iter(rig.clips.values()))
            clip['tracks'][0]['values'][0].pop()
            save_rig(rig,bump=False)
            # Rebind storage metadata to isolate semantic owner validation.
            for item in document['files']:
                path=frozen/item['path']
                if path.exists():
                    item.update(sha256=sha256(path),bytes=path.stat().st_size)
            for descriptor in rig.manifest['animations']:
                if descriptor['path'] not in {i['path'] for i in document['files']}:
                    document['files'].append(file(descriptor['path'],(frozen/descriptor['path']).read_bytes()))
            # The owner now references new immutable animation paths; drop old
            # unreferenced paths so the closure itself remains honest.
            old_animation_paths={i['path'] for i in document['files'] if i['path'].startswith('animations/')}
            new_animation_paths={i['path'] for i in rig.manifest['animations']}
            document['files']=[i for i in document['files'] if i['path'] not in old_animation_paths-new_animation_paths]
            document['files'].sort(key=lambda i:i['path'])
            document['total_bytes']=sum(i['bytes'] for i in document['files'])
            document['snapshot_sha256']=snapshot_hash(document)
            atomic_json(frozen/'snapshot.json',document)
            with self.assertRaises(Render3DError):verify_snapshot(frozen,NativeRigAdapter())
            completed=subprocess.run([sys.executable,str(ROOT/'scripts/render3d.py'),'verify-snapshot',str(frozen)],capture_output=True,text=True)
            self.assertEqual(completed.returncode,2)
            self.assertEqual(json.loads(completed.stdout)['code'],'RENDER3D_STALE_ASSET')
            self.assertNotIn('Traceback',completed.stderr)

    def test_cli_has_no_render_or_success_alias(self):
        completed=subprocess.run([sys.executable,str(ROOT/'scripts/render3d.py'),'--help'],capture_output=True,text=True)
        self.assertEqual(completed.returncode,0)
        self.assertIn('snapshot-rig',completed.stdout)
        self.assertNotIn('render,',completed.stdout)


if __name__=='__main__':unittest.main()

class OutputBoundaryRegressions(unittest.TestCase):
    def test_output_files_allow_supported_storage_budgets_without_widening_sources(self):
        image=dict(frame=0,file=dict(path='large.exr',sha256='0'*64,bytes=2**30),format='exr',bit_depth=32,pass_name='combined',resolution=[16384,16384])
        value=dict(schema_version=1,kind='render3d-result',job='job',request_sha256='0'*64,snapshot_sha256='1'*64,backend=backend(),status='partial',requested_frames=[0],successful=[0],failed=[],skipped=[],reused=[],images=[image],diagnostics=[],usage=dict(elapsed_seconds=None,peak_memory_bytes=None,output_bytes=None,measurement='unavailable'),logs=[],manifest=None,verification=dict(status='not-run',verifier=None,evidence=None))
        validate(value)
        image['file']['bytes']=2**40;validate(value)
        image['file']['bytes']=2**40+1
        with self.assertRaises(Render3DError):validate(value)
        with self.assertRaises(Render3DError):validate(dict(path='source.bin',sha256='0'*64,bytes=2**30),'FileRef')

    def test_library_diagnostics_retain_absolute_source_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'missing.json'
            with self.assertRaises(Render3DError) as raised:load(path)
            report=raised.exception.report
            validate(report,'Diagnostic')
            self.assertEqual(report['path'],str(path))
            bad=request();bad['snapshot']['path']=str(path)
            with self.assertRaises(Render3DError):validate(bad)

    def test_integral_float_versions_and_output_depths_are_rejected(self):
        for field in ('schema_version','bit_depth'):
            value=request()
            if field=='schema_version':value[field]=1.0
            else:value['settings']['output'][field]=8.0
            with self.subTest(field=field),self.assertRaises(Render3DError):validate(value)

    def test_gpu_required_cannot_select_cpu(self):
        value=request();value['settings'].update(device_policy='gpu-required',device='cpu')
        with self.assertRaises(Render3DError):validate(value)

    def test_frame_output_compatibility_cannot_be_bypassed_by_rehashing(self):
        from render3d_contracts import frame_hash
        frame=dict(schema_version=1,kind='render3d-frame',index=0,local_index=0,presentation_time=dict(numerator=0,denominator=1),source_time=dict(numerator=0,denominator=1),camera='camera',snapshot_sha256='0'*64,settings_sha256='1'*64,dependencies=['2'*64],output=settings()['output'],destination='out',cache_key='0'*64)
        frame['cache_key']=frame_hash(frame);validate(frame)
        for values in (dict(format='png',bit_depth=32),dict(format='exr',bit_depth=8,color_space='srgb')):
            bad=deepcopy(frame);bad['output'].update(values);bad['cache_key']=frame_hash(bad)
            with self.subTest(values=values),self.assertRaises(Render3DError):validate(bad)

    def test_frame_rejects_snapshot_replaced_between_authorization_and_verification(self):
        from unittest.mock import patch
        from rig_test_helpers import fixture
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rig=fixture(root,animated=False);frozen=root/'frozen'
            document=create_snapshot(rig.root,frozen,NativeRigAdapter())
            value=request();value['snapshot']=file('snapshot.json',(frozen/'snapshot.json').read_bytes())
            replacement=deepcopy(document);replacement['source']['id']='different'
            replacement['snapshot_sha256']=snapshot_hash(replacement)
            with patch('render3d_snapshot.verify_snapshot',return_value=replacement):
                with self.assertRaises(Render3DError):make_frame_spec(value,5,frozen/'snapshot.json')

    def test_staging_disk_exhaustion_has_storage_diagnostic(self):
        from unittest.mock import patch
        import errno
        from rig_test_helpers import fixture
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rig=fixture(root,animated=False)
            with patch('render3d_snapshot.tempfile.mkdtemp',side_effect=OSError(errno.ENOSPC,'disk full')):
                with self.assertRaises(Render3DError) as caught:create_snapshot(rig.root,root/'snapshot',NativeRigAdapter())
            self.assertEqual(caught.exception.report['category'],'storage-exhaustion')

    def test_result_specific_json_limit_preserves_default_decoder_limit(self):
        from rig_errors import decode_json,RigError
        value=dict(schema_version=1,kind='render3d-result',job='job',request_sha256='0'*64,snapshot_sha256='1'*64,backend=backend(),status='partial',requested_frames=[0],successful=[],failed=[],skipped=[0],reused=[],images=[],diagnostics=[],usage=dict(elapsed_seconds=None,peak_memory_bytes=None,output_bytes=None,measurement='unavailable'),logs=[],manifest=None,verification=dict(status='not-run',verifier=None,evidence=None))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.json'
            payload=json.dumps(value).encode()+b' '*(32*1024**2)
            path.write_bytes(payload)
            with self.assertRaises(RigError):decode_json(payload)
            self.assertEqual(load(path),value)
            self.assertEqual(load(path,'RenderResult'),value)
            path.write_bytes(json.dumps(request()).encode()+b' '*(32*1024**2))
            with self.assertRaises(Render3DError):load(path)
