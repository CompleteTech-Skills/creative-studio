"""Actual filesystem/CLI checks for bounded exact-byte LookDev delivery."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev import Project, INDEX
from lookdev_contracts import canonical_bytes
from rig_errors import RigError
from test_lookdev_contracts import material, reference, texture, image_bytes


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent/'project'
        self.root.mkdir()
        self.write_project([material()])

    def write_project(self, resources, payloads=None):
        payloads = payloads or {}
        names = []
        for resource in resources:
            name = resource['id']+'-'+resource['version']+'.json'
            names.append(name)
            (self.root/name).write_bytes(canonical_bytes(resource))
        for name, data in payloads.items():
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.root/INDEX).write_text(json.dumps(dict(schema_version=1,resources=names,payloads=list(payloads))))

    def test_cli_inspect_resolves_material_and_meaningful_exit(self):
        command=[sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/lookdev.py'),
                 '--project', str(self.root), 'materials', 'inspect', '--id', 'ceramic']
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['effective']['surface']['roughness'],0.24)
        result=subprocess.run(command[:-1]+['missing'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertEqual(json.loads(result.stdout)['status'],'failed')

    def test_versions_require_disambiguation(self):
        alternate=deepcopy(material());alternate['version']='2.0.0'
        self.write_project([material(),alternate])
        project=Project(self.root)
        self.assertEqual(len(project.list('materials')),2)
        with self.assertRaises(RigError):project.inspect('materials','ceramic')
        self.assertEqual(project.inspect('materials','ceramic','2.0.0')['reference']['version'],'2.0.0')

    def test_capture_export_roundtrip_and_sources_immutable(self):
        self.write_project([texture()],{'textures/albedo.png':image_bytes()})
        project=Project(self.root)
        original=dict(project.sources)
        (self.root/'textures/albedo.png').write_bytes(b'changed after capture')
        destination=self.parent/'export'
        report=project.export_native(destination)
        restored=Project(destination)
        self.assertEqual(restored.sources,original)
        self.assertEqual(restored.sha256,project.sha256)
        self.assertEqual(report['loss_report']['lost'],[])
        self.assertFalse(report['rendered'])
        with self.assertRaises(RigError):project.export_native(destination)
        self.assertEqual((destination/'textures/albedo.png').read_bytes(),image_bytes())

    def test_corrupt_texture_source_rejected(self):
        self.write_project([texture()],{'textures/albedo.png':b'corrupt'})
        with self.assertRaises(RigError):Project(self.root)

    def test_path_traversal_aliases_and_unknown_index_rejected(self):
        for index in [dict(schema_version=1,resources=['../outside.json'],payloads=[]),
                      dict(schema_version=1,resources=['a.json','A.json'],payloads=[]),
                      dict(schema_version=1,resources=['export-manifest.json'],payloads=[]),
                      dict(schema_version=1,resources=[],payloads=[],execute='evil')]:
            (self.root/INDEX).write_text(json.dumps(index))
            with self.assertRaises((RigError,OSError)):Project(self.root)

    def test_symlink_and_fifo_never_followed_or_blocked(self):
        source=self.root/'ceramic-1.0.0.json'
        original=source.read_bytes();source.unlink()
        external=self.parent/'external.json';external.write_bytes(original)
        source.symlink_to(external)
        with self.assertRaises(OSError):Project(self.root)
        source.unlink();os.mkfifo(source)
        with self.assertRaises(RigError):Project(self.root)

    def test_export_cannot_nest_in_source(self):
        with self.assertRaises(RigError):Project(self.root).export_native(self.root/'output')

    def test_registry_defensive_copy(self):
        project=Project(self.root)
        inspected=project.inspect('materials','ceramic')
        inspected['effective']['surface']['metallic']=1
        self.assertEqual(project.inspect('materials','ceramic')['effective']['surface']['metallic'],0)

    def test_export_parent_swap_cannot_redirect_writes(self):
        import lookdev
        output_parent=self.parent/'output-parent';output_parent.mkdir()
        foreign=self.parent/'foreign';foreign.mkdir()
        relocated=self.parent/'relocated'
        original=lookdev._write_at
        swapped=False
        def swap_parent(*args):
            nonlocal swapped
            if not swapped:
                swapped=True
                output_parent.rename(relocated)
                output_parent.symlink_to(foreign,target_is_directory=True)
            return original(*args)
        with patch.object(lookdev,'_write_at',side_effect=swap_parent):
            Project(self.root).export_native(output_parent/'delivery')
        self.assertEqual(list(foreign.iterdir()),[])
        self.assertEqual(Project(relocated/'delivery').sha256,Project(self.root).sha256)

    def test_racing_destination_never_replaced(self):
        import lookdev
        destination=self.parent/'delivery'
        original=lookdev.rename_no_replace_at
        def race(*args):
            destination.mkdir()
            (destination/'sentinel').write_text('user work')
            return original(*args)
        with patch.object(lookdev,'rename_no_replace_at',side_effect=race):
            with self.assertRaises(FileExistsError):
                Project(self.root).export_native(destination)
        self.assertEqual((destination/'sentinel').read_text(),'user work')
        self.assertEqual(list(destination.iterdir()),[destination/'sentinel'])
