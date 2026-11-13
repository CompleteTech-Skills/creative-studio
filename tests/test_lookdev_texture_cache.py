import hashlib
from pathlib import Path
import sys
import unittest

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_texture_cache import DerivedTextureCache, UploadProfile
from lookdev_textures import DecodedTexture
from rig_errors import RigError


class UploadCacheTests(unittest.TestCase):
    def texture(self,value=0.25):
        pixels=np.full((4,4,4),value,np.float32)
        return DecodedTexture(pixels,'PNG',hashlib.sha256(pixels.tobytes()).hexdigest(),'roughness','data')

    def test_profile_derivation_provenance_and_source_preservation(self):
        texture=self.texture();before=texture.pixels.copy()
        receipt,levels=DerivedTextureCache().prepare(texture,profile=UploadProfile(2,80))
        self.assertEqual(receipt['source_dimensions'],[4,4])
        self.assertEqual(receipt['mip_dimensions'],[[2,2],[1,1]])
        self.assertEqual(receipt['bytes'],80)
        np.testing.assert_array_equal(texture.pixels,before)
        self.assertEqual(sum(x.nbytes for x in levels),80)

    def test_exact_content_key_alias_protection_and_lru_eviction(self):
        cache=DerivedTextureCache(336)
        texture=self.texture()
        receipt,levels=cache.prepare(texture)
        levels[0][:]=1
        receipt['mip_dimensions'][0][0]=999
        second,copies=cache.prepare(texture)
        self.assertTrue(second['cache_hit'])
        self.assertEqual(second['mip_dimensions'][0],[4,4])
        np.testing.assert_array_equal(copies[0],texture.pixels)
        cache.prepare(self.texture(0.75))
        self.assertEqual(cache.entries,1);self.assertEqual(cache.bytes_used,336)
        self.assertFalse(cache.prepare(texture)[0]['cache_hit'])
        # Forged/reused source digest cannot alias different decoded content.
        changed=self.texture(0.8);changed.source_sha256=texture.source_sha256
        self.assertNotEqual(cache.prepare(changed)[0]['key'],receipt['key'])
        cache.clear();self.assertEqual(cache.bytes_used,0)

    def test_budget_rejects_before_build_and_interpretation_changes_key(self):
        texture=self.texture()
        for cache,profile in [(DerivedTextureCache(80),'browser'),
                              (DerivedTextureCache(),UploadProfile(4,80))]:
            with self.assertRaises(RigError):cache.prepare(texture,profile=profile)
            self.assertEqual(cache.bytes_used,0)
        cache=DerivedTextureCache()
        first=cache.prepare(texture)[0]['key']
        texture.role='metallic'
        self.assertNotEqual(cache.prepare(texture)[0]['key'],first)
        with self.assertRaises(RigError):cache.prepare(texture,profile='imaginary')
        for field,value in [('source_sha256',42),('role',[]),('encoding',None)]:
            malformed=self.texture();setattr(malformed,field,value)
            with self.subTest(field=field),self.assertRaises(RigError):cache.prepare(malformed)
        malformed=self.texture();malformed.pixels[:,:,3]=1.1
        with self.assertRaises(RigError):cache.prepare(malformed)
        for budget in (True,0,-1):
            with self.assertRaises(RigError):DerivedTextureCache(budget)


if __name__=='__main__':unittest.main()
