"""Real procedural assembly geometry, independent glTF inspection and safe failures."""
from copy import deepcopy
import base64
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from pygltflib import GLTF2
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_assemblies import parse_description, generate, export_assembly, revise, material, attach
from model3d_assemblies_contracts import validate_ir, schema
from model3d_assemblies_presets import known_recipes
from model3d_assemblies_shapes import build_shape, parameters
from model3d_topology import topology_report
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError


class Assemblies(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()

    def independent_array(self,doc,index):
        a=doc.accessors[index];v=doc.bufferViews[a.bufferView];b=doc.buffers[v.buffer]
        data=doc.binary_blob() if b.uri is None else base64.b64decode(b.uri.split(',',1)[1])
        widths={'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4}
        dtypes={5126:'<f4',5125:'<u4',5123:'<u2'}
        self.assertFalse(v.byteStride)
        return np.frombuffer(data,dtype=dtypes[a.componentType],count=a.count*widths[a.type],
                             offset=(v.byteOffset or 0)+(a.byteOffset or 0)).reshape(a.count,widths[a.type])

    def test_all_named_recipes_real_exports_independent_structure_and_extent(self):
        for name in known_recipes():
            with self.subTest(recipe=name):
                ir=parse_description(name+' seed=7 color=red');expected=generate(ir)
                folder=self.root/name;report=export_assembly(ir,folder,format='gltf' if name=='mug' else 'glb')
                doc=GLTF2().load(str(folder/('model.'+report['format'])))
                self.assertEqual(len(doc.meshes),len(ir['parts']));self.assertEqual(len(doc.nodes),len(ir['parts']))
                self.assertEqual(len(report['parts']),len(ir['parts']))
                by_node={n.name:i for i,n in enumerate(doc.nodes)}
                parents={child:i for i,node in enumerate(doc.nodes) for child in node.children}
                for part in ir['parts']:
                    i=by_node[part['id']];node=doc.nodes[i];mesh=doc.meshes[node.mesh]
                    self.assertEqual(mesh.name,part['id']);self.assertEqual(node.extras['semantic'],part['semantic'])
                    self.assertEqual(node.extras['attachments'],part['attachments'])
                    self.assertEqual(node.translation,part['transform']['translation'])
                    if part['parent'] is None:self.assertNotIn(i,parents)
                    else:self.assertEqual(parents[i],by_node[part['parent']])
                    primitive=mesh.primitives[0]
                    xyz=self.independent_array(doc,primitive.attributes.POSITION)
                    faces=self.independent_array(doc,primitive.indices).reshape(-1,3)
                    wanted=expected.editable[part['id']].mesh
                    np.testing.assert_allclose(xyz,wanted.positions,rtol=1e-6,atol=1e-7)
                    np.testing.assert_array_equal(faces,wanted.triangles)
                    self.assertEqual(primitive.material,0)
                self.assertEqual(doc.materials[0].pbrMetallicRoughness.baseColorFactor,[.8,.08,.06,1.])
                dims=np.asarray(report['bounds'][1])-np.asarray(report['bounds'][0])
                self.assertTrue(np.all(dims>.01));self.assertFalse(report['provider_calls'])
                for filename,record in report['files'].items():
                    data=(folder/filename).read_bytes();self.assertEqual(len(data),record['bytes'])
                    self.assertEqual(hashlib.sha256(data).hexdigest(),record['sha256'])
                self.assertEqual(json.loads((folder/'assembly.json').read_text()),ir)
                self.assertEqual(set(json.loads((folder/'editable.json').read_text())),{p['id'] for p in ir['parts']})

    def test_determinism_seed_and_exact_export_bytes(self):
        ir=parse_description('rock seed=99 color=#224466')
        a=generate(ir);b=generate(deepcopy(ir))
        for x,y in zip(a.geometry.meshes,b.geometry.meshes):
            np.testing.assert_array_equal(x.positions,y.positions);np.testing.assert_array_equal(x.triangles,y.triangles)
        export_assembly(ir,self.root/'a');export_assembly(ir,self.root/'b')
        for filename in ('model.glb','assembly.json','editable.json','acceptance.json'):
            self.assertEqual((self.root/'a'/filename).read_bytes(),(self.root/'b'/filename).read_bytes())
        other=generate(parse_description('rock seed=100 color=#224466'))
        self.assertTrue(any(not np.array_equal(x.positions,y.positions) for x,y in zip(a.geometry.meshes,other.geometry.meshes)))

    def test_hollow_tube_capsule_curve_and_extrusion_actual_closed_surfaces(self):
        forms=[('tube',dict(radius=.5,inner_radius=.3,height=1.)),
               ('capsule',dict(radius=.25,height=1.)),
               ('curve',dict(points=[[0,0,0],[0,1,0],[.5,2,0]],radius=.1)),
               ('extrusion',dict(polygon=[[-1,-1],[1,-1],[1,1],[0,.5],[-1,1]],height=.5))]
        for form,params in forms:
            with self.subTest(form=form):
                item=build_shape(form,params,name=form,limits=DEFAULT_LIMITS);mesh=item.mesh
                report=topology_report(mesh)
                self.assertFalse(report['boundary_edges']);self.assertFalse(report['nonmanifold_edges'])
                self.assertFalse(report['inconsistent_edges']);self.assertFalse(report['degenerate_faces'])
                p=mesh.positions;t=mesh.triangles
                volume=np.einsum('ij,ij->',p[t[:,0]],np.cross(p[t[:,1]],p[t[:,2]]))/6
                self.assertGreater(volume,0)
                if form=='tube':
                    radial=np.linalg.norm(p[:,[0,2]],axis=1)
                    self.assertAlmostEqual(radial.min(),.3);self.assertAlmostEqual(radial.max(),.5)
                    expected=16*math.sin(2*math.pi/16)*(.5**2-.3**2)/2
                    self.assertAlmostEqual(volume,expected)
                if form=='capsule':
                    np.testing.assert_allclose(p.max(axis=0)-p.min(axis=0),[.5,1.,.5])
                    expected=math.pi*.25**2*.5+4*math.pi*.25**3/3
                    self.assertLess(abs(volume-expected)/expected,.05)

    def test_copy_revision_and_parent_transform_world_geometry(self):
        ir=parse_description('robot');original=deepcopy(ir)
        root=ir['parts'][0];child=next(p for p in ir['parts'] if p['parent']==root['id'])
        transform=deepcopy(root['transform']);transform['rotation']=[0,0,math.sqrt(.5),math.sqrt(.5)]
        changed=revise(ir,root['id'],transform=transform)
        self.assertEqual(ir,original)
        for a,b in zip(ir['parts'][1:],changed['parts'][1:]):self.assertEqual(a,b)
        assembly=generate(changed)
        point=np.r_[assembly.editable[child['id']].mesh.positions[0],1.]
        expected=(assembly.world[child['id']]@point)[:3]
        np.testing.assert_allclose(assembly.geometry.by_id()[child['id']].positions[0],expected)
        export_assembly(changed,self.root/'rotated')

    def test_named_anchor_attachment_changes_actual_geometry_and_preserves_input(self):
        ir=parse_description('robot');before=deepcopy(ir)
        changed=attach(ir,'head','torso',parent_anchor='neck')
        self.assertEqual(ir,before)
        result=generate(changed)
        parent=next(p for p in changed['parts'] if p['id']=='torso')
        expected=result.world['torso']@np.r_[parent['attachments']['neck'],1.]
        actual=result.world['head']@np.array([0.,0,0,1])
        np.testing.assert_allclose(actual,expected)
        with self.assertRaises(RigError):attach(ir,'head','torso',own_anchor='unknown')
        with self.assertRaises(RigError):attach(ir,'torso','head')

    def test_multiple_material_assignments_and_actual_values(self):
        ir=parse_description('mug');ir['materials'].append(material('handle-material','metal','yellow'))
        handle=next(p for p in ir['parts'] if 'handle' in p['id'])
        ir=revise(ir,handle['id'],material_id='handle-material')
        folder=self.root/'materials';export_assembly(ir,folder)
        doc=GLTF2().load(str(folder/'model.glb'))
        mesh=next(m for m in doc.meshes if m.name==handle['id'])
        self.assertEqual(mesh.primitives[0].material,1)
        self.assertEqual(doc.materials[1].pbrMetallicRoughness.metallicFactor,1.)
        self.assertEqual(doc.materials[1].pbrMetallicRoughness.baseColorFactor,[.95,.7,.05,1.])

    def test_language_and_schema_boundaries(self):
        for text in ['photorealistic dragon flying','robot red','robot color=red wings=true','robot seed=-1',
                     'robot seed=4 seed=3','table height=nan','mug height=0m','staircase steps=100000000',
                     'robot height=2m trailing','robot material=unobtainium','robot color=orange','robot steps=3']:
            with self.subTest(text=text),self.assertRaises(RigError):parse_description(text)
        ir=parse_description('table height=200cm width=1000mm depth=1m')
        self.assertTrue(ir['parts']);self.assertEqual(schema()['additionalProperties'],False)
        for mutation in [lambda x:x.update(extra=True),lambda x:x.update(schema_version=2),
                         lambda x:x.update(seed=True),lambda x:x['parts'][0]['transform'].update(rotation=[0,0,0,0]),
                         lambda x:x['parts'][0]['parameters'].update(radius=1e12),
                         lambda x:x['materials'][0].update(roughness=float('nan'))]:
            invalid=deepcopy(ir);mutation(invalid)
            with self.assertRaises(RigError):validate_ir(invalid)
        for form,value in [('capsule',dict(radius=1,height=1)),('tube',dict(radius=1,inner_radius=2)),
                           ('curve',dict(points=[[0,0,0],[0,0,0]])),('extrusion',dict(polygon=[[0,0],[1,1],[0,1],[1,0]]))]:
            with self.assertRaises(RigError):build_shape(form,value,name='invalid',limits=DEFAULT_LIMITS)

    def test_missing_references_cycles_and_preallocation_budgets(self):
        ir=parse_description('robot');saved=deepcopy(ir)
        for mutation in [lambda x:x['parts'][0].update(parent=x['parts'][-1]['id']),
                         lambda x:x['parts'][0].update(parent='missing'),
                         lambda x:x['parts'][0].update(material='missing'),
                         lambda x:x['parts'][1].update(id=x['parts'][0]['id'])]:
            invalid=deepcopy(ir);mutation(invalid)
            with self.assertRaises(RigError):generate(invalid)
        with patch('model3d_assemblies_shapes.build_shape',side_effect=AssertionError('allocated')):
            for limits in [dict(vertices=3),dict(triangles=1),dict(memory_bytes=64),dict(parts=1)]:
                with self.assertRaises(RigError):generate(ir,limits=limits)
        self.assertEqual(saved,ir)

    def test_float32_collapsed_feature_cannot_publish_as_verified(self):
        ir=parse_description('table');part=deepcopy(ir['parts'][0])
        part.update(primitive='extrusion',parent=None,parameters=dict(
            polygon=[[999.99999,0],[1000,0],[1000,1],[999.99999,1]],height=1.))
        part['transform']=dict(translation=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1])
        ir['parts']=[part]
        source=generate(ir).geometry.meshes[0]
        self.assertFalse(topology_report(source)['degenerate_faces'])
        with self.assertRaises(RigError) as caught:export_assembly(ir,self.root/'collapsed')
        self.assertEqual(caught.exception.report['code'],'MODEL3D_ASSEMBLY_ROUNDTRIP')
        self.assertFalse((self.root/'collapsed').exists())
        self.assertFalse(list(self.root.glob('.model3d-assembly-*')))

    def test_publication_failures_preserve_destinations_and_remove_only_stage(self):
        ir=parse_description('mug');destination=self.root/'exists';destination.mkdir();(destination/'user').write_bytes(b'keep')
        with self.assertRaises(RigError):export_assembly(ir,destination)
        self.assertEqual((destination/'user').read_bytes(),b'keep')
        import atomic_publish
        original=atomic_publish._rename_no_replace
        def racing(stage,target):
            target.mkdir();(target/'racer').write_bytes(b'racing-owner')
            return original(stage,target)
        with patch.object(atomic_publish,'_rename_no_replace',side_effect=racing):
            with self.assertRaises((RigError,OSError)):export_assembly(ir,self.root/'race')
        self.assertEqual((self.root/'race/racer').read_bytes(),b'racing-owner')
        self.assertFalse(list(self.root.glob('.model3d-assembly-*')))
        invalid=deepcopy(ir);invalid['parts'][0]['transform']['scale']=[1e12,1,1]
        with self.assertRaises(RigError):export_assembly(invalid,self.root/'invalid')
        self.assertFalse((self.root/'invalid').exists())

    def test_cli_expected_failures_are_structured_and_capabilities_truthful(self):
        script=str(Path(__file__).resolve().parents[1]/'scripts/model3d_assemblies.py')
        result=subprocess.run([sys.executable,script,'create','--description','unreal dragon',
                               '--destination',str(self.root/'out')],capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertNotIn('Traceback',result.stderr)
        self.assertEqual(json.loads(result.stderr)['error']['code'],'MODEL3D_ASSEMBLY_LANGUAGE')
        self.assertFalse((self.root/'out').exists())
        result=subprocess.run([sys.executable,script,'capabilities'],capture_output=True,text=True,check=True)
        caps=json.loads(result.stdout);self.assertFalse(caps['network_calls'])
        self.assertIn('free-form-inference',caps['unsupported']);self.assertIn('mug',caps['recipes'])

if __name__=='__main__':unittest.main()
