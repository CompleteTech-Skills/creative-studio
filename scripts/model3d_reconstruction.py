"""Evidence-preserving local reconstruction results; no inference or approval service.

Canonical geometry is right-handed +Y up, +Z forward, meters. Sources remain exact
bytes. Image masks require an explicit interpretation and threshold; no semantic
labels, depth, symmetry or camera calibration are guessed from appearance.
"""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import numpy as np
from PIL import Image, ImageFile
from atomic_publish import _rename_no_replace
from rig_errors import RigError, finite_json, open_payload, portable_name, require
from rig_models import Geometry, Mesh, validate_geometry
from rig_contracts import DEFAULT_LIMITS

MAX_IMAGE_BYTES = 64*1024**2
MAX_IMAGE_PIXELS = 4_000_000
MAX_VERTICES = 250_000
MAX_TRIANGLES = 500_000

@dataclass(frozen=True)
class Evidence:
    name: str
    payload: bytes
    role: str
    width: int
    height: int

    @property
    def sha256(self):return hashlib.sha256(self.payload).hexdigest()

    def descriptor(self):
        return dict(name=self.name, role=self.role, bytes=len(self.payload), sha256=self.sha256,
                    dimensions_px=[self.width,self.height])

@dataclass
class Reconstruction:
    geometry: Geometry
    report: dict


def read_image(path, *, role='source'):
    """Read one immutable PNG/JPEG/WebP observation with bounded verified decode."""
    require(role in ('source','mask','depth','sketch','reference'), 'RECON_SOURCE', 'Unknown evidence role.')
    path=Path(path).absolute()
    require(path.suffix.lower() in ('.png','.jpg','.jpeg','.webp'), 'RECON_SOURCE', 'Choose PNG, JPEG or WebP.')
    with open_payload(path.parent,path.name) as (stream, info):
        require(0 < info.st_size <= MAX_IMAGE_BYTES, 'RECON_SOURCE_LIMIT', 'Encoded image exceeds64MiB.')
        payload=stream.read(MAX_IMAGE_BYTES+1)
        require(len(payload)==info.st_size,'RECON_SOURCE','Source changed while reading.')
    require(not ImageFile.LOAD_TRUNCATED_IMAGES,'RECON_SOURCE_POLICY','Strict source admission requires truncated image recovery disabled.')
    try:
        with Image.open(io.BytesIO(payload)) as image:
            require(image.format in ('PNG','JPEG','WEBP') and getattr(image,'n_frames',1)==1,
                    'RECON_SOURCE','Only single-frame PNG/JPEG/WebP observations are supported.')
            require(0 < image.width*image.height <= MAX_IMAGE_PIXELS,'RECON_SOURCE_LIMIT','Observation exceeds4million pixels.')
            width,height=image.size;image_format=image.format;image.verify()
        if image_format in ('PNG','JPEG'):
            # Reuse the warning-aware JPEG entropy and complete PNG raster
            # authorities. Pillow may silently recover damaged JPEG scan data.
            from rig_import import _decode_texture
            _decode_texture(payload,dict(DEFAULT_LIMITS,source_bytes=MAX_IMAGE_BYTES,memory_bytes=128*1024**2))
        with Image.open(io.BytesIO(payload)) as image:image.load()
    except (OSError,ValueError,Image.DecompressionBombError) as exc:
        if isinstance(exc,RigError):raise
        raise RigError('RECON_SOURCE','Observation cannot be verified and decoded.') from exc
    return Evidence(path.name,payload,role,width,height)


def mask_from_image(evidence, *, mode, threshold=127):
    """Explicit alpha or luminance membership at original resolution, without cleanup."""
    require(isinstance(evidence,Evidence),'RECON_SOURCE','Expected captured image evidence.')
    require(mode in ('alpha','luminance') and type(threshold) is int and 0<=threshold<=254,
            'RECON_MASK','Select alpha/luminance and an integer threshold0..254.')
    with Image.open(io.BytesIO(evidence.payload)) as image:
        require(image.size==(evidence.width,evidence.height),'RECON_SOURCE','Evidence dimensions disagree.')
        if mode=='alpha':
            require('A' in image.getbands() or 'transparency' in image.info,'RECON_MASK','Source has no authored alpha; supply a mask or explicit luminance interpretation.')
            values=np.asarray(image.convert('RGBA').getchannel('A'))
        else:values=np.asarray(image.convert('L'))
        return np.array(values>threshold,dtype=bool)


def _number(value, *, positive=False):
    require(type(value) in (int,float) and -1e6<=value<=1e6 and np.isfinite(value)
            and (value>0 if positive else True),'RECON_PARAMETER','Expected finite bounded metric values, not booleans.')
    return float(value)


def _array(value, dimensions, label):
    require(isinstance(value,(list,tuple,np.ndarray)),'RECON_PARAMETER',label+' must be a numeric array.')
    def shape_ok(item,shape):
        if isinstance(item,np.ndarray):return item.shape==shape
        return isinstance(item,(list,tuple)) and len(item)==shape[0] and (len(shape)==1 or all(shape_ok(row,shape[1:]) for row in item))
    require(shape_ok(value,dimensions),'RECON_PARAMETER',label+' has invalid shape.')
    try:array=np.asarray(value)
    except (ValueError,TypeError,OverflowError) as exc:raise RigError('RECON_PARAMETER',label+' is not a regular numeric array.') from exc
    require(array.shape==dimensions and array.dtype.kind in 'fiu' and np.isfinite(array).all()
            and np.max(np.abs(array),initial=0)<=1e6,'RECON_PARAMETER',label+' has invalid shape/range.')
    return array.astype(float)


def _budget(vertices,triangles):
    require(vertices<=MAX_VERTICES and triangles<=MAX_TRIANGLES,
            'RECON_LIMIT','Reconstruction exceeds250000vertices/500000triangles; reduce resolution.')


def _result(positions,triangles, *, method, parameters, evidence=None, uv=None, closed=True, classifications=None):
    _budget(len(positions),len(triangles))
    require(3<=len(positions) and len(triangles)>0,'RECON_EMPTY','Evidence did not produce a nonempty surface.')
    p=np.asarray(positions,dtype=float);t=np.asarray(triangles,dtype=np.int64)
    from model3d_topology import _normals,topology_report
    mesh=Mesh('reconstructed',p,t,name='Limited '+method,region='unresolved')
    mesh.attributes['NORMAL']=_normals(mesh)
    if uv is not None:mesh.attributes['TEXCOORD_0']=np.asarray(uv,dtype=float)
    geometry=Geometry([mesh],[dict(name='unreviewed-neutral',pbrMetallicRoughness=dict(baseColorFactor=[.65,.65,.65,1],metallicFactor=0,roughnessFactor=.65))])
    validate_geometry(geometry,DEFAULT_LIMITS)
    quality=topology_report(mesh)
    require(not quality['degenerate_faces'] and not quality['nonmanifold_edges'] and not quality['inconsistent_edges'],
            'RECON_TOPOLOGY','Constructed mesh is degenerate, nonmanifold or inconsistently wound.')
    require(not closed or not quality['boundary_edges'],'RECON_TOPOLOGY','Closed reconstruction has an open boundary.')
    # Edges alone do not prove manifold vertices: each vertex link must be one
    # cycle (or one path for an intentionally open mesh), rejecting point contacts.
    links=[[] for _ in positions]
    for a,b,c in triangles:
        links[a].append((b,c));links[b].append((c,a));links[c].append((a,b))
    for edges in links:
        graph={}
        for a,b in edges:graph.setdefault(a,set()).add(b);graph.setdefault(b,set()).add(a)
        require(bool(graph) and all(len(n)<=2 for n in graph.values()),'RECON_TOPOLOGY','Nonmanifold surface vertex.')
        seen=set();pending=[next(iter(graph))]
        while pending:
            node=pending.pop()
            if node not in seen:seen.add(node);pending.extend(graph[node]-seen)
        require(len(seen)==len(graph) and (not closed or all(len(n)==2 for n in graph.values())),
                'RECON_TOPOLOGY','Surface components touch at a nonmanifold vertex.')
    require(evidence is None or isinstance(evidence,(list,tuple)) and len(evidence)<=64,'RECON_SOURCE_LIMIT','Evidence requires at most64records.')
    stack=[(evidence or [],0)];nodes=0
    while stack:
        value,depth=stack.pop();nodes+=1
        require(nodes<=8192 and depth<=32,'RECON_SOURCE_LIMIT','Evidence exceeds node/depth budget.')
        if isinstance(value,dict):
            require(len(value)<=256 and all(isinstance(k,str) and len(k)<=256 for k in value),'RECON_SOURCE_LIMIT','Evidence keys exceed bounds.')
            stack.extend((v,depth+1) for v in value.values())
        elif isinstance(value,(list,tuple)):
            require(len(value)<=8192,'RECON_SOURCE_LIMIT','Evidence arrays exceed bounds.');stack.extend((v,depth+1) for v in value)
        else:require(value is None or type(value) is bool or isinstance(value,str) and len(value)<=8192 or type(value) in (int,float) and -1e100<=value<=1e100,'RECON_SOURCE_LIMIT','Evidence values exceed JSON bounds.')
    supplied=deepcopy(evidence or [])
    finite_json(supplied)
    require(len(json.dumps(supplied))<=1024**2,'RECON_SOURCE_LIMIT','Evidence metadata exceeds1MiB.')
    report=dict(schema_version=1,kind='hatch-model3d-limited-reconstruction',method=method,
        coordinate_system='right-handed-y-up-z-forward-meters',parameters=parameters,evidence=supplied,
        evidence_classification=classifications or dict(observed='Supplied contour, profile or sampled height values.',
            inferred='Thickness, hidden caps/walls and interpolated surfaces follow explicit parameters.',unknown='Unseen anatomy, materials and textures; no semantic part inference.'),
        completeness='partial',review='pending',provider_calls=0,closed=closed,topology=quality,
        limitations=['Limited deterministic reconstruction; not recovered complete anatomy or artistic approval.',
                     'No hidden texture inference, automatic camera calibration, generative provider or symmetry completion.'])
    return Reconstruction(geometry,report)


def _result_signature(result):
    """Bind numerical evidence to actual native arrays and parameters, not paths."""
    geometry=result.geometry
    validate_geometry(geometry,DEFAULT_LIMITS)
    digest=hashlib.sha256()
    def metadata(value):
        finite_json(value)
        encoded=json.dumps(value,sort_keys=True,allow_nan=False,ensure_ascii=True,separators=(',',':')).encode('ascii')
        require(len(encoded)<=16*1024**2,'RECON_SOURCE_LIMIT','Result metadata exceeds16MiB.')
        digest.update(len(encoded).to_bytes(8,'little'));digest.update(encoded)
    def array(value):
        require(isinstance(value,np.ndarray) and value.dtype.kind in 'bifu' and value.nbytes<=128*1024**2,
                'RECON_RESULT','Result arrays must be bounded numeric data.')
        metadata(dict(dtype=value.dtype.str,shape=list(value.shape)))
        digest.update(memoryview(np.ascontiguousarray(value)).cast('B'))
    metadata(dict(report=result.report,materials=geometry.materials,textures=geometry.textures,
                  samplers=geometry.samplers,extensions=geometry.extensions_used))
    for image in geometry.images:
        metadata(dict(name=image.name,mime_type=image.mime_type,bytes=len(image.data)))
        digest.update(image.data)
    for mesh in geometry.meshes:
        metadata(dict(id=mesh.id,name=mesh.name,region=mesh.region,source_node=mesh.source_node,
                      material=mesh.material,morph_defaults=mesh.morph_defaults))
        array(mesh.positions);array(mesh.triangles)
        for key,values in sorted(mesh.attributes.items()):metadata(key);array(values)
        for target,channels in sorted(mesh.morphs.items()):
            metadata(target)
            for key,values in sorted(channels.items()):metadata(key);array(values)
    if hasattr(result,'occupancy'):array(result.occupancy)
    return digest.hexdigest()


def _seal_result(result):
    """Internal method-completion seal; edited geometry requires new evidence."""
    result._reconstruction_signature=_result_signature(result)
    return result


def publish(result, destination, *, evidence=(), format='glb'):
    """Atomically publish genuine mesh, exact original observations and hash receipts."""
    from model3d_interchange import export_mesh
    require(hasattr(result,'geometry') and isinstance(result.report,dict),'RECON_RESULT','Expected a reconstruction result.')
    require(getattr(result,'_reconstruction_signature',None)==_result_signature(result),
            'RECON_STALE','Reconstruction geometry, occupancy or evidence changed; regenerate/recompute reconstruction evidence before publication.')
    # Snapshot mutable native arrays and receipts before exporting; later caller
    # edits cannot race the geometry/report pair used for this publication.
    result=deepcopy(result)
    require(result._reconstruction_signature==_result_signature(result),'RECON_STALE','Result changed while capturing its publication snapshot.')
    require(isinstance(evidence,(list,tuple)) and len(evidence)<=64 and all(isinstance(e,Evidence) and type(e.payload) is bytes for e in evidence),
            'RECON_SOURCE','Supply at most64captured observations.')
    require(sum(len(e.payload) for e in evidence)<=256*1024**2,'RECON_SOURCE_LIMIT','Evidence total exceeds256MiB.')
    destination=Path(destination).absolute();parent=destination.parent.resolve(strict=True);destination=parent/destination.name
    require(not destination.exists() and not destination.is_symlink(),'RECON_EXISTS','Publish to a new destination.')
    stage=Path(tempfile.mkdtemp(prefix='.reconstruction-',dir=parent))
    try:
        export_mesh(result.geometry,stage/'mesh',format=format)
        sources=[]
        for i,item in enumerate(evidence):
            portable_name(item.name)
            name=f'sources/{i:02d}-'+item.name;target=stage/name;target.parent.mkdir(exist_ok=True)
            target.write_bytes(item.payload);sources.append(dict(path=name,**item.descriptor()))
        report=deepcopy(result.report);report['sources']=sources
        if hasattr(result,'occupancy'):
            occupancy=result.occupancy
            require(isinstance(occupancy,np.ndarray) and occupancy.ndim==3 and occupancy.dtype.kind=='b' and occupancy.size<=262144,'RECON_LIMIT','Occupancy exceeds the visual-hull intermediate budget.')
            data=occupancy.tobytes(order='C');(stage/'occupancy.bin').write_bytes(data)
            report['occupancy_artifact']=dict(path='occupancy.bin',shape=list(occupancy.shape),encoding='uint8-bool-C-order-XYZ',bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        finite_json(report)
        data=json.dumps(report,indent=2,allow_nan=False).encode()
        require(len(data)<=16*1024**2,'RECON_SOURCE_LIMIT','Reconstruction receipt exceeds16MiB.')
        (stage/'reconstruction.json').write_bytes(data)
        files={p.relative_to(stage).as_posix():dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in stage.rglob('*') if p.is_file()}
        acceptance=dict(schema_version=1,kind='hatch-reconstruction-delivery',files=files,review='pending',provider_calls=0)
        (stage/'acceptance.json').write_text(json.dumps(acceptance,indent=2)+'\n')
        _rename_no_replace(stage,destination);return acceptance
    finally:
        if stage.exists():shutil.rmtree(stage)
