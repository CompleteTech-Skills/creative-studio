"""Exact asset dependency closure, compatibility, immutable replacement and recovery.

Library handles are explicit authorization, never paths discovered in metadata.
Native geometry remains under rig_import/rig_models/rig_skeleton authority.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile

from asset_library_contracts import canonical_bytes, seal, validate, portable_path
from rig_errors import RigError, object_hash, timestamp
from asset_dependency_schemas import validate as validate_document

MAX_NODES = 4096
MAX_EDGES = 65536
MAX_BINDINGS = 4096


def fail(code, message, asset=None):
    raise RigError('LIBRARY_DEPENDENCY_' + code, message, asset=asset)


def bounded(value, maximum, label):
    if type(value) is not int or not 1 <= value <= maximum:
        fail('LIMIT', label + ' must be a bounded positive integer.')
    return value


def reference(value):
    validate(value, 'AssetRef')
    return deepcopy(value)


def key(value):
    return value['id'], value['version'], value['sha256']


def locator(library, ref):
    return dict(library=library, reference=reference(ref))


def location(value):
    if not isinstance(value, dict) or set(value) != {'library', 'reference'}:
        fail('LOCATION', 'Use an explicit library alias and exact asset reference.')
    name = value['library']
    if not isinstance(name, str) or not name or len(name) > 128:
        fail('LOCATION', 'Invalid library alias.')
    return locator(name, value['reference'])


def location_key(value):
    return (value['library'], *key(value['reference']))


def dependency_edges(manifest):
    """Declared asset links only; opaque source metadata is not a binding."""
    edges = [dict(target=deepcopy(d['target']), purpose=d['purpose'],
                  required=d['required'], origin=d['origin'])
             for d in manifest['dependencies']]
    edges += [dict(target=deepcopy(r), purpose='provenance-parent', required=True,
                   origin='authored') for r in manifest['provenance']['parents']]
    edges += [dict(target=deepcopy(r), purpose='style-reference', required=True,
                   origin='authored') for r in manifest['metadata']['style_refs']]
    return edges


class DependencyGraph:
    """Bounded read-only graph over explicitly configured library authorities."""
    def __init__(self, libraries, *, max_nodes=MAX_NODES, max_edges=MAX_EDGES):
        if not isinstance(libraries, dict) or not libraries or len(libraries) > 64:
            fail('LOCATION', 'Configure one to 64 authorized library handles.')
        self.libraries = dict(libraries)
        for name in libraries:
            location(dict(library=name, reference=dict(id='check',version='1.0.0',sha256='0'*64)))
        self.max_nodes=bounded(max_nodes,MAX_NODES,'Node limit')
        self.max_edges=bounded(max_edges,MAX_EDGES,'Edge limit')

    def owner(self, value):
        value=location(value)
        if value['library'] not in self.libraries:
            fail('LOCATION', 'Library alias was not authorized.', value['library'])
        return self.libraries[value['library']]

    def inspect(self, value, *, verify_payloads=True):
        return self.owner(value).inspect(location(value)['reference'], verify_payloads=verify_payloads)

    def choose(self, ref, preferred):
        """Prefer the source library; fallback requires the *same* exact digest."""
        failures=[]
        for name in [preferred, *(n for n in self.libraries if n != preferred)]:
            value=locator(name,ref)
            try:
                self.inspect(value)
                return value
            except RigError as exc:
                failures.append(dict(library=name,code=exc.report['code']))
        fail('MISSING', 'Exact dependency is unavailable in authorized libraries: '+str(failures), ref['id'])

    def direct(self, value):
        value=location(value)
        return dependency_edges(self.inspect(value))

    def closure(self, roots, *, include_optional=True, strict=True):
        if type(include_optional) is not bool or type(strict) is not bool:
            fail('REQUEST','Boolean policy flags are required.')
        if not isinstance(roots,list) or not 1<=len(roots)<=self.max_nodes:
            fail('LIMIT','Supply a bounded nonempty root list.')
        roots=[location(r) for r in roots]
        for root in roots:self.owner(root)
        if len({location_key(r) for r in roots})!=len(roots):
            fail('DUPLICATE','Duplicate graph roots.')
        nodes={};edges=[];diagnostics=[];active=set();finished=set();identities={}
        # Enter/exit frames make traversal safe for long chains without Python recursion.
        pending=[('enter',root,None,True) for root in reversed(roots)]
        while pending:
            action,current,parent,required=pending.pop(); token=location_key(current)
            identity=(current['reference']['id'],current['reference']['version'])
            if action=='exit':
                active.remove(token);finished.add(token);continue
            if token in active:
                diagnostics.append(dict(code='CYCLE',asset=current,required=required));continue
            if token in finished:continue
            if len(nodes)>=self.max_nodes:
                fail('LIMIT','Dependency graph exceeds node limit.')
            try:manifest=self.inspect(current)
            except RigError as exc:
                diagnostics.append(dict(code='MISSING_OR_CORRUPT',asset=current,required=required,
                                        cause=exc.report['code']));continue
            old=identities.setdefault(identity,current['reference']['sha256'])
            if old!=current['reference']['sha256']:
                diagnostics.append(dict(code='HASH_CONFLICT',asset=current,required=required))
            nodes[token]=dict(**current,library_id=manifest['identity']['library'],
                files=[{k:f[k] for k in ('path','sha256','bytes')} for f in manifest['files']])
            active.add(token);pending.append(('exit',current,None,required))
            seen=set();children=[]
            for d in dependency_edges(manifest):
                duplicate=(key(d['target']),d['purpose'])
                if duplicate in seen:
                    diagnostics.append(dict(code='DUPLICATE_EDGE',asset=current,required=required));continue
                seen.add(duplicate)
                if len(edges)>=self.max_edges:fail('LIMIT','Dependency graph exceeds edge limit.')
                if not d['required'] and not include_optional:
                    edges.append(dict(source=current,target=locator(current['library'],d['target']),
                                      purpose=d['purpose'],required=False,status='excluded'))
                    continue
                child_required=required and d['required']
                # Catch same ID/version back-edges before following an invalid cyclic digest.
                cycle=next((t for t in active if t[1:3]==key(d['target'])[:2]),None)
                if cycle:
                    child=locator(current['library'],d['target'])
                    diagnostics.append(dict(code='CYCLE',asset=child,required=child_required))
                else:
                    try:child=self.choose(d['target'],current['library'])
                    except RigError as exc:
                        child=locator(current['library'],d['target'])
                        diagnostics.append(dict(code='MISSING_OR_CORRUPT',asset=child,
                                                required=child_required,cause=exc.report['code']))
                        edges.append(dict(source=current,target=child,purpose=d['purpose'],
                                          required=d['required'],status='unavailable'))
                        continue
                    children.append(('enter',child,current,child_required))
                edges.append(dict(source=current,target=child,purpose=d['purpose'],
                                  required=d['required'],status='cycle' if cycle else 'resolved'))
            pending.extend(reversed(children))
        # Requiredness is a graph property, not whichever DFS path arrives first.
        # Provenance is retained in locks, but historical versions are not active
        # runtime bindings and may legitimately differ from the current version.
        required_tokens={location_key(r) for r in roots}
        runtime_tokens=set(required_tokens)
        changed=True
        while changed:
            changed=False
            for edge in edges:
                source_token=location_key(edge['source']); target_token=location_key(edge['target'])
                if edge['required'] and source_token in required_tokens and target_token not in required_tokens:
                    required_tokens.add(target_token);changed=True
                if edge['required'] and edge['purpose']!='provenance-parent' and source_token in runtime_tokens and target_token not in runtime_tokens:
                    runtime_tokens.add(target_token);changed=True
        for diagnostic in diagnostics:
            diagnostic['required']=location_key(diagnostic['asset']) in required_tokens
        runtime_versions={}
        for token in sorted(runtime_tokens):
            previous=runtime_versions.setdefault(token[1],token[2])
            if previous!=token[2]:
                diagnostics.append(dict(code='VERSION_CONFLICT',asset=locator(token[0],dict(id=token[1],version=token[2],sha256=token[3])),required=True))
        node_list=sorted(nodes.values(),key=lambda n:location_key(n))
        edges.sort(key=lambda e:(location_key(e['source']),e['purpose'],location_key(e['target'])))
        result=dict(schema_version=1,kind='asset-dependency-graph',roots=roots,nodes=node_list,
                    edges=edges,diagnostics=diagnostics,complete=not any(d['required'] for d in diagnostics))
        if strict and not result['complete']:
            fail('INVALID','Required dependency closure failed: '+str(diagnostics)[:4096])
        return validate_document(result,'graph')

    def catalog(self):
        roots=[]
        for name,library in self.libraries.items():
            roots.extend(locator(name,r) for r in library.list_references(limit=self.max_nodes))
            if len(roots)>self.max_nodes:fail('LIMIT','Authorized catalog exceeds node limit.')
        return roots

    def reverse(self, value, *, roots=None):
        value=location(value)
        candidates=self.catalog() if roots is None else [location(r) for r in roots]
        direct=[];transitive=[]
        for consumer in candidates:
            if consumer==value:continue
            graph=self.closure([consumer],strict=False)
            if any(e['target']==value and e['source']==consumer for e in graph['edges']):direct.append(consumer)
            if any(location_key(n)==location_key(value) for n in graph['nodes']):transitive.append(consumer)
        return dict(asset=value,direct=direct,transitive=transitive)

    def lock(self, roots, *, include_optional=True):
        graph=self.closure(roots,include_optional=include_optional)
        result=dict(schema_version=1,kind='asset-dependency-lock',
                    include_optional=include_optional,graph=graph)
        result['sha256']=object_hash(result)
        return validate_document(result,'lock')

    def verify_lock(self, lock):
        validate_document(lock,'lock')
        if not isinstance(lock,dict) or set(lock)!={'schema_version','kind','include_optional','graph','sha256'}:
            fail('LOCK','Invalid lock document.')
        if lock['schema_version']!=1 or lock['kind']!='asset-dependency-lock':fail('LOCK','Unsupported lock version.')
        body={k:v for k,v in lock.items() if k!='sha256'}
        if object_hash(body)!=lock['sha256']:fail('LOCK','Lock bytes were altered.')
        current=self.lock(lock['graph']['roots'],include_optional=lock['include_optional'])
        if current!=lock:fail('STALE','Pinned closure or source-library resolution changed.')
        return current

    def recover(self, value, path, *, donors):
        """Recover exactly one expected blob from explicitly authorized donors."""
        value=location(value);portable_path(path)
        manifest=self.inspect(value,verify_payloads=False)
        record=next((f for f in manifest['files'] if f['path']==path),None)
        if record is None:fail('RECOVERY','File is not in the immutable inventory.',path)
        if not isinstance(donors,list) or not 1<=len(donors)<=self.max_nodes:
            fail('RECOVERY','Choose explicit bounded donor references.')
        for donor in donors:
            donor=location(donor);source=self.inspect(donor)
            candidate=next((f for f in source['files'] if f['sha256']==record['sha256'] and f['bytes']==record['bytes']),None)
            if candidate:
                payload=self.owner(donor).read_own_file(donor['reference'],candidate['path'])
                if len(payload)!=record['bytes'] or hashlib.sha256(payload).hexdigest()!=record['sha256']:
                    fail('RECOVERY','Donor changed during exact-byte recovery.')
                receipt=self.owner(value).recover_blob(record['sha256'],payload,expected_bytes=record['bytes'])
                self.inspect(value)
                return dict(asset=value,path=path,donor=donor,sha256=record['sha256'],receipt=receipt)
        fail('RECOVERY','No explicitly authorized donor contains the exact required bytes.',path)


def native_signature(graph, value, path):
    """Decode actual native bytes in a private snapshot; never trust names alone."""
    value=location(value);portable_path(path)
    library=graph.owner(value);manifest=graph.inspect(value)
    if path not in {f['path'] for f in manifest['files']}:
        fail('COMPATIBILITY','Native entry is outside the exact inventory.',path)
    with tempfile.TemporaryDirectory(prefix='hatch-dependency-native-') as temporary:
        root=Path(temporary).resolve()
        for record in manifest['files']:
            target=root/record['path'];target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(library.read_own_file(value['reference'],record['path']))
        if Path(path).name=='rig.json':
            from rig_models import load_rig
            from rig_skeleton import validate_skeleton
            model=load_rig((root/path).parent)
            skeleton=model.manifest['skeleton']
            if skeleton is not None:validate_skeleton(skeleton)
            rig_type=model.manifest['archetype']
        else:
            from rig_import import load_character_model
            from rig_skeleton import validate_skeleton
            if Path(path).suffix.lower() not in ('.glb','.gltf','.obj'):
                fail('COMPATIBILITY','Native compatibility only supports actual rig/GLB/glTF/OBJ files.')
            model=load_character_model(root/path)
            skeleton=model.skeleton
            if skeleton is not None:validate_skeleton(skeleton)
            rig_type='imported' if skeleton else 'static'
        bones=[] if skeleton is None else sorted(skeleton['bones'],key=lambda b:b['id'])
        hierarchy=[{k:b[k] for k in ('id','parent','role','deform','mirror')} for b in bones]
        rest=[{k:b[k] for k in ('id','rest','head','tail','length','roll')} for b in bones]
        def array_digest(value,dtype):
            import numpy as np
            array=np.ascontiguousarray(value,dtype=dtype)
            digest=hashlib.sha256(canonical_bytes(dict(dtype=dtype,shape=list(array.shape))))
            view=memoryview(array).cast('B')
            for start in range(0,len(view),1024*1024):digest.update(view[start:start+1024*1024])
            return digest.hexdigest()
        skins=[]
        for name,skin in sorted(model.weights.items()):
            skins.append(dict(mesh=name,joints=skin.joints,indices_sha256=array_digest(skin.indices,'<i8'),
                              weights_sha256=array_digest(skin.weights,'<f8'),inverse_bind_sha256=array_digest(skin.inverse_bind,'<f8')))
        morphs=[dict(mesh=m.id,defaults=m.morph_defaults,
                     targets={name:{semantic:array_digest(values,'<f8')
                                    for semantic,values in sorted(attributes.items())}
                              for name,attributes in sorted(m.morphs.items())})
                for m in model.geometry.meshes]
        root_modes=sorted({clip.get('root_motion','embedded') for clip in model.clips.values()})
        return dict(skeleton_sha256=object_hash(hierarchy),rest_pose_sha256=object_hash(rest),
                    skin_sha256=object_hash(skins),morph_sha256=object_hash(morphs),
                    material_sha256=object_hash(model.geometry.materials),
                    rig_type=rig_type,root_motion=root_modes,
                    bone_count=len(bones),mesh_count=len(model.geometry.meshes),
                    coordinate_system='right-handed-y-up-z-forward',unit_scale=1)


_REQUIREMENTS={'coordinate_system','unit_scale','skeleton_sha256','rest_pose_sha256',
               'skin_sha256','morph_sha256','material_sha256','rig_type','root_motion',
               'material_capabilities','texture_channels','animation_channels','renderers','formats','extensions'}
_LIST_REQUIREMENTS={'root_motion','material_capabilities','texture_channels','animation_channels','renderers','formats','extensions'}
_NATIVE_REQUIREMENTS={'skeleton_sha256','rest_pose_sha256','skin_sha256','morph_sha256','material_sha256','rig_type','root_motion'}


def check_compatibility(graph,value,requirements,*,native_path=None):
    if not isinstance(requirements,dict) or set(requirements)-_REQUIREMENTS:
        fail('COMPATIBILITY','Unknown compatibility requirement.')
    validate_document(requirements,'requirements')
    manifest=graph.inspect(value)
    actual=deepcopy(manifest['compatibility']);native=None
    if set(requirements)&_NATIVE_REQUIREMENTS:
        if native_path is None:fail('COMPATIBILITY','Native requirements need an explicit inventoried native entry.')
        native=native_signature(graph,value,native_path)
        # Decoding reports the importer's canonical space, not a conversion of
        # the asset's declared source units or coordinate contract.
        actual.update({field:native[field] for field in _NATIVE_REQUIREMENTS})
    mismatches=[]
    for field,expected in requirements.items():
        if field in _LIST_REQUIREMENTS:
            if not isinstance(expected,list) or len(expected)>128 or any(not isinstance(v,str) for v in expected) or len(set(expected))!=len(expected):
                fail('COMPATIBILITY','Expected a bounded unique string requirement list.',field)
            okay=set(expected)<=set(actual.get(field,[]))
        else:
            if type(expected) not in (str,int,float) or type(expected) is bool:
                fail('COMPATIBILITY','Expected a scalar exact requirement.',field)
            okay=type(expected) is str and expected==actual.get(field) or type(expected) in (int,float) and expected==actual.get(field)
        if not okay:mismatches.append(dict(field=field,expected=expected,actual=actual.get(field)))
    return dict(compatible=not mismatches,mismatches=mismatches,native=native,
                renderer_execution_verified=False)


def replacement_plan(graph,old,new,*,consumers,requirements=None,native_path=None):
    """Plan immutable library binding versions, never patch arbitrary project files."""
    old=location(old);new=location(new)
    graph.closure([old]);graph.closure([new])
    if old==new:fail('REPLACEMENT','Replacement must be a different exact version.')
    if not isinstance(consumers,list) or not 1<=len(consumers)<=MAX_BINDINGS:
        fail('REPLACEMENT','Choose explicit bounded consumer bindings.')
    compatibility=check_compatibility(graph,new,requirements or {},native_path=native_path)
    if not compatibility['compatible']:fail('COMPATIBILITY','Replacement does not satisfy exact binding requirements.')
    changes=[];seen=set()
    for consumer in consumers:
        if not isinstance(consumer,dict) or set(consumer)!={'asset','purposes'}:
            fail('REPLACEMENT','Each consumer needs an exact asset and explicit dependency purposes.')
        source=location(consumer['asset']);token=location_key(source)
        if token in seen:fail('DUPLICATE','A consumer appears more than once.')
        seen.add(token)
        purposes=consumer['purposes']
        if not isinstance(purposes,list) or not purposes or len(purposes)>256 or any(not isinstance(p,str) or not p for p in purposes) or len(set(purposes))!=len(purposes):
            fail('REPLACEMENT','Choose unique exact dependency purposes.')
        manifest=graph.inspect(source);indices=[]
        if graph.choose(old['reference'],source['library'])!=old:
            fail('REPLACEMENT','Selected old library differs from the binding resolution.')
        if graph.choose(new['reference'],source['library'])!=new:
            fail('REPLACEMENT','Selected replacement library cannot be preserved by this consumer.')
        # The current canonical store admits dependencies in its own library.
        # Cross-library project activation needs the scoped owner adapter.
        if source['library']!=new['library']:
            fail('REPLACEMENT','Cross-library bindings require an explicit scoped consumer adapter.')
        for purpose in purposes:
            matches=[i for i,d in enumerate(manifest['dependencies']) if d['purpose']==purpose and d['target']==old['reference']]
            if len(matches)!=1:fail('REPLACEMENT','Target binding is absent or ambiguous.',purpose)
            indices.extend(matches)
        changes.append(dict(asset=source,indices=sorted(indices),purposes=purposes,
                            original_manifest_sha256=manifest['manifest_sha256']))
    impacted={location_key(c['asset']):c['asset'] for c in changes}
    for change in changes:
        for dependent in graph.reverse(change['asset'])['transitive']:
            impacted[location_key(dependent)]=dependent
    result=dict(schema_version=1,kind='asset-replacement-plan',old=old,new=new,
                changes=changes,compatibility=compatibility,
                invalidated=sorted(impacted.values(),key=location_key),
                old_lock=graph.lock([c['asset'] for c in changes]),new_lock=graph.lock([new]),
                project_files_modified=False)
    result['sha256']=object_hash(result)
    return validate_document(result,'plan')


def apply_replacement(graph,plan,*,versions):
    """Publish new immutable consumer manifests through the existing store.

    The resulting binding versions are explicit opt-ins. Existing project pins
    and native scene payloads remain unchanged; production activation is #149.
    Each publication is an independent library transaction, documented in receipts.
    """
    validate_document(plan,'plan')
    if not isinstance(plan,dict) or plan.get('kind')!='asset-replacement-plan' or plan.get('schema_version')!=1:
        fail('REPLACEMENT','Invalid replacement plan.')
    if object_hash({k:v for k,v in plan.items() if k!='sha256'})!=plan.get('sha256'):
        fail('REPLACEMENT','Replacement plan has changed.')
    graph.verify_lock(plan['old_lock']);graph.verify_lock(plan['new_lock'])
    if not isinstance(versions,dict) or set(versions)!={c['asset']['reference']['id'] for c in plan['changes']}:
        fail('REPLACEMENT','Choose a new explicit version for every changed consumer.')
    # Validate all changes before the first mutation; no transaction spans libraries.
    prepared=[]
    consumer_ids=[c['asset']['reference']['id'] for c in plan['changes']]
    if len(set(consumer_ids))!=len(consumer_ids):
        fail('REPLACEMENT','Version selections require unique consumer identities.')
    for change in plan['changes']:
        source=location(change['asset']);manifest=graph.inspect(source);updated=deepcopy(manifest)
        if graph.choose(plan['old']['reference'],source['library'])!=plan['old'] or graph.choose(plan['new']['reference'],source['library'])!=plan['new']:
            fail('STALE','Source-library binding resolution changed.')
        if source['library']!=plan['new']['library']:
            fail('REPLACEMENT','Cross-library activation requires the scoped consumer adapter.')
        updated['version']=versions[manifest['identity']['id']]
        if updated['version']==manifest['version']:fail('REPLACEMENT','Existing versions cannot be replaced.')
        for index in change['indices']:
            if updated['dependencies'][index]['target']!=plan['old']['reference']:
                fail('STALE','Target binding changed.')
            updated['dependencies'][index]['target']=deepcopy(plan['new']['reference'])
        updated['provenance']['method']='derived'
        updated['provenance']['parents']=list(updated['provenance']['parents'])+[source['reference']]
        updated['provenance']['edits']=list(updated['provenance']['edits'])+['Dependency binding replacement plan '+plan['sha256']]
        updated=seal(updated)
        prepared.append((source,manifest,updated))
    # Validate full proposed runtime closures, including unchanged transitive
    # bindings, before creating any immutable version. These are planned exact
    # manifests, not substituted runtime payloads; all copied bytes were verified.
    overrides={location_key(locator(source['library'],dict(id=updated['identity']['id'],version=updated['version'],sha256=updated['manifest_sha256']))):updated
               for source,manifest,updated in prepared}
    class PlannedGraph(DependencyGraph):
        def inspect(self,value,*,verify_payloads=True):
            token=location_key(location(value))
            if token in overrides:return deepcopy(overrides[token])
            return super().inspect(value,verify_payloads=verify_payloads)
    preview=PlannedGraph(graph.libraries,max_nodes=graph.max_nodes,max_edges=graph.max_edges)
    for token in overrides:
        preview.closure([locator(token[0],dict(id=token[1],version=token[2],sha256=token[3]))])
    published=[]
    for source,manifest,updated in prepared:
        library=graph.owner(source)
        with tempfile.TemporaryDirectory(prefix='hatch-dependency-replacement-') as temporary:
            root=Path(temporary).resolve();files={}
            for record in manifest['files']:
                path=root/record['path'];path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(library.read_own_file(source['reference'],record['path']));files[record['path']]=path
            new=library.register(updated,files)
        published.append(dict(previous=source,current=locator(source['library'],new),
                              rollback=source,plan_sha256=plan['sha256']))
    return dict(published=published,atomic_scope='one-consumer-version',
                existing_projects_unchanged=True,invalidated=deepcopy(plan['invalidated']))


def normalize_static_units(graph,value,path,*,version):
    """Explicit local unit conversion, returning a new verified derived version.

    Only static geometry is supported. Rigged/animated data must use an owning
    retarget/conversion adapter; it is rejected instead of losing channels.
    """
    from asset_library import file_record
    from rig_import import load_character_model
    from rig_gltf import export_geometry
    import numpy as np
    value=location(value);portable_path(path)
    library=graph.owner(value);manifest=graph.inspect(value)
    if manifest['compatibility']['coordinate_system']!='right-handed-y-up-z-forward':
        fail('CONVERSION','This adapter only converts units in canonical 3D coordinates.')
    if Path(path).suffix.lower() not in ('.obj','.gltf','.glb'):
        fail('CONVERSION','Choose a static OBJ, glTF or GLB payload.')
    selected=next((f for f in manifest['files'] if f['path']==path),None)
    if selected is None or selected['role'] not in ('geometry','runtime'):
        fail('CONVERSION','Choose an active geometry or runtime entry; retained source evidence is not canonical geometry.')
    if version==manifest['version']:fail('CONVERSION','Conversion needs a new immutable version.')
    scale=manifest['compatibility']['unit_scale']
    with tempfile.TemporaryDirectory(prefix='hatch-dependency-conversion-') as temporary:
        root=Path(temporary).resolve();sources={}
        for record in manifest['files']:
            target=root/record['path'];target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(library.read_own_file(value['reference'],record['path']));sources[record['path']]=target
        if path not in sources:fail('CONVERSION','Native entry is not inventoried.')
        model=load_character_model(sources[path],scale=scale)
        if model.skeleton or model.weights or model.clips:
            fail('CONVERSION','Rigged or animated content requires its native conversion owner.')
        output='derived/unit-normalized.glb'
        evidence='derived/unit-normalization.json'
        if output in sources or evidence in sources:
            fail('CONVERSION','Reserved normalized output/evidence already exists; preserve it in a separate version plan.')
        target=root/output;target.parent.mkdir(parents=True,exist_ok=True)
        fixture = manifest['provenance']['fixture'] or model.report.get('fixture') is True
        export_geometry(model.geometry,target,provenance=dict(fixture=fixture,
                                                            operation='explicit-unit-normalization',source_sha256=value['reference']['sha256']))
        reopened=load_character_model(target)
        if reopened.geometry.vertex_count!=model.geometry.vertex_count or reopened.geometry.triangle_count!=model.geometry.triangle_count:
            fail('CONVERSION','Converted geometry changed topology.')
        for expected,actual in zip(model.geometry.meshes,reopened.geometry.meshes):
            if not np.array_equal(expected.triangles,actual.triangles) or not np.allclose(expected.positions,actual.positions,rtol=1e-6,atol=1e-7):
                fail('CONVERSION','Independent reopened geometry differs from normalized coordinates.')
        record=file_record(target,output,'runtime');sources[output]=target
        updated=deepcopy(manifest);updated['version']=version
        originals=[deepcopy(f) for f in manifest['files'] if f['role'] in ('geometry','runtime')]
        retained={f['path']:{**deepcopy(f),'role':'source'} for f in originals}
        # All evidence references must remain identical to their inventoried
        # records after demotion. Follow the canonical typed references rather
        # than interpreting file-looking keys inside opaque technical metadata.
        from asset_library_contracts import definitions
        contracts=definitions()
        def retain_sources(node,schema):
            if '$ref' in schema:
                name=schema['$ref'].rsplit('/',1)[-1]
                if name=='AssetFile':return deepcopy(retained.get(node['path'],node))
                schema=contracts[name]
            if isinstance(node,dict):
                for key,child_schema in schema.get('properties',{}).items():
                    if key in node:node[key]=retain_sources(node[key],child_schema)
            elif isinstance(node,list) and 'items' in schema:
                node=[retain_sources(child,schema['items']) for child in node]
            return node
        updated=retain_sources(updated,contracts['AssetVersion'])
        provenance=updated['provenance']
        known_sources={f['path'] for f in provenance['sources']}
        provenance['sources'].extend(deepcopy(f) for name,f in retained.items() if name not in known_sources)
        evidence_path=root/evidence
        evidence_path.write_bytes(canonical_bytes(dict(schema_version=1,kind='asset-static-unit-normalization',
            source=value['reference'],selected_entry=path,original_compatibility=manifest['compatibility'],
            original_active_files=originals,normalized_output=record)))
        evidence_record=file_record(evidence_path,evidence,'evidence',False);sources[evidence]=evidence_path
        updated['files'].extend([record,evidence_record])
        updated['compatibility']['unit_scale']=1;updated['compatibility']['formats']=['glb']
        updated['publication']['inventory'].extend([output,evidence])
        provenance['fixture']=fixture
        provenance['method']='derived';provenance['parents'].append(value['reference'])
        provenance['conversions']=provenance.get('conversions',[])+[dict(tool='asset-dependency-static-units',version='1.0.0',timestamp=timestamp(),
            operation='Explicit canonical static unit normalization; selected geometry reopened and verified; original units retained in conversion evidence.',
            inputs=[retained[path]],outputs=[record,evidence_record])]
        derived=library.register(seal(updated),sources)
    graph.closure([locator(value['library'],derived)])
    return dict(asset=locator(value['library'],derived),native_path=output,source=value,
                conversion='canonical-static-unit-normalization',geometry_verified=True,
                production_approved=False)


def dot(graph_report):
    """Safe Graphviz DOT with generated IDs; untrusted labels are JSON-escaped."""
    import json
    nodes=graph_report['nodes'];ids={location_key(n):'n'+str(i) for i,n in enumerate(nodes)}
    lines=['digraph assets {']
    for n in nodes:
        r=n['reference'];label=n['library']+':'+r['id']+'@'+r['version']
        lines.append('  '+ids[location_key(n)]+' [label='+json.dumps(label)+'];')
    for edge in graph_report['edges']:
        a=ids.get(location_key(edge['source']));b=ids.get(location_key(edge['target']))
        if a and b:lines.append('  '+a+' -> '+b+' [label='+json.dumps(edge['purpose'])+'];')
    return '\n'.join(lines+['}'])+'\n'
