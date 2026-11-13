"""Dependency operations against actual transactional library bytes."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from asset_library import AssetLibrary
from asset_library_contracts import seal
from asset_library_dependencies import (DependencyGraph, apply_replacement,
    check_compatibility, dot, locator, replacement_plan)
from rig_errors import RigError

ROOT=Path(__file__).resolve().parents[1]


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.library=AssetLibrary(self.root/'library',create=True)
        self.graph=DependencyGraph({'local':self.library})
        self.template=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())
        self.sources={f['path']:ROOT/'examples/asset-library-foundation'/f['path'] for f in self.template['files']}

    def publish(self,name,dependencies=(),version='1.0.0',**changes):
        doc=deepcopy(self.template);doc['identity']['id']=name;doc['identity']['slug']=name
        doc['identity']['name']=name;doc['version']=version
        doc['dependencies']=[dict(target=d[0],purpose=d[1],required=d[2],origin='authored',relationship='direct',capabilities=[]) for d in dependencies]
        doc.update(changes)
        return self.library.register(seal(doc),self.sources)

    def at(self,ref):return locator('local',ref)

    def test_real_transitive_reverse_lock_and_dot(self):
        texture=self.publish('texture');material=self.publish('material',[(texture,'texture',True)])
        character=self.publish('character',[(material,'material',True)])
        unrelated=self.publish('unrelated')
        graph=self.graph.closure([self.at(character)])
        self.assertTrue(graph['complete']);self.assertEqual(len(graph['nodes']),3)
        self.assertEqual(len(self.graph.direct(self.at(character))),1)
        reverse=self.graph.reverse(self.at(texture))
        self.assertEqual(reverse['direct'],[self.at(material)])
        self.assertEqual(reverse['transitive'],[self.at(character),self.at(material)])
        self.assertNotIn(self.at(unrelated),reverse['transitive'])
        lock=self.graph.lock([self.at(character)]);self.assertEqual(lock,self.graph.verify_lock(lock))
        with closing(sqlite3.connect(self.library.database)) as db,db:
            db.execute('DELETE FROM blobs WHERE sha256=?',(self.template['files'][0]['sha256'],))
        with self.assertRaises(RigError):self.graph.verify_lock(lock)
        self.assertIn('digraph assets',dot(graph));self.assertIn('local:character@1.0.0',dot(graph))

    def test_optional_missing_is_explicit_and_required_missing_fails(self):
        optional=self.publish('optional');required=self.publish('required')
        root=self.publish('root',[(optional,'preview',False),(required,'mesh',True)])
        with closing(sqlite3.connect(self.library.database)) as db,db:
            db.execute('DELETE FROM versions WHERE id=?',('optional',))
        report=self.graph.closure([self.at(root)])
        self.assertTrue(report['complete']);self.assertEqual(report['diagnostics'][0]['required'],False)
        excluded=self.graph.closure([self.at(root)],include_optional=False)
        self.assertEqual(excluded['diagnostics'],[])
        with closing(sqlite3.connect(self.library.database)) as db,db:
            db.execute('DELETE FROM versions WHERE id=?',('required',))
        with self.assertRaisesRegex(RigError,'Required dependency'):self.graph.closure([self.at(root)])
        diagnostic=self.graph.closure([self.at(root)],strict=False)
        self.assertFalse(diagnostic['complete'])

    def test_two_versions_in_runtime_closure_reject_and_limits_reject(self):
        first=self.publish('mesh');second=self.publish('mesh',version='2.0.0')
        root=self.publish('root',[(first,'left',True),(second,'right',True)])
        with self.assertRaisesRegex(RigError,'VERSION_CONFLICT'):self.graph.closure([self.at(root)])
        with self.assertRaises(RigError):DependencyGraph({'local':self.library},max_nodes=True)
        with self.assertRaises(RigError):self.graph.closure([self.at(first),self.at(first)])
        small=DependencyGraph({'local':self.library},max_nodes=1)
        with self.assertRaisesRegex(RigError,'node limit'):small.closure([self.at(root)])

    def test_lock_rejects_mutation_and_missing_authorized_library(self):
        item=self.publish('mesh');lock=self.graph.lock([self.at(item)])
        bad=deepcopy(lock);bad['graph']['nodes'][0]['files'][0]['bytes']+=1
        with self.assertRaisesRegex(RigError,'altered'):self.graph.verify_lock(bad)
        with self.assertRaisesRegex(RigError,'authorized'):self.graph.closure([locator('unknown',item)])

    def test_exact_blob_recovery_and_wrong_donor_preserve_records(self):
        item=self.publish('mesh');donor=AssetLibrary(self.root/'donor',create=True)
        donor_ref=donor.register(self.library.resolve(item),self.sources)
        self.graph=DependencyGraph({'local':self.library,'donor':donor})
        path=self.template['files'][0]['path'];before=self.library.resolve(item)
        with closing(sqlite3.connect(self.library.database)) as db,db:
            db.execute('DELETE FROM blobs WHERE sha256=?',(self.template['files'][0]['sha256'],))
        with self.assertRaises(RigError):self.graph.recover(self.at(item),path,donors=[])
        receipt=self.graph.recover(self.at(item),path,donors=[locator('donor',donor_ref)])
        self.assertEqual(receipt['sha256'],self.template['files'][0]['sha256'])
        self.assertEqual(self.library.resolve(item),before)
        self.assertEqual(self.library.read_file(item,path),self.sources[path].read_bytes())

    def test_targeted_replacement_retains_old_versions_and_unrelated_consumers(self):
        old=self.publish('mesh');new=self.publish('mesh',version='2.0.0')
        consumer=self.publish('character',[(old,'body',True)])
        untouched=self.publish('other-character',[(old,'body',True)])
        before=self.library.resolve(untouched)
        plan=replacement_plan(self.graph,self.at(old),self.at(new),
            consumers=[dict(asset=self.at(consumer),purposes=['body'])],
            requirements=dict(coordinate_system='right-handed-y-up-z-forward',formats=['obj']))
        self.assertEqual(plan['invalidated'],[self.at(consumer)])
        result=apply_replacement(self.graph,plan,versions={'character':'2.0.0'})
        current=result['published'][0]['current']
        self.assertEqual(self.library.resolve(current['reference'])['dependencies'][0]['target'],new)
        self.assertEqual(self.library.resolve(consumer)['dependencies'][0]['target'],old)
        self.assertEqual(self.library.resolve(untouched),before)
        self.assertEqual(result['published'][0]['rollback'],self.at(consumer))
        # Old provenance remains in locks without becoming a runtime version conflict.
        self.assertTrue(self.graph.closure([current])['complete'])
        with self.assertRaises(RigError):apply_replacement(self.graph,plan,versions={'character':'2.0.0'})

    def test_incompatible_coordinates_and_ambiguous_purposes_fail(self):
        old=self.publish('mesh');new=self.publish('mesh',version='2.0.0')
        character=self.publish('character',[(old,'body',True)])
        self.assertFalse(check_compatibility(self.graph,self.at(new),{'coordinate_system':'legacy-2d'})['compatible'])
        with self.assertRaises(RigError):replacement_plan(self.graph,self.at(old),self.at(new),
            consumers=[dict(asset=self.at(character),purposes=['absent'])])
        with self.assertRaises(RigError):check_compatibility(self.graph,self.at(new),{'unexpected':True})
        with self.assertRaises(RigError):check_compatibility(self.graph,self.at(new),{'skeleton_sha256':'a'*64})



class ReplacementRegressionTests(unittest.TestCase):
    setUp=DependencyTests.setUp
    publish=DependencyTests.publish
    at=DependencyTests.at
    def test_new_transitive_version_conflict_never_publishes(self):
        old=self.publish('mesh');new=self.publish('mesh',version='2.0.0')
        material=self.publish('material',[(old,'base-mesh',True)])
        consumer=self.publish('character',[(old,'body',True),(material,'surface',True)])
        plan=replacement_plan(self.graph,self.at(old),self.at(new),consumers=[dict(asset=self.at(consumer),purposes=['body'])])
        before=self.library.list_references()
        with self.assertRaisesRegex(RigError,'VERSION_CONFLICT'):
            apply_replacement(self.graph,plan,versions={'character':'2.0.0'})
        self.assertEqual(self.library.list_references(),before)

    def test_selected_library_cannot_be_discarded_by_replacement(self):
        old=self.publish('mesh');new=self.publish('mesh',version='2.0.0')
        consumer=self.publish('character',[(old,'body',True)])
        donor=AssetLibrary(self.root/'donor',create=True)
        donor.register(self.library.resolve(old),self.sources);donor.register(self.library.resolve(new),self.sources)
        self.graph=DependencyGraph({'local':self.library,'donor':donor})
        for oldloc,newloc in [(locator('donor',old),locator('donor',new)),(self.at(old),locator('donor',new))]:
            with self.subTest(old=oldloc,new=newloc),self.assertRaisesRegex(RigError,'library'):
                replacement_plan(self.graph,oldloc,newloc,consumers=[dict(asset=self.at(consumer),purposes=['body'])])

class DependencyAdmissionTests(unittest.TestCase):
    setUp=DependencyTests.setUp
    publish=DependencyTests.publish
    at=DependencyTests.at

    def test_self_cycle_and_untrusted_nested_requirements_fail_structurally(self):
        with self.assertRaisesRegex(RigError,'itself'):
            self.publish('self',[(dict(id='self',version='1.0.0',sha256='f'*64),'loop',True)])
        item=self.publish('item')
        for req in ({'formats':[{}]},{'unit_scale':float('nan')},{'unit_scale':True}):
            with self.subTest(requirements=req),self.assertRaises(RigError):
                check_compatibility(self.graph,self.at(item),req)

    def test_schema_generation_matches_retained_contracts(self):
        from asset_dependency_schemas import schemas
        for name,schema in schemas().items():
            self.assertEqual(json.loads((ROOT/'profiles'/name).read_text()),schema)


if __name__=='__main__':unittest.main()
