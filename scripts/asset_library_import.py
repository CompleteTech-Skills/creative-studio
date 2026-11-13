"""Bounded real-file asset imports; native owners retain geometry/storage authority.

No optional 3D package is imported until an actual geometry operation is requested.
All originals are snapshotted once using the library descriptor reader; native
parsers only see the private immutable snapshot, never the untrusted source tree.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import unquote, urlsplit

from asset_library import AssetLibrary, detect_format, read_source_bytes
from asset_library_contracts import (MAX_FILE_BYTES, MAX_VERSION_BYTES, canonical_bytes,
                                     seal, validate)
from rig_errors import RigError, decode_json, object_hash, timestamp

VERSION = '1.0.0'
MAX_FILES = 128
MAX_INPUT_BYTES = 96 * 1024**2
MAX_JOB_LOG = 2 * 1024**2
MAX_REPORT = 1024**2
BASIS = ('canonical', 'right-handed-z-up', 'left-handed-y-up')


def fail(code, message, asset=None):
    raise RigError('LIBRARY_IMPORT_' + code, message, stage='asset-import', asset=str(asset) if asset else None)


def dependency_errors(function):
    @wraps(function)
    def checked(*args,**kwargs):
        try:return function(*args,**kwargs)
        except ImportError as exc:
            fail('DEPENDENCY','Install optional requirements-rig.txt for real 3D operations: '+str(exc)[:512])
    return checked


def integer(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail('LIMIT', 'Expected a bounded integer.')
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        fail('LIMIT', 'Expected an exact finite integer.')
    if not minimum <= value <= maximum:
        fail('LIMIT', 'Integer outside configured resource range.')
    return int(value)


def relative(value):
    from rig_errors import portable_name
    if not isinstance(value, str) or len(value) > 1024 or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        fail('PATH', 'Source URI has invalid Unicode or excessive length.')
    if unicodedata.normalize('NFC',value)!=value:fail('PATH','Source paths require NFC Unicode spelling.')
    devices={'CON','PRN','AUX','NUL','CONIN$','CONOUT$',*[f'COM{i}' for i in '123456789¹²³'],*[f'LPT{i}' for i in '123456789¹²³']}
    for part in value.split('/'):
        if part.startswith(' ') or part.endswith((' ','.')) or part.split('.')[0].upper() in devices:
            fail('PATH','Nonportable Windows path alias.',value)
    try: result = portable_name(value)
    except (ValueError, UnicodeError) as exc: fail('PATH', str(exc))
    if result != value:
        fail('PATH', 'Source paths must use their canonical portable spelling.', value)
    return result


def document_fixture(document):
    """Reserved provenance markers survive asset/scene/node/material exports."""
    pending=[document];visited=0
    while pending:
        value=pending.pop();visited+=1
        if visited>1000000:fail('LIMIT','Provenance inspection exceeds bounded JSON budget.')
        if isinstance(value,dict):
            if value.get('hatchAssetSourceFixture') is True:return True
            if any(isinstance(value.get(k),dict) and value[k].get('fixture') is True for k in ('hatchSource','hatchRig')):return True
            pending.extend(value.values())
        elif isinstance(value,list):pending.extend(value)
    return False


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _json(payload):
    # Bound Python container allocation before the ordinary JSON decoder builds
    # the object graph. Embedded binary strings do not consume structural tokens.
    if not isinstance(payload,bytes) or len(payload)>32*1024**2:fail('LIMIT','JSON exceeds the bounded import profile.')
    quoted=False;escaped=False;depth=0;tokens=0
    for byte in payload:
        if quoted:
            if escaped:escaped=False
            elif byte==92:escaped=True
            elif byte==34:quoted=False
        elif byte==34:quoted=True
        elif byte in (123,91):
            depth+=1;tokens+=1
            if depth>128:fail('LIMIT','JSON nesting exceeds admission.')
        elif byte in (125,93):depth-=1
        elif byte in (44,58):tokens+=1
        if tokens>500000:fail('LIMIT','JSON structural complexity exceeds admission.')
    try: return decode_json(payload)
    except RigError: raise
    except (ValueError, TypeError, UnicodeError) as exc: fail('JSON', 'Invalid asset JSON: ' + str(exc))


def _gltf_json(payload, fmt):
    binary = None
    if fmt == 'glb':
        if len(payload) < 20: fail('GLB', 'Truncated GLB header.')
        magic, version, length = struct.unpack_from('<4sII', payload)
        if magic != b'glTF' or version != 2 or length != len(payload): fail('GLB', 'Invalid GLB container.')
        offset = 12; chunks = []
        while offset < len(payload):
            if len(chunks) >= 64 or offset+8 > len(payload): fail('GLB', 'Truncated/excessive GLB chunks.')
            size, kind = struct.unpack_from('<II', payload, offset); offset += 8
            if size % 4 or offset+size > len(payload): fail('GLB', 'Invalid GLB chunk bounds.')
            chunks.append((kind, payload[offset:offset+size])); offset += size
        if not chunks or chunks[0][0] != 0x4E4F534A or sum(k == 0x4E4F534A for k, _ in chunks) != 1:
            fail('GLB', 'GLB requires exactly one leading JSON chunk.')
        if any(k not in (0x4E4F534A, 0x004E4942) for k, _ in chunks) or sum(k == 0x004E4942 for k, _ in chunks) > 1:
            fail('UNSUPPORTED', 'Unknown/duplicate GLB chunks require a documented conversion adapter.')
        document = _json(chunks[0][1]); binary = next((b for k, b in chunks if k == 0x004E4942), None)
    else: document = _json(payload)
    if not isinstance(document, dict) or not isinstance(document.get('asset'), dict) or document['asset'].get('version') != '2.0':
        fail('FORMAT', 'Only genuine glTF 2.0 documents are supported.')
    from rig_gltf import MATERIAL_EXTENSIONS
    for key in ('extensionsRequired', 'extensionsUsed'):
        items = document.get(key, [])
        if not isinstance(items, list) or len(items) > 128 or any(not isinstance(v, str) for v in items) or len(set(items)) != len(items):
            fail('EXTENSION', 'Extensions must be a bounded unique string list.')
        missing = set(items) - MATERIAL_EXTENSIONS
        if missing: fail('UNSUPPORTED', 'Unsupported glTF extensions: ' + ', '.join(sorted(missing)))
    for key, maximum in [('nodes',4096), ('meshes',4096), ('skins',256), ('animations',64), ('images',256), ('buffers',128)]:
        if not isinstance(document.get(key, []), list) or len(document.get(key, [])) > maximum:
            fail('LIMIT', 'Invalid/excessive glTF collection: ' + key)
    return document, binary


def _uri_path(uri):
    if not isinstance(uri, str) or len(uri) > MAX_FILE_BYTES*2: fail('URI', 'Expected a bounded URI.')
    parsed = urlsplit(uri)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        fail('URI', 'Only local relative resources and typed embedded data are allowed.')
    try: name = unquote(parsed.path, errors='strict')
    except UnicodeError as exc: fail('URI', str(exc))
    return relative(name)


def _embedded(uri, expected):
    if not isinstance(uri, str) or not uri.startswith('data:'): return None
    prefix, comma, encoded = uri.partition(',')
    if not comma or not prefix.endswith(';base64') or len(encoded)*3//4 > MAX_FILE_BYTES:
        fail('URI', 'Embedded data must be bounded base64.')
    mime = prefix[5:-7]
    if mime not in expected: fail('URI', 'Unsupported embedded MIME type: ' + mime)
    try: payload = base64.b64decode(encoded, validate=True)
    except ValueError as exc: fail('URI', str(exc))
    if not payload: fail('URI', 'Embedded resources cannot be empty.')
    return payload


def _integer_document(document):
    """Normalize only declared glTF integer fields, never opaque extras/metadata."""
    value=deepcopy(document)
    def fields(record,names):
        if not isinstance(record,dict):fail('FORMAT','Expected a typed glTF object.')
        for name in names:
            if name in record:record[name]=integer(record[name],0,2**31-1)
    fields(value,('scene',))
    for key,names in [('buffers',('byteLength',)),('bufferViews',('buffer','byteOffset','byteLength','byteStride','target')),
                      ('images',('bufferView',)),('textures',('source','sampler')),
                      ('samplers',('magFilter','minFilter','wrapS','wrapT'))]:
        for record in value.get(key,[]):fields(record,names)
    for item in value.get('accessors',[]):
        fields(item,('bufferView','byteOffset','componentType','count'))
        if 'sparse' in item:
            fields(item['sparse'],('count',));fields(item['sparse']['indices'],('bufferView','byteOffset','componentType'))
            fields(item['sparse']['values'],('bufferView','byteOffset'))
    for item in value.get('nodes',[]):
        fields(item,('camera','mesh','skin'))
        if 'children' in item:item['children']=[integer(v,0,2**31-1) for v in item['children']]
    for scene in value.get('scenes',[]):scene['nodes']=[integer(v,0,2**31-1) for v in scene.get('nodes',[])]
    for skin in value.get('skins',[]):
        fields(skin,('skeleton','inverseBindMatrices'));skin['joints']=[integer(v,0,2**31-1) for v in skin.get('joints',[])]
    for mesh in value.get('meshes',[]):
        for primitive in mesh.get('primitives',[]):
            fields(primitive,('indices','material','mode'))
            primitive['attributes']={k:integer(v,0,2**31-1) for k,v in primitive.get('attributes',{}).items()}
            if 'targets' in primitive:primitive['targets']=[{k:integer(v,0,2**31-1) for k,v in target.items()} for target in primitive['targets']]
    for animation in value.get('animations',[]):
        for channel in animation.get('channels',[]):fields(channel,('sampler',));fields(channel['target'],('node',))
        for sampler in animation.get('samplers',[]):fields(sampler,('input','output'))
    extensions={'KHR_materials_clearcoat':('clearcoatTexture','clearcoatRoughnessTexture','clearcoatNormalTexture'),
                'KHR_materials_sheen':('sheenColorTexture','sheenRoughnessTexture'),
                'KHR_materials_specular':('specularTexture','specularColorTexture'),
                'KHR_materials_transmission':('transmissionTexture',),'KHR_materials_volume':('thicknessTexture',),
                'KHR_materials_iridescence':('iridescenceTexture','iridescenceThicknessTexture'),
                'KHR_materials_anisotropy':('anisotropyTexture',)}
    def texture(record):
        fields(record,('index','texCoord'))
        if 'KHR_texture_transform' in record.get('extensions',{}):fields(record['extensions']['KHR_texture_transform'],('texCoord',))
    for material in value.get('materials',[]):
        for name in ('normalTexture','occlusionTexture','emissiveTexture'):
            if name in material:texture(material[name])
        for name in ('baseColorTexture','metallicRoughnessTexture'):
            if name in material.get('pbrMetallicRoughness',{}):texture(material['pbrMetallicRoughness'][name])
        for extension,names in extensions.items():
            for name in names:
                if name in material.get('extensions',{}).get(extension,{}):texture(material['extensions'][extension][name])
    return value


class SourceSnapshot:
    """Private source closure with exact originals and deterministic source mapping."""
    def __init__(self, source, destination, *, maximum_bytes=MAX_INPUT_BYTES, cancelled=None):
        self.root = Path(destination).resolve(); self.root.mkdir(mode=0o700)
        self.maximum = integer(maximum_bytes, 1, MAX_INPUT_BYTES)
        self.cancelled = cancelled or (lambda: False)
        source = Path(source).absolute()
        if source.is_symlink(): fail('SYMLINK', 'Source leaf cannot be a symlink.')
        self.source_root = source.parent
        self.source = self.source_root / source.name
        self.files = {}; self.roles = {}; self.embedded = []; self._aliases = {}; self._directories = {}
        self.entry = self.add(relative(source.name), 'source')
        payload = self.files[self.entry]
        self.format, self.mime = detect_format(payload)
        if payload.startswith(b'BLENDER'): self.format, self.mime = 'blend', 'application/octet-stream'
        if self.format in ('glb', 'gltf'):
            self.document, _ = _gltf_json(payload, self.format)
            self._gltf_closure()
        elif self.format == 'obj':
            self.document = None; self._obj_closure()
        elif self.format == 'blend': self.document = None
        else: fail('UNSUPPORTED', 'Content is not a supported GLB/glTF/OBJ/trusted packed Blender asset.')

    def check_cancelled(self):
        if self.cancelled(): fail('CANCELLED', 'Import cancelled before publication.')

    def add(self, name, role):
        self.check_cancelled(); name = relative(name)
        alias = name.casefold()
        if alias in self._aliases and self._aliases[alias] != name: fail('ALIAS', 'Case-aliased source resources are not portable.')
        if alias in self._directories:fail('ALIAS','Source file aliases an existing resource directory.')
        parts=name.split('/')
        for depth in range(1,len(parts)):
            directory='/'.join(parts[:depth]);folded=directory.casefold()
            if folded in self._aliases:fail('ALIAS','Source resource directory aliases a file.')
            if folded in self._directories and self._directories[folded]!=directory:fail('ALIAS','Source directory spelling is not portable across case-insensitive hosts.')
        for depth in range(1,len(parts)):
            directory='/'.join(parts[:depth]);self._directories[directory.casefold()]=directory
        self._aliases[alias] = name
        if name not in self.files:
            if len(self.files) >= MAX_FILES: fail('LIMIT', 'Source dependency count exceeded.')
            payload = read_source_bytes(self.source_root / name, min(self.maximum, MAX_FILE_BYTES))
            if sum(map(len, self.files.values())) + len(payload) > self.maximum:
                fail('LIMIT', 'Complete source closure exceeds the aggregate byte budget.')
            self.files[name] = payload
            target = self.root / name; target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream: stream.write(payload)
        self.roles.setdefault(name, set()).add(role)
        return name

    def _resource(self, uri, role, expected):
        embedded = _embedded(uri, expected)
        if embedded is not None:
            self.embedded.append(dict(role=role, bytes=len(embedded), sha256=digest(embedded)))
            if sum(x['bytes'] for x in self.embedded) + sum(map(len, self.files.values())) > self.maximum*2:
                fail('LIMIT', 'Embedded source working-set budget exceeded.')
            return
        self.add(_uri_path(uri), role)

    def _gltf_closure(self):
        for buffer in self.document.get('buffers', []):
            if not isinstance(buffer, dict): fail('FORMAT', 'Invalid buffer record.')
            integer(buffer.get('byteLength'), 1, self.maximum)
            if 'uri' in buffer: self._resource(buffer['uri'], 'buffer', ('application/octet-stream','application/gltf-buffer'))
        for image in self.document.get('images', []):
            if not isinstance(image, dict) or ('uri' in image) == ('bufferView' in image): fail('FORMAT', 'Images need exactly one URI or bufferView.')
            if 'uri' in image: self._resource(image['uri'], 'texture', ('image/png','image/jpeg'))
        # Build the operational reader from exact captured buffers. JSON
        # integer spellings such as 3.0 are valid; normalize only known fields
        # in a copy while preserving every original file byte unchanged.
        from rig_contracts import DEFAULT_LIMITS
        from rig_gltf import GltfDocument
        _,binary=_gltf_json(self.files[self.entry],self.format)
        self.document=_integer_document(self.document)
        limits={**DEFAULT_LIMITS,'source_bytes':MAX_FILE_BYTES,'dependency_bytes':self.maximum,'influences':8}
        buffers=[]
        for index,record in enumerate(self.document.get('buffers',[])):
            if 'uri' in record:
                content=_embedded(record['uri'],('application/octet-stream','application/gltf-buffer'))
                if content is None:content=self.files[_uri_path(record['uri'])]
            else:
                content=binary if index==0 else None
            if content is None or len(content)<record['byteLength']:fail('FORMAT','Missing or truncated buffer data.')
            if 'uri' not in record and len(content)-record['byteLength']>3:fail('GLB','Invalid BIN padding.')
            buffers.append(content[:record['byteLength']])
        self.native_document=GltfDocument(self.root/self.entry,self.document,buffers,limits,
                                         [self.root/name for name in self.files])
        for image in self.document.get('images', []):
            if 'uri' in image: data = self.native_document.uri(image['uri'])
            else:
                data, start, size, _ = self.native_document.view(image['bufferView']); data = data[start:start+size]
                self.embedded.append(dict(role='texture', bytes=len(data), sha256=digest(data)))
            fmt, mime = detect_format(data)
            declared=image.get('mimeType')
            if isinstance(image.get('uri'),str) and image['uri'].startswith('data:'):
                declared=image['uri'][5:].split(';',1)[0]
            if fmt not in ('png','jpeg') or (declared is not None and declared != mime):
                fail('TEXTURE', 'Image content/header does not match its declared supported MIME type.')

    def _obj_closure(self):
        import shlex
        try: lines = io.StringIO(self.files[self.entry].decode('utf-8'),newline=None)
        except UnicodeError as exc: fail('FORMAT', str(exc))
        for number,line in enumerate(lines):
            if number>=1000000 or len(line)>65536:fail('LIMIT','OBJ text record budget exceeded.')
            tokens = shlex.split(line, comments=True)
            if tokens and tokens[0] not in {'v','vn','vt','f','o','g','s','mtllib','usemtl'}:
                fail('UNSUPPORTED','Unsupported OBJ directive: '+tokens[0])
            if tokens and tokens[0] in ('v','vn') and len(tokens)!=4:
                fail('UNSUPPORTED','OBJ profile requires explicit three-component positions/normals.')
            if tokens and tokens[0]=='vt' and len(tokens)!=3:
                fail('UNSUPPORTED','OBJ profile requires two-component UV coordinates.')
            if tokens and tokens[0] == 'mtllib':
                if len(tokens) != 2: fail('UNSUPPORTED', 'OBJ profile requires one quoted MTL filename per declaration.')
                mtl = self.add(relative(tokens[1]), 'material')
                for number,item in enumerate(io.StringIO(self.files[mtl].decode('utf-8'),newline=None)):
                    if number>=1000000 or len(item)>65536:fail('LIMIT','MTL text record budget exceeded.')
                    words = shlex.split(item, comments=True)
                    if words and words[0].startswith(('map_', 'bump', 'disp', 'decal')):
                        if words[0] != 'map_Kd' or len(words) != 2: fail('UNSUPPORTED', 'OBJ profile supports only plain diffuse texture maps.')
                        self.add(relative((PurePosixPath(mtl).parent / words[1]).as_posix()), 'texture')

    def inventory(self):
        return [dict(original_path=p, sha256=digest(b), bytes=len(b), roles=sorted(self.roles[p]))
                for p,b in sorted(self.files.items())]


def native_observation(path, limits):
    """Actual native geometry inspection, not an independent-reader approval."""
    from rig_import import load_character_model
    model = load_character_model(path, limits=limits)
    geometry = model.geometry
    import numpy as np
    bounds = [np.min([m.positions.min(axis=0) for m in geometry.meshes], axis=0).tolist(),
              np.max([m.positions.max(axis=0) for m in geometry.meshes], axis=0).tolist()]
    return model, dict(vertices=geometry.vertex_count, triangles=geometry.triangle_count,
        bounds=bounds, meshes=len(geometry.meshes),
        skins=len(model.weights), bones=len(model.skeleton['bones']) if model.skeleton else 0,
        morph_targets=sum(len(m.morphs) for m in geometry.meshes),
        clips=sorted(model.clips), materials=len(geometry.materials), textures=len(geometry.images),
        independent=False, reader='hatch-native')


def _pack_glb(document, buffers, image_payloads, *, unit_scale=1., basis='canonical', ground_offset=0., fixture=False):
    """Preserve all declared glTF objects/channels; add explicit basis root only."""
    import numpy as np
    value = deepcopy(document); blob = bytearray(); offsets = []
    for buffer in buffers:
        blob.extend(b'\0' * (-len(blob)%4)); offsets.append(len(blob)); blob.extend(buffer)
    for view in value.get('bufferViews', []):
        index = integer(view.get('buffer'), 0, len(buffers)-1)
        view['byteOffset'] = integer(view.get('byteOffset',0),0,len(buffers[index])) + offsets[index]
        view['buffer'] = 0
    for image, data in zip(value.get('images', []), image_payloads):
        if 'uri' in image:
            blob.extend(b'\0' * (-len(blob)%4)); start=len(blob); blob.extend(data)
            value.setdefault('bufferViews', []).append(dict(buffer=0, byteOffset=start, byteLength=len(data)))
            image.pop('uri'); image['bufferView']=len(value['bufferViews'])-1
            image['mimeType']=detect_format(data)[1]
    value['buffers']=[dict(byteLength=len(blob))]
    if fixture and not document_fixture(value):
        # An inert, unreferenced metadata node preserves arbitrary original
        # asset/scene extras and all original hierarchy/channel indices.
        value.setdefault('nodes',[]).append(dict(name='HatchAssetFixtureProvenance',
            extras=dict(hatchAssetSourceFixture=True)))
    matrix = np.eye(4)
    if basis == 'right-handed-z-up': matrix[:3,:3]=[[1,0,0],[0,0,1],[0,-1,0]]
    elif basis == 'left-handed-y-up': matrix[:3,:3]=[[1,0,0],[0,1,0],[0,0,-1]]
    matrix[:3,:3] *= unit_scale; matrix[1,3] = ground_offset
    if not np.allclose(matrix, np.eye(4)):
        nodes=value.setdefault('nodes',[])
        original_count=len(nodes);wrapped={}
        for scene in value.get('scenes', []):
            mapped=[]
            for index in scene.get('nodes',[]):
                index=integer(index,0,original_count-1)
                if index not in wrapped:
                    wrapped[index]=len(nodes)
                    nodes.append(dict(name='HatchExplicitNormalization'+str(index),children=[index],matrix=matrix.T.ravel().tolist()))
                mapped.append(wrapped[index])
            scene['nodes']=mapped
        if not value.get('scenes'): fail('NORMALIZATION','An explicit scene is required for root normalization.')
        # This reserved native extension stores world-rest tail coordinates.
        # Convert its known geometric value, not arbitrary opaque metadata.
        for node in nodes[:original_count]:
            extra=node.get('extras',{})
            bone=extra.get('hatchBone',{}) if isinstance(extra,dict) else {}
            if isinstance(bone,dict) and 'tail' in bone:
                tail=np.asarray(bone['tail'],dtype=float)
                if tail.shape!=(3,) or not np.isfinite(tail).all():fail('NORMALIZATION','Invalid reserved native bone tail metadata.')
                bone['tail']=(matrix[:3,:3]@tail+matrix[:3,3]).tolist()
    encoded=canonical_bytes(value); encoded+=b' '*(-len(encoded)%4)
    binary=bytes(blob)+b'\0'*(-len(blob)%4)
    size=12+8+len(encoded)+(8+len(binary) if binary else 0)
    return struct.pack('<4sII',b'glTF',2,size)+struct.pack('<II',len(encoded),0x4E4F534A)+encoded+(struct.pack('<II',len(binary),0x004E4942)+binary if binary else b'')


def normalize_gltf(snapshot, destination, *, unit_scale=1., basis='canonical', ground=False, limits=None, fixture=False):
    if isinstance(unit_scale,bool) or not isinstance(unit_scale,(int,float)) or not math.isfinite(unit_scale) or not 1e-6 <= unit_scale <= 1e6:
        fail('NORMALIZATION','Unit scale must be finite in [1e-6,1e6].')
    if basis not in BASIS or type(ground) is not bool: fail('NORMALIZATION','Unsupported basis/ground policy.')
    document=snapshot.native_document
    images=[]
    for image in document.json.get('images',[]):
        if 'uri' in image: data=document.uri(image['uri'])
        else:
            content,start,length,_=document.view(image['bufferView']);data=content[start:start+length]
        images.append(data)
    payload=_pack_glb(document.json,document.buffers,images,unit_scale=float(unit_scale),basis=basis,fixture=fixture)
    target=Path(destination);target.write_bytes(payload)
    observation=_native_or_unavailable(target, limits)
    offset=0.
    if ground:
        if 'bounds' not in observation:fail('UNSUPPORTED','Ground alignment requires the supported native geometry profile.')
        offset=-float(observation['bounds'][0][1])
        payload=_pack_glb(document.json,document.buffers,images,unit_scale=float(unit_scale),basis=basis,ground_offset=offset,fixture=fixture)
        target.write_bytes(payload);observation=_native_or_unavailable(target,limits)
    if len(payload)>MAX_FILE_BYTES: fail('LIMIT','Normalized GLB exceeds library file bound.')
    report=dict(source_basis=basis, source_unit_scale=float(unit_scale), ground_offset=offset,
                canonical='right-handed-y-up-z-forward', method='explicit-root-matrix',
                integer_representation='known glTF integer fields normalized; originals retained',
                original_nodes=len(document.json.get('nodes',[])), preserved_skins=len(document.json.get('skins',[])),
                preserved_animations=len(document.json.get('animations',[])),
                preserved_materials=len(document.json.get('materials',[])), losses=[])
    return target, observation, report


def _stop(process):
    # Never stop after only the leader exits: descendants can ignore SIGTERM.
    if os.name == 'posix':
        for operation in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(process.pid, operation)
            except ProcessLookupError: pass
            if operation == signal.SIGTERM: time.sleep(.05)
    else: process.kill()
    try: process.wait(timeout=2)
    except subprocess.TimeoutExpired: fail('PROCESS','Unable to reap owned worker.')


class BlenderAdapter:
    """Fixed worker, private source snapshot, Linux process/resource enforcement.

    Untrusted .blend is quarantined. Explicitly trusted packed .blend may be
    inspected with autoexec disabled and linked/scripts/drivers rejected.
    Other OS adapters report unavailable rather than pretend Linux enforcement.
    """
    def __init__(self, executable=None, *, timeout=60, memory_bytes=1024**3, cancelled=None):
        self.executable=Path(executable or os.environ.get('HATCH_BLENDER','')).absolute()
        self.timeout=integer(timeout,1,300);self.memory=integer(memory_bytes,384*1024**2,4*1024**3)
        self.cancelled=cancelled or (lambda:False);self._version=None

    def _run(self, command, root):
        if not sys.platform.startswith('linux') or not Path('/usr/bin/prlimit').is_file():
            fail('UNAVAILABLE','Trusted Blender worker currently requires Linux prlimit/process groups; other hosts are quarantined.')
        root=Path(root);log=root/'process.log'
        resource_command=['/usr/bin/prlimit','--as='+str(self.memory*8),'--cpu='+str(self.timeout+2),
                          '--fsize='+str(MAX_FILE_BYTES),'--nofile=128:128','--',*command]
        env={key:os.environ[key] for key in ('PATH','LANG','LC_ALL') if key in os.environ}
        env.update(BLENDER_USER_CONFIG=str(root/'config'),BLENDER_USER_SCRIPTS=str(root/'scripts'),
                   BLENDER_USER_DATAFILES=str(root/'data'),TMPDIR=str(root),OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2')
        start=time.monotonic(); peak=0
        with log.open('xb') as stream:
            try: process=subprocess.Popen(resource_command,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,
                                          env=env,cwd=root,start_new_session=True)
            except OSError as exc: fail('UNAVAILABLE','Cannot launch trusted Blender: '+str(exc))
            reason=None
            try:
                while process.poll() is None:
                    if self.cancelled(): reason='CANCELLED';break
                    if time.monotonic()-start>self.timeout: reason='TIMEOUT';break
                    if log.stat().st_size>MAX_JOB_LOG:reason='LOG_LIMIT';break
                    resident=0
                    for member in Path('/proc').iterdir():
                        if not member.name.isdigit():continue
                        try:
                            fields=(member/'stat').read_text().rsplit(')',1)[1].split()
                            if int(fields[2])!=process.pid:continue
                            line=next((l for l in (member/'status').read_text().splitlines() if l.startswith('VmRSS:')),None)
                            if line:resident+=int(line.split()[1])*1024
                        except (FileNotFoundError,ProcessLookupError,PermissionError):pass
                    peak=max(peak,resident)
                    if peak>self.memory:reason='MEMORY';break
                    time.sleep(.02)
                if reason:
                    _stop(process);fail(reason,'Trusted Blender worker exceeded '+reason.lower()+' admission.')
                process.wait()
                if process.returncode: fail('WORKER','Blender failed: '+log.read_bytes()[-2048:].decode('utf-8',errors='replace'))
                if log.stat().st_size>MAX_JOB_LOG: fail('LOG_LIMIT','Worker log exceeds configured bound.')
            finally:
                _stop(process)
        return dict(seconds=time.monotonic()-start,peak_rss_bytes=peak,log_sha256=digest(log.read_bytes()),
                    command_policy='fixed-package-worker/private-cwd/no-shell/factory/autoexec-disabled')

    def capabilities(self):
        if self._version:return deepcopy(self._version)
        if not self.executable.is_file() or self.executable.is_symlink(): fail('UNAVAILABLE','Choose an installed regular Blender executable.')
        with tempfile.TemporaryDirectory(prefix='hatch-library-blender-probe-') as temp:
            self._run([str(self.executable),'--version'],Path(temp))
            text=(Path(temp)/'process.log').read_text(errors='replace')
            match=re.search(r'Blender (\d+)\.(\d+)\.(\d+)',text)
            if not match or tuple(map(int,match.groups()))[:2]!=(4,5):fail('VERSION','This adapter is exercised with Blender4.5 LTS only.')
            self._version=dict(name='Blender',version='.'.join(match.groups()),executable=str(self.executable),
                               trusted_scripts=True,untrusted_blend=False,platform='linux',executed_probe=True)
            return deepcopy(self._version)

    @dependency_errors
    def execute(self, mode, source, destination, *, parameters=None, trusted_blend=False, fixture=False):
        capability=self.capabilities(); destination=Path(destination);destination.mkdir(mode=0o700)
        if mode=='blend' and not trusted_blend:fail('QUARANTINE','Untrusted .blend requires explicit trusted-packed policy; no file was opened.')
        worker=read_source_bytes(Path(__file__).with_name('asset_import_worker.py'),MAX_REPORT)
        (destination/'worker.py').write_bytes(worker)
        outputs={k:str(destination/v) for k,v in dict(blend='asset.blend',glb='roundtrip.glb',preview='preview.png',report='report.json').items()}
        job=dict(mode=mode,input=str(source) if source else None,parameters=parameters,fixture=fixture,**outputs)
        (destination/'job.json').write_bytes(canonical_bytes(job))
        execution=self._run([str(self.executable),'--background','--factory-startup','--disable-autoexec','--threads','2',
                             '--python-exit-code','1','--python',str(destination/'worker.py'),'--',str(destination/'job.json')],destination)
        report=_json(read_source_bytes(destination/'report.json',MAX_REPORT))
        if not isinstance(report,dict) or str(report.get('blender_version','')).split(' ')[0]!=capability['version'] or not report.get('vertices',0):
            fail('OUTPUT','Missing/inconsistent actual Blender inspection.')
        if mode!='reopen':
            for key in ('blend','glb','preview'):read_source_bytes(outputs[key],MAX_FILE_BYTES)
            from PIL import Image
            with Image.open(outputs['preview']) as image:
                image.load()
                if image.size!=(128,128) or image.format!='PNG':fail('OUTPUT','Blender preview is not the requested decoded PNG.')
            reopened=self.execute('reopen',outputs['blend'],destination/'reopened',trusted_blend=True)
            if reopened['inspection']['vertices']!=report['vertices'] or reopened['inspection']['armatures']!=report['armatures']:
                fail('OUTPUT','Saved Blender file did not independently reopen with its geometry/rig.')
        return dict(inspection=report,execution=execution,capability=capability,worker_sha256=digest(worker),outputs=outputs)


def _graph_admission(document):
    nodes=document.get('nodes',[]);parents={};children={};degree=[0]*len(nodes)
    for index,node in enumerate(nodes):
        if not isinstance(node,dict) or not isinstance(node.get('children',[]),list):fail('GRAPH','Invalid scene node.')
        linked=[integer(v,0,len(nodes)-1) for v in node.get('children',[])]
        if len(set(linked))!=len(linked):fail('GRAPH','Repeated child in hierarchy.')
        children[index]=linked
        for child in linked:
            if child in parents:fail('GRAPH','A node cannot have multiple transform parents.')
            parents[child]=index;degree[child]+=1
    ready=[i for i,v in enumerate(degree) if not v];seen=0;depth=[1]*len(nodes)
    while ready:
        current=ready.pop();seen+=1
        if depth[current]>128:fail('LIMIT','Node hierarchy exceeds 128 levels.')
        for child in children[current]:
            depth[child]=max(depth[child],depth[current]+1);degree[child]-=1
            if not degree[child]:ready.append(child)
    if seen!=len(nodes):fail('GRAPH','Node hierarchy has a cycle.')
    for scene in document.get('scenes',[]):
        roots=scene.get('nodes',[])
        if len(set(roots))!=len(roots) or any(v in parents for v in roots):fail('GRAPH','Invalid selected scene roots.')


def _check_gltf(path, limits, captured_document=None):
    """Bounded accessor/graph and independent Khronos checks before publication."""
    from rig_gltf import read_gltf, scene_graph, WIDTHS
    from rig_interop import validate_gltf
    document=captured_document if captured_document is not None else read_gltf(path,limits)
    accessors=document.json.get('accessors',[])
    if len(accessors)>16384:fail('LIMIT','Too many accessors.')
    allocation=0
    for item in accessors:
        if not isinstance(item,dict):fail('FORMAT','Accessor must be an object.')
        count=integer(item.get('count'),1,limits['vertices']*16)
        allocation+=count*WIDTHS.get(item.get('type'),128)*8
        if allocation>limits['memory_bytes']//2:fail('LIMIT','Expanded accessor working set exceeds admission.')
    _graph_admission(document.json)
    try:scene_graph(document)
    except RigError:raise
    except (TypeError,ValueError,KeyError,IndexError,RecursionError) as exc:fail('FORMAT','Invalid native scene graph: '+str(exc)[:1024])
    for i in range(len(accessors)):document.accessor(i)
    report=validate_gltf(path,timeout=min(120,limits['timeout_seconds']))
    if not report.get('ok'):fail('VALIDATION','Independent Khronos reader rejected actual output.')
    return document,report


def skin_associations(path,limits):
    """Verify rest-space vertex/joint-weight associations across Blender export.

    Sorting ignores harmless primitive/vertex reordering and normal splits.
    Unnamed/ambiguous joints require a separate correspondence adapter.
    """
    from rig_gltf import read_gltf,scene_graph
    import numpy as np
    document=read_gltf(path,limits);_,world,order=scene_graph(document)
    value=document.json;rows=set();used_names=set();joint_names={}
    for skin in value.get('skins',[]):
        names=[]
        for index in skin['joints']:
            name=value['nodes'][index].get('name')
            if not isinstance(name,str) or not name:fail('UNSUPPORTED','Blender skin verification requires named joints.')
            if index not in joint_names and name in used_names:fail('UNSUPPORTED','Blender skin verification requires unambiguous joint names.')
            joint_names[index]=name;used_names.add(name);names.append(name)
    maximum=0
    for node_index in order:
        node=value['nodes'][node_index]
        if 'mesh' not in node or 'skin' not in node:continue
        skin=value['skins'][node['skin']];names=[joint_names[i] for i in skin['joints']]
        for primitive in value['meshes'][node['mesh']]['primitives']:
            attrs=primitive['attributes'];positions=document.accessor(attrs['POSITION'])
            positions=(np.c_[positions,np.ones(len(positions))]@world[node_index].T)[:,:3]
            groups=sorted(int(k[7:]) for k in attrs if k.startswith('JOINTS_'))
            pairs=[(document.accessor(attrs['JOINTS_'+str(g)]),document.accessor(attrs['WEIGHTS_'+str(g)])) for g in groups]
            for vertex,position in enumerate(positions):
                weights={}
                for joints,values in pairs:
                    for joint,weight in zip(joints[vertex],values[vertex]):
                        if weight>0:
                            name=names[int(joint)];weights[name]=weights.get(name,0.)+float(weight)
                total=sum(weights.values())
                if total<=0:fail('LOSS','A declared skinned vertex has no positive binding.')
                maximum=max(maximum,len(weights))
                association=tuple(sorted((name,round(weight/total,6)) for name,weight in weights.items() if round(weight/total,6)>0))
                rows.add((tuple(round(float(v),5) for v in position),association))
    return dict(rows=sorted(rows),max_influences=maximum)


def _native_or_unavailable(path,limits):
    try:
        _,result=native_observation(path,limits)
        return result
    except (TypeError,KeyError,IndexError,AttributeError) as exc:
        return dict(independent=False,reader='hatch-native',status='unsupported-native-profile',
                    diagnostic=dict(code='NATIVE_METADATA_PROFILE',message=str(exc)[:1024]))
    except RigError as exc:
        # Preserve complete glTF data rather than falsely claim the positive-
        # scale native rig profile can evaluate every valid reflected skin.
        if exc.report['code'] not in ('RIG_SCHEMA','RIG_BIND_SHAPE','RIG_GLTF_TRANSFORM'):
            raise
        return dict(independent=False,reader='hatch-native',status='unsupported-native-profile',
                    diagnostic=exc.report)


def _record(payload,path,role):
    fmt,mime=detect_format(payload)
    return dict(path=path,role=role,sha256=digest(payload),bytes=len(payload),format=fmt,mime_type=mime,required=True)


def _wireframe(model):
    """Real geometry diagnostic only; never a shaded/production preview claim."""
    import numpy as np
    import html
    points=[m.positions for m in model.geometry.meshes]
    bounds=[np.min([p.min(axis=0) for p in points],axis=0),np.max([p.max(axis=0) for p in points],axis=0)]
    span=max(float(np.max(bounds[1]-bounds[0])),1e-12)
    lines=[]
    for mesh in model.geometry.meshes:
        if len(lines)+len(mesh.triangles)*3>6000:break
        p=(mesh.positions-bounds[0])/span*108+10
        for triangle in mesh.triangles:
            for a,b in ((0,1),(1,2),(2,0)):
                x,y=p[triangle[a],:2];xx,yy=p[triangle[b],:2]
                lines.append(f'<path d="M{x:.4f},{128-y:.4f}L{xx:.4f},{128-yy:.4f}"/>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
            '<rect width="128" height="128" fill="white"/><g stroke="black" fill="none" stroke-width=".3">'+
            ''.join(lines)+'</g></svg>').encode('ascii')


def generation_parameters(parameters):
    """Admit the exact bounded native box/sphere generation profile without executing it."""
    if not isinstance(parameters,dict) or set(parameters)!={'primitive','dimensions','color','animated'}:
        fail('REQUEST','Generator needs explicit primitive/dimensions/color/animated parameters.')
    if parameters['primitive'] not in ('box','sphere') or type(parameters['animated'])is not bool:
        fail('UNSUPPORTED','Only real box/sphere generation is implemented.')
    for key,width in [('dimensions',3),('color',4)]:
        values=parameters[key]
        if not isinstance(values,list) or len(values)!=width or any(isinstance(v,bool) or not isinstance(v,(int,float)) or (isinstance(v,float) and not math.isfinite(v)) for v in values):
            fail('REQUEST','Invalid numeric generator '+key)
    if any(not .001<=v<=1000 for v in parameters['dimensions']) or any(not 0<=v<=1 for v in parameters['color']):fail('REQUEST','Generator values outside bounded profile.')
    if parameters['primitive']=='sphere' and len(set(parameters['dimensions']))!=1:fail('UNSUPPORTED','Sphere generator requires equal dimensions; no ignored parameters.')
    return deepcopy(parameters)


class ImportPipeline:
    """A cohesive, synchronous import transaction over the existing registry.

    Batch imports are independently atomic per item and bounded to eight items.
    The import provider never owns a second acquisition registry or scheduler.
    Failed verification/cancellation performs no library register operation.
    """
    def __init__(self,library,*,blender=None,timeout=60,memory_bytes=1024**3,cancelled=None):
        if not isinstance(library,AssetLibrary):fail('LIBRARY','Use the canonical AssetLibrary registry.')
        self.library=library;self.timeout=integer(timeout,1,300)
        self.memory=integer(memory_bytes,384*1024**2,2*1024**3)
        self.cancelled=cancelled or (lambda:False)
        self.blender=BlenderAdapter(blender,timeout=self.timeout,memory_bytes=self.memory,cancelled=self.cancelled)

    def capabilities(self,*,probe_blender=False):
        result=dict(schema_version=1,provider='hatch-library-import',version=VERSION,
                    formats=['glb','gltf','obj'],canonical='right-handed-y-up-z-forward',
                    format_detection='content/header',external_resources='bounded-local-only',
                    untrusted_blend='quarantine',batch_limit=8,
                    normalized_payload='self-contained-glb',ordinary_2d_requires_3d=False,
                    additional_formats='unsupported: FBX/USD/Alembic/STL/PLY adapters not implemented',
                    procedural_provider='blender-box-sphere-v1',
                    blender=dict(status='not-probed'),approval='never-inferred')
        if probe_blender:
            try:result['blender']=dict(status='available',**self.blender.capabilities())
            except RigError as exc:result['blender']=dict(status='unavailable',diagnostic=exc.report)
        return result

    @dependency_errors
    def probe(self,source):
        with tempfile.TemporaryDirectory(prefix='hatch-library-probe-') as temp:
            snapshot=SourceSnapshot(source,Path(temp)/'source',maximum_bytes=min(MAX_INPUT_BYTES,(self.memory-256*1024**2)//12),cancelled=self.cancelled)
            return dict(schema_version=1,format=snapshot.format,source_sha256=digest(snapshot.files[snapshot.entry]),
                        files=snapshot.inventory(),embedded=snapshot.embedded,
                        estimated_memory_bytes=256*1024**2+12*sum(map(len,snapshot.files.values())),
                        normalization='not-executed',geometry_validated=False,
                        trust='quarantined' if snapshot.format=='blend' else 'local-resource-preflight-only')

    def _import_asset(self,source,*,identity,version,metadata,license_file,fixture=False,
                     unit_scale=1.,basis='canonical',ground=False,blender_roundtrip=True,
                     trusted_packed_blend=False,provenance_method='import',generator_record=None):
        validate(identity,'AssetIdentity');validate(metadata,'AssetMetadata')
        validate(dict(id=identity['id'],version=version,sha256='0'*64),'AssetRef')
        if type(fixture)is not bool or type(blender_roundtrip)is not bool or type(trusted_packed_blend)is not bool:
            fail('REQUEST','Fixture/backend/trust policies must be boolean.')
        if provenance_method not in ('import','generated','fixture'):fail('REQUEST','Unsupported provenance method.')
        if provenance_method=='generated' and generator_record is None:fail('PROVENANCE','Generated imports require actual generator evidence.')
        with tempfile.TemporaryDirectory(prefix='hatch-library-import-') as temp:
            root=Path(temp);snapshot=SourceSnapshot(source,root/'source',maximum_bytes=min(MAX_INPUT_BYTES,(self.memory-256*1024**2)//12),cancelled=self.cancelled)
            if snapshot.format=='blend' and not trusted_packed_blend:
                fail('QUARANTINE','Untrusted Blender input preserved; select explicit trusted-packed policy after independent source review.')
            source_bytes=sum(map(len,snapshot.files.values()))
            estimate=256*1024**2+12*source_bytes
            if estimate>self.memory:fail('LIMIT','Conservative import working-set estimate exceeds memory admission.')
            license_payload=read_source_bytes(license_file,MAX_REPORT)
            from rig_contracts import DEFAULT_LIMITS
            limits={**DEFAULT_LIMITS,'source_bytes':MAX_FILE_BYTES,'dependency_bytes':MAX_INPUT_BYTES,
                    'memory_bytes':self.memory,'timeout_seconds':self.timeout,'influences':8}
            tool=None;input_path=snapshot.root/snapshot.entry
            if snapshot.format in ('glb','gltf'):
                source_doc,source_validation=_check_gltf(input_path,limits,snapshot.native_document)
                target,observation,conversion=normalize_gltf(snapshot,root/'normalized.glb',unit_scale=unit_scale,
                                                             basis=basis,ground=ground,limits=limits,fixture=fixture)
            else:
                # Blender handles format-specific decoding; mandatory original
                # source closure was already captured and is never rewritten.
                tool=self.blender.execute(snapshot.format,input_path,root/'conversion',trusted_blend=trusted_packed_blend,fixture=fixture)
                converted=SourceSnapshot(tool['outputs']['glb'],root/'converted',cancelled=self.cancelled)
                target,observation,conversion=normalize_gltf(converted,root/'normalized.glb',unit_scale=unit_scale,
                                                              basis=basis,ground=ground,limits=limits,fixture=fixture)
                if tool['inspection'].get('fixture') and not fixture:fail('PROVENANCE','Blender fixture source cannot be relabeled as production.')
                converted_doc,_=_gltf_json(read_source_bytes(target),'glb')
                if len({b['armature'] for b in tool['inspection']['skin_bindings']})>len(converted_doc.get('skins',[])):fail('LOSS','Initial native conversion lost admitted skin bindings.')
                if tool['inspection']['shape_keys']>observation.get('morph_targets',-1):fail('LOSS','Initial native conversion lost shape-key channels.')
                if tool['inspection']['animation_paths'] and not converted_doc.get('animations'):fail('LOSS','Initial native conversion lost all admitted animation channels.')
                source_validation=dict(reader='Blender',inspection=tool['inspection'],
                    conversion_profile='MESH/ARMATURE/EMPTY, positive TRS/pose/shape-value channel admission, simple Principled surface',
                    declared_losses=['Blender animation curves are sampled into glTF channels; native controls/interpolation/NLA editing metadata are not runtime channels.',
                        'Source world lighting/compositing and unused application datablocks are not represented in the runtime asset or neutral diagnostic render. Originals remain exact.',
                        'Native skin export uses Blender numeric precision; differences below the reported association precision are not certified. Exact native originals remain available.'],
                    limitations=['Blender/OBJ source semantics are represented by inspected glTF export; unused datablocks and unsupported application-only metadata are not runtime channels.'])
            source_fixture=document_fixture(snapshot.document) if snapshot.document else False
            if source_fixture and not fixture:fail('PROVENANCE','Fixture source cannot be relabeled as production.')
            _,independent=_check_gltf(target,limits)
            primary=read_source_bytes(target,MAX_FILE_BYTES)
            normalized_doc,_=_gltf_json(primary,'glb')
            blender=None;roundtrip_observation=None
            if observation.get('status')=='unsupported-native-profile' and not blender_roundtrip:
                fail('UNSUPPORTED','This native profile requires actual independent Blender inspection.')
            if blender_roundtrip:
                blender=self.blender.execute('gltf',target,root/'blender',fixture=fixture)
                _,roundtrip_validation=_check_gltf(blender['outputs']['glb'],limits)
                roundtrip_observation=_native_or_unavailable(blender['outputs']['glb'],limits)
                # A derivative cannot silently lose a declared capability. The
                # original normalized GLB is always the runtime authority.
                raw_roundtrip,_=_gltf_json(read_source_bytes(blender['outputs']['glb']),'glb')
                for key in ('skins','animations'):
                    if normalized_doc.get(key) and len(raw_roundtrip.get(key,[]))<len(normalized_doc[key]):
                        fail('LOSS','Blender roundtrip lost declared '+key+'; output was not published.')
                source_binding=skin_associations(target,limits)
                derivative_binding=skin_associations(blender['outputs']['glb'],limits)
                if source_binding['rows']!=derivative_binding['rows']:fail('LOSS','Blender roundtrip changed rest-space vertex/joint-weight associations.')
                binding_check=dict(independent=False,reader='hatch-native-accessor/actual-Blender-output',
                    association_sha256=object_hash(source_binding['rows']),max_influences=source_binding['max_influences'],
                    unique_weighted_vertices=len(source_binding['rows']),position_precision_m=1e-5,weight_precision=1e-6,ok=True)
                derivative_losses=[]
                if normalized_doc.get('skins'):derivative_losses.append('Blender skin associations checked at weight precision 1e-6 and rest-position precision 1e-5m; smaller derivative differences are not certified. Primary GLB retains input weights.')
                source_names=[a.get('name') for a in normalized_doc.get('animations',[])]
                converted_names=[a.get('name') for a in raw_roundtrip.get('animations',[])]
                if source_names!=converted_names:derivative_losses.append('Blender derivative animation names/order differ; primary GLB retains its names, and initial native authoring losses are declared.')
                if normalized_doc.get('animations'):derivative_losses.append('Blender derivative animation is frame-sampled; primary retains input glTF interpolation. Native Blender source interpolation remains in exact originals; see source_validation.declared_losses.')
                omitted=set(normalized_doc.get('extensionsUsed',[]))-set(raw_roundtrip.get('extensionsUsed',[]))
                if omitted:derivative_losses.append('Blender derivative omits optional extensions: '+','.join(sorted(omitted)))
                source_targets=sum(len(p.get('targets',[])) for m in normalized_doc.get('meshes',[]) for p in m.get('primitives',[]))
                actual_targets=sum(len(p.get('targets',[])) for m in raw_roundtrip.get('meshes',[]) for p in m.get('primitives',[]))
                if actual_targets<source_targets:fail('LOSS','Blender roundtrip lost morph targets.')
            if self.cancelled():fail('CANCELLED','Cancelled before immutable publication.')
            payloads={'runtime/asset.glb':primary,'source/license.txt':license_payload}
            mapping=[]
            for name,data in sorted(snapshot.files.items()):
                suffix=Path(name).suffix.lower()
                if not re.fullmatch(r'\.[a-z0-9]{1,8}',suffix):suffix='.bin'
                logical='source/'+digest(data)+suffix
                payloads[logical]=data;mapping.append(dict(original_path=name,logical_path=logical,sha256=digest(data),bytes=len(data)))
            payloads['source/map.json']=canonical_bytes(dict(schema_version=1,entry=snapshot.entry,files=mapping,embedded=snapshot.embedded))
            if blender:
                payloads.update({'native/asset.blend':read_source_bytes(blender['outputs']['blend']),
                                 'native/roundtrip.glb':read_source_bytes(blender['outputs']['glb']),
                                 'preview/blender.png':read_source_bytes(blender['outputs']['preview'])})
            else:
                model,_=native_observation(target,limits)
                payloads['preview/wireframe.svg']=_wireframe(model)
            report=dict(schema_version=1,kind='asset-library-import-report',tool_version=VERSION,
                source_format=snapshot.format,source_inventory=snapshot.inventory(),embedded=snapshot.embedded,
                source_validation=source_validation,normalization=conversion,native_observation=observation,
                independent_output=independent,blender=blender,
                roundtrip_observation=roundtrip_observation,fixture=fixture,
                native_derivative_losses=derivative_losses if blender else [],
                skin_binding_comparison=binding_check if blender else None,
                source_sha256=digest(snapshot.files[snapshot.entry]),output_sha256=digest(primary),
                estimate=dict(memory_bytes=estimate,input_bytes=source_bytes),
                limitations=['No human/production approval inferred.','Primary retains admitted glTF hierarchy/skin/morph/material/channels; initial native conversion losses are explicit in source_validation.',
                             'Additional format adapters return unsupported; no remote AI generation provider is implemented.'],
                production_approved=False)
            # Never store ephemeral absolute private paths as portable outputs.
            if blender:
                report['blender']=dict(inspection=blender['inspection'],execution=blender['execution'],
                    capability={k:v for k,v in blender['capability'].items() if k!='executable'},
                    worker_sha256=blender['worker_sha256'],saved_and_reopened=True)
            if generator_record:report['generator']=generator_record
            encoded=canonical_bytes(report)
            if len(encoded)>MAX_REPORT:fail('LIMIT','Import verification report exceeds bound.')
            payloads['evidence/import.json']=encoded
            if len(payloads)>256 or sum(map(len,payloads.values()))>MAX_VERSION_BYTES:fail('LIMIT','Final version inventory exceeds admission.')
            files=[_record(data,name,'runtime' if name.startswith('runtime/') else 'license' if name=='source/license.txt'
                           else 'source' if name.startswith('source/') else 'evidence') for name,data in sorted(payloads.items())]
            source_records=[next(f for f in files if f['path']==x['logical_path']) for x in mapping]
            license_record=next(f for f in files if f['path']=='source/license.txt')
            tools=[dict(name='hatch-library-import',version=VERSION),dict(name='Khronos glTF Validator',version=str(independent.get('validator','unknown')))]
            if blender:tools.append(dict(name='Blender',version=blender['capability']['version']))
            manifest=seal(dict(schema_version=1,kind='asset-library-version',identity=deepcopy(identity),version=version,files=files,
                dependencies=[],metadata=deepcopy(metadata),previews=[],
                compatibility=dict(coordinate_system='right-handed-y-up-z-forward',unit_scale=1,rig_signature=[],skeleton_signature=[],
                    material_capabilities=['gltf-pbr'],texture_channels=[],animation_channels=['gltf-native'] if normalized_doc.get('animations') else [],
                    renderers=['gltf-2.0'],formats=['glb'],extensions=list(normalized_doc.get('extensionsUsed',[])),consumer_capabilities=[]),
                provenance=dict(method='fixture' if fixture else provenance_method,origin=metadata['source_url'],fixture=fixture,
                    sources=source_records,provider=[],tools=tools,parents=[],edits=['Explicit normalization retained in evidence/import.json.'],
                    license_evidence=[license_record],permissions=[],uncertainty=['Unreviewed engineering import; see actual conversion report.'],
                    source_records=[dict(file=next(f for f in files if f['path']==x['logical_path']),
                        source_uri='local:'+x['original_path'],original_filename=x['original_path'],acquired_at=timestamp(),
                        author=metadata['author']) for x in mapping],
                    conversions=[dict(tool='hatch-library-import',version=VERSION,timestamp=timestamp(),
                        operation='Explicit basis/unit/root normalization; independent outputs retained.',
                        inputs=source_records,outputs=[next(f for f in files if f['path']=='runtime/asset.glb')])]),
                publication=dict(visibility='private',distribution='not-evaluated',license_evaluation='evidence-retained',
                    inventory=[f['path'] for f in files],verification='payload-verified',integration='not-exercised')))
            staging=root/'publication';staging.mkdir()
            sources={}
            for name,data in payloads.items():
                path=staging/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);sources[name]=path
            if self.cancelled():fail('CANCELLED','Cancelled before registration transaction.')
            reference=self.library.register(manifest,sources)
            # The canonical owner independently reopens every stored byte.
            self.library.resolve(reference)
            return dict(reference=reference,report=report,manifest_sha256=reference['sha256'],status='imported-payload-verified')

    @dependency_errors
    def import_asset(self,source,*,identity,version,metadata,license_file,fixture=False,
                     unit_scale=1.,basis='canonical',ground=False,blender_roundtrip=True,
                     trusted_packed_blend=False):
        return self._import_asset(source,identity=identity,version=version,metadata=metadata,
            license_file=license_file,fixture=fixture,unit_scale=unit_scale,basis=basis,
            ground=ground,blender_roundtrip=blender_roundtrip,trusted_packed_blend=trusted_packed_blend)

    def import_batch(self,requests):
        if not isinstance(requests,list) or not 1<=len(requests)<=8:fail('LIMIT','Batch needs one to eight explicit requests.')
        result=[]
        for request in requests:
            if self.cancelled():fail('CANCELLED','Batch cancelled before next independent transaction.')
            if not isinstance(request,dict):fail('REQUEST','Batch requests must be mappings.')
            result.append(self.import_asset(**request))
        return result

    def restore_original(self,reference,destination):
        """Reconstruct the original dependency tree, preserving all source bytes."""
        from atomic_publish import _rename_no_replace
        manifest=self.library.resolve(reference)
        mapping=_json(self.library.read_file(reference,'source/map.json'))
        if not isinstance(mapping,dict) or set(mapping)!={'schema_version','entry','files','embedded'} or mapping['schema_version']!=1:
            fail('SOURCE_MAP','Invalid original source relationship map.')
        if not isinstance(mapping['files'],list) or not 1<=len(mapping['files'])<=MAX_FILES:fail('SOURCE_MAP','Invalid source inventory.')
        names=set();payloads={};total=0
        for entry in mapping['files']:
            if not isinstance(entry,dict) or set(entry)!={'original_path','logical_path','sha256','bytes'}:fail('SOURCE_MAP','Invalid source map record.')
            name=relative(entry['original_path']);alias=name.casefold()
            if alias in names:fail('ALIAS','Original source paths alias.')
            names.add(alias)
            data=self.library.read_file(reference,entry['logical_path'])
            if len(data)!=entry['bytes'] or digest(data)!=entry['sha256']:fail('SOURCE_MAP','Original source bytes differ.')
            total+=len(data)
            if total>MAX_INPUT_BYTES:fail('LIMIT','Original source closure exceeds bound.')
            payloads[name]=data
        if relative(mapping['entry']) not in payloads:fail('SOURCE_MAP','Original entry is absent.')
        if any('/'.join(n.split('/')[:end]) in names for n in names for end in range(1,len(n.split('/')))):
            fail('ALIAS','Source files collide with directories.')
        destination=Path(destination).absolute()
        if destination.exists() or destination.is_symlink():fail('EXISTS','Restore needs a genuinely new destination.')
        destination=destination.parent.resolve(strict=True)/destination.name
        stage=Path(tempfile.mkdtemp(prefix='.'+destination.name+'-stage-',dir=destination.parent))
        try:
            for name,data in payloads.items():
                path=stage/name;path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('xb') as stream:stream.write(data)
            _rename_no_replace(stage,destination)
        finally:
            if stage.exists():shutil.rmtree(stage)
        return dict(entry=str(destination/mapping['entry']),files={n:digest(p) for n,p in payloads.items()},restored_exact_bytes=True)

    @dependency_errors
    def generate(self,*,identity,version,metadata,license_file,parameters,fixture=False):
        parameters=generation_parameters(parameters)
        with tempfile.TemporaryDirectory(prefix='hatch-library-generate-') as temp:
            result=self.blender.execute('generate',None,Path(temp)/'generation',parameters=parameters,fixture=fixture)
            generator=dict(provider='blender-box-sphere-v1',provider_version=VERSION,parameters=deepcopy(parameters),
                           blender_version=result['capability']['version'],worker_sha256=result['worker_sha256'],
                           actual_output_sha256=digest(read_source_bytes(result['outputs']['glb'])),
                           execution=result['execution'],fixture=fixture)
            return self._import_asset(result['outputs']['glb'],identity=identity,version=version,metadata=metadata,
                license_file=license_file,fixture=fixture,provenance_method='generated',generator_record=generator)


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['probe','import','generate','capabilities'])
    parser.add_argument('--library',required=True,type=Path)
    parser.add_argument('--request',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--blender')
    args=parser.parse_args(argv)
    try:
        pipeline=ImportPipeline(AssetLibrary(args.library),blender=args.blender)
        if args.command=='probe':result=pipeline.probe(args.source)
        elif args.command=='capabilities':result=pipeline.capabilities(probe_blender=True)
        else:
            if args.request is None:fail('REQUEST','A real bounded JSON request is required.')
            request=_json(read_source_bytes(args.request,MAX_REPORT))
            if not isinstance(request,dict):fail('REQUEST','Request must be an object.')
            result=pipeline.generate(**request) if args.command=='generate' else pipeline.import_asset(args.source,**request)
        print(json.dumps(result,ensure_ascii=True,allow_nan=False,indent=2));return 0
    except (RigError,OSError,TypeError,ValueError,RecursionError) as exc:
        error=exc.report if isinstance(exc,RigError) else dict(code='LIBRARY_IMPORT_INPUT',message=str(exc),stage='asset-import')
        print(json.dumps(dict(status='failed',diagnostic=error),ensure_ascii=True));return 2


if __name__=='__main__':raise SystemExit(main())
