"""Run from repo root with PYTHONPATH=scripts:tests. One rotated finite-parent control."""
from pathlib import Path
source = Path('tests/independent/test_rig_fold_anisotropic_solve.py').read_text()
before = '    goal=dict'
after = '''    tracks.append(fixtures.step("b0","rotation",[0.,.5],[[0.,0.,0.,1.],axis_angle([0.,0.,1.],math.pi/4).tolist()]))
    goal=dict'''
assert source.count(before) == 1
source = source.replace(before, after)
before = "        print('witness skin and closed endpoint passed')"
after = '''        rotation = np.array([[2**-.5,-2**-.5,0],[2**-.5,2**-.5,0],[0,0,1]])
        local_quarter = np.array([[0.,0.,-1.],[0.,1.,0.],[1.,0.,0.]])
        world_turn = rotation @ local_quarter @ rotation.T
        # Check column directions individually, including the tiny parent axis.
        for n in names:
            basis = world[n][:3,:3]
            after_basis = after[n][:3,:3]
            magnitudes = np.max(np.abs(basis), axis=0)
            np.testing.assert_allclose(after_basis/magnitudes, world_turn @ (basis/magnitudes),rtol=1e-12,atol=1e-12)
        equilibrated = p/np.max(np.abs(p),axis=1)[:,None]
        print("row-scaled Y column",equilibrated[:,1].tolist())
        print("rigid world turn, native FK, skin, pole and closed endpoint passed")'''
assert source.count(before) == 1
source = source.replace(before, after)
before = "        for op, fn in"
after = """        saved = deepcopy({k:v for k,v in vars(runtime).items() if k in ('state','entered','time','source_pose','_input_pose','_source_input_pose','_source_contact_correction','root_offset','transition_count')})
        assert len(saved) == 9
        for op, fn in"""
assert source.count(before) == 1
source = source.replace(before, after)
before = '                expected_status.append(False)'
after = """                assert err.report['code'] == 'RIG_SINGULAR_TRANSFORM'
                if op == 'runtime':
                    for k,v in saved.items():
                        if isinstance(v,np.ndarray):
                            np.testing.assert_array_equal(v,getattr(runtime,k))
                        else:
                            assert getattr(runtime,k) == v,k
                    print('All nine runtime transaction fields preserved.')
                expected_status.append(False)"""
assert source.count(before) == 1
source = source.replace(before, after)
exec(compile(source, 'native_rotated_fold_control', 'exec'))
