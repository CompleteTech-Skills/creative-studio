"""Optional native managed asset import acceptance."""
import unittest
from asset_library_operations import AssetOperations
from rig_errors import RigError
import test_asset_operations as operations
class NativeOperationsTests(unittest.TestCase):
    setUp=operations.AssetOperationsTests.setUp
    # Avoid inheriting the ordinary cases twice while sharing the real fixture.
    def test_real_obj_import_private_stage_preserves_original_and_exact_publication(self):
        p=dict(source=str(self.fixture.mesh),identity=self.fixture.manifest['identity'],version='1.0.0',
               metadata=self.fixture.manifest['metadata'],license_file=str(self.fixture.license),fixture=True,blender_roundtrip=False)
        before=self.fixture.mesh.read_bytes()
        job=self.owner.submit('import',p)
        self.assertEqual(self.library.list_references(),[])
        result=self.owner.run_job(job['id'])
        self.assertEqual(result['status'],'published')
        reference=result['receipt']['reference'];manifest=self.library.resolve(reference)
        originals=[f for f in manifest['files'] if f['role']=='source' and f['format']=='obj']
        self.assertTrue(originals)
        self.assertEqual(self.library.read_file(reference,originals[0]['path']),before)
        self.assertEqual(self.fixture.mesh.read_bytes(),before)
        self.assertEqual(self.owner.run_job(job['id'])['receipt'],result['receipt'])
        self.assertEqual(len(self.library.list_references()),1)

    def test_source_change_after_queue_fails_before_publication(self):
        p=dict(source=str(self.fixture.mesh),identity=self.fixture.manifest['identity'],version='1.0.0',
               metadata=self.fixture.manifest['metadata'],license_file=str(self.fixture.license),fixture=True,blender_roundtrip=False)
        job=self.owner.submit('import',p)
        self.fixture.mesh.write_bytes(self.fixture.mesh.read_bytes()+b'# changed\n')
        with self.assertRaises(RigError):self.owner.run_job(job['id'])
        self.assertEqual(self.library.list_references(),[])


