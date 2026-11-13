import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_mesh import main
from model3d_primitives import primitive
from model3d_coordinates import transform_editable,conversion_matrix
from model3d_interchange import export_mesh
from rig_import import load_character_model

class MeshCLI(unittest.TestCase):
    def invoke(self,args):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(args)
        return code,json.loads(out.getvalue() or err.getvalue())
    def test_actual_primitive_editable_export_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'box'
            code,report=self.invoke(['primitive','--kind','box','--normal-mode','flat','--destination',str(dest),'--format','obj'])
            self.assertEqual(code,0)
            topology=json.loads((dest/'editable.json').read_text())
            self.assertEqual(len(topology['faces']),6);self.assertEqual(len(topology['positions']),8)
            for name,record in report['export']['files'].items():
                data=(dest/name).read_bytes();self.assertEqual(hashlib.sha256(data).hexdigest(),record['sha256'])
            self.assertEqual(load_character_model(dest/'model.obj').geometry.triangle_count,12)
    def test_actual_import_snapshot_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); export_mesh(primitive('box').geometry(),root/'source')
            source=root/'source/model.glb'; before=source.read_bytes()
            code,report=self.invoke(['import','--file',str(source),'--destination',str(root/'copy')])
            self.assertEqual(code,0);self.assertEqual(source.read_bytes(),before)
            self.assertEqual(report['sources'][0]['sha256'],hashlib.sha256(before).hexdigest())
            self.assertEqual((root/'copy/source/model.glb').read_bytes(), before)
            saved = json.loads((root/'copy/source.json').read_text())
            self.assertEqual(saved['files'][0]['sha256'], hashlib.sha256(before).hexdigest())
            for name,record in report['export']['files'].items():
                data=(root/'copy'/name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(),record['sha256'])
    def test_changed_snapshot_never_publishes_export(self):
        from model3d_mesh_import import import_mesh
        from rig_errors import RigError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_mesh(primitive('box').geometry(), root/'input')
            with import_mesh(root/'input/model.glb') as captured:
                copied = captured.root/captured.source_records[0]['path']
                copied.write_bytes(copied.read_bytes() + b'changed')
                destination = root/'changed'
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, destination, source_snapshot=captured)
                self.assertFalse(destination.exists())
                self.assertEqual(sorted(p.name for p in root.iterdir()), ['input'])

    def test_structured_negative_and_destination_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'existing';dest.mkdir();(dest/'sentinel').write_text('owned')
            for params in ['{"segments":true}','{"width":0}']:
                code,report=self.invoke(['primitive','--kind','box','--parameters',params,'--destination',str(dest)])
                self.assertEqual(code,2);self.assertIn('error',report)
            code,_=self.invoke(['primitive','--kind','box','--destination',str(dest)])
            self.assertEqual(code,2);self.assertEqual((dest/'sentinel').read_text(),'owned')
    def test_transformed_editable_source_and_render_remain_bound(self):
        original=primitive('box',normal_mode='flat')
        transformed=transform_editable(original,conversion_matrix(source_basis='left-handed-y-up',unit_scale=.01,pivot=[1,2,3]))
        self.assertEqual(transformed.faces[0],tuple(reversed(original.faces[0])))
        np.testing.assert_allclose(transformed.source_positions,(original.source_positions-[1,2,3])*[.01,.01,-.01])
        self.assertFalse(transformed.source_positions.flags.writeable)
        with tempfile.TemporaryDirectory() as tmp:
            export_mesh(transformed.geometry(),Path(tmp)/'out',editable=transformed)
            stored=json.loads((Path(tmp)/'out/editable.json').read_text())
            np.testing.assert_allclose(stored['positions'],transformed.source_positions)
    def test_tiny_unreimportable_geometry_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'tiny'
            code,report=self.invoke(['primitive','--kind','box','--parameters','{"width":1e-46,"height":1e-46,"depth":1e-46}', '--destination',str(dest)])
            self.assertEqual(code,2);self.assertFalse(dest.exists())
    def test_float32_translation_collapse_never_published(self):
        from model3d_coordinates import transform_geometry
        from rig_errors import RigError
        geometry=primitive('box').geometry()
        translated=transform_geometry(geometry,conversion_matrix(translation=[1e9,1e9,1e9]))
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'collapsed'
            with self.assertRaises(RigError):export_mesh(translated,dest)
            self.assertFalse(dest.exists())
        self.assertGreater(np.ptp(translated.meshes[0].positions,axis=0).max(),0)
    def test_small_component_float32_loss_is_rejected(self):
        from rig_models import Geometry
        from rig_errors import RigError
        big=primitive('box').mesh
        small=primitive('box',{'width':1e-8,'height':1e-8,'depth':1e-8},name='small').mesh
        small.positions=small.positions+2
        geometry=Geometry([big,small],primitive('box').geometry().materials)
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'out'
            with self.assertRaises(RigError):export_mesh(geometry,dest)
            self.assertFalse(dest.exists())
    def test_subnormal_source_face_underflow_does_not_hide_encoding_loss(self):
        from rig_models import Mesh,Geometry
        from rig_errors import RigError
        points=np.array([[0.,0,0],[1,0,0],[0,1,0],[0,0,1]])
        faces=np.array([[0,2,1],[0,1,3],[0,3,2],[1,2,3]])
        mesh=Mesh('two',np.concatenate([points,points*1e-200]),np.concatenate([faces,faces+4]))
        geometry=Geometry([mesh],[{}])
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'out'
            with self.assertRaises(RigError):export_mesh(geometry,dest)
            self.assertFalse(dest.exists())
    def test_primary_dispatch_capabilities(self):
        from model3d import main as primary
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=primary(['mesh','capabilities'])
        self.assertEqual(code,0);self.assertFalse(json.loads(out.getvalue())['network_calls'])
    def test_encoded_precision_cannot_reverse_skinny_face(self):
        from rig_models import Mesh,Geometry
        from rig_errors import RigError
        positions=np.array([[8391.055765444897,8173.624858768416,0],[8390.414464501771,8169.685507304802,0],[8390.848791174218,8172.353470438178,0]])
        mesh=Mesh('skinny',positions,np.array([[0,1,2]]),attributes={'NORMAL':np.tile([0.,0,-1],(3,1))})
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ['glb','gltf']:
                dest=Path(tmp)/fmt
                with self.assertRaises(RigError):export_mesh(Geometry([mesh],[{}]),dest,format=fmt)
                self.assertFalse(dest.exists())
        np.testing.assert_array_equal(mesh.positions,positions)
    def test_capabilities_truthful(self):
        code,report=self.invoke(['capabilities']);self.assertEqual(code,0)
        self.assertFalse(report['network_calls']);self.assertIn('general-remeshing',report['unavailable'])

if __name__=='__main__':unittest.main()
