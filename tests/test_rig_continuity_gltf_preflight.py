"""Canonical metadata admission against actual captured GLB input."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from rig_gltf import GltfBuilder,GltfDocument,read_gltf,preflight_gltf
from rig_import import load_character_model
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError


class GltfPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def document(self,*,instances=2,accessor_copies=0,vertices=None,node_transform=None):
        builder=GltfBuilder()
        position=builder.accessor(vertices if vertices is not None else [[0,0,0],[1,0,0],[0,1,0]],'VEC3')
        builder.json.update(nodes=[dict(mesh=0) for _ in range(instances+1)],
            meshes=[dict(primitives=[dict(attributes=dict(POSITION=position))])],scenes=[dict(nodes=list(range(instances)))])
        builder.json['nodes'][0].update(deepcopy(node_transform or {}))
        for _ in range(accessor_copies):builder.json['accessors'].append(deepcopy(builder.json['accessors'][0]))
        path=self.root/'actual.glb';builder.write(path);size=path.stat().st_size
        return read_gltf(path),size

    def animated_document(self, *, primitives=1, keys=2, width=1, cubic=False, channels=1):
        builder=GltfBuilder()
        position=builder.accessor([[0,0,0],[1,0,0],[0,1,0]],'VEC3')
        times=builder.accessor([[float(i)] for i in range(keys)],'SCALAR')
        values=builder.accessor([[0.] for _ in range(keys*width*(3 if cubic else 1))],'SCALAR')
        primitive=dict(attributes=dict(POSITION=position), targets=[dict(POSITION=position) for _ in range(width)])
        builder.json.update(nodes=[dict(mesh=0) for _ in range(channels)],
            meshes=[dict(primitives=[deepcopy(primitive) for _ in range(primitives)])],
            scenes=[dict(nodes=list(range(channels)))],
            animations=[dict(samplers=[dict(input=times,output=values,interpolation='CUBICSPLINE' if cubic else 'LINEAR')],
                channels=[dict(sampler=0,target=dict(node=i,path='weights')) for i in range(channels)])])
        path=self.root/'animation.glb';builder.write(path)
        return read_gltf(path),path.stat().st_size

    def test_real_200_track_animation_rejected_before_accessor_decode(self):
        document,size=self.animated_document(primitives=200,keys=3000)
        with patch.object(document,'accessor',side_effect=AssertionError('decoded before admission')) as decode:
            with self.assertRaises(RigError) as caught:
                preflight_gltf(document,encoded_bytes=size,limits=dict(memory_bytes=5*1024**2))
        self.assertEqual(caught.exception.report['code'],'RIG_LIMIT_MEMORY')
        decode.assert_not_called()

    def test_cumulative_shared_sampler_cubic_and_exact_key_boundary(self):
        document,size=self.animated_document(primitives=2,keys=3,width=2,cubic=True,channels=2)
        with patch.object(document,'accessor',side_effect=AssertionError('decoded before admission')) as decode:
            result=preflight_gltf(document,encoded_bytes=size,limits=dict(keyframes=12))
            self.assertEqual((result['animation_tracks'],result['animation_keyframes']),(4,12))
            with self.assertRaises(RigError) as caught:
                preflight_gltf(document,encoded_bytes=size,limits=dict(keyframes=11))
            self.assertEqual(caught.exception.report['code'],'RIG_LIMIT_KEYFRAMES')
        decode.assert_not_called()
        linear,linear_size=self.animated_document(primitives=2,keys=3,width=2,channels=2)
        linear_result=preflight_gltf(linear,encoded_bytes=linear_size)
        self.assertGreater(result['estimated_memory_bytes'],linear_result['estimated_memory_bytes'])
        boundary=linear_result['estimated_memory_bytes']
        self.assertEqual(preflight_gltf(linear,encoded_bytes=linear_size,
                                       limits=dict(memory_bytes=boundary))['estimated_memory_bytes'],boundary)
        with self.assertRaises(RigError) as caught:
            preflight_gltf(linear,encoded_bytes=linear_size,limits=dict(memory_bytes=boundary-1))
        self.assertEqual(caught.exception.report['code'],'RIG_LIMIT_MEMORY')

    def test_animation_time_metadata_rejected_before_import_decodes_accessors(self):
        for kind,component in [('VEC2',5126),('SCALAR',5123)]:
            with self.subTest(kind=kind,component=component):
                builder=GltfBuilder()
                position=builder.accessor([[0,0,0],[1,0,0],[0,1,0]],'VEC3')
                times=builder.accessor([[0,1],[2,3],[4,5]] if kind == 'VEC2' else [0,1,2],kind,component=component)
                values=builder.accessor([0.]*6,'SCALAR')
                builder.json.update(nodes=[dict(mesh=0)],meshes=[dict(primitives=[dict(
                    attributes=dict(POSITION=position),targets=[dict(POSITION=position)]*2)])],
                    scenes=[dict(nodes=[0])],animations=[dict(samplers=[dict(input=times,output=values)],
                    channels=[dict(sampler=0,target=dict(node=0,path='weights'))])])
                path=self.root/'invalid-time.glb';builder.write(path)
                with patch.object(GltfDocument,'accessor',side_effect=AssertionError('decoded before admission')) as decode:
                    with self.assertRaises(RigError) as caught:
                        load_character_model(path,limits=dict(DEFAULT_LIMITS,keyframes=3))
                self.assertEqual(caught.exception.report['code'],'RIG_GLTF_ACCESSOR')
                decode.assert_not_called()

    def test_animation_scalar_time_import_at_key_boundary(self):
        for cubic in (False,True):
            with self.subTest(cubic=cubic):
                document,_=self.animated_document(primitives=2,keys=3,width=2,cubic=cubic,channels=2)
                model=load_character_model(document.path,limits=dict(DEFAULT_LIMITS,keyframes=12))
                tracks=next(iter(model.clips.values()))['tracks']
                self.assertEqual(len(tracks),4)
                self.assertEqual(sum(len(track['times']) for track in tracks),12)
                self.assertEqual('in_tangents' in tracks[0],cubic)

    def test_counts_actual_selected_mesh_instances_and_restrictive_limits(self):
        document,size=self.document()
        result=preflight_gltf(document,encoded_bytes=size,limits=dict(vertices=6,triangles=2))
        self.assertEqual((result['vertices'],result['triangles'],result['mesh_instances'],result['active_nodes']),(6,2,2,2))
        self.assertLess(result['estimated_memory_bytes'],DEFAULT_LIMITS['memory_bytes'])
        self.assertEqual(result['method'],'native-gltf-load-conservative-v1')
        for limits,code in [(dict(vertices=5),'RIG_LIMIT_VERTICES'),(dict(triangles=1),'RIG_LIMIT_TRIANGLES')]:
            with self.subTest(limits=limits),self.assertRaises(RigError) as caught:
                preflight_gltf(document,encoded_bytes=size,limits=limits)
            self.assertEqual(caught.exception.report['code'],code)

    def test_aggregate_accessor_memory_rejects_individually_small_arrays(self):
        document,size=self.document(instances=1,accessor_copies=40,vertices=[[0,0,0]]*6000)
        self.assertLess(6000*3*8*4,16*1024**2)
        with self.assertRaises(RigError) as caught:
            preflight_gltf(document,encoded_bytes=size,limits=dict(memory_bytes=16*1024**2))
        self.assertEqual(caught.exception.report['code'],'RIG_LIMIT_MEMORY')

    def test_captured_document_never_reopens_source_path(self):
        document,size=self.document();document.path.unlink()
        result=preflight_gltf(document,encoded_bytes=size)
        self.assertEqual(result['vertices'],6)

    def test_malformed_counts_references_and_graphs_remain_typed(self):
        for value in (True,-1,1.5,'3',None):
            document,size=self.document();document.json['accessors'][0]['count']=value
            with self.subTest(value=value),self.assertRaises(RigError):
                preflight_gltf(document,encoded_bytes=size)
        for patch in [lambda d:d['nodes'][0].update(mesh=True),
                      lambda d:d['meshes'][0]['primitives'][0].update(attributes=[]),
                      lambda d:d['nodes'][0].update(children=[0])]:
            document,size=self.document();patch(document.json)
            with self.assertRaises(RigError):preflight_gltf(document,encoded_bytes=size)

    def test_runtime_caps_do_not_mutate_or_widen_document_policy(self):
        document,size=self.document();document.limits=dict(DEFAULT_LIMITS,vertices=6)
        original=deepcopy(document.limits)
        result=preflight_gltf(document,encoded_bytes=size,limits=dict(vertices=12))
        self.assertEqual(result['limits']['vertices'],6);self.assertEqual(document.limits,original)
        for invalid in (dict(vertices=True),dict(unknown=1)):
            with self.assertRaises(RigError):preflight_gltf(document,encoded_bytes=size,limits=invalid)
        for invalid in (0,-1,False,None):
            with self.assertRaises(RigError):preflight_gltf(document,encoded_bytes=invalid)

    def test_real_glb_text_transform_values_have_structured_diagnostics(self):
        for field,value in [('translation',['bad',0,0]),('scale',['bad',1,1]),
                            ('rotation',['bad',0,0,1]),('matrix',['bad']*16)]:
            with self.subTest(field=field):
                document,size=self.document(node_transform={field:value})
                source=document.path.read_bytes();metadata=deepcopy(document.json)
                with patch.object(document,'accessor',side_effect=AssertionError('preflight must not materialize accessors')) as materialize:
                    with self.assertRaises(RigError) as caught:
                        preflight_gltf(document,encoded_bytes=size)
                self.assertEqual(caught.exception.report['code'],'RIG_GLTF_PREFLIGHT')
                materialize.assert_not_called()
                self.assertEqual(document.path.read_bytes(),source)
                self.assertEqual(document.json,metadata)

    def test_real_glb_valid_transform_controls_preserve_sources_without_materialization(self):
        controls=[('translation',[1,2,3]),('scale',[1,2,3]),('rotation',[0,0,0,1]),
                  ('matrix',[1,0,0,0,0,1,0,0,0,0,1,0,1,2,3,1])]
        for field,value in controls:
            with self.subTest(field=field):
                document,size=self.document(node_transform={field:value})
                source=document.path.read_bytes();metadata=deepcopy(document.json)
                with patch.object(document,'accessor',side_effect=AssertionError('preflight must not materialize accessors')) as materialize:
                    result=preflight_gltf(document,encoded_bytes=size)
                self.assertEqual((result['vertices'],result['triangles']),(6,2))
                self.assertFalse(result['images_admitted'])
                materialize.assert_not_called()
                self.assertEqual(document.path.read_bytes(),source)
                self.assertEqual(document.json,metadata)

    def test_existing_native_transform_errors_keep_their_original_codes(self):
        for field,value,code in [('rotation',[0,0,0,0],'RIG_QUATERNION'),
                                 ('scale',[1,0,1],'RIG_SINGULAR_TRANSFORM')]:
            with self.subTest(field=field):
                document,size=self.document(node_transform={field:value})
                source=document.path.read_bytes()
                with patch.object(document,'accessor',side_effect=AssertionError('preflight must not materialize accessors')) as materialize:
                    with self.assertRaises(RigError) as caught:
                        preflight_gltf(document,encoded_bytes=size)
                self.assertEqual(caught.exception.report['code'],code)
                materialize.assert_not_called()
                self.assertEqual(document.path.read_bytes(),source)


if __name__=='__main__':unittest.main()
