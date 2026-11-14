"""Helper payloads must respect render admission before geometry or path writes."""
import hashlib
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from cinematics3d_contracts import Registry,canonical_bytes
from cinematics3d_blender import compile_plan
from render3d_blender import BlenderBackend
from render3d_contracts import Render3DError
from render3d_snapshot import Scene3DAdapter,create_snapshot
from scene3d_example import build_example
from test_render3d_contracts import request
from test_rig_cinematics3d_contracts import doc
from test_rig_cinematics3d_blender import inventory


class CinematicStorage(unittest.TestCase):
    def test_known_helper_bytes_reject_before_geometry_decode_or_destination_creation(self):
        for resource,limit,category in (('storage_bytes',512*1024,'storage-exhaustion'),
                                        ('memory_bytes',1024**2,'system-memory')):
            with self.subTest(resource=resource),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);build_example(root/'source');create_snapshot(root/'source',root/'snapshot',Scene3DAdapter())
                raw=(root/'snapshot/snapshot.json').read_bytes();value=request()
                value.update(scene=dict(id='example-scene',version='1.0.0'),shot=dict(id='example-shot',version='1.0.0'),
                    snapshot=dict(path='snapshot.json',sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw)),
                    frame_range=dict(start=0,end=1),source_start=dict(numerator=0,denominator=1))
                value['settings']['resolution']=[1,1];value['settings']['resources'][resource]=limit
                stack=doc('stack','stack',scope='shot',parent=None,effects=[],bypass=[])
                registry=Registry([('stack.json',canonical_bytes(stack))],external={'owned-input.bin':b'x'*(2*1024**2)})
                compiled=compile_plan(registry,registry.reference('stack','1.0.0'),value,inventory())
                backend=object.__new__(BlenderBackend);target=root/'new-parent/output'
                with patch('render3d_blender._prepare_scene',side_effect=AssertionError('Payload admission must precede geometry decoding')) as prepare:
                    with self.assertRaises(Render3DError) as caught:
                        backend.render_frames(compiled['request'],root/'snapshot',target,cinematics=compiled)
                self.assertEqual(caught.exception.report['category'],category)
                prepare.assert_not_called();self.assertFalse(target.parent.exists())



class ProbeDescriptorProtocol(unittest.TestCase):
    @unittest.skipUnless(__import__('atomic_publish').private_stage_supported(),
                         'Linux procfs directory capability adapter required')
    def test_cinematic_probe_inherits_a_live_registered_directory_descriptor(self):
        import os, stat, json
        from unittest.mock import patch
        from test_rig_cinematics3d_blender import inventory
        import render3d_blender as backend_module
        from rig_errors import scoped_directory_descriptor, sha256
        backend=object.__new__(backend_module.BlenderBackend)
        backend.executable=sys.executable
        value=inventory()
        metadata=dict(application_version=value['application_version'],
                      executable_sha256=sha256(sys.executable),
                      worker_sha256=sha256(backend_module.WORKER))
        descriptors=[]
        def supervise(command, *, cwd, pass_fds, **limits):
            self.assertEqual(len(pass_fds),1)
            descriptor=pass_fds[0];descriptors.append(descriptor)
            self.assertTrue(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            self.assertEqual(command[-1],str(descriptor))
            self.assertEqual(Path(command[-2]),cwd/'job.json')
            with scoped_directory_descriptor(cwd) as owned:
                self.assertIsNotNone(owned)
            (cwd/'cinematics-probe.json').write_text(json.dumps(value))
        with patch.object(backend,'inspect_version',return_value=metadata), \
             patch.object(backend_module,'_supervise',side_effect=supervise):
            self.assertEqual(backend.cinematics_probe(),value)
        self.assertEqual(len(descriptors),1)
        with self.assertRaises(OSError):os.fstat(descriptors[0])


class RetainedFileIntegrity(unittest.TestCase):
    def fixture(self,root):
        values={'documents/effect.json':b'{"source":true}','plan.json':b'{"plan":true}','probe.json':b'{"probe":true}'}
        for name,payload in values.items():
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload)
        refs={name:dict(bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest()) for name,payload in values.items()}
        return values,refs

    def test_complete_inventory_matches_actual_file_bytes(self):
        from render3d_verify import verify_retained_files
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);_,refs=self.fixture(root)
            self.assertEqual(verify_retained_files(root,refs,max_total_bytes=1024,strict=True),refs)
            (root/'empty.log').write_bytes(b'')
            refs['empty.log']=dict(bytes=0,sha256=hashlib.sha256(b'').hexdigest())
            self.assertEqual(verify_retained_files(root,refs,max_total_bytes=1024,strict=True),refs)

    def test_every_declared_configuration_role_rejects_replacement(self):
        from render3d_verify import verify_retained_files
        for name in ('documents/effect.json','plan.json','probe.json'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);_,refs=self.fixture(root);(root/name).write_bytes(b'{}')
                with self.assertRaises(Render3DError):verify_retained_files(root,refs,max_total_bytes=1024,strict=True)

    def test_unknown_file_empty_directory_and_missing_input_reject(self):
        from render3d_verify import verify_retained_files
        for mutation in ('extra-file','extra-dir','missing'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);_,refs=self.fixture(root)
                if mutation=='extra-file':(root/'unowned.json').write_bytes(b'{}')
                elif mutation=='extra-dir':(root/'unowned').mkdir()
                else:(root/'plan.json').unlink()
                with self.assertRaises(Render3DError):verify_retained_files(root,refs,max_total_bytes=1024,strict=True)

    def test_malformed_sizes_paths_and_aliases_fail_before_file_reads(self):
        from render3d_verify import verify_retained_files
        for bad in (True,-1,1.5,10**400):
            with self.subTest(size=type(bad).__name__),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);_,refs=self.fixture(root);refs['plan.json']['bytes']=bad
                with self.assertRaises(Render3DError):verify_retained_files(root,refs,max_total_bytes=1024)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);_,refs=self.fixture(root)
            for name in ('../plan.json','/plan.json','PLAN.JSON','a/'*1025+'x'):
                changed=dict(refs);changed[name]=refs['plan.json']
                with self.assertRaises(Render3DError):verify_retained_files(root,changed,max_total_bytes=1024)

    @unittest.skipUnless(__import__('os').name=='posix','Actual symlink and FIFO admission is POSIX-specific')
    def test_symlink_ancestors_and_fifo_are_rejected_without_opening_payloads(self):
        import os
        from render3d_verify import verify_retained_files
        for replacement in ('leaf-link','parent-link','fifo'):
            with self.subTest(replacement=replacement),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)/'owned';root.mkdir();_,refs=self.fixture(root)
                outside=root.parent/'outside';outside.mkdir();(outside/'effect.json').write_bytes(b'{"source":true}')
                if replacement=='parent-link':
                    (root/'documents/effect.json').unlink();(root/'documents').rmdir();(root/'documents').symlink_to(outside,target_is_directory=True)
                else:
                    target=root/'documents/effect.json';target.unlink()
                    if replacement=='leaf-link':target.symlink_to(outside/'effect.json')
                    else:os.mkfifo(target)
                with self.assertRaises(Render3DError):verify_retained_files(root,refs,max_total_bytes=1024)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'owned';root.mkdir();_,refs=self.fixture(root)
            alias=root.parent/'alias';alias.symlink_to(root,target_is_directory=True)
            with self.assertRaises(Render3DError):verify_retained_files(alias,refs,max_total_bytes=1024,strict=True)


@unittest.skipUnless(__import__('os').environ.get('HATCH_RENDER3D_REAL_TESTS')=='1',
                     'Actual Blender gate requires HATCH_RENDER3D_REAL_TESTS=1')
class RetainedCinematicPublication(unittest.TestCase):
    def test_actual_worker_rejects_mutated_retained_payloads_after_progress(self):
        import json,zipfile
        import render3d_blender as implementation
        archive=ROOT/'examples/cinematics3d/backend-current/engineering-evidence.zip'
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);snapshot=root/'snapshot';snapshot.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                for name in bundle.namelist():
                    if name.startswith('native/snapshot/'):
                        relative=name[len('native/snapshot/'):]
                        from rig_errors import safe_path
                        target=safe_path(snapshot,relative);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(bundle.read(name))
                original=json.loads(bundle.read('native/baseline-request.json'))
                documents=[(name[len('native/baseline/cinematics/documents/'):],bundle.read(name)) for name in bundle.namelist()
                           if name.startswith('native/baseline/cinematics/documents/')]
                external={name[len('native/baseline/cinematics/inputs/'):]:bundle.read(name) for name in bundle.namelist()
                          if name.startswith('native/baseline/cinematics/inputs/')}
            registry=Registry(documents,external=external);backend=BlenderBackend();probe=backend.cinematics_probe()
            stack=json.loads(dict(documents)['native-stack.json'])
            stack_reference=registry.reference(stack['id'],stack['version'])
            supervisor=implementation._supervise
            roles=['documents/dof.json','inputs/optics.json','plan.json','probe.json',
                   '../worker-report.json','../blender.log','../source/job.json','../source/asset-0.glb','extra.json']
            for index,role in enumerate(roles+[None]):
                with self.subTest(role=role):
                    current=__import__('copy').deepcopy(original);current['id']='retained-'+str(index)
                    current['settings'].update(resolution=[32,32],samples=1)
                    current['destination']['path']=current['id']
                    compiled=compile_plan(registry,stack_reference,current,probe,input_bytes=external)
                    observed={}
                    def observe(command,**options):
                        observed['stage']=Path(options['cwd'])
                        return supervisor(command,**options)
                    def progress(frame):
                        if role is not None:
                            target=observed['stage']/'cinematics'/role
                            target.write_bytes(b'{}')
                    target=root/current['id']
                    with patch.object(implementation,'_supervise',side_effect=observe):
                        if role is None:
                            result=backend.render_frames(compiled['request'],snapshot,target,cinematics=compiled,progress=progress)
                            self.assertEqual(result['verification']['status'],'passed')
                            verification=json.loads((target/'verification.json').read_bytes())
                            self.assertIn('plan.json',verification['retained_cinematics'])
                            self.assertIn('probe.json',verification['retained_cinematics'])
                            print(json.dumps(dict(case='unchanged',status='published',
                                retained_cinematic_files=len(verification['retained_cinematics']),
                                retained_artifact_files=len(verification['retained_artifacts']))),flush=True)
                        else:
                            with self.assertRaises(Render3DError) as caught:
                                backend.render_frames(compiled['request'],snapshot,target,cinematics=compiled,progress=progress)
                            self.assertEqual(caught.exception.report['category'],'corrupt-output')
                            self.assertFalse(target.exists(),'Mutated retained provenance must never be published')
                            print(json.dumps(dict(case=role,status='rejected',category=caught.exception.report['category'],
                                destination_created=target.exists())),flush=True)


if __name__=='__main__':unittest.main()
