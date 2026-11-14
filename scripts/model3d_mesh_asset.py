"""Build strict Model3DAsset documents from actual native geometry and supplied evidence.

This pure document builder does not publish assets, infer licenses, approve review,
normalize source graphs or create source evidence. Encoded geometry remains the
native authority; material records summarize core glTF PBR, not every extension.
Parts describe the native world-space mesh view: identity transforms and zero
pivots are explicit conventions, not recovered authored hierarchy/pivots. Skins,
animation and original node graphs remain authoritative in the unchanged GLB.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import io
import math
import re
from pathlib import Path
import tempfile

from model3d_contracts import COORDINATES, LIMITS, validate_contract, schemas
from rig_errors import decode_json, object_hash, require, finite_json, MAX_JSON_BYTES, portable_name


def _fragment(value, name):
    from jsonschema import Draft202012Validator
    finite_json(value)
    schema = schemas()['model3d.schema.json']
    schema.pop('oneOf'); schema['$ref'] = '#/$defs/' + name
    error = next(Draft202012Validator(schema).iter_errors(value),None)
    require(error is None, 'MODEL3D_SCHEMA', 'Invalid supplied ' + name + ': ' + (error.message[:512] if error else ''))


def _canonical_binding(payload, geometry_record):
    require(isinstance(payload, dict) and {'canonical_ref','manifest'} <= set(payload) and set(payload) <= {'canonical_ref','manifest','source_entry'},
            'MODEL3D_REFERENCE', 'Canonical evidence must retain exact reference and original manifest.')
    ref, manifest = payload['canonical_ref'], payload['manifest']
    require(isinstance(ref, dict) and set(ref) == {'id','version','sha256'} and isinstance(manifest, dict),
            'MODEL3D_REFERENCE', 'Malformed canonical reference evidence.')
    # Explicit boundary projection of public AssetRef v1. This is structural
    # compatibility and hash binding only, never registry authentication.
    library_id = r'[a-z][a-z0-9_-]{0,63}'
    library_version = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    digest = r'[0-9a-f]{64}'
    def matches(value, pattern, maximum):
        return isinstance(value,str) and len(value) <= maximum and re.fullmatch(pattern,value) is not None
    require(matches(ref['id'],library_id,64) and matches(ref['version'],library_version,32)
            and matches(ref['sha256'],digest,64), 'MODEL3D_REFERENCE', 'Canonical AssetRef v1 fields are malformed.')
    identity = manifest.get('identity', {})
    require(isinstance(identity, dict) and all(matches(identity.get(key),library_id,64)
                for key in ('id','namespace','library')) and ref['id'] == identity['id']
            and matches(manifest.get('version'),library_version,32) and ref['version'] == manifest['version']
            and matches(manifest.get('manifest_sha256'),digest,64)
            and ref['sha256'] == manifest['manifest_sha256']
            and ref['sha256'] == object_hash({key:value for key,value in manifest.items() if key != 'manifest_sha256'}),
            'MODEL3D_REFERENCE', 'Canonical reference, namespace-bearing manifest and immutable hash disagree.')
    inventory = manifest.get('files', [])
    require(isinstance(inventory,list) and 1 <= len(inventory) <= 256,
            'MODEL3D_REFERENCE', 'Canonical inventory must be a bounded nonempty list.')
    names = set(); total = 0
    for item in inventory:
        require(isinstance(item,dict) and {'path','sha256','bytes'} <= set(item),
                'MODEL3D_REFERENCE', 'Canonical inventory record is incomplete.')
        name = item['path']; count = item['bytes']
        require(isinstance(name,str) and 1 <= len(name) <= 1024
                and all(re.fullmatch(r'[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?',part) is not None
                        for part in name.split('/')),
                'MODEL3D_REFERENCE', 'Canonical inventory path is malformed.')
        portable_name(name)
        require(name.casefold() not in names and not any(name.casefold().startswith(other+'/')
                or other.startswith(name.casefold()+'/') for other in names),
                'MODEL3D_REFERENCE', 'Canonical inventory has duplicate, alias or file/directory prefix paths.')
        names.add(name.casefold())
        require(matches(item['sha256'],digest,64) and not isinstance(count,bool)
                and isinstance(count,(int,float)) and (isinstance(count,int) or math.isfinite(count))
                and count == int(count) and 1 <= count <= 64*1024**2,
                'MODEL3D_REFERENCE', 'Canonical inventory size or digest is malformed.')
        total += int(count)
        require(total <= 256*1024**2, 'MODEL3D_REFERENCE', 'Canonical version inventory exceeds its byte limit.')
    from model3d_mesh_import import select_canonical_geometry
    require('source_entry' not in payload or isinstance(payload['source_entry'],str),
            'MODEL3D_ENTRY','Optional source_entry must be an exact active inventory path.')
    runtime=select_canonical_geometry(manifest,entry=payload.get('source_entry'))
    media={'glb':'model/gltf-binary','gltf':'model/gltf+json','obj':'model/obj'}
    require(geometry_record['media_type']==media[runtime['format']],
            'MODEL3D_ENTRY','Canonical active entry format and supplied geometry media type disagree.')
    require(runtime['sha256']==geometry_record['sha256'] and runtime['bytes']==geometry_record['bytes'],
            'MODEL3D_REFERENCE','Selected canonical active entry does not bind these exact geometry bytes.')


def build_asset(root, geometry, *, asset_id, version, category, sources, provenance,
                files=None, limits=None, canonical_evidence=None):
    """Return a fully payload-validated document; never write into caller root.

    All provenance/source/acquisition/license claims are supplied by the caller.
    When canonical_evidence is supplied it references an existing JSON payload
    containing canonical_ref and the original namespace-bearing registry manifest.
    Optional source_entry selects an active geometry/runtime inventory path; a
    unique active entry remains the default. Original source-role files never
    become canonical geometry or receive another coordinate conversion.
    The namespace is retained in that immutable evidence, never flattened to ID.
    """
    from model3d_validation import _Snapshot, validate_document
    from model3d_mesh_import import _load
    from rig_contracts import DEFAULT_LIMITS
    from PIL import Image
    _fragment(geometry,'file')
    _fragment(provenance,'provenance')
    if canonical_evidence is not None:
        _fragment(canonical_evidence,'file')
        require(canonical_evidence['bytes'] <= MAX_JSON_BYTES, 'MODEL3D_REFERENCE', 'Canonical JSON evidence exceeds decoder limits.')
    require(geometry['media_type'] in ('model/gltf-binary','model/gltf+json','model/obj'),
            'MODEL3D_FORMAT', 'Canonical geometry must be a real supported model.')
    selected_limits = deepcopy(LIMITS if limits is None else limits)
    require(isinstance(selected_limits, dict) and set(selected_limits) == set(LIMITS),
            'MODEL3D_LIMITS', 'Supply every Model3D limit or use defaults.')
    for key, value in selected_limits.items():
        require(not isinstance(value, bool) and isinstance(value, (int,float))
                and (isinstance(value,int) or math.isfinite(value)) and value == int(value)
                and 1 <= value <= LIMITS[key], 'MODEL3D_LIMITS', 'Limits require bounded whole JSON numbers.', asset=key)
        selected_limits[key] = int(value)
    require(isinstance(sources,list) and 1 <= len(sources) <= 128, 'MODEL3D_SOURCE', 'Exact source documents are mandatory.')
    for source in sources: validate_contract(source)
    require(files is None or isinstance(files,list) and 1 <= len(files) <= selected_limits['files'],
            'MODEL3D_INVENTORY', 'File inventory must be a bounded list.')
    inventory = deepcopy(files if files is not None else [geometry])
    for record in inventory: _fragment(record,'file')
    require(geometry in inventory and len({r['path'] for r in inventory}) == len(inventory),
            'MODEL3D_INVENTORY', 'Exact geometry and unique file paths are required in inventory.')
    root = Path(root).resolve()
    cache = Path.home()/'.cache'/'hatch-fleet'/'tmp'; cache.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='model3d-builder-',dir=cache) as temporary:
        snapshot = _Snapshot(root,temporary,selected_limits)
        # Capture every supplied asset/source/evidence record via the same strict
        # descriptor-safe reader used by authoritative Model3D validation.
        from model3d_contracts import file_references
        for record in inventory: snapshot.capture(record)
        for source in sources:
            for record in file_references(source): snapshot.capture(record)
        for record in provenance.get('evidence',[]): snapshot.capture(record)
        license_record = provenance.get('license',{}).get('evidence')
        if license_record is not None: snapshot.capture(license_record)
        if canonical_evidence is not None:
            require(canonical_evidence['media_type']=='application/json', 'MODEL3D_REFERENCE', 'Canonical evidence must be JSON.')
            path = snapshot.capture(canonical_evidence)
            _canonical_binding(decode_json(path.read_bytes()),geometry)
        native_limits = dict(DEFAULT_LIMITS,source_bytes=selected_limits['file_bytes'],
                             dependency_bytes=selected_limits['total_bytes'],vertices=selected_limits['vertices'],
                             triangles=selected_limits['triangles'],memory_bytes=selected_limits['memory_bytes'],influences=8)
        model = _load(Path(temporary)/geometry['path'], scale=1., limits=native_limits)
        actual_paths = {p.relative_to(temporary).as_posix() for p in model.source_files}
        require(actual_paths <= {r['path'] for r in inventory}, 'MODEL3D_INVENTORY', 'Actual native dependencies must occur in supplied inventory.')
        require(not model.report.get('fixture') or provenance['fixture'], 'MODEL3D_PROVENANCE', 'Embedded fixture provenance must be preserved.')
        materials = []
        roles = {'base_color':('pbrMetallicRoughness','baseColorTexture'),
                 'metallic_roughness':('pbrMetallicRoughness','metallicRoughnessTexture'),
                 'normal':(None,'normalTexture'),'occlusion':(None,'occlusionTexture'),'emissive':(None,'emissiveTexture')}
        for slot, native in enumerate(model.geometry.materials):
            pbr = native.get('pbrMetallicRoughness',{})
            textures,dimensions = {},{}
            for role,(parent,key) in roles.items():
                descriptor = (native.get(parent,{}) if parent else native).get(key)
                if descriptor is None: continue
                image = model.geometry.images[model.geometry.textures[descriptor['index']]['source']]
                digest = hashlib.sha256(image.data).hexdigest()
                matches = [r for r in inventory if r['sha256']==digest and r['bytes']==len(image.data)
                           and r['media_type']==image.mime_type]
                require(matches, 'MODEL3D_TEXTURE', 'Material texture requires an exact separately supplied image record.', asset=role)
                texcoord = descriptor.get('extensions',{}).get('KHR_texture_transform',{}).get('texCoord',descriptor.get('texCoord',0))
                textures[role] = dict(file=deepcopy(matches[0]),texcoord=texcoord,
                                      color_space='srgb' if role in ('base_color','emissive') else 'linear-data')
                with Image.open(io.BytesIO(image.data)) as decoded: dimensions[role]=list(decoded.size)
            materials.append(dict(schema_version=1,kind='model3d-material',id=f'material-{slot}',version=version,
                name=native.get('name',f'Material {slot}'),slot=slot,base_color=pbr.get('baseColorFactor',[1,1,1,1]),
                metallic=pbr.get('metallicFactor',1),roughness=pbr.get('roughnessFactor',1),
                alpha_mode=native.get('alphaMode','OPAQUE'),alpha_cutoff=native.get('alphaCutoff',.5),
                double_sided=native.get('doubleSided',False),textures=textures,provenance=deepcopy(provenance),
                evidence_class='observed',texture_dimensions=dimensions,procedural_parameters=[],projection_evidence=[]))
        parts,details=[],[]
        reports={item['id']:item for item in model.report['meshes']}
        for i,mesh in enumerate(model.geometry.meshes):
            parts.append(dict(schema_version=1,kind='model3d-part',id=f'part-{i}',name=mesh.name or mesh.id,parent=None,
                local_transform=dict(translation=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1]),
                mesh_ids=[mesh.id],material_ids=[f'material-{mesh.material}'],source_ids=[source['id'] for source in sources],
                uncertainty_ids=[],version=version,category=category,pivot_m=[0,0,0],attachments=[],anatomical_side='unknown',
                decomposition_components=[],mesh_regions=[],visible_evidence=[],inferred_geometry=[],confidence=None,
                confidence_method=None,rig_association=None,review_state='pending',edit_history=[]))
            report=reports[mesh.id]
            details.append(dict(mesh_id=mesh.id,geometry=deepcopy(geometry),lod=0,vertices=len(mesh.positions),
                triangles=len(mesh.triangles),material_slot=mesh.material,normals='NORMAL' in mesh.attributes,
                tangents='TANGENT' in mesh.attributes,uv_sets=[j for j in (0,1) if f'TEXCOORD_{j}' in mesh.attributes],
                topology=dict(boundary_edges=report['boundary_edges'],nonmanifold_edges=report['nonmanifold_edges'],
                              degenerate_faces=report['degenerate_triangles']),evidence_class='observed'))
        document=dict(schema_version=1,kind='model3d-asset',id=asset_id,version=version,category=category,
            coordinate_system=deepcopy(COORDINATES),sources=deepcopy(sources),files=inventory,geometry=deepcopy(geometry),
            measurements=dict(vertices=model.geometry.vertex_count,triangles=model.geometry.triangle_count,
                              bounds=dict(minimum=model.report['bounds'][0],maximum=model.report['bounds'][1])),
            parts=parts,materials=materials,dependencies=[],uncertainties=[],provenance=deepcopy(provenance),
            review=dict(status='pending',evidence=[]),limits=selected_limits,generation_revision=version,identity=None,
            pivot_m=[0,0,0],mesh_details=details,provider_receipts=[],previews=[],quality_reports=[],repair_history=[],
            available_formats=[Path(geometry['path']).suffix.lower()[1:]],completeness='partial')
        if canonical_evidence is not None:
            document['extensions']=[dict(namespace='asset-library-canonical',payload=deepcopy(canonical_evidence))]
            if canonical_evidence not in document['provenance']['evidence']:
                document['provenance']['evidence'].append(deepcopy(canonical_evidence))
        validate_contract(document)
        validate_document(document,root)
        return document
