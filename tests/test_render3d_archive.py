"""Real pixel/closure archive roundtrip and hostile container tests."""
from pathlib import Path
import sys,json,tempfile,unittest,zipfile
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from render3d_archive import archive_render,restore_render
from render3d_contracts import Render3DError
from rig_errors import atomic_json,sha256
import test_render3d_verify as fixtures

@unittest.skipUnless(fixtures.DEPENDENCIES,'optional render decoders unavailable')
class Archives(unittest.TestCase):
    def delivery(self,root):
        request,result,delivery,snapshot,backend=fixtures.ImageVerification().scene_result(root)
        from render3d_verify import verify_result
        report=verify_result(request,result,delivery,snapshot_root=snapshot,expected_backend=backend)
        atomic_json(delivery/'verification.json',report)
        atomic_json(delivery/'manifest.json',{'images':result['images']})
        def ref(name):
            p=delivery/name;return {'path':name,'sha256':sha256(p),'bytes':p.stat().st_size}
        result['manifest']=ref('manifest.json')
        result['verification']={'status':'passed','verifier':report['verifier'],'evidence':ref('verification.json')}
        atomic_json(delivery/'result.json',result)
        return request,delivery,snapshot

    def test_roundtrip_preserves_actual_pixels_and_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);request,delivery,snapshot=self.delivery(root)
            archive_render(delivery,root/'film.zip',request=request,snapshot_root=snapshot)
            report=restore_render(root/'film.zip',root/'restored')
            self.assertEqual(report['decoded_frames'],2)
            self.assertEqual((delivery/'frames/000000.png').read_bytes(),(root/'restored/frames/000000.png').read_bytes())
            self.assertEqual((snapshot/'snapshot.json').read_bytes(),(root/'restored/snapshot/snapshot.json').read_bytes())
            with self.assertRaises(Render3DError):restore_render(root/'film.zip',root/'restored')

    def test_unlisted_traversal_and_tampering_fail_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);request,delivery,snapshot=self.delivery(root)
            archive_render(delivery,root/'film.zip',request=request,snapshot_root=snapshot)
            with zipfile.ZipFile(root/'film.zip') as original:records=[(i.filename,original.read(i)) for i in original.infolist()]
            for mode in ('traversal','corrupt','alias'):
                path=root/(mode+'.zip')
                with zipfile.ZipFile(path,'w') as archive:
                    for name,data in records:
                        if mode=='corrupt' and name.endswith('000000.png'):data=b'bad'
                        archive.writestr(name,data)
                    if mode=='traversal':archive.writestr('../escape',b'bad')
                    if mode=='alias':archive.writestr('payload/FRAMES/x',b'bad')
                with self.subTest(mode=mode),self.assertRaises(Render3DError):restore_render(path,root/mode)
                self.assertFalse((root/mode).exists())

    def test_racing_restore_destination_survives(self):
        import render3d_archive
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);request,delivery,snapshot=self.delivery(root)
            archive_render(delivery,root/'film.zip',request=request,snapshot_root=snapshot)
            destination=root/'race';rename=render3d_archive._rename_no_replace
            def race(source,target):
                destination.mkdir();(destination/'user').write_text('retain');return rename(source,target)
            with patch('render3d_archive._rename_no_replace',side_effect=race):
                with self.assertRaises(Render3DError):restore_render(root/'film.zip',destination)
            self.assertEqual((destination/'user').read_text(),'retain')

    @unittest.skipUnless(hasattr(__import__('os'),'mkfifo'),'FIFO support required')
    def test_inventoried_file_replaced_with_fifo_rejects_without_blocking(self):
        import os
        import render3d_archive as module
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            item=source/'file';item.write_bytes(b'original')
            inventory=module._inventory(source)
            item.unlink();os.mkfifo(item)
            with self.assertRaises(Render3DError):
                module._copy_file(item,root/'target',inventory[0])

    @unittest.skipUnless(hasattr(__import__('os'),'mkfifo'),'FIFO support required')
    def test_restore_archive_replaced_with_fifo_rejects_without_blocking(self):
        import os
        import render3d_archive as module
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);request,delivery,snapshot=self.delivery(root)
            path=root/'film.zip';archive_render(delivery,path,request=request,snapshot_root=snapshot)
            original=module._open_archive
            def replace(name):
                path.unlink();os.mkfifo(path);return original(name)
            with patch('render3d_archive._open_archive',side_effect=replace):
                with self.assertRaises(Render3DError):restore_render(path,root/'restored')
            self.assertFalse((root/'restored').exists())

    def test_oversized_central_directory_rejects_before_zipfile_allocation(self):
        import struct
        import render3d_archive as module
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'huge.zip'
            with path.open('wb') as stream:
                stream.seek(1024**3)
                stream.write(struct.pack('<4s4H2LH',b'PK\x05\x06',0,0,65535,65535,1024**3,0,0))
            with patch('render3d_archive.zipfile.ZipFile',side_effect=AssertionError('must not allocate')):
                with self.assertRaises(Render3DError):restore_render(path,root/'restored')
