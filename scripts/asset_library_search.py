"""Offline, rebuildable search over verified immutable asset-library records.

The SQLite file is a derived cache. It cannot publish assets or grant geometry,
review or distribution approval. Every query rechecks its exact source snapshot.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Protocol

from asset_library_contracts import ID_PATTERN, canonical_bytes, validate
from rig_errors import RigError, decode_json, object_hash

MAX_ASSETS = 4096
MAX_QUERY = 2048
MAX_RESULTS = 100
MAX_INDEX_BYTES = 64 * 1024 * 1024
FILTERS = {'kind', 'subtype', 'category', 'tags', 'collections', 'favorite', 'author',
           'publisher', 'license', 'scope', 'library', 'version', 'format', 'material',
           'rig_type', 'animation', 'renderer', 'method', 'capabilities', 'review_status',
           'created_after', 'created_before', 'max_triangles', 'max_texture_pixels', 'used_by'}
ALL_FILTERS = {'tags','collections','capabilities','animation','renderer','material'}
LIST_FILTERS = FILTERS - {'favorite', 'max_triangles', 'max_texture_pixels',
                         'created_after', 'created_before'}
STOP = {'a','an','the','find','show','me','that','is','are','and','or','for','of','with',
        'suitable','can','reusable','some','please','asset','assets'}


def fail(code, message):
    raise RigError('LIBRARY_SEARCH_' + code, message, stage='asset-search')


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERN, value):
        fail('REQUEST', 'Expected a portable identifier.')
    return value


def _strings(value, label, maximum=128):
    if (not isinstance(value, list) or len(value) > maximum or
        any(not isinstance(v,str) or not 1 <= len(v) <= 256 for v in value) or
        len(set(value)) != len(value)):
        fail('REQUEST', label + ' must be a bounded unique string list.')
    canonical_bytes(value)
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        fail('LIMIT', 'Expected a bounded integer.')
    return value


def _location(value):
    if not isinstance(value, dict) or set(value) != {'library','reference'}:
        fail('REQUEST', 'Asset locations require a library alias and exact reference.')
    _identifier(value['library']); validate(value['reference'], 'AssetRef')
    return value


def _key(value):
    return (value['library'], value['reference']['id'], value['reference']['version'],
            value['reference']['sha256'])


def parse_query(query):
    """Extract only documented, deterministic constraints; no model inference."""
    if not isinstance(query,str) or len(query) > MAX_QUERY:
        fail('REQUEST', 'Query must be text of at most 2048 characters.')
    canonical_bytes(query)
    normalized = query.casefold()
    constraints = {}
    reasons = []
    kinds = {'characters':'character','character':'character','materials':'material',
             'material':'material','textures':'texture','texture':'texture',
             'cameras':'camera','camera':'camera','lights':'light','lighting':'light',
             'environments':'environment','environment':'environment','props':'prop'}
    tokens = re.findall(r'[\w]+', normalized, re.UNICODE)
    found = sorted({kinds[t] for t in tokens if t in kinds})
    if len(found) == 1:
        constraints['kind'] = found
        reasons.append('explicit asset kind: ' + found[0])
        tokens = [t for t in tokens if t not in kinds]
    if re.search(r'\blow[- ](?:polygon|poly)\b', normalized):
        constraints['max_triangles'] = 10000
        reasons.append('low-polygon means at most 10000 recorded triangles')
        tokens = [t for t in tokens if t not in {'low','polygon','poly'}]
    if re.search(r'\bcan walk\b', normalized):
        constraints['capabilities'] = ['walk']
        reasons.append('explicit walking capability: walk')
        tokens = [t for t in tokens if t != 'walk']
    return dict(requires_compatibility=bool(re.search(r'\bcompatible with\b',normalized)),
                tokens=list(dict.fromkeys(t for t in tokens if t not in STOP))[:64],
                constraints=constraints, reasons=reasons,
                inference='deterministic lexical rules; no embeddings or language model')


def _filters(value):
    if not isinstance(value,dict) or set(value) - FILTERS:
        fail('REQUEST','Unknown search filter.')
    canonical_bytes(value)
    for name, expected in value.items():
        if name in LIST_FILTERS: _strings(expected,name)
        elif name == 'favorite':
            if type(expected) is not bool: fail('REQUEST','favorite must be boolean.')
        elif name.startswith('max_'): _integer(expected,0,10**12)
        else:
            if not isinstance(expected,str) or len(expected)>64:
                fail('REQUEST','Date filters require bounded RFC3339 timestamps.')
            validate(dict(id='date',namespace='date',library='date',slug='date',name='date',
                          kind='reference',subtype='date',created_at=expected,
                          attribution='date',owner='date'),'AssetIdentity')
    return value


class EmbeddingProvider(Protocol):
    model_id: str
    model_version: str
    runtime_available: bool
    requires_credentials: bool
    credentials_available: bool
    transmits_metadata: bool
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class ProviderRegistry:
    """Explicit trusted Python adapters, disabled until configured by the caller."""
    def __init__(self):
        self._providers = {}

    def register(self, name, provider):
        _identifier(name)
        if name in self._providers: fail('PROVIDER','Provider is already registered.')
        for field in ('model_id','model_version'):
            text = getattr(provider,field,None)
            if not isinstance(text,str) or not 1 <= len(text) <= 256:
                fail('PROVIDER','Provider requires exact model identity and version.')
        for field in ('runtime_available','requires_credentials','credentials_available','transmits_metadata'):
            if type(getattr(provider,field,None)) is not bool:
                fail('PROVIDER','Provider must declare runtime, credential and transmission gates.')
        if not callable(getattr(provider,'encode',None)): fail('PROVIDER','Provider cannot encode.')
        self._providers[name] = provider

    def get(self, name, *, enabled=False, authorize_private_metadata=False):
        if type(enabled) is not bool or type(authorize_private_metadata) is not bool:
            fail('PROVIDER','Provider gates require explicit booleans.')
        if name not in self._providers: fail('PROVIDER','Unknown embedding provider/model.')
        provider = self._providers[name]
        if not enabled: fail('PROVIDER','Embedding providers are disabled by default.')
        if not provider.runtime_available: fail('PROVIDER','Embedding runtime is unavailable.')
        if provider.requires_credentials and not provider.credentials_available:
            fail('PROVIDER','Embedding credentials are unavailable.')
        if provider.transmits_metadata and not authorize_private_metadata:
            fail('PRIVATE','Explicit private-metadata transmission authorization is required.')
        return provider

    def capabilities(self):
        return [dict(name=n,model_id=p.model_id,model_version=p.model_version,
                     runtime_available=p.runtime_available,requires_credentials=p.requires_credentials,
                     credentials_available=p.credentials_available,transmits_metadata=p.transmits_metadata)
                for n,p in sorted(self._providers.items())]


def _vectors(values, count):
    if not isinstance(values,list) or len(values)!=count:
        fail('EMBEDDING','Provider returned an incorrect vector count.')
    dimensions = None
    result = []
    for vector in values:
        if not isinstance(vector,list) or not 1<=len(vector)<=2048:
            fail('EMBEDDING','Vectors require one to 2048 dimensions.')
        if dimensions is None: dimensions=len(vector)
        if dimensions!=len(vector): fail('EMBEDDING','Vector dimensions differ.')
        if any(type(v) not in (float,int) or not math.isfinite(v) or abs(v)>1e12 for v in vector):
            fail('EMBEDDING','Vectors require bounded finite numeric values.')
        norm=math.hypot(*vector)
        if norm==0: fail('EMBEDDING','Zero vectors cannot define cosine similarity.')
        result.append([v/norm for v in vector])
    return result


class SearchIndex:
    """Bounded snapshot search using existing library and optional lifecycle APIs.

    Configure explicit library aliases. ``versions`` maps aliases to native
    AssetVersions handles for collections. ``usage_resolver`` is the native
    ScopedResolver; only explicitly listed project IDs are inspected.
    """
    def __init__(self, root, libraries, *, create=False, versions=None,
                 usage_resolver=None, project_ids=(), compatibility=None, dependency_graph=None):
        if not isinstance(libraries,dict) or not 1<=len(libraries)<=32:
            fail('REQUEST','Configure one to 32 explicit libraries.')
        for name in libraries: _identifier(name)
        self.libraries=dict(libraries)
        self.versions=dict(versions or {})
        if set(self.versions)-set(libraries): fail('REQUEST','Collection owner is not configured.')
        self.usage_resolver=usage_resolver
        self.project_ids=_strings(list(project_ids),'Project IDs',128)
        for name in self.project_ids: _identifier(name)
        if self.project_ids and usage_resolver is None: fail('REQUEST','Usage requires native binding authority.')
        self.compatibility=compatibility
        self.dependency_graph=dependency_graph
        if dependency_graph is not None and dependency_graph.libraries!=self.libraries:
            fail('REQUEST','Dependency graph must use the same explicit library handles.')
        self.root=Path(root).absolute()
        if create: self.root.mkdir(mode=0o700,parents=False,exist_ok=False)
        info=self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or self.root.is_symlink() or
            getattr(info,'st_file_attributes',0)&0x400 or
            (os.name!='nt' and (info.st_uid!=os.geteuid() or info.st_mode&0o077))):
            fail('PATH','Index root must be an owned private directory.')
        self._identity=(info.st_dev,info.st_ino)
        self.database=self.root/'search.sqlite3'
        if create:
            fd=os.open(self.database,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
            os.close(fd)
            with self._connect() as db:
                db.executescript('''CREATE TABLE state(key TEXT PRIMARY KEY, value BLOB NOT NULL);
                    CREATE TABLE entries(id INTEGER PRIMARY KEY, document BLOB NOT NULL, text TEXT NOT NULL);
                    CREATE VIRTUAL TABLE words USING fts5(text);
                    CREATE TABLE vectors(id INTEGER PRIMARY KEY, vector BLOB NOT NULL);''')
                db.execute('INSERT INTO state VALUES(?,?)',('schema',b'1'))
        with self._connect() as db:
            if db.execute("SELECT value FROM state WHERE key='schema'").fetchone()!=(b'1',):
                fail('INDEX','Unsupported index schema; create a new derived index.')

    @contextmanager
    def _connect(self):
        info=self.root.lstat()
        if (self.root.is_symlink() or (info.st_dev,info.st_ino)!=self._identity or
            getattr(info,'st_file_attributes',0)&0x400 or
            (os.name!='nt' and (info.st_uid!=os.geteuid() or info.st_mode&0o077))):
            fail('PATH','Index root was replaced or lost private ownership.')
        info=self.database.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or
            getattr(info,'st_file_attributes',0)&0x400 or info.st_size>MAX_INDEX_BYTES or
            (os.name!='nt' and (info.st_uid!=os.geteuid() or info.st_mode&0o077))):
            fail('PATH','Index must be a bounded private regular file.')
        db=sqlite3.connect(self.database.as_uri()+'?mode=rw',uri=True,timeout=5)
        try:
            db.execute('PRAGMA trusted_schema=OFF');db.execute('PRAGMA synchronous=FULL')
            page_size=db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count='+str(MAX_INDEX_BYTES//page_size))
            with db: yield db
        except sqlite3.DatabaseError as exc:
            raise RigError('LIBRARY_SEARCH_INDEX','Invalid derived index: '+str(exc),stage='asset-search') from exc
        finally: db.close()

    def _catalog(self):
        result=[]
        for name,library in self.libraries.items():
            if not callable(getattr(library,'list_references',None)):
                fail('CAPABILITY','Library requires the native list_references API or an explicit rebuild selection.')
            result.extend(dict(library=name,reference=r) for r in library.list_references(limit=MAX_ASSETS))
        if len(result)>MAX_ASSETS: fail('LIMIT','Combined library catalog exceeds 4096 records.')
        return sorted(result,key=_key)

    def _usage(self):
        return {p:self.usage_resolver.bindings(p) for p in self.project_ids}

    def _capture(self, locations):
        entries=[];diagnostics=[];size=0
        usage=self._usage()
        for location in locations:
            _location(location)
            alias=location['library'];library=self.libraries.get(alias)
            if library is None: fail('REQUEST','Unconfigured library alias.')
            try:
                if self.dependency_graph is not None:
                    self.dependency_graph.closure([location])
                    manifest=self.dependency_graph.inspect(location)
                else:
                    manifest=library.resolve(location['reference'])
                annotations=library.annotations(location['reference'])
                collections=(self.versions[alias].collections(location['reference']) if alias in self.versions
                             else dict(revision=0,collections=[]))
                used=sorted(p for p,state in usage.items() if location in state['bindings'].values())
                entry=dict(location=location,manifest=manifest,annotations=annotations,
                           collections=collections,used_by=used)
                size+=len(canonical_bytes(entry))
                if size>MAX_INDEX_BYTES//4: fail('LIMIT','Metadata exceeds 16 MiB.')
                entries.append(entry)
            except RigError as exc:
                if exc.report['code']=='LIBRARY_SEARCH_LIMIT':raise
                diagnostics.append(dict(location=location,code=exc.report['code'],complete=False))
        return dict(entries=entries,diagnostics=diagnostics,usage=usage)

    def rebuild(self, locations=None):
        explicit=locations is not None
        if not explicit: locations=self._catalog()
        if not isinstance(locations,list) or len(locations)>MAX_ASSETS: fail('LIMIT','Too many index inputs.')
        locations=sorted([_location(v) for v in decode_json(canonical_bytes(locations))],key=_key)
        if len({_key(v) for v in locations})!=len(locations): fail('REQUEST','Duplicate index input.')
        snapshot=self._capture(locations)
        # Capture twice before publication to detect source/annotation/binding races.
        if self._capture(locations)!=snapshot: fail('STALE','Sources changed during index rebuild.')
        if not explicit and self._catalog()!=locations: fail('STALE','Catalog changed during index rebuild.')
        state=dict(schema_version=1,locations=locations,explicit_selection=explicit,snapshot=snapshot)
        payload=canonical_bytes(state)
        if len(payload)>MAX_INDEX_BYTES//2: fail('LIMIT','Derived metadata exceeds 32 MiB.')
        revision=hashlib.sha256(payload).hexdigest()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM entries');db.execute('DELETE FROM words');db.execute('DELETE FROM vectors')
            db.execute("DELETE FROM state WHERE key IN ('snapshot','revision','embedding')")
            for number,entry in enumerate(snapshot['entries'],1):
                text=self._text(entry)
                db.execute('INSERT INTO entries VALUES(?,?,?)',(number,canonical_bytes(entry),text))
                db.execute('INSERT INTO words(rowid,text) VALUES(?,?)',(number,text))
            db.executemany('INSERT INTO state VALUES(?,?)',[('snapshot',payload),('revision',revision.encode())])
        return dict(revision=revision,indexed=len(snapshot['entries']),diagnostics=snapshot['diagnostics'],
                    embedding_used=False)

    @staticmethod
    def _text(entry):
        m=entry['manifest'];a=entry['annotations']
        values=[m['identity']['name'],m['identity']['subtype'],m['metadata']['description'],
                m['metadata']['category'],m['metadata']['author'],m['metadata']['publisher'],
                *m['metadata']['keywords'],*a['tags'],*entry['collections']['collections'],
                *m['metadata']['capabilities'],*m['compatibility']['material_capabilities'],
                *m['compatibility']['animation_channels']]
        return ' '.join(values)

    def _current(self,db):
        row=db.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()
        if not row: fail('INDEX','Index is empty; rebuild it from verified sources.')
        state=decode_json(row[0])
        if (not isinstance(state,dict) or set(state)!={'schema_version','locations','explicit_selection','snapshot'} or
            state['schema_version']!=1 or type(state['explicit_selection']) is not bool or
            not isinstance(state['locations'],list) or len(state['locations'])>MAX_ASSETS or
            not isinstance(state['snapshot'],dict) or
            set(state['snapshot'])!={'entries','diagnostics','usage'} or
            not isinstance(state['snapshot']['entries'],list)):
            fail('INDEX','Malformed derived snapshot; rebuild it.')
        expected=hashlib.sha256(row[0]).hexdigest()
        revision=db.execute("SELECT value FROM state WHERE key='revision'").fetchone()
        if revision!=(expected.encode(),): fail('INDEX','Derived snapshot digest differs.')
        if (not state['explicit_selection'] and self._catalog()!=state['locations'] or
            self._capture(state['locations'])!=state['snapshot']):
            fail('STALE','Library bytes, metadata, collections or usage changed; rebuild the index.')
        expected_entries=state['snapshot']['entries']
        stored=db.execute('SELECT id,document,text FROM entries ORDER BY id').fetchall()
        if len(stored)!=len(expected_entries):fail('INDEX','Derived row count differs; rebuild it.')
        for (number,blob,text),entry in zip(stored,expected_entries):
            if decode_json(blob)!=entry or text!=self._text(entry):
                fail('INDEX','Derived row differs from source snapshot; rebuild it.')
        words=db.execute('SELECT rowid,text FROM words ORDER BY rowid').fetchall()
        if words!=[(r[0],r[2]) for r in stored]:fail('INDEX','Derived lexical rows differ; rebuild it.')
        return expected,state

    def rebuild_embeddings(self,registry,name,*,enabled=False,authorize_private_metadata=False):
        provider=registry.get(name,enabled=enabled,authorize_private_metadata=authorize_private_metadata)
        with self._connect() as db:
            db.execute('BEGIN')
            revision,_=self._current(db)
            rows=db.execute('SELECT id,text FROM entries ORDER BY id').fetchall()
        version=(provider.model_id,provider.model_version)
        vectors=_vectors(provider.encode([r[1] for r in rows]),len(rows)) if rows else []
        if (provider.model_id,provider.model_version)!=version:fail('PROVIDER','Provider identity changed during encoding.')
        if sum(len(canonical_bytes(v)) for v in vectors)>MAX_INDEX_BYTES//4:
            fail('LIMIT','Embedding cache exceeds 16 MiB.')
        identity=dict(provider=name,model_id=provider.model_id,model_version=provider.model_version,
                      dimensions=len(vectors[0]) if vectors else 0,revision=revision,
                      vector_sha256=object_hash([[r[0],v] for r,v in zip(rows,vectors)]))
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current,_=self._current(db)
            if current!=revision: fail('STALE','Index changed during embedding computation.')
            db.execute('DELETE FROM vectors')
            db.executemany('INSERT INTO vectors VALUES(?,?)',[(r[0],canonical_bytes(v)) for r,v in zip(rows,vectors)])
            db.execute("INSERT OR REPLACE INTO state VALUES('embedding',?)",(canonical_bytes(identity),))
        return identity

    @staticmethod
    def _matches(entry,filters):
        m=entry['manifest'];a=entry['annotations'];i=m['identity'];c=m['compatibility'];md=m['metadata']
        technical=[f.get('technical_metadata',{}) for f in m['files']]
        actual=dict(kind=[i['kind']],subtype=[i['subtype']],category=[md['category']],tags=a['tags'],
                    collections=entry['collections']['collections'],author=[md['author']],publisher=[md['publisher']],
                    license=[md['license']],scope=[m['publication']['visibility']],library=[entry['location']['library']],
                    version=[m['version']],format=[f['format'] for f in m['files']],
                    material=c['material_capabilities'],rig_type=[v['rig_type'] for v in technical if isinstance(v.get('rig_type'),str)],
                    animation=c['animation_channels'],renderer=c['renderers'],method=[m['provenance']['method']],
                    capabilities=list(set(md['capabilities']+c['consumer_capabilities']+c['animation_channels'])),
                    review_status=[md['review_status']],used_by=entry['used_by'])
        for name,expected in filters.items():
            if name in LIST_FILTERS:
                if name in ALL_FILTERS:
                    if not set(expected)<=set(actual[name]):return False
                elif expected and not set(expected)&set(actual[name]):return False
            elif name=='favorite':
                if a['favorite']!=expected:return False
            elif name.startswith('max_'):
                field={'max_triangles':'triangles','max_texture_pixels':'texture_pixels'}[name]
                formats = ({'glb','gltf','obj'} if field=='triangles' else {'png','jpeg','webp'})
                roles = ({'geometry','mesh','model'} if field=='triangles' else {'texture'})
                relevant=[f.get('technical_metadata',{}) for f in m['files']
                          if f['format'] in formats or f['role'] in roles or field in f.get('technical_metadata',{})]
                if any(field not in v for v in relevant):return False
                values=[v[field] for v in relevant]
                if (not values or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in values)
                    or sum(values)>expected):return False
            else:
                from datetime import datetime
                stamp=datetime.fromisoformat(i['created_at'].replace('Z','+00:00'))
                limit=datetime.fromisoformat(expected.replace('Z','+00:00'))
                if name=='created_after' and stamp<limit or name=='created_before' and stamp>limit:return False
        return True

    def search(self,query='',*,filters=None,sort='relevance',limit=25,cursor=None,
               compatibility=None,native_path=None,registry=None,provider=None,enabled=False,authorize_private_metadata=False):
        parsed=parse_query(query)
        if filters is not None and not isinstance(filters,dict):fail('REQUEST','Filters must be an object.')
        filters=_filters(dict(filters) if filters is not None else {})
        for name,value in parsed['constraints'].items():
            if name not in filters: filters[name]=value
        if sort not in ('relevance','name','created_at','version'):fail('REQUEST','Unknown sort.')
        _integer(limit,1,MAX_RESULTS)
        if 'collections' in filters and not self.versions:fail('CAPABILITY','Collections require native lifecycle authority.')
        if 'used_by' in filters and not self.project_ids:fail('CAPABILITY','Usage requires explicit native project bindings.')
        if parsed['requires_compatibility'] and compatibility is None:
            fail('REQUEST','Supply explicit native compatibility requirements for this query.')
        if compatibility is not None and self.compatibility is None and self.dependency_graph is None:fail('CAPABILITY','Native compatibility authority is unavailable.')
        request=dict(query=query,filters=filters,sort=sort,compatibility=compatibility,native_path=native_path,provider=provider)
        canonical_bytes(request)
        request_hash=object_hash(request);offset=0
        with self._connect() as db:
            db.execute('BEGIN')
            revision,state=self._current(db)
            lexical={}
            if parsed['tokens']:
                expression=' OR '.join('"'+t.replace('"','""')+'"' for t in parsed['tokens'])
                lexical=dict(db.execute('SELECT rowid,bm25(words) FROM words WHERE words MATCH ?',(expression,)))
            vector_scores={};embedding_identity=None;query_vector_sha256=None
            if provider is not None:
                if registry is None:fail('PROVIDER','Configure an explicit provider registry.')
                adapter=registry.get(provider,enabled=enabled,authorize_private_metadata=authorize_private_metadata)
                row=db.execute("SELECT value FROM state WHERE key='embedding'").fetchone()
                if not row:fail('EMBEDDING','No embeddings; rebuild with the selected provider.')
                embedding_identity=decode_json(row[0])
                if (embedding_identity['revision']!=revision or embedding_identity['provider']!=provider or
                    embedding_identity['model_id']!=adapter.model_id or embedding_identity['model_version']!=adapter.model_version):
                    fail('EMBEDDING','Embedding model/version differs; rebuild vectors.')
                version=(adapter.model_id,adapter.model_version)
                q=_vectors(adapter.encode([query]),1)[0]
                if (adapter.model_id,adapter.model_version)!=version:fail('PROVIDER','Provider identity changed during query.')
                query_vector_sha256=object_hash(q)
                if len(q)!=embedding_identity['dimensions']:fail('EMBEDDING','Query vector dimensions differ.')
                vector_rows=[(n,decode_json(blob)) for n,blob in db.execute('SELECT id,vector FROM vectors ORDER BY id')]
                entry_ids=[n for (n,) in db.execute('SELECT id FROM entries ORDER BY id')]
                if ([n for n,_ in vector_rows]!=entry_ids or
                    object_hash([[n,v] for n,v in vector_rows])!=embedding_identity['vector_sha256']):
                    fail('EMBEDDING','Embedding rows changed or are incomplete; rebuild them.')
                for number,raw in vector_rows:
                    v=_vectors([raw],1)[0]
                    if len(v)!=len(q):fail('EMBEDDING','Stored vector dimensions differ.')
                    vector_scores[number]=sum(a*b for a,b in zip(q,v))
            retrieval_revision=object_hash(dict(index=revision,embedding=embedding_identity,query_vector=query_vector_sha256))
            if cursor is not None:
                try:
                    if not isinstance(cursor,str) or len(cursor)>2048:raise ValueError()
                    token=decode_json(base64.b64decode(cursor,validate=True))
                    if set(token)!={'revision','request','offset'}:raise ValueError()
                    if token['revision']!=retrieval_revision or token['request']!=request_hash:fail('CURSOR','Cursor belongs to a different snapshot/query.')
                    offset=_integer(token['offset'],0,MAX_ASSETS)
                except (ValueError,TypeError) as exc:
                    if isinstance(exc,RigError):raise
                    fail('CURSOR','Malformed pagination cursor.')
            results=[]
            for number,blob,_ in db.execute('SELECT id,document,text FROM entries'):
                entry=decode_json(blob)
                if not self._matches(entry,filters):continue
                if parsed['tokens'] and number not in lexical and provider is None:continue
                comp=None
                if compatibility is not None:
                    if self.dependency_graph is not None:
                        from asset_library_dependencies import check_compatibility
                        comp=check_compatibility(self.dependency_graph,entry['location'],compatibility,native_path=native_path)
                    else:
                        comp=self.compatibility(entry['location'],compatibility)
                    if not comp['compatible']:continue
                lexical_score=-lexical.get(number,0)
                score=lexical_score+vector_scores.get(number,0)+1+(1 if comp is not None else 0)
                reasons=list(parsed['reasons'])+[f'filter {n} matched' for n in sorted(filters)]
                if number in lexical:reasons.append('lexical tokens matched indexed metadata')
                if comp is not None:reasons.append('native compatibility requirements matched')
                results.append(dict(location=entry['location'],name=entry['manifest']['identity']['name'],
                    version=entry['manifest']['version'],created_at=entry['manifest']['identity']['created_at'],
                    score=score,lexical_score=lexical_score,semantic_score=vector_scores.get(number),
                    completeness_score=1,compatibility_score=1 if comp is not None else None,
                    complete=True,readiness='payload-verified; geometry/review/license approval independent',reasons=reasons))
            if sort=='relevance':results.sort(key=lambda r:(-r['score'],r['name'].casefold(),_key(r['location'])))
            elif sort=='version':results.sort(key=lambda r:(tuple(-int(p) for p in r['version'].split('.')),_key(r['location'])))
            elif sort=='created_at':
                from datetime import datetime
                results.sort(key=lambda r:(datetime.fromisoformat(r['created_at'].replace('Z','+00:00')),_key(r['location'])))
            else:results.sort(key=lambda r:(r[sort].casefold(),_key(r['location'])))
            page=results[offset:offset+limit];next_cursor=None
            if offset+limit<len(results):
                next_cursor=base64.b64encode(canonical_bytes(dict(revision=retrieval_revision,request=request_hash,offset=offset+limit))).decode()
            # Detect source mutation during query evaluation before returning any hit.
            current,_=self._current(db)
            if current!=revision:fail('STALE','Snapshot changed during query.')
            return dict(revision=revision,retrieval_revision=retrieval_revision,total=len(results),results=page,next_cursor=next_cursor,
                        excluded=state['snapshot']['diagnostics'],query=parsed,embedding_used=provider is not None,
                        embedding=embedding_identity)
