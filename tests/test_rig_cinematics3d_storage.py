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


if __name__=='__main__':unittest.main()
