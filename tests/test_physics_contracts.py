"""Physics configuration tests: no solver or production approval is claimed."""
import copy
from fractions import Fraction
import hashlib
import importlib.abc
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from physics_contracts import (COORDINATES, DEFAULT_GRAVITY, PHYSICAL_UNITS, Diagnostic,
                               PhysicsError, combine_materials, definitions, document_hash,
                               generate_schemas, load_contract, material_preset, schemas, validate_contract)


def transform():
    return dict(translation=[0, 0, 0], rotation=[0, 0, 0, 1])


def asset(identifier='mesh', kind='geometry'):
    return dict(id=identifier, version='1.0.0', path='assets/' + identifier + '.json',
                sha256='0'*64, kind=kind)


def fixture():
    material = dict(id='rubber', density=1100, static_friction=.8, dynamic_friction=.6,
                    restitution=.5, linear_damping=.04, angular_damping=.1,
                    collision_margin=.001, pbr_material=None, combination='bullet-product')
    body = dict(id='ball', category='rigid-body', mode='dynamic', authority='physics',
                parent=None, asset=None, transform=transform(),
                mass=dict(kilograms=1, center_of_mass=[0, 0, 0], inertia=[[.1, 0, 0], [0, .1, 0], [0, 0, .1]]),
                linear_velocity=[0, 0, 0], angular_velocity=[0, 0, 0],
                colliders=[dict(id='ball-collider', transform=transform(),
                                shape=dict(type='sphere', radius=.5), material='rubber',
                                group=1, mask=4294967295, sensor=False)], animation=None)
    return dict(schema_version=1, kind='hatch-3d-physics-world', id='contract-fixture',
                version='1.0.0', coordinate_system=copy.deepcopy(COORDINATES),
                physical_units=copy.deepcopy(PHYSICAL_UNITS), scene=asset('scene', 'scene'),
                fps=dict(numerator=24, denominator=1), frame_range=dict(start=0, end=24),
                gravity=list(DEFAULT_GRAVITY),
                solver=dict(backend='blender', adapter_version='1.0.0', fixed_step=dict(numerator=1, denominator=240),
                            substeps=1, iterations=20, seed=1, quality='preview', required_categories=['rigid-body'],
                            reproducibility='not-guaranteed'),
                budgets=dict(bodies=100, dynamic_bodies=100, colliders=100, constraints=10000, triangles=100000,
                             vertices=100000, cloth_vertices=100000, soft_body_vertices=100000, hair_strands=100000,
                             particles=100000, fluid_secondary_particles=100000, volume_cells=1000000, gas_cells=1000000, liquid_cells=1000000,
                             output_frames=10000, steps=10000, duration_seconds=120, memory_bytes=1073741824, vram_bytes=0,
                             cache_bytes=1073741824, bake_file_bytes=1073741824, temporary_bytes=1073741824,
                             cpu_workers=1, concurrent_jobs=1, restart_attempts=2, timeout_seconds=120),
                assets=[], materials=[material], bodies=[body], components=[], joints=[], forces=[], events=[])


def backend():
    # Synthetic contract fixture metadata, never represented as actual execution.
    return dict(id='blender', adapter_version='1.0.0', solver_name='synthetic-contract-fixture',
                solver_version='unexecuted', application_version='unexecuted', platform='fixture',
                precision='mixed', device='cpu', executable_sha256='0'*64)


def usage():
    return dict(elapsed_seconds=0, peak_memory_bytes=0, output_bytes=4, solver_steps=0)


def cache_fixture():
    return dict(schema_version=1, kind='hatch-3d-physics-cache', id='cache', world_sha256='0'*64,
                physics_input_sha256='1'*64, backend=backend(), frame_range=dict(start=0,end=24),
                fps=dict(numerator=24,denominator=1), fixed_step=dict(numerator=1,denominator=240),
                substeps=1, sample_count=24, files=[dict(path='cache/states.bin', sha256='2'*64, bytes=4)],
                upstream_hashes=[], seed=1, reproducibility='not-guaranteed', numerical_tolerance=1e-5,
                usage=usage(), fixture=True, review_status='unreviewed')


class PhysicsContractTests(unittest.TestCase):
    def reject(self, world, code=None, kind='PhysicsWorld'):
        with self.assertRaises(PhysicsError) as caught:
            validate_contract(world, kind)
        if code:
            self.assertEqual(caught.exception.report['code'], code, caught.exception.report)
        return caught.exception

    def test_world_roundtrip_hash_and_no_mutation(self):
        world=fixture(); before=copy.deepcopy(world)
        self.assertIs(validate_contract(world), world)
        self.assertEqual(world,before)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'world.json'; path.write_text(json.dumps(world))
            self.assertEqual(load_contract(path),world)
        self.assertEqual(document_hash(world), document_hash(json.loads(json.dumps(world))))
        world['gravity'][1]=-1
        self.assertNotEqual(document_hash(world),document_hash(before))

    def test_generated_schema_parity_and_valid_structure(self):
        from jsonschema import Draft202012Validator
        generate_schemas(check=True)
        for name, schema in schemas().items():
            Draft202012Validator.check_schema(schema)
            self.assertEqual(json.loads((ROOT/'profiles'/name).read_text()),schema)
        Draft202012Validator(schemas()['physics-3d-world.schema.json']).validate(fixture())
        self.assertEqual(set(definitions()['PhysicsWorld']['required']),set(fixture()))

    def test_structural_runtime_schema_rejection_parity(self):
        from jsonschema import Draft202012Validator
        schema=Draft202012Validator(schemas()['physics-3d-world.schema.json'])
        changes=[lambda w:w.update(schema_version=2),lambda w:w.update(ignored=True),
                 lambda w:w['bodies'][0]['mass'].update(kilograms=-1),
                 lambda w:w['bodies'][0]['colliders'][0].update(mask=2**32),
                 lambda w:w['solver'].update(backend_options={'python':'untrusted'}),
                 lambda w:w['fps'].update(denominator=0),lambda w:w['bodies'][0]['transform'].update(scale=[1,2,1]),
                 lambda w:w['scene'].update(sha256='bad'),lambda w:w.update(version='01.0.0'),
                 lambda w:w.update(id='world\n'),lambda w:w.update(version='1.0.0\n'),
                 lambda w:w['scene'].update(sha256='a'*64+'\n')]
        for change in changes:
            with self.subTest(change=change):
                world=fixture(); change(world)
                self.assertFalse(schema.is_valid(world)); self.reject(world,'PHYSICS_SCHEMA')

    def test_nonfinite_numbers_duplicate_keys_and_bounded_json(self):
        for value in [float('nan'),float('inf'),-float('inf')]:
            world=fixture();world['gravity'][0]=value;self.reject(world,'PHYSICS_JSON')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'world.json'
            for data in ['{"schema_version":1,"schema_version":1}', '{"x":NaN}', '{"x":1e999}', '{"x":'+ '['*100+'0'+']'*100+'}']:
                path.write_text(data)
                with self.assertRaises(PhysicsError):load_contract(path)
            with path.open('wb') as stream: stream.truncate(32*1024*1024+1)
            with self.assertRaises(PhysicsError):load_contract(path)

    def test_bad_rotations_authority_mass_and_inertia(self):
        mutations=[lambda b:b['transform'].update(rotation=[0,0,0,0]),
                   lambda b:b['transform'].update(rotation=[.5,0,0,0]),
                   lambda b:b.update(authority='animation'),lambda b:b.update(parent='ball'),
                   lambda b:b['mass'].update(kilograms=0),lambda b:b.update(animation='anim'),
                   lambda b:b['mass'].update(inertia=[[1,2,0],[0,1,0],[0,0,1]]),
                   lambda b:b['mass'].update(inertia=[[1,0,0],[0,-1,0],[0,0,1]]),
                   lambda b:b['mass'].update(inertia=[[10,0,0],[0,1,0],[0,0,1]])]
        for change in mutations:
            world=fixture();change(world['bodies'][0]);self.reject(world)
        world=fixture();world['bodies'][0]['transform']['rotation']=[0,0,1,0]
        validate_contract(world)

    def test_safe_asset_paths_hashes_and_pbr_links(self):
        from jsonschema import Draft202012Validator
        validator=Draft202012Validator(schemas()['physics-3d-world.schema.json'])
        bad_paths=['../outside.json','/tmp/file','C:/file','assets\\file','assets/CON.json',
                   'assets/a/../b.json','assets/trailing.','assets/space /file.json',
                   'assets//file.json','assets/./file.json','assets/CoN.x','assets/conin$.txt',
                   'assets/file.json\n']
        for path in bad_paths:
            world=fixture();world['scene']['path']=path
            with self.subTest(path=path):
                self.assertFalse(validator.is_valid(world));self.reject(world)
        for path in ['assets/.hidden.json','assets/space name.json','assets/αβ.json']:
            world=fixture();world['scene']['path']=path
            self.assertTrue(validator.is_valid(world));validate_contract(world)
        world=fixture();world['materials'][0]['pbr_material']='mat';self.reject(world,'PHYSICS_REFERENCE')
        world['assets']=[asset('mat','material')];validate_contract(world)
        world['materials'][0]['dynamic_friction']=1;self.reject(world,'PHYSICS_MATERIAL')

    def test_reference_closure_duplicate_ids_and_cycles(self):
        world=fixture();world['bodies'].append(copy.deepcopy(world['bodies'][0]));self.reject(world,'PHYSICS_DUPLICATE_ID')
        for field,value in [('asset','missing'),('parent','missing')]:
            world=fixture();world['bodies'][0][field]=value;self.reject(world)
        world=fixture();body=world['bodies'][0]
        for b in [body,copy.deepcopy(body)]:
            b.update(mode='static',authority='static');b['mass']['kilograms']=0;b['mass']['inertia']=[[0,0,0]]*3
            if b is not body:
                b['id']='other';b['colliders'][0]['id']='other-collider';world['bodies'].append(b)
        body['parent']='other';world['bodies'][1]['parent']='ball';self.reject(world,'PHYSICS_CYCLE')

    def test_collider_shapes_triangle_validation_and_compounds(self):
        world=fixture();world['assets']=[asset()]
        shape=dict(type='convex-hull',source='mesh',vertices=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]],
                   triangles=[[0,2,1],[0,1,3],[0,3,2],[1,2,3]])
        world['bodies'][0]['colliders'][0]['shape']=shape;validate_contract(world)
        for triangles in [[[0,1,4]]*4,[[0,0,1]]*4,[[0,1,2]]*4]:
            bad=copy.deepcopy(world);bad['bodies'][0]['colliders'][0]['shape']['triangles']=triangles;self.reject(bad,'PHYSICS_GEOMETRY')
        bad=copy.deepcopy(world);bad['bodies'][0]['colliders'][0]['shape']['vertices'][2]=[2,0,0];self.reject(bad,'PHYSICS_GEOMETRY')
        bad=copy.deepcopy(world);bad['bodies'][0]['colliders'][0]['shape']['type']='triangle-mesh';self.reject(bad,'PHYSICS_GEOMETRY')
        world['bodies'][0]['colliders'][0]['shape']=dict(type='compound',children=[dict(transform=transform(),shape=shape),dict(transform=transform(),shape=dict(type='capsule',radius=.1,height=1,axis='Y'))])
        validate_contract(world)
        world['budgets']['vertices']=3;self.reject(world,'PHYSICS_BUDGET')

    def test_rational_time_half_open_events_and_exact_budget(self):
        world=fixture();world['fps']=dict(numerator=30000,denominator=1001);world['frame_range']['end']=30
        world['solver']['fixed_step']=dict(numerator=1,denominator=240)
        world['budgets']['steps']=241;validate_contract(world)
        world['budgets']['steps']=240;self.reject(world,'PHYSICS_BUDGET')
        world=fixture();world['fps']=dict(numerator=48,denominator=2);self.reject(world,'PHYSICS_RATE')
        world=fixture();world['frame_range']['end']=0;self.reject(world)
        world=fixture();event=dict(id='event',frame=23,sequence=0,type='impulse',target='ball',actor=None,vector=[1,0,0])
        world['events']=[event];validate_contract(world)
        event['frame']=24;self.reject(world,'PHYSICS_TIME');event['frame']=1
        other=copy.deepcopy(event);other['id']='second';world['events'].append(other);self.reject(world,'PHYSICS_TIME')
        other['sequence']=1;validate_contract(world)

    def test_joint_endpoints_axis_limits_and_force_applicability(self):
        world=fixture();other=copy.deepcopy(world['bodies'][0]);other['id']='other';other['colliders'][0]['id']='other-collider';world['bodies'].append(other)
        joint=dict(id='hinge',type='hinge',body_a='ball',body_b='other',anchor_a=transform(),anchor_b=transform(),axis=[0,1,0],limits=[-1,1],stiffness=0,damping=0,motor_velocity=0,motor_max_force=0,collide_connected=False)
        world['joints']=[joint];validate_contract(world)
        joint['body_b']='ball';self.reject(world,'PHYSICS_REFERENCE');joint['body_b']='other'
        joint['axis']=[0,0,0];self.reject(world,'PHYSICS_AXIS');joint['axis']=[0,1,0]
        joint['limits']=[1,-1];self.reject(world,'PHYSICS_RANGE');joint['limits']=[-1,1]
        force=dict(id='wind',type='wind',bodies=['ball'],vector=[1,0,0],point=[0,0,0],frame_range=dict(start=0,end=24),strength=1,seed=1,categories=['rigid-body'])
        world['forces']=[force];validate_contract(world)
        force['categories']=['cloth'];self.reject(world,'PHYSICS_CATEGORY')
        force['categories']=['rigid-body'];force['frame_range']['end']=25;self.reject(world,'PHYSICS_TIME')

    def test_deformable_particle_and_volume_contracts_and_budgets(self):
        world=fixture();world['assets']=[asset()]
        cloth=dict(id='cloth',category='cloth',geometry='mesh',material='rubber',parent=None,pins=[0,3],vertex_count=4,stiffness=10,damping=.1,mass=1,colliders=['ball'])
        particles=dict(id='particles',category='particles',geometry='mesh',count=100,lifetime_seconds=1,seed=2,mass=.1,radius=.01,initial_velocity=[0,1,0],colliders=['ball'])
        gas=dict(id='smoke',category='gas',transform=transform(),size=[1,2,3],resolution=[10,10,10],density=1,viscosity=.1,temperature=300,combustion=True,emitters=['ball'],colliders=['ball'])
        world['components']=[cloth,particles,gas];world['solver']['required_categories']+=['cloth','particles','gas'];validate_contract(world)
        world['budgets']['volume_cells']=999;self.reject(world,'PHYSICS_BUDGET');world['budgets']['volume_cells']=1000
        world['budgets']['particles']=99;self.reject(world,'PHYSICS_BUDGET');world['budgets']['particles']=100
        cloth['pins']=[4];self.reject(world,'PHYSICS_GEOMETRY');cloth['pins']=[0]
        gas['category']='liquid';gas['max_secondary_particles']=100;world['solver']['required_categories'].append('liquid');self.reject(world,'PHYSICS_CATEGORY')
        gas['combustion']=False;gas['max_secondary_particles']=100;validate_contract(world)

    def test_material_combinations_presets_and_backend_policy(self):
        material=material_preset('rubber')
        self.assertAlmostEqual(combine_materials(material,material)['static_friction'],.64)
        self.assertAlmostEqual(combine_materials(material,material)['restitution'],.25)
        material['static_friction']=5
        self.assertEqual(combine_materials(material,material)['static_friction'],10)
        self.assertEqual(material_preset('wood')['density'],700)
        material['combination']='average'
        with self.assertRaises(PhysicsError):combine_materials(material,material)
        with self.assertRaises(PhysicsError):material_preset('unknown')
        rubber=material_preset('rubber');rubber['density']=1
        self.assertEqual(material_preset('rubber')['density'],1100)

    def test_cache_and_job_roundtrips_and_negative_contracts(self):
        cache=cache_fixture();validate_contract(cache,'PhysicsCache')
        cache['sample_count']=23;self.reject(cache,'PHYSICS_CACHE_RANGE','PhysicsCache');cache['sample_count']=24
        cache['files'].append(dict(path='CACHE/STATES.BIN',sha256='2'*64,bytes=4));self.reject(cache,'PHYSICS_PATH_ALIAS','PhysicsCache');cache['files'].pop()
        cache['usage']['output_bytes']=3;self.reject(cache,'PHYSICS_CACHE_SIZE','PhysicsCache')
        world=fixture();job=dict(schema_version=1,kind='hatch-3d-physics-job',id='job',world=asset('world','physics-world'),world_sha256='0'*64,backend=backend(),output_directory='cache/job',input_files=[dict(path='world.json',sha256='0'*64,bytes=1)],frame_range=world['frame_range'],fps=world['fps'],fixed_step=world['solver']['fixed_step'],substeps=1,budgets=world['budgets'],status='pending',attempt=1,recovery='clean-restart',checkpoint=None,usage=usage())
        validate_contract(job,'PhysicsJob')
        job['attempt']=4;self.reject(job,'PHYSICS_BUDGET','PhysicsJob');job['attempt']=1
        job['recovery']='verified-checkpoint';self.reject(job,'PHYSICS_RECOVERY','PhysicsJob')
        job['checkpoint']=job['input_files'][0];validate_contract(job,'PhysicsJob')
        job['world']['sha256']='f'*64;self.reject(job,'PHYSICS_REFERENCE','PhysicsJob')
        diagnostic=Diagnostic('PHYSICS_CAPABILITY','No backend was executed.',category='capability-gap')
        validate_contract(diagnostic.__dict__,'PhysicsDiagnostic')


    def test_character_fracture_and_all_category_contract_boundaries(self):
        world=fixture();body=world['bodies'][0];body['category']='character'
        world['solver']['required_categories'].append('character')
        self.reject(world,'PHYSICS_CATEGORY')
        body['character_controller']=dict(max_speed=4,step_height=.2,slope_limit_radians=.5)
        validate_contract(world)
        body['category']='rigid-body';self.reject(world,'PHYSICS_CATEGORY')
        del body['character_controller'];body['category']='fracture'
        world['solver']['required_categories'].append('fracture');self.reject(world,'PHYSICS_CATEGORY')
        world['assets']=[asset()]
        fragments=[]
        for index in range(2):
            fragment=copy.deepcopy(body);fragment.update(id='fragment'+str(index),category='rigid-body')
            fragment['colliders'][0]['id']='fragment-collider'+str(index)
            world['bodies'].append(fragment);fragments.append(fragment['id'])
        body['fracture']=dict(source='mesh',fragments=fragments,break_force=100,break_torque=100)
        validate_contract(world)
        body['fracture']['fragments']=['ball','fragment0'];self.reject(world,'PHYSICS_REFERENCE')
        for category in ['cloth','soft-body','hair']:
            component=dict(id='deformable',category=category,geometry='mesh',material='rubber',parent=None,
                pins=[0],vertex_count=4,stiffness=10,damping=.1,mass=1,colliders=['ball'])
            if category=='hair': component['strand_count']=1
            world=fixture();world['assets']=[asset()];world['components']=[component]
            world['solver']['required_categories'].append(category);validate_contract(world)

    def test_contract_loader_rejects_nonregular_inputs_without_blocking(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'world.json';source.write_text(json.dumps(fixture()))
            if os.name!='nt':
                link=root/'link';link.symlink_to(source)
                with self.assertRaises(PhysicsError):load_contract(link)
            if hasattr(os,'mkfifo'):
                fifo=root/'fifo';os.mkfifo(fifo)
                with self.assertRaises(PhysicsError):load_contract(fifo)
            with self.assertRaises(PhysicsError):load_contract(root)
            with self.assertRaises(PhysicsError) as caught:load_contract('bad\x00path')
            validate_contract(caught.exception.report,'PhysicsDiagnostic')

    def test_integral_json_numbers_remain_consistent_and_unmodified(self):
        def floats(value):
            if type(value) is int:return float(value)
            if isinstance(value,list):return [floats(item) for item in value]
            if isinstance(value,dict):return {key:floats(item) for key,item in value.items()}
            return value
        world=floats(fixture());before=copy.deepcopy(world)
        validate_contract(world);self.assertEqual(world,before)
        world['assets']=[asset()]
        world['bodies'][0]['colliders'][0]['shape']=dict(type='convex-hull',source='mesh',
            vertices=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]],triangles=[[0.,2.,1.],[0.,1.,3.],[0.,3.,2.],[1.,2.,3.]])
        validate_contract(world)
        cache=floats(cache_fixture());validate_contract(cache,'PhysicsCache')

    def test_oversized_integer_unsupported_python_numbers_and_error_receipts(self):
        from decimal import Decimal
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'world.json';path.write_text('{"schema_version":'+'1'*5000+'}')
            with self.assertRaises(PhysicsError) as caught:load_contract(path)
            validate_contract(caught.exception.report,'PhysicsDiagnostic')
            from unittest.mock import patch
            from rig_errors import RigError
            from physics_contracts import decode_json
            # Shared decoder counts actual UTF-8 bytes, whether reached through
            # its text API or through the domain's real-file bytes loader.
            text='"éééé"'
            with patch('rig_errors.MAX_JSON_BYTES',8):
                for payload in (text,text.encode('utf-8')):
                    with self.assertRaises(RigError) as caught:decode_json(payload)
                    self.assertEqual(caught.exception.report['code'],'RIG_LIMIT_JSON')
            world=fixture();world['scene']['path']='assets/éé.json'
            payload=json.dumps(world,ensure_ascii=False).encode('utf-8');path.write_bytes(payload)
            with patch('rig_errors.MAX_JSON_BYTES',len(payload)):
                self.assertEqual(load_contract(path),world)
            with patch('rig_errors.MAX_JSON_BYTES',len(payload)-1):
                with self.assertRaises(PhysicsError) as caught:load_contract(path)
                self.assertEqual(caught.exception.report['code'],'PHYSICS_JSON')
                validate_contract(caught.exception.report,'PhysicsDiagnostic')
            self.assertEqual(path.read_bytes(),payload)
            # The shared decoder now owns parse-error translation. Its caller
            # must not accidentally conceal an unrelated programming ValueError.
            with patch('physics_contracts.decode_json',side_effect=ValueError('programming sentinel')):
                with self.assertRaises(ValueError) as caught:load_contract(path)
                self.assertIs(type(caught.exception),ValueError)
        for value in [Decimal('1'),complex(1,2),10**5000]:
            world=fixture();world['gravity'][0]=value;self.reject(world,'PHYSICS_JSON')
        for change in [lambda w:w['solver'].update(backend='x'*2000),lambda w:w.update({'x'*4000:1}),
                       lambda w:w['bodies'][0]['transform'].update(rotation=[0]*2000)]:
            world=fixture();change(world);error=self.reject(world)
            self.assertLessEqual(len(error.report['message']),1024)
            self.assertLessEqual(len(error.report['path']),2048)
            validate_contract(error.report,'PhysicsDiagnostic')

    def test_standalone_material_semantics_match_world_and_combination(self):
        material=fixture()['materials'][0];material['dynamic_friction']=material['static_friction']+1
        self.reject(material,'PHYSICS_MATERIAL','PhysicalMaterial')
        with self.assertRaises(PhysicsError):combine_materials(material,material)
        world=fixture();world['materials']=[material]
        error=self.reject(world,'PHYSICS_MATERIAL')
        self.assertEqual(error.report['path'],'/materials/0/dynamic_friction')

    def test_uniformly_tiny_inertia_and_meshes_preserve_physical_validity(self):
        for scale in [1e-200,1e-7,1e7]:
            world=fixture();world['assets']=[asset()]
            world['bodies'][0]['mass']['inertia']=[[scale,0,0],[0,scale,0],[0,0,scale]]
            world['bodies'][0]['colliders'][0]['shape']=dict(type='convex-hull',source='mesh',
                vertices=[[0,0,0],[scale,0,0],[0,scale,0],[0,0,scale]],
                triangles=[[0,2,1],[0,1,3],[0,3,2],[1,2,3]])
            validate_contract(world)
            world['bodies'][0]['mass']['inertia'][0][0]=10*scale
            self.reject(world,'PHYSICS_INERTIA')
        world=fixture();world['assets']=[asset()];body=world['bodies'][0]
        body.update(mode='static',authority='static');body['mass'].update(kilograms=0,inertia=[[0,0,0]]*3)
        body['colliders'][0]['shape']=dict(type='triangle-mesh',source='mesh',
            vertices=[[0,0,0],[1,0,0],[0,0,1]],triangles=[[0,1,2]])
        world['budgets']['triangles']=1;validate_contract(world)
        world['budgets']['triangles']=0;self.reject(world,'PHYSICS_BUDGET')

    def test_aggregate_leaf_shape_triangle_frame_and_constraint_budgets(self):
        world=fixture();shape=world['bodies'][0]['colliders'][0]['shape']
        world['bodies'][0]['colliders'][0]['shape']=dict(type='compound',children=[
            dict(transform=transform(),shape=copy.deepcopy(shape)),
            dict(transform=transform(),shape=dict(type='compound',children=[dict(transform=transform(),shape=shape)]))])
        world['budgets']['colliders']=1;self.reject(world,'PHYSICS_BUDGET')
        world['budgets']['colliders']=2;validate_contract(world)
        world['budgets']['dynamic_bodies']=0;self.reject(world,'PHYSICS_BUDGET');world['budgets']['dynamic_bodies']=1
        world['budgets']['output_frames']=23;self.reject(world,'PHYSICS_BUDGET');world['budgets']['output_frames']=24
        world['assets']=[asset()]
        world['components']=[dict(id='cloth',category='cloth',geometry='mesh',material='rubber',parent=None,
            pins=[0,1],vertex_count=4,stiffness=10,damping=.1,mass=1,colliders=['ball'])]
        world['solver']['required_categories'].append('cloth')
        world['budgets']['constraints']=1;self.reject(world,'PHYSICS_BUDGET')
        world['budgets']['constraints']=2;validate_contract(world)
        world['budgets']['cloth_vertices']=3;self.reject(world,'PHYSICS_BUDGET')

    def test_fracture_ownership_cycles_and_shared_fragments_reject(self):
        world=fixture();world['assets']=[asset()];world['solver']['required_categories'].append('fracture')
        template=world['bodies'][0];world['bodies']=[]
        for identifier in ['a','b','c','d']:
            body=copy.deepcopy(template);body['id']=identifier;body['colliders'][0]['id']=identifier+'-collider'
            world['bodies'].append(body)
        a,b,c,d=world['bodies'];a['category']=b['category']='fracture'
        a['fracture']=dict(source='mesh',fragments=['b','c'],break_force=10,break_torque=10)
        b['fracture']=dict(source='mesh',fragments=['a','d'],break_force=10,break_torque=10)
        self.reject(world,'PHYSICS_CYCLE')
        b['fracture']['fragments']=['c','d'];self.reject(world,'PHYSICS_OWNERSHIP')
        b['category']='rigid-body';del b['fracture'];validate_contract(world)

    def test_contract_import_and_schema_generation_without_optional_3d(self):
        code="""import importlib.abc,sys
sys.path.insert(0,'scripts')
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in ('numpy','bpy','pybullet','pygltflib','playwright'): raise ImportError(fullname)
sys.meta_path.insert(0,Block())
import physics_contracts
physics_contracts.generate_schemas(check=True)
import hatch
sys.argv=['hatch.py','capabilities'];hatch.main()
assert not any(x in sys.modules for x in ('numpy','bpy','pybullet','pygltflib'))
"""
        run=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)


if __name__=='__main__':unittest.main()
