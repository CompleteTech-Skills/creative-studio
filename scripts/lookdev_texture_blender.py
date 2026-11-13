"""Consume disposable CPU texture pyramids through the canonical Blender image owner."""
import hashlib
import json

from lookdev_texture_cache import DerivedTextureCache
from lookdev_textures import DecodedTexture, create_blender_image
from rig_errors import require


def create_cached_blender_image(cache, texture, *, name, profile='browser', mip_level=0):
    """Install one explicitly selected derived level; return image and provenance.

    Cache memory and Blender image lifetime are independent: evicting disposable
    CPU arrays never invalidates an installed shader image. Caller owns shader
    assignment and removal on rollback. This does not claim GPU residency or
    install a second material mapper.
    """
    require(isinstance(cache, DerivedTextureCache), 'TEXTURE_CACHE', 'Expected the shared derived texture cache.')
    require(type(mip_level) is int and mip_level >= 0, 'TEXTURE_MIP', 'Mip level must be a nonnegative integer.')
    require(isinstance(name, str) and 0 < len(name) <= 128, 'TEXTURE_NAME', 'Expected bounded image name.')
    receipt, levels = cache.prepare(texture, profile=profile)
    require(mip_level < len(levels), 'TEXTURE_MIP', 'Requested mip level is outside the admitted pyramid.')
    selected = levels[mip_level]
    digest = hashlib.sha256(selected.tobytes(order='C')).hexdigest()
    derived = DecodedTexture(selected, 'DERIVED-FLOAT32', texture.source_sha256,
                             texture.role, texture.encoding, 4, 32)
    image = create_blender_image(derived, name=name)
    provenance = dict(receipt, selected_mip_level=mip_level,
                      selected_dimensions=[derived.width, derived.height],
                      selected_float32_rgba_sha256=digest,
                      selected_bytes=selected.nbytes,
                      backend='Blender-float-image', adapter_version='1.0.0')
    try:
        image['texture_derivation'] = json.dumps(provenance, sort_keys=True, separators=(',', ':'))
    except Exception:
        import bpy
        bpy.data.images.remove(image)
        raise
    return image, provenance
