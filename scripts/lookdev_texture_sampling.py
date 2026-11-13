"""Explicit canonical sampler admission for already verified Blender images."""
import hashlib
from pathlib import Path

from lookdev_contracts import content_hash, validate_resource
from rig_errors import require

_SOURCE_SHA256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def sampler_plan(document):
    validate_resource(document)
    require(document['kind']=='texture3d','TEXTURE_KIND','Sampler requires a canonical Texture3D document.')
    require(document['filter'] in ('nearest','linear'),'TEXTURE_SAMPLER_UNSUPPORTED',
            'Explicit canonical mipmap filtering is not verified by the Blender sampler adapter.')
    require(document['wrap'] in ('repeat','clamp'),'TEXTURE_SAMPLER_UNSUPPORTED',
            'Mirrored repeat requires an explicit coordinate adapter; it cannot use repeat silently.')
    result=dict(document_sha256=content_hash(document),source_sha256=document['file']['sha256'],
        role=document['role'],encoding=document['encoding'],filter=document['filter'],wrap=document['wrap'],
        interpolation={'nearest':'Closest','linear':'Linear'}[document['filter']],
        extension={'repeat':'REPEAT','clamp':'EXTEND'}[document['wrap']],
        adapter_source_sha256=_SOURCE_SHA256)
    return result


def configure_blender_sampler(node,document):
    """Apply canonical sampling only after image provenance and all settings admit."""
    plan=sampler_plan(document)
    require(getattr(node,'bl_idname',None)=='ShaderNodeTexImage','TEXTURE_SAMPLER',
            'Canonical sampling requires an image texture shader node.')
    image=node.image
    require(image is not None and image.get('source_sha256')==plan['source_sha256']
            and image.get('texture_role')==plan['role'] and image.get('source_encoding')==plan['encoding'],
            'TEXTURE_SAMPLER_SOURCE','Sampler image differs from the verified canonical texture source.')
    node.interpolation=plan['interpolation']
    node.extension=plan['extension']
    return plan
