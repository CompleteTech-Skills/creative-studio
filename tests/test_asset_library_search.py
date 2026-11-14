"""Actual library registration and offline search; provider doubles test gates only."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from asset_library import AssetLibrary
from asset_library_contracts import seal
from asset_library_search import SearchIndex, ProviderRegistry, parse_query
from rig_errors import RigError

ROOT=Path(__file__).resolve().parents[1]


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.library=AssetLibrary(self.root/'library',create=True)
        self.example=ROOT/'examples/asset-library-foundation'
        self.template=json.loads((self.example/'manifest.json').read_bytes())
        self.refs=[]
        self.index=SearchIndex(self.root/'index',{'local':self.library},create=True)

    def register(self,name,*,kind='prop',keywords=(),capabilities=(),triangles=12,version='1.0.0'):
        m=deepcopy(self.template)
        m['identity'].update(id=name,slug=name,name=name.replace('-',' ').title(),kind=kind)
        m['version']=version;m['metadata'].update(keywords=list(keywords),capabilities=list(capabilities))
        m['compatibility']['animation_channels']=list(capabilities)
        m['files'][0]['technical_metadata']={'triangles':triangles}
        m['provenance']['sources']=[m['files'][0]]
        ref=self.library.register(seal(m),{f['path']:self.example/f['path'] for f in m['files']})
        loc={'library':'local','reference':ref};self.refs.append(loc)
        return loc

    def assert_code(self,code,operation):
        with self.assertRaises(RigError) as raised:operation()
        self.assertEqual(raised.exception.report['code'],'LIBRARY_SEARCH_'+code)

    def test_actual_registered_assets_query_and_explainable_constraints(self):
        walker=self.register('cartoon-robot',kind='character',keywords=['small','cartoon','robot'],capabilities=['walk'])
        self.register('stone-tower',keywords=['medieval','stone','village'])
        self.register('high-tree',keywords=['tree','outdoor'],triangles=20000)
        low=self.register('low-tree',keywords=['tree','outdoor'],triangles=100)
        report=self.index.rebuild(self.refs)
        self.assertEqual(report['indexed'],4)
        response=self.index.search('Find a small cartoon robot that can walk.')
        self.assertEqual([r['location'] for r in response['results']],[walker])
        self.assertFalse(response['embedding_used'])
        self.assertTrue(any('walk' in reason for reason in response['results'][0]['reasons']))
        self.assertEqual([r['location'] for r in self.index.search('Find low-polygon trees for an outdoor scene.')['results']],[low])
        self.assertEqual(self.index.search('medieval stone village')['total'],1)

    def test_singular_and_plural_prop_light_queries_bind_kind_on_real_assets(self):
        prop=self.register('fixture-box',kind='prop')
        # Register a genuine glTF point-light payload, with no query words in
        # its name/keywords, so lexical matching cannot conceal a missing kind.
        from asset_library import file_record
        document={'asset':{'version':'2.0'},'extensionsUsed':['KHR_lights_punctual'],
                  'extensions':{'KHR_lights_punctual':{'lights':[{'type':'point','intensity':10}]}},
                  'nodes':[{'extensions':{'KHR_lights_punctual':{'light':0}}}],
                  'scenes':[{'nodes':[0]}],'scene':0}
        payload=self.root/'lamp.gltf';payload.write_text(json.dumps(document))
        manifest=deepcopy(self.template)
        manifest['identity'].update(id='fixture-lamp',slug='fixture-lamp',name='Fixture Lamp',kind='light')
        record=file_record(payload,'payload/lamp.gltf',role='light')
        manifest['files']=[record,manifest['files'][1]]
        manifest['provenance']['sources']=[record]
        manifest['publication']['inventory']=[f['path'] for f in manifest['files']]
        manifest['compatibility']['formats']=['gltf']
        reference=self.library.register(seal(manifest),{
            record['path']:payload,manifest['files'][1]['path']:self.example/manifest['files'][1]['path']})
        light={'library':'local','reference':reference}
        self.index.rebuild([prop,light])
        for kind,words,expected in [('prop',('prop','props'),prop),('light',('light','lights'),light)]:
            for word in words:
                with self.subTest(query=word):
                    response=self.index.search('Find a '+word)
                    self.assertEqual(response['query']['constraints'],{'kind':[kind]})
                    self.assertEqual(response['query']['tokens'],[])
                    self.assertEqual([row['location'] for row in response['results']],[expected])

    def test_stable_sort_and_snapshot_cursor_preserve_all_tied_results(self):
        for name in ['delta','alpha','beta','gamma']:self.register(name,keywords=['matching'])
        self.index.rebuild(self.refs)
        first=self.index.search('matching',limit=2,sort='name')
        again=self.index.search('matching',limit=2,sort='name')
        self.assertEqual(first,again)
        second=self.index.search('matching',limit=2,sort='name',cursor=first['next_cursor'])
        self.assertEqual([r['name'] for r in first['results']+second['results']],['Alpha','Beta','Delta','Gamma'])
        self.assertIsNone(second['next_cursor'])
        self.assert_code('CURSOR',lambda:self.index.search('other',cursor=first['next_cursor']))
        self.assert_code('CURSOR',lambda:self.index.search(cursor='###'))

    def test_native_catalog_refresh_and_explicit_subset_have_distinct_scope(self):
        first=self.register('first',keywords=['common'])
        initial=self.index.rebuild()
        self.assertEqual(initial['indexed'],1)
        self.register('second',keywords=['common'])
        self.assert_code('STALE',lambda:self.index.search('common'))
        self.assertEqual(self.index.rebuild()['indexed'],2)
        self.assertEqual(self.index.search('common')['total'],2)
        self.index.rebuild([first])
        self.register('third',keywords=['common'])
        self.assertEqual(self.index.search('common')['total'],1)

    def test_index_relocation_reopens_against_relocated_library(self):
        self.register('portable',keywords=['portable'])
        self.index.rebuild()
        self.library.root.rename(self.root/'moved-library')
        self.index.root.rename(self.root/'moved-index')
        reopened=SearchIndex(self.root/'moved-index',{'local':AssetLibrary(self.root/'moved-library')})
        self.assertEqual(reopened.search('portable')['total'],1)

    def test_versions_are_exact_and_semver_sort_is_numeric(self):
        self.register('robot',version='2.0.0');self.register('robot',version='10.0.0')
        self.index.rebuild(self.refs)
        results=self.index.search(filters={'version':['2.0.0','10.0.0']},sort='version')['results']
        self.assertEqual([r['version'] for r in results],['10.0.0','2.0.0'])

    def test_annotation_changes_and_payload_corruption_invalidate_snapshot(self):
        loc=self.register('robot');self.index.rebuild(self.refs)
        ref=loc['reference']
        self.library.update_annotations(ref,expected_revision=0,tags=['favorite-robot'],favorite=True,note='')
        self.assert_code('STALE',lambda:self.index.search())
        self.index.rebuild(self.refs)
        self.assertEqual(self.index.search(filters={'tags':['favorite-robot'],'favorite':True})['total'],1)
        with closing(sqlite3.connect(self.library.database)) as db, db:db.execute('UPDATE blobs SET payload=?',(b'corrupt',))
        self.assert_code('STALE',lambda:self.index.search())
        rebuilt=self.index.rebuild(self.refs)
        self.assertEqual(rebuilt['indexed'],0);self.assertEqual(len(rebuilt['diagnostics']),1)
        self.assertEqual(self.index.search()['total'],0)

    def test_structured_filters_and_native_compatibility_callback(self):
        char=self.register('hero',kind='character');self.register('scenery')
        self.index.rebuild(self.refs)
        self.assertEqual(self.index.search(filters={'kind':['character'],'license':['CC0-1.0'],'format':['obj']})['results'][0]['location'],char)
        self.assertEqual(self.index.search(filters={'max_triangles':1})['total'],0)
        self.assert_code('CAPABILITY',lambda:self.index.search(compatibility={'renderers':['cycles']}))
        calls=[]
        def compatible(location,requirements):
            calls.append((location,requirements));return {'compatible':location==char}
        self.index.compatibility=compatible
        self.assertEqual(self.index.search(compatibility={'renderers':['cycles']})['total'],1)
        self.assertEqual(len(calls),2)

    def test_index_is_derived_and_corruption_or_missing_index_never_becomes_authority(self):
        self.register('hero');self.index.rebuild(self.refs)
        with closing(sqlite3.connect(self.index.database)) as db, db:
            db.execute('UPDATE entries SET text=?',('forged metadata',))
        self.assert_code('INDEX',lambda:self.index.search())
        self.index.rebuild(self.refs)
        self.assertEqual(self.index.search('hero')['total'],1)
        self.index.database.unlink()
        with self.assertRaises((RigError,FileNotFoundError)):self.index.search()
        self.assertEqual(self.library.resolve(self.refs[0]['reference'])['identity']['id'],'hero')

    def test_hostile_inputs_never_enter_sql_or_fts_syntax(self):
        self.register('hero');self.index.rebuild(self.refs)
        self.assertEqual(self.index.search('" OR 1=1; DROP TABLE words --')['total'],0)
        for filters in ({'unknown':[]},{'max_triangles':True},{'kind':'character'},{'favorite':1},{'tags':['x','x']}):
            with self.subTest(filters=filters),self.assertRaises(RigError):self.index.search(filters=filters)
        for query in ('x'*2049,{},'\ud800'):
            with self.assertRaises(RigError):self.index.search(query)
        self.assert_code('CAPABILITY',lambda:self.index.search(filters={'collections':['test']}))
        self.assert_code('CAPABILITY',lambda:self.index.search(filters={'used_by':['project']}))
        self.assert_code('LIMIT',lambda:self.index.search(limit=True))

    def test_creation_time_sort_compares_instants_and_budgets_sum_files(self):
        refs=[]
        for name,date in [('older','2026-10-23T01:00:00+02:00'),('newer','2026-10-23T00:00:00Z')]:
            manifest=deepcopy(self.template)
            manifest['identity'].update(id=name,slug=name,name=name,created_at=date)
            manifest['files'][0]['technical_metadata']={'triangles':6}
            manifest['files'][1]['technical_metadata']={'triangles':6}
            manifest['provenance']['sources']=[manifest['files'][0]]
            manifest['provenance']['license_evidence']=[manifest['files'][1]]
            ref=self.library.register(seal(manifest),{f['path']:self.example/f['path'] for f in manifest['files']})
            refs.append({'library':'local','reference':ref})
        self.index.rebuild(refs)
        self.assertEqual([r['name'] for r in self.index.search(sort='created_at')['results']],['older','newer'])
        self.assertEqual(self.index.search(filters={'max_triangles':10})['total'],0)
        self.assertEqual(self.index.search(filters={'max_triangles':12})['total'],2)

    def test_resource_budgets_reject_partially_measured_payloads(self):
        manifest=deepcopy(self.template)
        manifest['files'][0]['technical_metadata']={'triangles':1}
        other=deepcopy(manifest['files'][0]);other['path']='mesh/second.obj'
        other.pop('technical_metadata')
        manifest['files'].append(other)
        manifest['publication']['inventory'].append(other['path'])
        manifest['provenance']['sources']=[manifest['files'][0],other]
        payloads={f['path']:self.example/('mesh/triangle.obj' if f['role']=='geometry' else f['path'])
                  for f in manifest['files']}
        ref=self.library.register(seal(manifest),payloads)
        self.index.rebuild([{'library':'local','reference':ref}])
        self.assertEqual(self.index.search(filters={'max_triangles':10})['total'],0)
        # Nongeometry license evidence does not need invented triangle counts.
        one=self.register('single',triangles=1)
        self.index.rebuild([one])
        self.assertEqual(self.index.search(filters={'max_triangles':1})['total'],1)
        from PIL import Image
        texture=self.root/'texture.png'
        Image.new('RGBA',(1,1),(255,0,0,255)).save(texture)
        import hashlib
        payload=texture.read_bytes()
        record=dict(path='textures/a.png',role='texture',sha256=hashlib.sha256(payload).hexdigest(),
                    bytes=len(payload),format='png',mime_type='image/png',required=True,
                    technical_metadata={'texture_pixels':1})
        manifest=deepcopy(self.template)
        manifest['identity'].update(id='textured',slug='textured',name='Textured')
        manifest['files'] += [record,dict(record,path='textures/b.png')]
        manifest['files'][-1].pop('technical_metadata')
        manifest['publication']['inventory'] += ['textures/a.png','textures/b.png']
        sources={f['path']:self.example/f['path'] for f in self.template['files']}
        sources.update({'textures/a.png':texture,'textures/b.png':texture})
        ref=self.library.register(seal(manifest),sources)
        self.index.rebuild([{'library':'local','reference':ref}])
        self.assertEqual(self.index.search(filters={'max_texture_pixels':10})['total'],0)

    def test_provider_absence_unknown_model_and_remote_private_metadata_gate(self):
        self.register('hero');self.index.rebuild(self.refs)
        registry=ProviderRegistry()
        self.assert_code('PROVIDER',lambda:self.index.rebuild_embeddings(registry,'missing',enabled=True))
        class ProtocolDouble:
            model_id='protocol-test';model_version='fixture-1';runtime_available=True
            requires_credentials=True;credentials_available=False;transmits_metadata=True
            calls=0
            def encode(self,texts):self.calls+=1;return [[1.0,0.5] for _ in texts]
        provider=ProtocolDouble();registry.register('test',provider)
        self.assert_code('PROVIDER',lambda:self.index.rebuild_embeddings(registry,'test'))
        self.assert_code('PROVIDER',lambda:self.index.rebuild_embeddings(registry,'test',enabled=True))
        provider.credentials_available=True
        self.assert_code('PRIVATE',lambda:self.index.rebuild_embeddings(registry,'test',enabled=True))
        self.assertEqual(provider.calls,0)
        identity=self.index.rebuild_embeddings(registry,'test',enabled=True,authorize_private_metadata=True)
        self.assertEqual(identity['model_version'],'fixture-1')
        response=self.index.search('hero',registry=registry,provider='test',enabled=True,authorize_private_metadata=True)
        self.assertTrue(response['embedding_used'])
        provider.model_version='fixture-2'
        self.assert_code('EMBEDDING',lambda:self.index.search('hero',registry=registry,provider='test',enabled=True,authorize_private_metadata=True))
        # This double proves dispatch/authorization only, never model quality.

    def test_semantic_cursors_bind_vector_rebuilds_and_query_vectors(self):
        for name in ('alpha','beta','gamma'):self.register(name)
        self.index.rebuild(self.refs)
        class ChangingProvider:
            model_id='protocol-test';model_version='fixed';runtime_available=True
            requires_credentials=False;credentials_available=True;transmits_metadata=False
            reverse=False;query_reverse=False
            def encode(self,texts):
                if len(texts)==1:return [[0.,1.] if self.query_reverse else [1.,0.]]
                result=[[1.,0.],[.7,.7],[0.,1.]]
                return list(reversed(result)) if self.reverse else result
        provider=ChangingProvider();registry=ProviderRegistry();registry.register('test',provider)
        self.index.rebuild_embeddings(registry,'test',enabled=True)
        first=self.index.search(registry=registry,provider='test',enabled=True,limit=1)
        provider.reverse=True
        self.index.rebuild_embeddings(registry,'test',enabled=True)
        self.assert_code('CURSOR',lambda:self.index.search(registry=registry,provider='test',enabled=True,limit=1,cursor=first['next_cursor']))
        fresh=self.index.search(registry=registry,provider='test',enabled=True,limit=1)
        provider.query_reverse=True
        self.assert_code('CURSOR',lambda:self.index.search(registry=registry,provider='test',enabled=True,limit=1,cursor=fresh['next_cursor']))

    def test_bad_embedding_vectors_are_rejected_without_publishing(self):
        self.register('hero');self.index.rebuild(self.refs)
        class InvalidProvider:
            model_id='invalid';model_version='1';runtime_available=True
            requires_credentials=False;credentials_available=True;transmits_metadata=False
            def encode(self,texts):return [[float('nan')]]
        registry=ProviderRegistry();registry.register('invalid',InvalidProvider())
        self.assert_code('EMBEDDING',lambda:self.index.rebuild_embeddings(registry,'invalid',enabled=True))
        self.assertEqual(self.index.search('hero')['total'],1)

    def test_index_existing_destination_and_symlink_are_rejected(self):
        with self.assertRaises(FileExistsError):SearchIndex(self.root/'index',{'local':self.library},create=True)
        link=self.root/'link';link.symlink_to(self.index.root,target_is_directory=True)
        self.assert_code('PATH',lambda:SearchIndex(link,{'local':self.library}))

    def test_parser_is_explicit_about_lexical_rules(self):
        parsed=parse_query('Show reusable indoor lighting setups.')
        self.assertEqual(parsed['constraints'],{'kind':['light']})
        self.assertIn('no embeddings',parsed['inference'])
        with self.assertRaises(RigError):self.index.search('Find characters compatible with this animation rig.')


class LocalEmbeddingAdmissionTests(unittest.TestCase):
    def test_unknown_model_and_unpinned_revision_reject_before_runtime(self):
        from asset_search_embeddings import LocalSentenceTransformer
        with self.assertRaises(RigError):LocalSentenceTransformer('.',expected_sha256='latest')
        with self.assertRaises(RigError):LocalSentenceTransformer('.',expected_sha256='0'*64,model_id='unknown')

    def test_custom_python_or_pickle_models_fail_snapshot_admission(self):
        from asset_search_embeddings import model_snapshot
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            (root/'modules.json').write_text('[]')
            (root/'config.json').write_text('{"model_type":"bert"}')
            (root/'model.bin').write_bytes(b'not a safe tensor')
            with self.assertRaises(RigError):model_snapshot(root)

    def test_model_scan_bounds_files_and_directory_entries(self):
        from asset_search_embeddings import model_snapshot
        from unittest.mock import patch
        import asset_search_embeddings as embedding
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            for i in range(140): (root / f'{i:03}.json').write_bytes(b'{}')
            real_read=embedding.read_source_bytes
            with patch.object(embedding, 'read_source_bytes', wraps=real_read) as read:
                with self.assertRaises(RigError) as error: model_snapshot(root)
                self.assertEqual(error.exception.report['code'], 'LIBRARY_SEARCH_LIMIT')
                self.assertEqual(read.call_count, 128)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            for i in range(513): (root / f'{i:03}').mkdir()
            real_scandir=embedding.os.scandir
            seen=[]
            class CountingScan:
                def __init__(self, path): self.iterator=real_scandir(path)
                def __enter__(self): return self
                def __exit__(self, *args): self.iterator.close()
                def __iter__(self):
                    for entry in self.iterator:
                        seen.append(entry.name)
                        yield entry
            with patch.object(embedding.os, 'scandir', CountingScan):
                with self.assertRaises(RigError) as error: model_snapshot(root)
            self.assertEqual(error.exception.report['code'], 'LIBRARY_SEARCH_LIMIT')
            self.assertEqual(len(seen), 513)

    def test_model_snapshot_valid_nested_closure_and_symlink(self):
        from asset_search_embeddings import model_snapshot
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            (root/'modules.json').write_text('[{"type":"sentence_transformers.models.Pooling","path":"nested"}]')
            (root/'config.json').write_text('{"model_type":"bert"}')
            (root/'nested').mkdir()
            (root/'nested'/'weights.safetensors').write_bytes(b'weights')
            digest, payloads=model_snapshot(root)
            self.assertEqual(len(digest), 64)
            self.assertEqual(payloads['nested/weights.safetensors'], b'weights')
            (root/'nested'/'link').symlink_to(root/'config.json')
            with self.assertRaises(RigError): model_snapshot(root)

    def test_genuine_local_provider_is_explicitly_unavailable_without_runtime(self):
        from asset_search_embeddings import LocalSentenceTransformer
        from unittest.mock import patch
        with patch('asset_search_embeddings.importlib.util.find_spec',return_value=None):
            provider=LocalSentenceTransformer('.',expected_sha256='0'*64)
        self.assertFalse(provider.runtime_available)
        with self.assertRaises(RigError):provider.encode(['private name'])


if __name__=='__main__':unittest.main()
