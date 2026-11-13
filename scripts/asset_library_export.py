"""Verified exports of canonical library payloads; no second asset registry.

Publication is one exclusive directory rename. GLB/glTF preserve the admitted
runtime data exactly; native export copies a registered packed Blender artifact
only after an explicitly trusted, fixed-worker reopen and measured roundtrip.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import TypedDict
from functools import wraps

from asset_library import AssetLibrary, read_source_bytes
from asset_library_contracts import MAX_FILE_BYTES, canonical_bytes, validate
from rig_errors import RigError, object_hash

VERSION = '1.0.0'
MAX_EXPORT_BYTES = 256 * 1024**2
MAX_CLOSURE = 256
FORMATS = ('glb', 'gltf', 'blend')


class ExportReceipt(TypedDict):
    schema_version: int
    kind: str
    reference: dict
    format: str
    entry: str
    inventory: list[dict]
    receipt_sha256: str


def fail(code, message):
    raise RigError('LIBRARY_EXPORT_' + code, message, stage='asset-export')


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _parent(destination):
    """Admit an existing non-symlink parent, including all ancestor components."""
    destination = Path(destination).absolute()
    if destination.name in ('', '.', '..') or os.path.lexists(destination):
        fail('DESTINATION', 'Choose a new export directory; existing paths are preserved.')
    for path in (destination.parent, *destination.parent.parents):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            fail('PATH', 'Destination ancestors must be actual directories without links/junctions.')
    return destination


def _closure(library, reference):
    pending = [reference]; seen = {}
    while pending:
        current = pending.pop()
        validate(current, 'AssetRef')
        key = (current['id'], current['version'], current['sha256'])
        if key in seen: continue
        if len(seen) >= MAX_CLOSURE: fail('LIMIT', 'Export closure exceeds 256 exact versions.')
        # resolve verifies every inventoried byte and recursively pinned dependency.
        manifest = library.resolve(current)
        seen[key] = dict(reference=deepcopy(current), manifest=manifest)
        pending.extend(x['target'] for x in manifest['dependencies'])
        pending.extend(manifest['provenance']['parents'])
        pending.extend(manifest['metadata']['style_refs'])
    return sorted(seen.values(), key=lambda x: (x['reference']['id'], x['reference']['version']))


def _capabilities(document):
    value = {'geometry', 'hierarchy'} if document.get('meshes') else {'hierarchy'}
    for key, label in [('materials','materials'), ('images','textures'), ('skins','skins'), ('animations','animation')]:
        if document.get(key): value.add(label)
    if any(p.get('targets') for m in document.get('meshes', []) for p in m.get('primitives', [])):
        value.add('morphs')
    return sorted(value)


def _equivalent(source, output):
    """Exact decoded glTF semantics and buffer bytes across container changes."""
    a = deepcopy(source.json); b = deepcopy(output.json)
    for doc in (a, b):
        for buffer in doc.get('buffers', []): buffer.pop('uri', None)
    if a != b or source.buffers != output.buffers:
        fail('LOSS', 'Export changed admitted glTF data or decoded buffer bytes.')
    return dict(ok=True, method='exact-gltf-document-and-buffer-comparison',
                document_sha256=object_hash(a), buffer_sha256=[_digest(x) for x in source.buffers])


def _optional_dependencies(function):
    @wraps(function)
    def run(*args, **kwargs):
        try: return function(*args, **kwargs)
        except ImportError as exc:
            fail('DEPENDENCY', 'Install optional requirements-rig.txt for verified geometry export: '+str(exc)[:512])
    return run


def _surface_signature(model):
    """World-space triangle corners, morph deltas, UVs and decoded PBR bindings.

    Cyclic corner rotation and triangle/mesh/vertex order may differ. Reversed
    winding, changed interior vertices, morph values, UVs or materials may not.
    """
    import io
    import numpy as np
    from PIL import Image
    geometry=model.geometry
    def image_signature(index):
        image=geometry.images[index]
        with Image.open(io.BytesIO(image.data)) as raster:
            raster.load(); rgba=raster.convert('RGBA')
            return dict(size=list(rgba.size),rgba_sha256=_digest(rgba.tobytes()))
    def texture_signature(index):
        texture=geometry.textures[index]
        sampler=geometry.samplers[texture['sampler']] if 'sampler' in texture else {}
        return dict(image=image_signature(texture['source']), sampler=dict(
            magFilter=sampler.get('magFilter',9729),minFilter=sampler.get('minFilter',9987),
            wrapS=sampler.get('wrapS',10497),wrapT=sampler.get('wrapT',10497)))
    def normalized(value, texture=False):
        if isinstance(value,dict):
            return {k:(texture_signature(v) if k=='index' and texture else normalized(v,k.endswith('Texture')))
                    for k,v in value.items() if k not in ('name','extras')}
        if isinstance(value,list): return [normalized(x) for x in value]
        if isinstance(value,(int,float)) and not isinstance(value,bool):return round(float(value),6)
        return value
    materials=[]
    for material in geometry.materials:
        item=deepcopy(material);item.setdefault('alphaMode','OPAQUE');item.setdefault('doubleSided',False)
        if item['alphaMode']=='MASK':item.setdefault('alphaCutoff',.5)
        item.setdefault('emissiveFactor',[0,0,0])
        pbr=item.setdefault('pbrMetallicRoughness',{})
        pbr.setdefault('baseColorFactor',[1,1,1,1]);pbr.setdefault('metallicFactor',1);pbr.setdefault('roughnessFactor',1)
        for name,field,default in [('KHR_materials_ior','ior',1.5),('KHR_materials_emissive_strength','emissiveStrength',1)]:
            extension=item.get('extensions',{}).get(name)
            if extension=={field:default}:item['extensions'].pop(name)
        if not item.get('extensions'):item.pop('extensions',None)
        materials.append(object_hash(normalized(item)))
    rows=[]
    for mesh in geometry.meshes:
        material=materials[mesh.material] if materials else None
        def corner(index):
            index=int(index)
            values=dict(position=np.round(mesh.positions[index],5).tolist(),
                morphs={name:{key:np.round(array[index],5).tolist() for key,array in target.items()}
                        for name,target in sorted(mesh.morphs.items())},
                defaults={k:round(float(v),6) for k,v in sorted(mesh.morph_defaults.items())},
                attributes={key:np.round(array[index],6).tolist() for key,array in sorted(mesh.attributes.items())})
            return canonical_bytes(values).decode('utf-8')
        for triangle in mesh.triangles:
            points=[corner(index) for index in triangle]
            ordered=min(tuple(points[i:]+points[:i]) for i in range(3))
            rows.append((material,ordered))
    return sorted(rows)

def _joint_signature(path, limits):
    """Named joint hierarchy/rest and inverse binds independent of node indices."""
    import numpy as np
    from rig_gltf import read_gltf, scene_graph
    document=read_gltf(path,limits);parents,world,_=scene_graph(document)
    nodes=document.json.get('nodes',[])
    joints=set(index for skin in document.json.get('skins',[]) for index in skin['joints'])
    names={index:nodes[index].get('name') for index in joints}
    if any(not isinstance(name,str) or not name for name in names.values()) or len(set(names.values()))!=len(names):
        fail('UNSUPPORTED','Native skeleton comparison needs unique named joints.')
    result={}
    for index in joints:
        parent=parents.get(index)
        while parent is not None and parent not in joints:parent=parents.get(parent)
        result[names[index]]=dict(parent=names[parent] if parent is not None else None,
            world=np.round(world[index],5).tolist(),inverse_bind=[])
    for skin in document.json.get('skins',[]):
        matrices=document.accessor(skin['inverseBindMatrices']).reshape(-1,4,4) if 'inverseBindMatrices' in skin else np.tile(np.eye(4),(len(skin['joints']),1,1))
        for index,matrix in zip(skin['joints'],matrices):
            result[names[index]]['inverse_bind'].append(np.round(matrix,5).tolist())
    for value in result.values():value['inverse_bind']=sorted(value['inverse_bind'],key=canonical_bytes)
    return result

def _native_compare(source_path, output_path, limits):
    """Measured rest/animation checks reuse the native model/deformation owner."""
    import numpy as np
    from asset_library_import import native_observation, skin_associations
    from rig_models import RigAsset
    from rig_interop import compare_imported
    source, before = native_observation(source_path, limits)
    output, after = native_observation(output_path, limits)
    for model in (source,output):
        estimate=sum(len(mesh.triangles)*3*(1024+128*(3+sum(array.shape[1] for array in mesh.attributes.values())+sum(array.shape[1] for target in mesh.morphs.values() for array in target.values()))) for mesh in model.geometry.meshes)
        if estimate>limits['memory_bytes']//4:fail('LIMIT','Decoded surface-comparison working set exceeds export memory budget.')
    if _joint_signature(source_path,limits) != _joint_signature(output_path,limits):
        fail('LOSS','Native reopen changed named joint hierarchy/rest matrices or inverse binds.')
    if _surface_signature(source) != _surface_signature(output):
        fail('LOSS', 'Native reopen changed decoded triangle geometry, morphs, UVs, PBR factors or texture pixels.')
    if before['triangles'] != after['triangles'] or before['morph_targets'] != after['morph_targets']:
        fail('LOSS', 'Native reopen changed triangle or morph-target count.')
    if before['textures'] != after['textures'] or before['materials'] != after['materials']:
        fail('LOSS', 'Native reopen changed material or texture inventory.')
    if not np.allclose(before['bounds'], after['bounds'], rtol=0, atol=1e-5):
        fail('LOSS', 'Native reopen changed measured geometry bounds beyond 1e-5 meters.')
    if skin_associations(source_path, limits)['rows'] != skin_associations(output_path, limits)['rows']:
        fail('LOSS', 'Native reopen changed rest-space skin associations.')
    if before['clips'] != after['clips']: fail('LOSS', 'Native reopen changed named animation clips.')
    comparison = None
    if source.clips:
        if not source.skeleton: fail('UNSUPPORTED', 'Native sampled object animation requires a separate evaluator.')
        rig = RigAsset(dict(skeleton=source.skeleton, controllers=dict(ik=[],constraints=[])),
                       source.geometry, source.weights, source.clips)
        comparison = compare_imported(rig, output)
        if not comparison['ok']: fail('LOSS', 'Native animation roundtrip exceeds measured deformation tolerance.')
    return dict(ok=True, before=before, after=after, animation=comparison,
                limitations=['Native geometry/material counts and bounds are measured; arbitrary shader equivalence and artistic appearance require visual review.'])


class ExportPipeline:
    def __init__(self, library: AssetLibrary, *, blender=None, timeout=60, memory_bytes=1024**3, cancelled=None):
        if not isinstance(library, AssetLibrary): fail('LIBRARY', 'An existing canonical AssetLibrary is required.')
        from asset_library_import import BlenderAdapter, integer
        self.library = library
        self.timeout = integer(timeout, 1, 300)
        self.memory = integer(memory_bytes, 384*1024**2, 4*1024**3)
        self.cancelled = cancelled or (lambda: False)
        self.blender = BlenderAdapter(blender, timeout=self.timeout, memory_bytes=self.memory, cancelled=self.cancelled)

    def capabilities(self):
        return dict(version=VERSION, formats=list(FORMATS), optional_unavailable=['obj','fbx','usd','alembic'],
                    publication='exclusive-atomic-directory', batch_maximum=8, batch_concurrency=1,
                    native_policy='explicit-trusted-packed-existing-artifact', production_approval=False)

    @_optional_dependencies
    def export(self, reference, destination, *, format='glb', runtime_path='runtime/asset.glb',
               required_capabilities=(), allowed_losses=(), trusted_native=False) -> ExportReceipt:
        from asset_library_import import _check_gltf, _gltf_json, relative
        from rig_contracts import DEFAULT_LIMITS
        from atomic_publish import _rename_no_replace
        if format not in FORMATS: fail('UNSUPPORTED', 'No verified exporter for requested format.')
        if type(trusted_native) is not bool: fail('REQUEST', 'Native trust must be an explicit boolean.')
        for value in (required_capabilities, allowed_losses):
            if not isinstance(value, (list,tuple)) or len(value)>64 or any(not isinstance(x,str) or len(x)>2048 for x in value):
                fail('REQUEST', 'Capabilities/losses must be bounded explicit string lists.')
        destination = _parent(destination); relative(runtime_path)
        if self.cancelled(): fail('CANCELLED', 'Cancelled before export.')
        closure = _closure(self.library, reference)
        manifest = next(x['manifest'] for x in closure if x['reference']==reference)
        runtime = self.library.read_file(reference, runtime_path)
        if len(runtime)>MAX_EXPORT_BYTES or len(runtime)*12+256*1024**2>self.memory:
            fail('LIMIT', 'Encoded runtime exceeds export working-set admission.')
        document, binary = _gltf_json(runtime, 'glb')
        if any('uri' in x for x in document.get('buffers',[])) or any('uri' in x for x in document.get('images',[])):
            fail('UNSUPPORTED', 'Export requires a self-contained canonical runtime GLB.')
        capabilities = _capabilities(document)
        if set(required_capabilities)-set(capabilities): fail('CAPABILITY', 'Requested capabilities are absent from actual runtime data.')
        limits = {**DEFAULT_LIMITS, 'source_bytes':MAX_EXPORT_BYTES, 'dependency_bytes':MAX_EXPORT_BYTES,
                  'memory_bytes':self.memory, 'timeout_seconds':self.timeout, 'influences':8}
        losses = []
        if format=='blend':
            if not trusted_native: fail('QUARANTINE', 'Native export requires explicit trust for the registered packed Blender artifact.')
            if 'native/asset.blend' not in {x['path'] for x in manifest['files']}:
                fail('UNAVAILABLE', 'No registered packed Blender artifact; import with native roundtrip enabled first.')
            from rig_errors import decode_json
            report = decode_json(self.library.read_file(reference, 'evidence/import.json'))
            losses = report.get('native_derivative_losses', [])
            if not isinstance(losses,list) or any(not isinstance(x,str) for x in losses):fail('EVIDENCE','Invalid native derivative loss inventory.')
            if set(losses)-set(allowed_losses): fail('LOSS_POLICY', 'Native derivative losses need explicit approval: '+str(losses))
        with tempfile.TemporaryDirectory(prefix='.hatch-export-', dir=destination.parent) as temporary:
            stage = Path(temporary); payload_dir=stage/'published'; payload_dir.mkdir(mode=0o700)
            source = stage/'source.glb'; source.write_bytes(runtime)
            captured, input_validation = _check_gltf(source, limits)
            if format in ('glb','gltf'):
                if format=='glb':
                    entry='asset.glb'; (payload_dir/entry).write_bytes(runtime)
                else:
                    entry='asset.gltf'; external=deepcopy(document)
                    if external.get('buffers'):
                        if len(external['buffers'])!=1 or binary is None:fail('UNSUPPORTED','Canonical GLB needs one embedded buffer.')
                        external['buffers'][0]['uri']='asset.bin'
                        (payload_dir/'asset.bin').write_bytes(binary[:external['buffers'][0]['byteLength']])
                    (payload_dir/entry).write_bytes(canonical_bytes(external))
                output, validation = _check_gltf(payload_dir/entry, limits)
                comparison = _equivalent(captured, output)
                native = None
            else:
                entry='asset.blend'
                native_payload=self.library.read_file(reference,'native/asset.blend')
                if not native_payload.startswith(b'BLENDER'):fail('FORMAT','Registered native artifact lacks a Blender header.')
                native_source=stage/'registered.blend';native_source.write_bytes(native_payload)
                executed=self.blender.execute('blend',native_source,stage/'native-check',trusted_blend=True,
                    fixture=bool(manifest['provenance'].get('fixture',False)))
                _, validation = _check_gltf(executed['outputs']['glb'], limits)
                comparison = _native_compare(source, executed['outputs']['glb'], limits)
                (payload_dir/entry).write_bytes(native_payload)
                native=dict(inspection=executed['inspection'], saved_and_reopened=True,
                            worker_sha256=executed['worker_sha256'], capability={k:v for k,v in executed['capability'].items() if k!='executable'})
            inventory=[]
            for path in sorted(payload_dir.iterdir()):
                payload=read_source_bytes(path,MAX_FILE_BYTES)
                inventory.append(dict(path=path.name,bytes=len(payload),sha256=_digest(payload)))
            if sum(x['bytes'] for x in inventory)>MAX_EXPORT_BYTES:fail('LIMIT','Output inventory exceeds bound.')
            receipt=dict(schema_version=1,kind='asset-library-export',tool_version=VERSION,
                reference=deepcopy(reference),format=format,entry=entry,inventory=inventory,
                source_identity=manifest['identity'],source_provenance=manifest['provenance'],
                source_publication=manifest['publication'],
                closure=[x['reference'] for x in closure],capabilities=capabilities,approved_losses=losses,
                input_validation=input_validation,output_validation=validation,comparison=comparison,native=native,
                publication='exclusive-atomic-directory',production_approved=False,
                limitations=['Export is a verified payload adapter; closure packages, license authorization, visual approval and installation are separate authorities.'])
            receipt['receipt_sha256']=object_hash(receipt)
            (payload_dir/'export.json').write_bytes(canonical_bytes(receipt))
            # All bytes/receipts complete before a single exclusive publication.
            if self.cancelled():fail('CANCELLED','Cancelled before export publication.')
            _parent(destination)
            _rename_no_replace(payload_dir,destination)
            return receipt

    def export_batch(self, requests):
        if not isinstance(requests,list) or not 1<=len(requests)<=8:fail('LIMIT','Export batch needs one to eight requests.')
        results=[]
        for request in requests:
            if not isinstance(request,dict):fail('REQUEST','Each export request must be a mapping.')
            if self.cancelled():fail('CANCELLED','Cancelled before next batch export.')
            results.append(self.export(**request))
        return results
