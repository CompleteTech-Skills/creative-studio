"""Bounded disposable upload buffers; canonical originals stay with AssetLibrary."""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import re

from lookdev_contracts import content_hash
from lookdev_textures import DecodedTexture, Limits, COLOR_ROLES, DATA_ROLES, mipmaps
from rig_errors import require


@dataclass(frozen=True)
class UploadProfile:
    max_dimension: int
    max_upload_bytes: int

    def __post_init__(self):
        require(type(self.max_dimension) is int and 1 <= self.max_dimension <= 16384
                and type(self.max_upload_bytes) is int and 16 <= self.max_upload_bytes <= 4*1024**3,
                'TEXTURE_PROFILE', 'Upload profile requires bounded dimension and float-buffer byte limits.')


PROFILES = {'mobile': UploadProfile(1024, 24*1024**2),
            'browser': UploadProfile(2048, 96*1024**2),
            'offline': UploadProfile(4096, 384*1024**2)}


class DerivedTextureCache:
    """LRU of immutable CPU float32 upload pyramids, never a persistent asset store.

    Admission includes every RGBA mip byte before construction. Cached arrays are
    private; callers receive copies and cannot change another render's buffers.
    Eviction releases only these disposable derived arrays. GPU allocations and
    renderer-native compression stay with the renderer/exporter owners.
    """
    def __init__(self, byte_budget=128*1024**2):
        require(type(byte_budget) is int and 16 <= byte_budget <= 4*1024**3,
                'TEXTURE_CACHE', 'Cache requires a bounded positive byte budget.')
        self.byte_budget=byte_budget
        self._entries=OrderedDict()
        self._bytes=0

    @property
    def bytes_used(self): return self._bytes

    @property
    def entries(self): return len(self._entries)

    def clear(self):
        self._entries.clear()
        self._bytes=0

    def prepare(self, texture, *, profile='browser'):
        import numpy as np
        chosen=PROFILES.get(profile) if isinstance(profile,str) else profile
        require(isinstance(chosen,UploadProfile),'TEXTURE_PROFILE','Unknown upload profile.')
        require(isinstance(texture,DecodedTexture) and isinstance(texture.role,str)
                and isinstance(texture.encoding,str) and isinstance(texture.source_sha256,str)
                and texture.role in COLOR_ROLES|DATA_ROLES
                and texture.encoding in ({'srgb','linear-srgb'} if texture.role in COLOR_ROLES else {'data'})
                and re.fullmatch('[0-9a-f]{64}',texture.source_sha256 or '') is not None,
                'TEXTURE_CACHE','Expected decoded texture with exact source hash and interpretation.')
        pixels=texture.pixels
        require(isinstance(pixels,np.ndarray) and pixels.dtype==np.float32
                and pixels.ndim==3 and pixels.shape[2]==4,
                'TEXTURE_CACHE','Expected float32 RGBA pixels.')
        h,w=pixels.shape[:2]
        require(0<h<=16384 and 0<w<=16384 and h*w<=Limits().pixels,
                'TEXTURE_LIMIT','Upload source exceeds decoded pixel bounds.')
        require(np.isfinite(pixels).all() and (pixels>=0).all(),
                'TEXTURE_CACHE','Upload pixels must remain finite and nonnegative.')
        dimensions=[]
        while True:
            dimensions.append((w,h))
            if (w,h)==(1,1):break
            w,h=max(1,w//2),max(1,h//2)
        first=next(i for i,(w,h) in enumerate(dimensions) if max(w,h)<=chosen.max_dimension)
        count=sum(w*h*16 for w,h in dimensions[first:])
        require(count<=chosen.max_upload_bytes and count<=self.byte_budget,
                'TEXTURE_LIMIT','Derived upload pyramid exceeds profile/cache byte budget.')
        digest=hashlib.sha256(pixels.tobytes(order='C')).hexdigest()
        key=content_hash(dict(adapter='texture-upload-1',source_sha256=texture.source_sha256,
            decoded_sha256=digest,role=texture.role,encoding=texture.encoding,
            shape=list(pixels.shape),max_dimension=chosen.max_dimension,max_upload_bytes=chosen.max_upload_bytes))
        hit=key in self._entries
        if hit:
            levels,receipt=self._entries.pop(key)
        else:
            # Source working-set admission remains enforced by the shared mipmap
            # operation. Only levels retained for upload count against this cache.
            levels=([pixels]+mipmaps(texture))[first:]
            levels=tuple(level.copy() for level in levels)
            receipt=dict(key=key,source_sha256=texture.source_sha256,decoded_sha256=digest,
                role=texture.role,encoding=texture.encoding,source_dimensions=[texture.width,texture.height],
                upload_dimensions=list(dimensions[first]),mip_dimensions=[list(x) for x in dimensions[first:]],
                bytes=count,operation='linear-area-mipmap',source_unchanged=True)
            while self._entries and self._bytes+count>self.byte_budget:
                _,(_,evicted)=self._entries.popitem(last=False)
                self._bytes-=evicted['bytes']
            self._bytes+=count
        self._entries[key]=(levels,receipt)
        return dict(deepcopy(receipt),cache_hit=hit),tuple(level.copy() for level in levels)
