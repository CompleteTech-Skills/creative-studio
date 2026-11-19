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

    def test_forged_entry_count_rejected_before_zipfile_allocation(self):
        import struct
        import render3d_archive as module
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'forged.zip'
            header=bytearray(46);header[:4]=b'PK\x01\x02';struct.pack_into('<H',header,28,1)
            central=(bytes(header)+b'x')*10
            path.write_bytes(central+struct.pack('<4s4H2LH',b'PK\x05\x06',0,0,1,1,len(central),0,0))
            with patch('render3d_archive.zipfile.ZipFile',side_effect=AssertionError('must not allocate')):
                with self.assertRaises(Render3DError):restore_render(path,Path(temporary)/'result')


@unittest.skipUnless(fixtures.DEPENDENCIES,'optional render decoders unavailable')
class ArchiveGuards(unittest.TestCase):
    """Each restore guard is reached by an archive whose inventory still agrees."""
    delivery=Archives.delivery

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);request,delivery,snapshot=self.delivery(self.root)
        archive_render(delivery,self.root/'film.zip',request=request,snapshot_root=snapshot)
        with zipfile.ZipFile(self.root/'film.zip') as original:
            self.members={i.filename:original.read(i) for i in original.infolist()}
        self.index=json.loads(self.members.pop('inventory.json'))
        self.frame=next(n for n in self.members if n.endswith('frames/000000.png'))

    def forge(self,name,*,members=None,records=None,extra=()):
        members=dict(self.members if members is None else members)
        files=[dict(r) for r in (self.index['files'] if records is None else records)]
        path=self.root/(name+'.zip')
        with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('inventory.json',json.dumps(dict(self.index,files=files)))
            for member,data in members.items():archive.writestr(member,data)
            for info,data in extra:archive.writestr(info,data)
        return path

    def refused(self,path,message):
        destination=self.root/(path.stem+'-out')
        with self.assertRaisesRegex(Render3DError,message):restore_render(path,destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.render-restore-*')),[])

    def record(self,member):
        return next(r for r in self.index['files'] if 'payload/'+r['path']==member)

    def test_same_size_byte_change_fails_the_member_hash(self):
        data=bytearray(self.members[self.frame]);data[-1]^=1
        self.refused(self.forge('flip',members={**self.members,self.frame:bytes(data)}),'Archived file hash differs')

    def test_compressed_member_is_refused(self):
        members=dict(self.members);data=members.pop(self.frame)
        info=zipfile.ZipInfo(self.frame);info.compress_type=zipfile.ZIP_DEFLATED
        self.refused(self.forge('deflated',members=members,extra=[(info,data)]),'size or compression')

    def test_symlink_member_is_refused(self):
        import stat
        members=dict(self.members);data=members.pop(self.frame)
        info=zipfile.ZipInfo(self.frame);info.external_attr=(stat.S_IFLNK|0o777)<<16
        self.refused(self.forge('symlink',members=members,extra=[(info,data)]),'nonregular')

    def test_duplicate_member_is_refused(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            path=self.forge('duplicate',extra=[(zipfile.ZipInfo(self.frame),self.members[self.frame])])
        self.refused(path,'duplicate entries')

    def test_listed_case_alias_is_refused(self):
        source=self.record(self.frame);alias=dict(source,path=source['path'].replace('frames/','FRAMES/'))
        members={**self.members,'payload/'+alias['path']:self.members[self.frame]}
        self.refused(self.forge('alias',members=members,records=[*self.index['files'],alias]),'alias')

    def test_reforged_consistent_inventory_is_reverified(self):
        import hashlib
        data=bytearray(self.members[self.frame]);data[-1]^=1
        records=[dict(r,sha256=hashlib.sha256(bytes(data)).hexdigest()) if 'payload/'+r['path']==self.frame else r
                 for r in self.index['files']]
        path=self.forge('reforged',members={**self.members,self.frame:bytes(data)},records=records)
        # The member hash agrees; only independent re-verification decodes the PNG.
        self.refused(path,'PNG CRC mismatch')

    def test_encrypted_member_is_refused_as_render_error(self):
        # ZipFile.writestr clears flag_bits, so set the encryption bit in the
        # written local and central headers of one member.
        import struct
        path=self.forge('encrypted');raw=bytearray(path.read_bytes())
        with zipfile.ZipFile(path) as archive:info=archive.getinfo(self.frame)
        raw[info.header_offset+6]|=1
        name=self.frame.encode();central=raw.find(b'PK\x01\x02')
        while central>=0:
            length=struct.unpack_from('<H',raw,central+28)[0]
            if bytes(raw[central+46:central+46+length])==name:raw[central+8]|=1;break
            central=raw.find(b'PK\x01\x02',central+46)
        self.assertGreaterEqual(central,0)
        path.write_bytes(bytes(raw))
        self.refused(path,'nonregular/encrypted')


class JunctionSources(unittest.TestCase):
    def test_inventory_refuses_a_directory_junction(self):
        import os
        import render3d_archive as module
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'source';(source/'linked').mkdir(parents=True)
            (source/'linked'/'file').write_bytes(b'outside')
            real=os.scandir
            class Entry:
                def __init__(self,entry):self.entry=entry
                def __getattr__(self,name):return getattr(self.entry,name)
                def is_junction(self):return self.entry.name=='linked'
            class Listing:
                def __init__(self,path):self.listing=real(path)
                def __enter__(self):return (Entry(e) for e in self.listing.__enter__())
                def __exit__(self,*args):return self.listing.__exit__(*args)
            with patch('render3d_archive.os.scandir',side_effect=Listing):
                with self.assertRaisesRegex(Render3DError,'junction'):module._inventory(source)

    def test_archive_refuses_junction_delivery_and_snapshot_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'delivery').mkdir();(root/'snapshot').mkdir()
            for junction,message in (('delivery','Delivery root'),('snapshot','Snapshot root')):
                with self.subTest(root=junction):
                    original=Path.is_junction if hasattr(Path,'is_junction') else None
                    def is_junction(path,_name=junction):return path.name==_name
                    with patch.object(Path,'is_junction',is_junction,create=True):
                        with self.assertRaisesRegex(Render3DError,message):
                            archive_render(root/'delivery',root/(junction+'.zip'),snapshot_root=root/'snapshot')
                    self.assertFalse((root/(junction+'.zip')).exists())
