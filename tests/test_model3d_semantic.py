from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_primitives import editable_mesh,primitive
from model3d_semantic import *
from model3d_semantic_evidence import *
from model3d_semantic_io import export_asset,load_asset,save_asset
from rig_models import Geometry
from rig_errors import RigError,atomic_json,read_json,sha256


def translation(x=0,y=0,z=0):
    matrix_=np.eye(4);matrix_[:3,3]=[x,y,z];return matrix_


def fixture():
    body=primitive('box',dict(width=1,height=2,depth=1),name='body').mesh
    head=primitive('box',dict(width=.5,height=.5,depth=.5),name='head').mesh
    asset=create_asset(Geometry([body,head],materials=[{'pbrMetallicRoughness':dict(baseColorFactor=[.2,.4,.6,1],metallicFactor=0,roughnessFactor=.65)}]),asset_id='fixture')
    asset=reparent_part(asset,'head','body')
    return transform_part(asset,'head',translation(y=1.5))


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.asset=fixture()

    def test_transform_hierarchy_reparent_and_original_immutable(self):
        source=deepcopy(self.asset);before=part_signatures(source)
        moved=transform_part(self.asset,'body',translation(x=3))
        np.testing.assert_allclose(world_matrices(moved)['head'][:3,3],[3,1.5,0])
        detached=reparent_part(moved,'head',None)
        np.testing.assert_allclose(evaluated_geometry(moved).by_id()['head'].positions,evaluated_geometry(detached).by_id()['head'].positions)
        self.assertEqual(part_signatures(self.asset),before)
        grouped=group_parts(self.asset,'character-root',['body'])
        self.assertEqual(grouped.parts['body']['parent'],'character-root')
        self.assertEqual(grouped.parts['character-root']['meshes'],[])
        np.testing.assert_allclose(evaluated_geometry(grouped).by_id()['head'].positions,evaluated_geometry(self.asset).by_id()['head'].positions)
        with self.assertRaises(RigError):group_parts(self.asset,'bad-group',['body'],parent='head')
        with self.assertRaises(RigError):reparent_part(self.asset,'body','head')
        self.assertEqual(part_signatures(self.asset),before)

    def test_parent_hiding_invalidates_child_only_bound_derivative(self):
        tracked=register_derivative(self.asset,'head-preview',['head'],'a'*64)
        tracked=register_derivative(tracked,'body-preview',['body'],'b'*64)
        head_hidden=set_visibility(tracked,'head',False)
        self.assertTrue(head_hidden.derivatives['head-preview']['stale'])
        self.assertFalse(head_hidden.derivatives['body-preview']['stale'])
        self.assertEqual([m.id for m in evaluated_geometry(head_hidden).meshes],['body'])
        parent_hidden=set_visibility(tracked,'body',False)
        self.assertTrue(parent_hidden.derivatives['head-preview']['stale'])
        with self.assertRaises(RigError):evaluated_geometry(parent_hidden)

    def test_duplicate_subtree_keeps_native_arrays_and_separate_ids(self):
        linked=label_part(self.asset,'body',semantic='torso',attachments=[dict(id='head-anchor',point=[0,1,0],target='head')])
        duplicate=duplicate_part(linked,'body','copy')
        self.assertEqual(len(duplicate.parts),4)
        self.assertEqual(duplicate.parts['copy:head']['parent'],'copy')
        self.assertEqual(duplicate.parts['copy']['attachments'][0]['target'],'copy:head')
        self.assertEqual(duplicate.parts['body']['attachments'][0]['target'],'head')
        self.assertEqual(duplicate.geometry.vertex_count,self.asset.geometry.vertex_count*2)
        duplicate.geometry.by_id()['copy:body'].positions[0,0]=99
        self.assertNotEqual(self.asset.geometry.by_id()['body'].positions[0,0],99)
        with self.assertRaises(RigError):duplicate_part(self.asset,'body','body')

    def test_split_and_combine_actual_mesh_preserve_attributes(self):
        split=split_part(self.asset,'head',{'front':list(range(6)),'back':list(range(6,12))})
        self.assertEqual(split.parts['head']['meshes'],[])
        self.assertEqual(split.geometry.triangle_count,self.asset.geometry.triangle_count)
        combined=combine_parts(split,['front','back'],'joined')
        self.assertEqual(combined.parts['joined']['parent'],'head')
        self.assertEqual(combined.geometry.triangle_count,self.asset.geometry.triangle_count)
        source=evaluated_geometry(self.asset).by_id()['head'];result=evaluated_geometry(combined).by_id()['joined:mesh']
        np.testing.assert_allclose(np.sort(source.positions[source.triangles].reshape(-1,3),axis=0),
                                   np.sort(result.positions[result.triangles].reshape(-1,3),axis=0))
        self.assertIn('NORMAL',result.attributes)
        with self.assertRaises(RigError):split_part(self.asset,'head',{'a':[0],'b':[0]})

    def test_replace_and_material_change_are_versioned_and_selective(self):
        tracked=register_derivative(self.asset,'body-only',['body'],'c'*64)
        material={'pbrMetallicRoughness':dict(baseColorFactor=[1,0,0,1],metallicFactor=0,roughnessFactor=.65)}
        changed=change_material(tracked,'head',material)
        self.assertFalse(changed.derivatives['body-only']['stale'])
        self.assertEqual(changed.geometry.by_id()['body'].material,0)
        self.assertEqual(changed.geometry.by_id()['head'].material,1)
        replacement=primitive('sphere',dict(radius=.25,segments=8,rings=4),name='new-head').mesh
        replaced=replace_part(changed,'head',[replacement])
        self.assertEqual(replaced.parts['head']['meshes'],['new-head'])
        self.assertGreater(replaced.version,self.asset.version)
        self.assertEqual(self.asset.parts['head']['meshes'],['head'])

    def test_rigid_pose_moves_geometry_and_catalog_mapping_is_not_animation(self):
        posed=define_pose(self.asset,'tilt',{'head':translation(x=.4,y=1.5)})
        mapped=map_states(posed,{'idle':'tilt','running':None})
        applied=apply_pose(mapped,'tilt')
        self.assertAlmostEqual(world_matrices(applied)['head'][0,3],.4)
        self.assertEqual(mapped.states['running']['status'],'unavailable')
        self.assertFalse(mapped.states['idle']['animation'])
        with self.assertRaises(RigError):map_states(posed,{'made-up-state':'tilt'})

    def test_frozen_identity_measures_ratios_color_and_rejects_forbidden_edits(self):
        source=self.root/'identity.json';source.write_text(json.dumps({'schema_version':1,'proportions':{'head_to_body':.5},'palette':{'body':'#336699'}}))
        contract,data=identity_contract(source,required_parts=['head','body'],forbidden_parts=['wings'],
            ratios=[dict(numerator='head',denominator='body',axis=0,minimum=.49,maximum=.51)],
            colors=[dict(part='head',rgba=[.2,.4,.6,1],maximum_error=.001)])
        bound=bind_identity(self.asset,contract,data)
        self.assertTrue(validate_identity(bound)['ok'])
        self.assertEqual(contract['ratios'][0]['scope'],'owned-meshes')
        scale=np.diag([2,1,1,1])@translation(y=1.5)
        with self.assertRaises(RigError) as caught:transform_part(bound,'head',scale)
        self.assertEqual(caught.exception.report['code'],'MODEL3D_IDENTITY')
        with self.assertRaises(RigError):change_material(bound,'head',{'pbrMetallicRoughness':{'baseColorFactor':[1,0,0,1]}})
        colored=deepcopy(bound);colored.geometry.by_id()['head'].attributes['COLOR_0']=np.tile([1,0,0,1],(8,1))
        self.assertFalse(validate_identity(colored)['ok'])
        self.assertEqual(hashlib.sha256(bound.evidence[contract['source_sha256']]).hexdigest(),contract['source_sha256'])

    def test_style_uses_real_registry_snapshot_and_explicit_adaptation(self):
        styled=bind_style(self.asset,'clay',adaptations=dict(geometry='Keep supplied geometry.',material='Use authored rough clay PBR.',texture='No inferred markings.',render='Review under shared lighting.'))
        self.assertEqual(styled.style['snapshot']['resolved_id'],'clay')
        self.assertFalse(styled.style['geometry_generated'])
        with self.assertRaises(RigError):bind_style(self.asset,'clay',adaptations={})

    def test_snapshot_and_hierarchical_glb_gltf_roundtrip(self):
        saved=save_asset(self.asset,self.root/'revision1');loaded=load_asset(self.root/'revision1')
        self.assertEqual(part_signatures(loaded),part_signatures(self.asset))
        moved=transform_part(loaded,'head',translation(x=.2,y=1.5));save_asset(moved,self.root/'revision2')
        self.assertEqual(load_asset(self.root/'revision1').version,self.asset.version)
        for format_ in ('glb','gltf'):
            receipt=export_asset(moved,self.root/format_,format=format_)
            self.assertEqual(receipt['vertices'],16);self.assertTrue(receipt['independent_reimport'])
            from rig_gltf import read_gltf
            document=read_gltf(self.root/format_/('model.'+format_))
            nodes=document.json['nodes'];part_nodes={n['extras']['hatchSemantic']['id']:n for n in nodes if 'hatchSemantic' in n.get('extras',{})}
            head_index=next(i for i,n in enumerate(nodes) if n is part_nodes['head'])
            self.assertIn(head_index,part_nodes['body']['children'])
            self.assertEqual(part_nodes['head']['matrix'][12],.2)
        with self.assertRaises(RigError):save_asset(moved,self.root/'revision1')

    def test_snapshot_tamper_and_export_destination_race_fail_closed(self):
        save_asset(self.asset,self.root/'saved');manifest=read_json(self.root/'saved/semantic.json')
        (self.root/'saved'/manifest['geometry']['data']['path']).write_bytes(b'broken')
        with self.assertRaises(RigError):load_asset(self.root/'saved')
        import model3d_semantic_io as io
        original=io._rename_no_replace;destination=self.root/'raced'
        def race(stage,target):
            Path(target).mkdir();(Path(target)/'owned').write_bytes(b'other writer');original(stage,target)
        with patch.object(io,'_rename_no_replace',side_effect=race):
            with self.assertRaises(FileExistsError):export_asset(self.asset,destination)
        self.assertEqual((destination/'owned').read_bytes(),b'other writer')

    def test_decomposition_mapping_retains_actual_original_bytes_without_depth_claim(self):
        from decomposition_fixtures import make_fixture,FIXTURE_ORIGIN
        from character_decomposition import create_decomposition
        source=make_fixture(self.root/'drawing','cartoon','PNG')
        run=self.root/'decomposition';manifest=create_decomposition(source.source,run,annotations=source.annotation,
                                                                    fixture=True,origin=FIXTURE_ORIGIN)
        component=next(c['id'] for c in manifest['components'] if c['category']!='unassigned')
        mapped=map_decomposition(self.asset,run,{'head':component})
        record=mapped.parts['head']['evidence'][0]
        self.assertEqual(record['component_id'],component)
        self.assertIn('2D evidence only',record['spatial_mapping'])
        self.assertEqual(mapped.evidence[manifest['source']['sha256']],Path(source.source).read_bytes())
        save_asset(mapped,self.root/'mapped');restored=load_asset(self.root/'mapped')
        self.assertEqual(restored.parts['head']['evidence'],mapped.parts['head']['evidence'])

    def test_invalid_hierarchy_normal_precision_and_budget_fail_before_publication(self):
        original=part_signatures(self.asset)
        with self.assertRaises(RigError):transform_part(self.asset,'head',np.zeros((4,4)))
        with patch('model3d_semantic.DEFAULT_LIMITS',{**DEFAULT_LIMITS,'vertices':20}):
            with self.assertRaises(RigError):duplicate_part(self.asset,'body','overflow')
        points=[[0.,0,0],[1,0,0],[0,1,0],[2,0,0],[2+1e-8,0,0],[2,1e-8,0]]
        small=create_asset(editable_mesh(points,[[0,1,2],[3,4,5]]).geometry())
        with self.assertRaises(RigError):export_asset(small,self.root/'collapsed')
        self.assertFalse((self.root/'collapsed').exists())
        self.assertEqual(part_signatures(self.asset),original)

    def test_versioned_uv_texture_and_projection_wrappers_preserve_local_hierarchy(self):
        from model3d_semantic_surfaces import generate_asset_uv,texture_asset,project_asset
        from model3d_materials import procedural_texture
        from test_model3d_surfaces import source,projection,world
        mapped=generate_asset_uv(self.asset,method='box',resolution=(256,256),padding=2)
        textured=texture_asset(mapped,0,procedural_texture(width=16,height=16,seed=7))
        self.assertGreater(textured.version,self.asset.version)
        self.assertEqual(textured.parts['head']['parent'],'body')
        before=textured.geometry.by_id()['head'].positions.copy()
        view=dict(image=source((255,0,0,255)),camera_world=world(),projection=projection(),
                  calibration=dict(status='supplied',reference='Explicit synthetic camera.'))
        projected=project_asset(textured,[view])
        np.testing.assert_array_equal(projected.geometry.by_id()['head'].positions,before)
        self.assertIn('COLOR_0',projected.geometry.by_id()['head'].attributes)
        report=export_asset(projected,self.root/'surface-export')
        self.assertTrue(report['independent_reimport'])
        restored=load_asset(self.root/'surface-export/native')
        self.assertEqual(part_signatures(restored),part_signatures(projected))
        self.assertEqual(len(restored.geometry.images),1)

    def test_cli_expected_invalid_specs_leave_no_destination(self):
        from model3d_semantic_cli import main
        from contextlib import redirect_stdout,redirect_stderr
        import io
        save_asset(self.asset,self.root/'saved');bad=self.root/'bad-spec.json';atomic_json(bad,{'part_id':[],'visible':True})
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()) as error:
            code=main(['edit','--source',str(self.root/'saved'),'--operation','visibility','--spec',str(bad),'--destination',str(self.root/'bad')])
        self.assertEqual(code,2);self.assertNotIn('Traceback',error.getvalue())
        self.assertFalse((self.root/'bad').exists())
        from model3d_semantic_cli import edit
        source=create_asset(primitive('box',name='replacement').geometry(),fixture=True)
        export_asset(source,self.root/'fixture-source')
        replaced=edit(self.asset,'replace',dict(part_id='head',source=str(self.root/'fixture-source/model.glb'),material_slot=0))
        self.assertTrue(replaced.fixture)
        export_asset(replaced,self.root/'fixture-result')
        from rig_import import load_character_model
        self.assertTrue(load_character_model(self.root/'fixture-result/model.glb').report['fixture'])

    def test_snapshot_parse_uses_captured_original_after_source_file_replacement(self):
        import rig_models
        from rig_models import save_geometry
        save_asset(self.asset,self.root/'saved');manifest=read_json(self.root/'saved/semantic.json')
        original_path=self.root/'saved'/manifest['geometry']['data']['path']
        changed=deepcopy(self.asset.geometry);changed.by_id()['body'].positions=changed.by_id()['body'].positions.copy()+[10,0,0]
        descriptor=save_geometry(self.root/'changed',changed);different=(self.root/'changed'/descriptor['data']['path']).read_bytes()
        native_read=rig_models._read_bundle
        def race(private_path,limits):
            original_path.write_bytes(different)
            self.assertNotEqual(Path(private_path),original_path)
            return native_read(private_path,limits)
        with patch.object(rig_models,'_read_bundle',side_effect=race):loaded=load_asset(self.root/'saved')
        np.testing.assert_array_equal(loaded.geometry.by_id()['body'].positions,self.asset.geometry.by_id()['body'].positions)
        self.assertNotEqual(sha256(original_path),manifest['geometry']['data']['sha256'])

    def test_malformed_self_hashed_snapshot_and_opaque_material_extras(self):
        save_asset(self.asset,self.root/'saved');original=read_json(self.root/'saved/semantic.json')
        for mutate in [lambda v:v.update(geometry={}),lambda v:v.update(parts=[]),
                       lambda v:v['parts']['body'].update(attachments=[dict(id='bad',point=[0,0,0],target=[])]),
                       lambda v:v.update(history={})]:
            value=deepcopy(original);mutate(value);value['sha256']=object_hash({k:v for k,v in value.items() if k!='sha256'})
            atomic_json(self.root/'saved/semantic.json',value)
            with self.assertRaises(RigError):load_asset(self.root/'saved')
        asset=deepcopy(self.asset);asset.geometry.materials[0]['extras']={'customTexture':{'index':999}}
        moved=transform_part(asset,'head',translation(y=2))
        self.assertEqual(moved.geometry.materials[0]['extras'],{'customTexture':{'index':999}})

    def test_hierarchy_depth_scalar_types_and_preallocation_budget(self):
        from model3d_semantic import SemanticAsset,world_matrices
        parts={};parent=None
        for i in range(129):
            key='part'+str(i);parts[key]=dict(parent=parent,matrix=np.eye(4).tolist());parent=key
        with self.assertRaises(RigError):world_matrices(SemanticAsset(self.asset.geometry,parts))
        mixed=np.eye(4).tolist();mixed[0][0]=True
        with self.assertRaises(RigError):transform_part(self.asset,'head',mixed)
        with patch('model3d_semantic.DEFAULT_LIMITS',{**DEFAULT_LIMITS,'memory_bytes':1}),\
                patch('model3d_semantic.deepcopy',side_effect=AssertionError('copied before budget')):
            with self.assertRaises(RigError):create_asset(self.asset.geometry)


if __name__=='__main__':unittest.main()
