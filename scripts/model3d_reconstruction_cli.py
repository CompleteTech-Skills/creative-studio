"""Strict versioned requests over existing local reconstruction engines.

This standalone dispatcher performs no inference/provider calls and imports no
numerical backend for schema/help/capabilities discovery. Central CLI and schema
registration are separate additive wiring points: main(), schema(), schemas().
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unicodedata

MAX_REQUEST_BYTES=4*1024**2
SCHEMA_NAME='model3d-reconstruction-request.schema.json'
METHODS=('contour-extrusion','profile-revolution','heightfield','mask-extrusion','visual-hull')


def schema():
    """Fresh Draft2020-12 request schema; paths are checked against root at runtime."""
    def obj(properties,required=None):return dict(type='object',properties=properties,required=list(properties) if required is None else required,additionalProperties=False)
    def arr(items,minimum,maximum):return dict(type='array',items=items,minItems=minimum,maxItems=maximum)
    number=dict(type='number',minimum=-1e6,maximum=1e6)
    positive=dict(type='number',exclusiveMinimum=0,maximum=1e6)
    point=arr(number,2,2)
    matrix=arr(arr(number,4,4),4,4)
    source=obj(dict(path=dict(type='string',minLength=1,maxLength=1024),role=dict(enum=['source','mask','depth','sketch','reference'])))
    interpretation=obj(dict(channel=dict(enum=['alpha','luminance']),threshold=dict(type='integer',minimum=0,maximum=254),stride=dict(type='integer',minimum=1,maximum=32)),['channel','threshold'])
    camera_properties=dict(calibration=dict(enum=['known','estimated','unknown']),near_m=positive,far_m=positive,camera_to_world=matrix,world_to_camera=matrix,
                           intrinsics=arr(arr(number,3,3),3,3),orthographic=arr(number,4,4))
    camera=obj(camera_properties,['calibration','near_m','far_m'])
    camera['allOf']=[dict(oneOf=[dict(required=['camera_to_world'],**{'not':dict(required=['world_to_camera'])}),dict(required=['world_to_camera'],**{'not':dict(required=['camera_to_world'])})]),dict(oneOf=[dict(required=['intrinsics'],**{'not':dict(required=['orthographic'])}),dict(required=['orthographic'],**{'not':dict(required=['intrinsics'])})])]
    view_interpretation=deepcopy(interpretation);view_interpretation['properties']['stride']=dict(type='integer',const=1)
    view=obj(dict(name=dict(type='string',pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),mask=source,interpretation=view_interpretation,camera=camera))
    header=dict(schema_version=dict(type='integer',const=1),kind=dict(const='hatch-model3d-reconstruction-request'),method=dict(enum=list(METHODS)),
                export_format=dict(enum=['glb','gltf','obj']),notes=dict(type='string',maxLength=4096))
    shared=dict(sources=arr(source,0,16))
    definitions={
        'contour-extrusion':(dict(outer=arr(point,3,256),holes=arr(arr(point,3,256),0,16),thickness_m=positive,**shared),['outer','thickness_m']),
        'profile-revolution':(dict(profile=arr(point,2,512),segments=dict(type='integer',minimum=3,maximum=512),cap_ends=dict(type='boolean'),**shared),['profile','segments','cap_ends']),
        'heightfield':(dict(heights_m=arr(arr(number,2,256),2,256),spacing_m=arr(positive,2,2),base_thickness_m=positive,**shared),['heights_m','spacing_m','base_thickness_m']),
        'mask-extrusion':(dict(source=source,mask=source,interpretation=interpretation,pixel_size_m=positive,thickness_m=positive,origin_m=point),['source','interpretation','pixel_size_m','thickness_m']),
        'visual-hull':(dict(views=arr(view,2,16),bounds_m=arr(arr(number,3,3),2,2),resolution=arr(dict(type='integer',minimum=1,maximum=256),3,3)),['views','bounds_m','resolution']),
    }
    variants=[]
    for name,(fields,required) in definitions.items():
        properties=deepcopy(header);properties['method']=dict(const=name);properties.update(deepcopy(fields))
        variants.append(obj(properties,['schema_version','kind','method',*required]))
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','title':'Hatch bounded reconstruction request v1','oneOf':variants,
            'description':'Local evidence reconstruction only. Runtime checks paths, numerical/topological consistency, calibration, budgets and immutable source bytes. No approval or provider claim.'}


def schemas():return {SCHEMA_NAME:schema()}


def capabilities():
    return dict(schema_version=1,kind='hatch-model3d-reconstruction-capabilities',request_schema=SCHEMA_NAME,
                methods=list(METHODS),formats=['glb','gltf','obj'],source_formats=['png','jpeg','webp'],
                coordinates='right-handed +Y up +Z forward meters',network_calls=False,provider_calls=False,
                implemented=['strict-request-schema','frozen-root-relative-image-capture','source-request-hash-retention','bounded-native-reconstruction','actual-native-export-reimport','exclusive-publication'],
                limitations=['No automatic calibration, model provider or hidden anatomy inference.','Cancellation is cooperative at capture/compute/export/publish boundaries; core methods are synchronous and bounded.','Central CLI/schema registry and durable job wiring are separate integration points.'])


def validate_request(value):
    """Validate bounded JSON and return an independent request copy."""
    from rig_errors import RigError,require,finite_json,decode_json
    finite_json(value)
    count=0
    stack=[value]
    while stack:
        item=stack.pop();count+=1
        require(count<=200000,'RECON_REQUEST_LIMIT','Request exceeds200000JSON nodes.')
        if isinstance(item,dict):stack.extend(item.values())
        elif isinstance(item,(list,tuple)):stack.extend(item)
    size=0
    try:
        for part in json.JSONEncoder(ensure_ascii=True,allow_nan=False).iterencode(value):
            size+=len(part)
            require(size<=MAX_REQUEST_BYTES,'RECON_REQUEST_LIMIT','Request exceeds4MiB.')
    except (TypeError,ValueError,UnicodeError) as exc:
        if isinstance(exc,RigError):raise
        raise RigError('RECON_REQUEST_JSON','Request must contain ordinary JSON values.') from exc
    try:from jsonschema import Draft202012Validator
    except ImportError as exc:raise RigError('RECON_REQUEST_DEPENDENCY','Install optional rig dependencies for request validation.') from exc
    error=next(Draft202012Validator(schema()).iter_errors(value),None)
    require(error is None,'RECON_REQUEST_SCHEMA',(error.message[:1024] if error else 'Invalid request.'),asset=('/'+'/'.join(map(str,error.absolute_path)) if error else None))
    # Validate portable spellings before capturing any source or publishing.
    from rig_errors import portable_name
    aliases={}
    for source in _sources(value):
        name=portable_name(source['path']);alias=unicodedata.normalize('NFC',name).casefold()
        require(name==unicodedata.normalize('NFC',name) and (alias not in aliases or aliases[alias]==name),
                'RECON_REQUEST_PATH','Source paths must use NFC and cannot case-collide.')
        aliases[alias]=name
    require(not any('/'.join(name.split('/')[:i]) in aliases for name in aliases for i in range(1,len(name.split('/')))),
            'RECON_REQUEST_PATH','One source file cannot also be a directory prefix.')
    return decode_json(json.dumps(value,allow_nan=False,ensure_ascii=True,separators=(',',':')))


def _sources(request):
    method=request['method']
    if method=='mask-extrusion':return [request['source']]+([request['mask']] if 'mask' in request else [])
    if method=='visual-hull':return [view['mask'] for view in request['views']]
    return request.get('sources',[])


def execute_request(request, *, root, destination, format=None, on_stage=None, request_bytes=None):
    """Run one strict request, atomically publish output, retain exact observations.

    root is the caller-selected trusted local source root; every document path is
    relative and no-follow. JSON cannot supply roots, publication paths or code.
    on_stage(name) is a Python-only cooperative cancellation/observation hook.
    """
    from rig_errors import RigError,require,open_payload,safe_path,decode_json
    from model3d_reconstruction import read_image,mask_from_image,publish,_seal_result,MAX_IMAGE_BYTES
    from model3d_reconstruction_solids import extrude_contour,extrude_mask,revolve_profile,heightfield
    from model3d_reconstruction_views import calibrated_view,carve_visual_hull
    from atomic_publish import _rename_no_replace
    request=validate_request(request)
    selected=format if format is not None else request.get('export_format','glb')
    require(selected in ('glb','gltf','obj'),'RECON_REQUEST_FORMAT','Select glb, gltf or obj export.')
    require(on_stage is None or callable(on_stage),'RECON_REQUEST_CALLBACK','on_stage must be a Python callable, never a JSON field.')
    def stage(name):
        if on_stage is not None:on_stage(name)
    if request_bytes is None:request_bytes=(json.dumps(request,sort_keys=True,allow_nan=False)+'\n').encode()
    require(type(request_bytes) is bytes and len(request_bytes)<=MAX_REQUEST_BYTES,'RECON_REQUEST_LIMIT','Original request bytes exceed4MiB.')
    captured_request=validate_request(decode_json(request_bytes))
    canonical=lambda value:json.dumps(value,sort_keys=True,allow_nan=False,ensure_ascii=True,separators=(',',':'))
    require(canonical(captured_request)==canonical(request),'RECON_REQUEST_CHANGED','Original request bytes differ from the validated request, including numeric field types.')
    root=Path(root).resolve(strict=True);require(root.is_dir(),'RECON_REQUEST_ROOT','Root must be an existing source directory.')
    identity=root.stat();root_identity=(identity.st_dev,identity.st_ino)
    destination=Path(destination).absolute();parent=destination.parent.resolve(strict=True);destination=parent/destination.name
    require(not destination.exists() and not destination.is_symlink(),'RECON_EXISTS','Destination must be new; existing contents are preserved.')
    records=_sources(request);roles={}
    for record in records:
        path=record['path'];require(path not in roles or roles[path]==record['role'],'RECON_REQUEST_SOURCE','One source path cannot have conflicting roles.')
        roles[path]=record['role']
    stage('capture')
    with tempfile.TemporaryDirectory(prefix='recon-request-') as snapshot_dir:
        snapshot=Path(snapshot_dir);evidence={};captured=0
        for name,role in roles.items():
            with open_payload(root,name,root_identity=root_identity) as (stream,info):
                require(0<info.st_size<=MAX_IMAGE_BYTES and captured+info.st_size<=256*1024**2,'RECON_REQUEST_LIMIT','Source closure exceeds image/aggregate byte limits.')
                payload=stream.read(info.st_size+1);require(len(payload)==info.st_size,'RECON_REQUEST_SOURCE','Source changed during capture.')
            target=safe_path(snapshot,name);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
            evidence[name]=read_image(target,role=role);captured+=len(payload)
        def mask(record,interpretation):
            source=evidence[record['path']]
            values=mask_from_image(source,mode=interpretation['channel'],threshold=int(interpretation['threshold']))
            stride=int(interpretation.get('stride',1))
            return values[::stride,::stride]
        source_receipts=[dict(original_path=name,**item.descriptor()) for name,item in evidence.items()]
        stage('compute')
        method=request['method']
        if method=='contour-extrusion':result=extrude_contour(request['outer'],holes=request.get('holes',[]),thickness=request['thickness_m'],evidence=source_receipts)
        elif method=='profile-revolution':result=revolve_profile(request['profile'],segments=int(request['segments']),cap_ends=request['cap_ends'],evidence=source_receipts)
        elif method=='heightfield':result=heightfield(request['heights_m'],spacing=request['spacing_m'],base_thickness=request['base_thickness_m'],evidence=source_receipts)
        elif method=='mask-extrusion':
            source=evidence[request['source']['path']];selected_record=request.get('mask',request['source'])
            used=evidence[selected_record['path']]
            require((source.width,source.height)==(used.width,used.height),'RECON_REQUEST_MASK','Source and mask must have identical original dimensions.')
            stride=int(request['interpretation'].get('stride',1))
            result=extrude_mask(mask(selected_record,request['interpretation']),pixel_size=request['pixel_size_m']*stride,thickness=request['thickness_m'],origin=request.get('origin_m',[0,0]),evidence=source_receipts)
        else:
            views=[]
            for view in request['views']:
                camera=view['camera'];kwargs=dict(name=view['name'],calibration=camera['calibration'],near=camera['near_m'],far=camera['far_m'],evidence=dict(source=next(s for s in source_receipts if s['original_path']==view['mask']['path']),interpretation=view['interpretation']))
                for key in ('camera_to_world','world_to_camera','intrinsics','orthographic'):
                    if key in camera:kwargs[key]=camera[key]
                views.append(calibrated_view(mask(view['mask'],view['interpretation']),**kwargs))
            result=carve_visual_hull(views,request['bounds_m'],[int(v) for v in request['resolution']])
        result.report['request_binding']=dict(schema_version=1,path='request.json',bytes=len(request_bytes),sha256=hashlib.sha256(request_bytes).hexdigest(),method=method)
        result.report['request_sources']=source_receipts
        _seal_result(result)
        stage('export')
        with tempfile.TemporaryDirectory(prefix='.recon-request-',dir=parent) as outer:
            artifact=Path(outer)/'artifact'
            acceptance=publish(result,artifact,evidence=list(evidence.values()),format=selected)
            (artifact/'request.json').write_bytes(request_bytes)
            acceptance['files']['request.json']=dict(bytes=len(request_bytes),sha256=hashlib.sha256(request_bytes).hexdigest())
            (artifact/'acceptance.json').write_text(json.dumps(acceptance,indent=2)+'\n')
            report=json.loads((artifact/'reconstruction.json').read_text())
            stage('publish')
            try:_rename_no_replace(artifact,destination)
            except FileExistsError as exc:raise RigError('RECON_EXISTS','Another writer created the destination; it was preserved.') from exc
        return dict(schema_version=1,ok=True,method=method,destination=str(destination),export_format=selected,acceptance=acceptance,report=report)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('schema',help='Print the strict version1 request schema.')
    commands.add_parser('capabilities',help='Discover actual local methods and boundaries.')
    validate=commands.add_parser('validate-request',help='Validate JSON request fields and portable paths without decoding observations.')
    run=commands.add_parser('request',help='Execute a bounded reconstruction request and publish real geometry/evidence.')
    for command in (validate,run):command.add_argument('--file',required=True,help='Bounded local request JSON file.')
    run.add_argument('--root',required=True,help='Trusted root for relative source observation paths.')
    run.add_argument('--destination',required=True,help='New output directory; existing contents are preserved.')
    run.add_argument('--export-format',choices=['glb','gltf','obj'],help='Explicit override of the request export format.')
    run.add_argument('--report',action='store_true',help='Print the full actual reconstruction/acceptance report, not only a summary.')
    args=parser.parse_args(argv)
    from rig_errors import RigError,require,open_payload,decode_json
    try:
        if args.command=='schema':value=schema()
        elif args.command=='capabilities':value=capabilities()
        else:
            source=Path(args.file).absolute()
            with open_payload(source.parent,source.name) as (stream,info):
                require(0<info.st_size<=MAX_REQUEST_BYTES,'RECON_REQUEST_LIMIT','Request file exceeds4MiB.')
                payload=stream.read(info.st_size+1);require(len(payload)==info.st_size,'RECON_REQUEST_CHANGED','Request changed while reading.')
            request=validate_request(decode_json(payload))
            if args.command=='validate-request':value=dict(ok=True,schema_version=1,method=request['method'],source_read=False)
            else:
                actual=execute_request(request,root=args.root,destination=args.destination,format=args.export_format,request_bytes=payload)
                value=actual if args.report else dict(ok=True,schema_version=1,method=actual['method'],destination=actual['destination'],export_format=actual['export_format'],files=len(actual['acceptance']['files']),review='pending')
        print(json.dumps(value,indent=2,allow_nan=False));return 0
    except RigError as exc:
        print(json.dumps(dict(ok=False,error=exc.report),allow_nan=False));return 2
    except OSError as exc:
        print(json.dumps(dict(ok=False,error=dict(code='RECON_REQUEST_IO',message=str(exc)))));return 2

if __name__=='__main__':raise SystemExit(main())
