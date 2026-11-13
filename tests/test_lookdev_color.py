"""Shared color authority tests; ordinary 2D discovery needs no optional NumPy."""
from copy import deepcopy
from pathlib import Path
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_color import configuration,validate_configuration
from lookdev_contracts import content_hash
from rig_errors import RigError


class ColorTests(unittest.TestCase):
    def test_supported_kelvin_tint_and_exposure_bounds(self):
        for kelvin,tint,stops in [(1800,-500,-32),(6500,10,0),(100000,500,32)]:
            value=configuration(temperature_kelvin=kelvin,tint=tint,exposure=stops)
            self.assertEqual(value['temperature_kelvin'],kelvin)
            self.assertEqual(value['tint'],tint);self.assertEqual(value['exposure_stops'],stops)
            self.assertEqual(validate_configuration(value),value)

    def test_native_unsupported_values_reject_instead_of_clamping(self):
        for kwargs in ({'temperature_kelvin':1799},{'temperature_kelvin':100001},
                       {'tint':501},{'tint':-501},{'exposure':-33},{'exposure':33},
                       {'temperature_kelvin':True},{'exposure':float('nan')},
                       {'view':'aces'},{'output':'linear-png'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(RigError):configuration(**kwargs)

    def test_authoritative_profile_hash_covers_wb_exposure_and_encoding(self):
        value=configuration();copy=deepcopy(value)
        for field,replacement in [('temperature_kelvin',3200),('tint',-15),('exposure_stops',1),('output','scene-linear-exr')]:
            changed=deepcopy(value);changed[field]=replacement
            with self.subTest(field=field),self.assertRaises(RigError):validate_configuration(changed)
        self.assertEqual(value,copy)
        self.assertNotEqual(configuration()['sha256'],configuration(temperature_kelvin=3200)['sha256'])
        extra=deepcopy(value);extra['camera_white_balance']=3200
        extra['sha256']=content_hash({k:v for k,v in extra.items() if k!='sha256'})
        with self.assertRaises(RigError):validate_configuration(extra)
