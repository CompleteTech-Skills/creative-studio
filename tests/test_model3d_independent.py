"""Independent in-process pygltflib decoding of actual foundation exports."""
import base64
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
from pygltflib import GLTF2
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_primitives import primitive
from model3d_interchange import export_mesh
from model3d_coordinates import conversion_matrix,transform_geometry

class IndependentExports(unittest.TestCase):
    def array(self,document,index,root):
        accessor=document.accessors[index];view=document.bufferViews[accessor.bufferView]
        buffer=document.buffers[view.buffer]
        if buffer.uri is None:data=document.binary_blob()
        elif buffer.uri.startswith('data:'):data=base64.b64decode(buffer.uri.split(',',1)[1])
        else:data=(root/buffer.uri).read_bytes()
        widths={'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4}
        dtype={5126:'<f4',5125:'<u4',5123:'<u2'}[accessor.componentType]
        offset=(view.byteOffset or 0)+(accessor.byteOffset or 0)
        self.assertFalse(view.byteStride)
        return np.frombuffer(data,dtype=dtype,count=accessor.count*widths[accessor.type],offset=offset).reshape(accessor.count,widths[accessor.type])
    def test_six_real_primitives_two_formats_independent_accessors(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ['box','plane','sphere','cylinder','cone','torus']:
                geometry=transform_geometry(primitive(kind).geometry(),conversion_matrix(source_basis='right-handed-z-up',unit_scale=.01,translation=[1,2,3]))
                wanted=geometry.meshes[0]
                for fmt in ['glb','gltf']:
                    with self.subTest(kind=kind,format=fmt):
                        root=Path(tmp)/(kind+fmt);export_mesh(geometry,root,format=fmt)
                        document=GLTF2().load(str(root/('model.'+fmt)))
                        self.assertEqual(document.asset.version,'2.0')
                        part=document.meshes[0].primitives[0]
                        positions=self.array(document,part.attributes.POSITION,root)
                        normals=self.array(document,part.attributes.NORMAL,root)
                        indices=self.array(document,part.indices,root).reshape(-1,3)
                        self.assertEqual(len(positions),len(wanted.positions))
                        np.testing.assert_allclose(positions,wanted.positions,rtol=1e-6,atol=1e-6)
                        np.testing.assert_array_equal(indices,wanted.triangles)
                        np.testing.assert_allclose(normals,wanted.attributes['NORMAL'],atol=1e-6)
                        self.assertEqual(document.meshes[0].name,wanted.name)
                        self.assertEqual(document.materials[part.material].pbrMetallicRoughness.metallicFactor,0)
                        self.assertEqual(len(document.nodes),1)

if __name__=='__main__':unittest.main()
