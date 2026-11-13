"""Consume disposable CPU texture pyramids through the canonical Blender image owner."""
import hashlib
import json
from pathlib import Path

from lookdev_texture_cache import DerivedTextureCache
from lookdev_textures import COLOR_ROLES, DecodedTexture, create_blender_image
from rig_errors import require

# Capture the actual imported helper origins. Reject mixed checkouts rather than
# certify sibling source bytes while executing a previously imported module.
import lookdev_texture_cache as _cache_module
import lookdev_textures as _textures_module
import lookdev_contracts as _contracts_module
import rig_errors as _errors_module


def _source_hashes():
    own=Path(__file__).resolve()
    modules={own.name: own}
    for module in (_cache_module,_textures_module,_contracts_module,_errors_module):
        path=Path(module.__file__).resolve()
        require(path.suffix=='.py' and path.parent==own.parent,
                'TEXTURE_MODULE_ORIGIN','Texture helpers must execute from the same frozen source checkout.')
        modules[path.name]=path
    return {name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in modules.items()}


_MODULE_SHA256=_source_hashes()


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
                      actual_decoded_encoding='linear-srgb' if texture.role in COLOR_ROLES else 'data',
                      pixel_layout='float32-rgba', decoded_row_order='top-down',
                      native_row_order='bottom-up',
                      backend='Blender-float-image', adapter_version='1.0.0',
                      module_sha256=dict(_MODULE_SHA256))
    try:
        image['texture_derivation'] = json.dumps(provenance, sort_keys=True, separators=(',', ':'))
    except Exception:
        import bpy
        bpy.data.images.remove(image)
        raise
    return image, provenance
