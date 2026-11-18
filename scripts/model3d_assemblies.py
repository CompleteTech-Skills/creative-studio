"""Deterministic local text/IR assemblies, native editable meshes and verified exports."""
from __future__ import annotations
import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile

from rig_errors import RigError, require, decode_json, read_json, object_hash, sha256
from model3d_assemblies_contracts import VERSION, LIMITS, FAMILIES, FORMS, validate_ir, schema

COLORS={'white':[.9,.9,.9,1.],'black':[.03,.03,.03,1.],'red':[.8,.08,.06,1.],
        'blue':[.05,.25,.8,1.],'green':[.08,.5,.15,1.],'yellow':[.95,.7,.05,1.],
        'brown':[.35,.16,.06,1.],'gray':[.45,.45,.45,1.],'pink':[.9,.3,.5,1.]}


def material(identifier='main',family='plastic',color='blue'):
    require(family in FAMILIES,'MODEL3D_ASSEMBLY_LANGUAGE','Unsupported material family.')
    if isinstance(color,str):
        if re.fullmatch(r'#[0-9a-fA-F]{6}',color):rgba=[int(color[i:i+2],16)/255 for i in (1,3,5)]+[1.]
        else:
            require(color in COLORS,'MODEL3D_ASSEMBLY_LANGUAGE','Use a documented color name or #RRGGBB.')
            rgba=COLORS[color].copy()
    else:rgba=deepcopy(color)
    return dict(id=identifier,family=family,color=rgba,metallic=1. if family=='metal' else 0.,
                roughness=.15 if family in ('glass','ceramic') else .65)


def parse_description(text):
    """Parse one recipe and explicit key=value attributes; never ignore a word.

    Example: robot height=2m width=1m depth=.6m color=blue material=metal seed=7
    Dimension suffixes m/cm/mm are explicit; an omitted suffix means meters.
    """
    from model3d_assemblies_presets import recipe,known_recipes
    require(isinstance(text,str) and 0<len(text)<=8192,'MODEL3D_ASSEMBLY_LANGUAGE','Description must be bounded nonempty text.')
    try:words=shlex.split(text,comments=False)
    except ValueError as exc:raise RigError('MODEL3D_ASSEMBLY_LANGUAGE','Invalid quoted description.') from exc
    require(words and words[0] in known_recipes(),'MODEL3D_ASSEMBLY_LANGUAGE',
            'Supported recipe required: '+', '.join(known_recipes())+'. Free-form inference is unavailable.')
    options={}
    for word in words[1:]:
        key,separator,value=word.partition('=')
        require(separator and key in {'height','width','depth','seed','color','material','steps','id'} and key not in options,
                'MODEL3D_ASSEMBLY_LANGUAGE','Every attribute must be a unique documented key=value token.',asset=word)
        options[key]=value
    dimensions={}
    for key in ('height','width','depth'):
        raw=options.get(key,'2m' if key=='height' else '1m')
        match=re.fullmatch(r'((?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))(m|cm|mm)?',raw)
        require(match is not None,'MODEL3D_ASSEMBLY_LANGUAGE','Dimensions require decimal m, cm or mm values.')
        amount=float(match[1])*{'m':1.,'cm':.01,'mm':.001,None:1.}[match[2]]
        require(math.isfinite(amount) and .01<=amount<=100.,'MODEL3D_ASSEMBLY_LANGUAGE','Dimensions must be within .01..100 meters.')
        dimensions[key]=amount
    integers={}
    for key,default,maximum in [('seed','0',2**32-1),('steps','5',32)]:
        raw=options.get(key,default)
        require(re.fullmatch(r'[0-9]{1,10}',raw) is not None and (0 if key=='seed' else 1)<=int(raw)<=maximum,
                'MODEL3D_ASSEMBLY_LANGUAGE','Invalid bounded integer attribute.',asset=key)
        integers[key]=int(raw)
    require('steps' not in options or words[0]=='staircase','MODEL3D_ASSEMBLY_LANGUAGE','steps applies only to staircase.')
    family=options.get('material',{'mug':'ceramic','tree':'wood','rock':'stone','sword':'metal'}.get(words[0],'plastic'))
    ir=dict(schema_version=1,kind='hatch-model3d-assembly',id=options.get('id',words[0]),seed=integers['seed'],
            description=text,fixture=True,materials=[material(family=family,color=options.get('color','blue'))],
            parts=recipe(words[0],**dimensions,**integers))
    return validate_ir(ir)


@dataclass
class Assembly:
    ir: dict
    geometry: object
    editable: dict
    world: dict


def generate(value, *, limits=None):
    """Create editable native local meshes after complete request preflight."""
    import numpy as np
    from model3d_assemblies_shapes import build_shape
    from model3d_coordinates import transform_geometry
    from rig_math import qmatrix
    from rig_models import Geometry,validate_geometry
    from rig_contracts import DEFAULT_LIMITS
    ir=validate_ir(value,limits=limits)
    native_limits=dict(DEFAULT_LIMITS,vertices=LIMITS['vertices'],triangles=LIMITS['triangles'],memory_bytes=LIMITS['memory_bytes'])
    materials=[dict(name=m['id'],pbrMetallicRoughness=dict(baseColorFactor=m['color'],metallicFactor=m['metallic'],roughnessFactor=m['roughness']),
                    **({'alphaMode':'BLEND','doubleSided':True} if m['color'][3]<1 else {})) for m in ir['materials']]
    slots={m['id']:i for i,m in enumerate(ir['materials'])};parts={p['id']:p for p in ir['parts']}
    world={};editable={};meshes=[]
    def matrix(name):
        if name not in world:
            part=parts[name];tr=part['transform'];local=np.eye(4)
            local[:3,:3]=qmatrix(tr['rotation'])@np.diag(tr['scale']);local[:3,3]=tr['translation']
            world[name]=(matrix(part['parent'])@local if part['parent'] else local)
        return world[name]
    for part in ir['parts']:
        item=build_shape(part['primitive'],part['parameters'],name=part['id'],limits=native_limits)
        item.mesh.material=slots[part['material']];item.mesh.region=part['semantic']
        editable[part['id']]=item
        transformed=transform_geometry(Geometry([item.mesh],materials),matrix(part['id']),limits=native_limits)
        meshes.append(transformed.meshes[0])
    geometry=Geometry(meshes,materials);validate_geometry(geometry,native_limits)
    return Assembly(ir,geometry,editable,world)


def revise(value, part_id, *, transform=None, parent=..., material_id=None, parameters=None):
    """Copy and validate an explicit part revision; unaffected records stay identical."""
    ir=validate_ir(value);part=next((p for p in ir['parts'] if p['id']==part_id),None)
    require(part is not None,'MODEL3D_ASSEMBLY_REFERENCE','Unknown part selection.',asset=part_id)
    if transform is not None:part['transform']=deepcopy(transform)
    if parent is not ...:part['parent']=parent
    if material_id is not None:part['material']=material_id
    if parameters is not None:part['parameters']=deepcopy(parameters)
    return validate_ir(ir)



def attach(value, part_id, parent_id, *, own_anchor='origin', parent_anchor='origin'):
    """Reparent a selected part and align actual local anchors, returning a revision."""
    import numpy as np
    from rig_math import qmatrix
    ir=validate_ir(value);parts={part['id']:part for part in ir['parts']}
    require(part_id in parts and parent_id in parts,'MODEL3D_ASSEMBLY_REFERENCE','Attachment selects a missing part.')
    child=parts[part_id];parent=parts[parent_id]
    require(own_anchor in child['attachments'] and parent_anchor in parent['attachments'],
            'MODEL3D_ASSEMBLY_REFERENCE','Attachment selects a missing named anchor.')
    linear=qmatrix(child['transform']['rotation'])@np.diag(child['transform']['scale'])
    translation=np.asarray(parent['attachments'][parent_anchor])-linear@child['attachments'][own_anchor]
    child['parent']=parent_id;child['transform']['translation']=translation.tolist()
    return validate_ir(ir)


def export_assembly(value, destination, *, format='glb', limits=None):
    """Stage native hierarchical GLB/glTF and exact editable IR; verify before publish."""
    import numpy as np
    from rig_gltf import GltfBuilder,_export_geometry
    from rig_import import load_character_model
    from rig_models import Geometry
    from atomic_publish import _rename_no_replace
    require(format in ('glb','gltf'),'MODEL3D_ASSEMBLY_FORMAT','Hierarchical assembly export supports glb or gltf.')
    assembly=generate(value,limits=limits)
    destination=Path(destination).absolute();parent=destination.parent.resolve(strict=True);destination=parent/destination.name
    require(not destination.exists() and not destination.is_symlink(),'MODEL3D_ASSEMBLY_EXISTS','Destination must be new.')
    stage=Path(tempfile.mkdtemp(prefix='.model3d-assembly-',dir=parent))
    try:
        builder=GltfBuilder()
        local=Geometry([assembly.editable[p['id']].mesh for p in assembly.ir['parts']],assembly.geometry.materials)
        nodes=_export_geometry(builder,local)
        roots=[]
        for part in assembly.ir['parts']:
            node=builder.json['nodes'][nodes[part['id']]];node.update(deepcopy(part['transform']))
            node['extras']=dict(semantic=part['semantic'],attachments=part['attachments'],assemblyPart=part['id'])
            if part['parent'] is None:roots.append(nodes[part['id']])
            else:builder.json['nodes'][nodes[part['parent']]].setdefault('children',[]).append(nodes[part['id']])
        builder.json['scenes']=[dict(nodes=roots)]
        request_hash=object_hash(assembly.ir)
        builder.json['asset']['extras']=dict(hatchSource=dict(fixture=assembly.ir['fixture'],origin='deterministic-procedural',
            generator='model3d-assemblies',version=VERSION,seed=assembly.ir['seed'],request_sha256=request_hash))
        artifact=stage/('model.'+format);builder.write(artifact)
        model=load_character_model(artifact)
        actual=model.geometry.by_id();expected=assembly.geometry.by_id()
        require(set(actual)==set(expected),'MODEL3D_ASSEMBLY_ROUNDTRIP','Export changed semantic mesh identities.')
        measurements=[]
        for part in assembly.ir['parts']:
            name=part['id'];a=expected[name];b=actual[name]
            require(len(a.positions)==len(b.positions) and np.array_equal(a.triangles,b.triangles)
                    and b.material==a.material and b.name==a.name,'MODEL3D_ASSEMBLY_ROUNDTRIP','Export changed part topology, names or materials.',asset=name)
            # Verify every face at its own scale; a large component cannot
            # conceal float32 collapse or reversed tiny features within it.
            from model3d_topology import _degenerate,_face_vectors
            corners=a.positions[a.triangles];encoded=b.positions[b.triangles]
            spans=np.max(np.abs(corners[:,1:]-corners[:,:1]),axis=(1,2))
            errors=np.max(np.abs(corners-encoded),axis=(1,2))
            require(np.all(errors<=spans*1e-3),'MODEL3D_ASSEMBLY_ROUNDTRIP','Export changed per-face coordinates.',asset=name)
            before_degenerate=_degenerate(a);after_degenerate=_degenerate(b)
            require(not np.any(~before_degenerate & after_degenerate),
                    'MODEL3D_ASSEMBLY_ROUNDTRIP','Encoded precision collapsed a nondegenerate face.',asset=name)
            before_vectors,_=_face_vectors(a);after_vectors,_=_face_vectors(b)
            require(np.all(np.sum(before_vectors*after_vectors,axis=1)>=0),
                    'MODEL3D_ASSEMBLY_ROUNDTRIP','Encoded precision reversed a face.',asset=name)
            measurements.append(dict(id=name,semantic=part['semantic'],parent=part['parent'],material=part['material'],
                vertices=len(b.positions),triangles=len(b.triangles),bounds=[b.positions.min(axis=0).tolist(),b.positions.max(axis=0).tolist()]))
        require(model.geometry.materials==assembly.geometry.materials,'MODEL3D_ASSEMBLY_ROUNDTRIP','Export changed material values.')
        (stage/'assembly.json').write_text(json.dumps(assembly.ir,sort_keys=True,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        editable={name:dict(positions=item.source_positions.tolist(),faces=item.faces,
                  triangle_faces=item.triangle_faces.tolist(),normal_mode=item.normal_mode) for name,item in assembly.editable.items()}
        (stage/'editable.json').write_text(json.dumps(editable,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n',encoding='utf-8')
        files={p.name:dict(bytes=p.stat().st_size,sha256=sha256(p)) for p in sorted(stage.iterdir())}
        report=dict(schema_version=1,kind='hatch-model3d-assembly-acceptance',generator_version=VERSION,
            request_sha256=request_hash,seed=assembly.ir['seed'],fixture=assembly.ir['fixture'],origin='deterministic-procedural',
            review_state='not-reviewed',network_calls=0,provider_calls=0,format=format,files=files,parts=measurements,
            bounds=model.report['bounds'],vertices=model.geometry.vertex_count,triangles=model.geometry.triangle_count,
            validation=dict(native_reimport=True,hierarchy_exported=True,materials_verified=True),
            dependencies=dict(python=sys.version.split()[0],numpy=np.__version__),
            limitations=['Finite recipe/IR language; no free-form learned inference.',
                'Separate editable surfaces may overlap; no Boolean union or collision/intersection certification.',
                'Curve sweeps are polygonal approximations; self-intersection is unmeasured.',
                'Materials are PBR scalar finishes; no generated textures or UVs.',
                'No skin, animation, artistic approval, Blender or render verification is implied.'])
        (stage/'acceptance.json').write_text(json.dumps(report,sort_keys=True,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        _rename_no_replace(stage,destination)
        return report
    finally:
        if stage.exists():shutil.rmtree(stage)



def demonstrate(destination):
    """Execute all named recipes in both hierarchical formats with retained hashes."""
    from model3d_assemblies_presets import known_recipes
    from atomic_publish import _rename_no_replace
    destination=Path(destination).absolute();parent=destination.parent.resolve(strict=True);destination=parent/destination.name
    require(not destination.exists() and not destination.is_symlink(),'MODEL3D_ASSEMBLY_EXISTS','Demonstration destination must be new.')
    stage=Path(tempfile.mkdtemp(prefix='.model3d-assembly-demo-',dir=parent))
    try:
        results=[]
        for name in known_recipes():
            ir=parse_description(name+' seed=7')
            for format in ('glb','gltf'):
                folder=name+'-'+format
                result=export_assembly(ir,stage/folder,format=format)
                results.append(dict(recipe=name,directory=folder,acceptance=result))
        files={p.relative_to(stage).as_posix():dict(bytes=p.stat().st_size,sha256=sha256(p))
               for p in sorted(stage.rglob('*')) if p.is_file()}
        report=dict(schema_version=1,fixture=True,review_state='not-reviewed',provider_calls=0,
                    exports=results,files=files,limitations=['Procedural engineering fixtures; no artistic approval.',
                       'Native reimport and per-part numerical validation executed; Blender/render/browser gates remain separate.'])
        (stage/'acceptance.json').write_text(json.dumps(report,sort_keys=True,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        _rename_no_replace(stage,destination)
        return report
    finally:
        if stage.exists():shutil.rmtree(stage)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('capabilities');commands.add_parser('schema')
    demo=commands.add_parser('demo');demo.add_argument('--destination',required=True)
    for command in ('explain','create'):
        sub=commands.add_parser(command);source=sub.add_mutually_exclusive_group(required=True)
        source.add_argument('--description');source.add_argument('--file')
        if command=='create':sub.add_argument('--destination',required=True);sub.add_argument('--format',choices=['glb','gltf'],default='glb')
    args=parser.parse_args(argv)
    try:
        if args.command=='demo':result=demonstrate(args.destination)
        elif args.command=='schema':result=schema()
        elif args.command=='capabilities':
            from model3d_assemblies_presets import known_recipes
            result=dict(schema_version=1,version=VERSION,recipes=known_recipes(),forms=sorted(FORMS),limits=LIMITS,
                network_calls=False,language='strict documented recipe attributes or versioned JSON IR',
                unsupported=['free-form-inference','learned-textures','boolean-solids','automatic-rigging','visual-approval'])
        else:
            ir=parse_description(args.description) if args.description is not None else validate_ir(read_json(args.file))
            result=ir if args.command=='explain' else export_assembly(ir,args.destination,format=args.format)
        print(json.dumps(result,sort_keys=True,indent=2,allow_nan=False));return 0
    except (RigError,OSError,UnicodeError) as exc:
        print(json.dumps(dict(ok=False,error=exc.report if isinstance(exc,RigError) else dict(code='MODEL3D_ASSEMBLY_IO',message=str(exc)))),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
