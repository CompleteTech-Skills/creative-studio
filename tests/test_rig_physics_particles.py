"""Real CPU particle execution and exact-source cache tests."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import math
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import physics_backends as core
from physics_backend_example import build_inputs
from physics_particles import ParticleBackend, admit, verify
from physics_particle_worker import run


def fixture():
    world,payloads=build_inputs(frames=49)
    world['bodies']=[world['bodies'][1]]
    world['solver'].update(backend='particles-native',required_categories=['particles','rigid-body'])
    world['components']=[dict(id='ball',category='particles',geometry='ball-asset',count=24,lifetime_seconds=1.5,
        seed=91,mass=2.,radius=.05,initial_velocity=[0.,4.,0.],colliders=[])]
    world['budgets']['particles']=24
    settings=dict(schema_version=1,kind='hatch-physics-particle-settings',world_sha256=core.digest(core.canonical(world)),
        emitters=[dict(component='ball',shape='point',rate=dict(numerator=10,denominator=1),burst=1,
        start=dict(numerator=0,denominator=1),end=dict(numerator=1,denominator=1),spread=0.,drag=0.,restitution=.5,collision='none')])
    return world,settings,payloads


class ParticleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def test_actual_subprocess_evolves_gravity_and_exact_birth_death_events(self):
        world,settings,payloads=fixture();backend=ParticleBackend();prepared=backend.prepare(world,settings,payloads)
        try:
            result=backend.simulate(prepared)
            states=core.parse((prepared.root/'particles.json').read_bytes())
            particle=states['samples'][12]['particles'][0]
            self.assertAlmostEqual(particle['position'][1],3+4*.5-9.81*.5*.5/2,places=11)
            self.assertAlmostEqual(particle['velocity'][1],4-9.81*.5,places=11)
            self.assertEqual(particle['age'],.5)
            events=core.parse((prepared.root/'events.json').read_bytes())['events']
            self.assertEqual(sum(e['type']=='birth' for e in events),10)
            self.assertEqual(sum(e['type']=='death' for e in events),6)
            report=backend.bake(result,self.root/'cache')
            self.assertEqual(report['sample_count'],49)
            self.assertTrue((self.root/'cache/geometry/frame-0012.obj').is_file())
            self.assertGreater((self.root/'cache/geometry/frame-0012.obj').stat().st_size,100)
            self.assertEqual(verify(self.root/'cache')['status'],'hash-and-state-verified')
            with self.assertRaises(core.BackendError):backend.simulate(prepared)
        finally:backend.cleanup(prepared)

    def test_real_deterministic_seed_and_shape_variation(self):
        world,settings,payloads=fixture();backend=ParticleBackend()
        settings['emitters'][0]['shape']='mesh-surface';settings['emitters'][0]['spread']=.5
        values=[]
        for index in range(2):
            prepared=backend.prepare(world,settings,payloads)
            try:
                backend.simulate(prepared);values.append((prepared.root/'particles.json').read_bytes())
            finally:backend.cleanup(prepared)
        self.assertEqual(values[0],values[1])
        settings['emitters'][0]['collision']='bounce'
        with self.assertRaises(core.BackendError):admit(world,settings)

    def test_changed_inputs_and_unsafe_sources_fail(self):
        world,settings,payloads=fixture();backend=ParticleBackend()
        with self.assertRaises(core.BackendError):backend.prepare(world,settings,{'../bad':b'x'})
        bad=deepcopy(settings);bad['world_sha256']='0'*64
        with self.assertRaises(core.BackendError):admit(world,bad)
        prepared=backend.prepare(world,settings,payloads)
        try:
            (prepared.root/'inputs/ball.obj').write_bytes(b'changed')
            with self.assertRaises(core.BackendError):backend.simulate(prepared)
        finally:backend.cleanup(prepared)

    def test_malformed_unknown_and_excessive_settings_reject(self):
        world,settings,_=fixture()
        for mutate in [lambda s:s.update(script='bad'),lambda s:s['emitters'][0].update(drag=float('nan')),
            lambda s:s['emitters'].append(deepcopy(s['emitters'][0])),lambda s:s['emitters'][0].update(burst=1000000)]:
            value=deepcopy(settings);mutate(value)
            with self.assertRaises(core.BackendError):admit(world,value)
        world['components'][0]['count']=1000000;settings['world_sha256']=core.digest(core.canonical(world))
        with self.assertRaises(core.BackendError):admit(world,settings)

    def test_legacy_adapters_reject_new_backend_explicitly(self):
        world,settings,_=fixture()
        for backend in (core.BlenderBackend('/missing'),core.PyBulletBackend('/missing')):
            with self.assertRaises(core.BackendError) as caught:backend.validate_scene(world)
            self.assertEqual(caught.exception.report['code'],'PHYSICS_BACKEND')

    def test_force_drag_and_mass_response(self):
        from physics_particle_worker import _integrate
        particle=dict(position=[0.,0.,0.],velocity=[3.,0.,0.])
        _integrate(particle,[2.,0.,0.],.5,Fraction(1))
        terminal=4.;decay=math.exp(-.5)
        self.assertAlmostEqual(particle['velocity'][0],terminal+(3-terminal)*decay,places=13)
        self.assertAlmostEqual(particle['position'][0],terminal+(3-terminal)*(1-decay)/.5,places=13)
        from physics_particle_worker import _acceleration
        world,settings,_=fixture();component=world['components'][0]
        world['forces']=[dict(id='push',type='force',vector=[1,0,0],strength=4.,bodies=['ball'],
            categories=['particles'],frame_range=dict(start=0,end=49),seed=1)]
        acceleration,_=_acceleration(world,component,settings['emitters'][0],Fraction(0),Fraction(1,120))
        self.assertEqual(acceleration,[2.,-9.81,0.])
        component['mass']=4.
        acceleration,_=_acceleration(world,component,settings['emitters'][0],Fraction(0),Fraction(1,120))
        self.assertEqual(acceleration,[1.,-9.81,0.])

    def test_small_drag_continuously_approaches_ballistic_motion(self):
        from decimal import Decimal, localcontext
        from physics_particle_worker import _integrate
        for rate in (1e-20, 1e-12, .001999, .002001, .5):
            with self.subTest(rate=rate), localcontext() as context:
                context.prec=70
                k=Decimal(str(rate));dt=Decimal('.5');decay=(-k*dt).exp()
                c1=(1-decay)/k;c2=(dt-c1)/k
                expected_position=Decimal(3)+Decimal(4)*c1-Decimal('9.81')*c2
                expected_velocity=Decimal(4)*decay-Decimal('9.81')*c1
                particle=dict(position=[0.,3.,0.],velocity=[0.,4.,0.])
                _integrate(particle,[0.,-9.81,0.],rate,Fraction(1,2))
                self.assertAlmostEqual(particle['position'][1],float(expected_position),places=11)
                self.assertAlmostEqual(particle['velocity'][1],float(expected_velocity),places=11)
        world,settings,payloads=fixture();settings['emitters'][0]['drag']=1e-20
        backend=ParticleBackend();prepared=backend.prepare(world,settings,payloads)
        try:
            backend.simulate(prepared)
            particle=core.parse((prepared.root/'particles.json').read_bytes())['samples'][12]['particles'][0]
            self.assertAlmostEqual(particle['position'][1],3+4*.5-9.81*.5*.5/2,places=11)
            self.assertAlmostEqual(particle['velocity'][1],4-9.81*.5,places=11)
        finally:backend.cleanup(prepared)

    def test_integer_valued_json_numbers_keep_source_identity_through_bake(self):
        world,settings,payloads=fixture();world['components'][0].update(count=24.,seed=91.)
        settings['world_sha256']=core.digest(core.canonical(world))
        original=core.canonical(world);backend=ParticleBackend();prepared=backend.prepare(world,settings,payloads)
        try:
            result=backend.simulate(prepared);destination=self.root/'integer-valued-cache'
            backend.bake(result,destination);verify(destination)
            self.assertEqual((destination/'world.json').read_bytes(),original)
            self.assertEqual(core.canonical(world),original)
        finally:backend.cleanup(prepared)

    def test_cancel_and_step_budget_fail(self):
        import threading
        world,settings,payloads=fixture();backend=ParticleBackend();prepared=backend.prepare(world,settings,payloads)
        token=threading.Event();token.set()
        try:
            with self.assertRaises(core.BackendError) as caught:backend.simulate(prepared,cancel_token=token)
            self.assertEqual(caught.exception.report['code'],'PHYSICS_CANCELLED')
            world['budgets']['steps']=1
            job=core.parse((prepared.root/'worker-job.json').read_bytes());job['world']=world
            with self.assertRaises(ValueError):run(job)
        finally:backend.cleanup(prepared)

    def test_off_grid_event_subdivisions_respect_step_budget(self):
        world,settings,payloads=fixture()
        world['solver']['fixed_step']=dict(numerator=1,denominator=240)
        world['solver']['substeps']=1
        world['budgets']['steps']=490
        settings['emitters'][0]['rate']=dict(numerator=23,denominator=1)
        settings['world_sha256']=core.digest(core.canonical(world))
        backend=ParticleBackend();prepared=backend.prepare(world,settings,payloads)
        try:
            job=core.parse((prepared.root/'worker-job.json').read_bytes())
            with self.assertRaisesRegex(ValueError,'Event subdivisions'):
                run(job)
        finally:backend.cleanup(prepared)

    def test_bake_enforces_per_file_budget_without_publication(self):
        world,settings,payloads=fixture()
        world['budgets']['bake_file_bytes']=1
        settings['world_sha256']=core.digest(core.canonical(world))
        backend=ParticleBackend();prepared=backend.prepare(world,settings,payloads)
        destination=self.root/'bounded-cache'
        try:
            backend.simulate(prepared)
            with self.assertRaises(core.BackendError) as caught:backend.bake(prepared,destination)
            self.assertEqual(caught.exception.report['code'],'PARTICLE_LIMIT')
            self.assertFalse(destination.exists())
            self.assertFalse(list(self.root.glob('.bounded-cache.particle-*')))
        finally:backend.cleanup(prepared)

    def test_retained_per_file_budget_includes_manifest_before_bulk_reads(self):
        from unittest.mock import patch
        world,settings,payloads=fixture()
        world['frame_range']['end']=2;world['components'][0]['count']=1
        world['budgets']['bake_file_bytes']=40000
        settings['world_sha256']=core.digest(core.canonical(world))
        backend=ParticleBackend();prepared=backend.prepare(world,settings,payloads)
        destination=self.root/'manifest-budget-cache'
        try:
            backend.bake(backend.simulate(prepared),destination)
            target=destination/'particle-cache.json';manifest=core.parse(target.read_bytes())
            manifest['usage']['padding']=''
            remaining=40000-len(core.canonical(manifest))
            self.assertGreater(remaining,0)
            manifest['usage']['padding']='x'*remaining
            target.write_bytes(core.canonical(manifest))
            self.assertEqual(target.stat().st_size,40000)
            self.assertEqual(verify(destination)['status'],'hash-and-state-verified')
            manifest['usage']['padding']+='x';target.write_bytes(core.canonical(manifest))
            reads=[];original_read=core._Reader.read
            def record_read(reader,name,*args,**kwargs):
                reads.append(name)
                return original_read(reader,name,*args,**kwargs)
            with patch.object(core._Reader,'read',record_read):
                with self.assertRaises(core.BackendError) as caught:verify(destination)
            self.assertEqual(caught.exception.report['code'],'PARTICLE_LIMIT')
            self.assertEqual(reads,['particle-cache.json','world.json','settings.json'])
            self.assertEqual((destination/'world.json').read_bytes(),core.canonical(world))
        finally:backend.cleanup(prepared)

    def test_display_geometry_mutation_is_rejected_even_if_rehashed(self):
        world,settings,payloads=fixture();backend=ParticleBackend();prepared=backend.prepare(world,settings,payloads)
        try:
            backend.bake(backend.simulate(prepared),self.root/'cache')
            target=self.root/'cache/geometry/frame-0000.obj';target.write_bytes(b'v 0 0 0\n')
            manifest=core.parse((self.root/'cache/particle-cache.json').read_bytes())
            for record in manifest['files']:
                if record['path']=='geometry/frame-0000.obj':
                    record.update(bytes=target.stat().st_size,sha256=core.digest(target.read_bytes()))
            (self.root/'cache/particle-cache.json').write_bytes(core.canonical(manifest))
            with self.assertRaises(core.BackendError):verify(self.root/'cache')
        finally:backend.cleanup(prepared)

    @unittest.skipUnless(__import__('os').environ.get('HATCH_PHYSICS_BACKEND_TEST')=='1', 'real optional native tests opt in')
    def test_native_bounce_and_removal_use_real_bullet_contacts(self):
        import os
        from physics_particle_example import build_inputs
        interpreter=os.environ.get('HATCH_PYBULLET_PYTHON',sys.executable)
        for mode in ('bounce','remove'):
            world,settings,payloads=build_inputs('sparks',collision=mode)
            backend=ParticleBackend(interpreter);prepared=backend.prepare(world,settings,payloads)
            try:
                backend.simulate(prepared)
                states=core.parse((prepared.root/'particles.json').read_bytes())
                events=core.parse((prepared.root/'events.json').read_bytes())['events']
                report=core.parse((prepared.root/'report.json').read_bytes())
                self.assertEqual(report['collision']['backend']['version'],'3.2.7')
                self.assertGreater(report['collision']['contacts'],0)
                self.assertEqual(report['births'],64)
                self.assertTrue(any(e['type']=='collision' for e in events))
                if mode=='bounce':
                    self.assertTrue(any(p['velocity'][1]>0 and p['age']>1 for s in states['samples'] for p in s['particles']))
                    self.assertTrue(all(p['position'][1]>=p['radius']-1e-6 for s in states['samples'][24:] for p in s['particles']))
                else:
                    self.assertTrue(any(e['type']=='death' and e['reason']=='collision' for e in events))
                    self.assertEqual(states['samples'][-1]['particles'],[])
                self.assertEqual(backend.bake(prepared,self.root/mode)['status'],'hash-and-state-verified')
            finally:backend.cleanup(prepared)
