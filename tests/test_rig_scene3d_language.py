"""Actual native scene edits, immutable reopening and hostile language plans."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from rig_errors import RigError
from rig_math import axis_angle
from scene3d_contracts import load_project
from scene3d_edit import apply_commands
from scene3d_example import build_example, node
from scene3d_graph import SceneGraph
from scene3d_language import compile_description, compile_revision, apply_plan
from scene3d_prefabs import PrefabLibrary
from scene3d_snapshot import snapshot_project


class LanguagePlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "original"
        self.bundle = build_example(self.source)
        self.graph = SceneGraph.from_bundle(self.bundle, "example-scene.json", source_root=self.source)
        self.library = PrefabLibrary.from_files(self.source, self.bundle)

    def test_compilation_is_pure_deterministic_and_preserves_words(self):
        original = self.graph.document()
        text = "Move the camera two meters north; scale mesh by 2"
        plan = compile_description(self.graph, text)
        self.assertEqual(plan, compile_description(self.graph, text))
        self.assertEqual(plan["original_text"], text)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["planner"], "offline-grammar-v1")
        self.assertEqual(self.graph.document(), original)

    def test_real_revisions_reopen_and_original_geometry_remains_exact(self):
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        plan = compile_revision(self.graph, "duplicate mesh as copy; position copy at 1, 0, 3; rotate copy 90 degrees around y")
        result = apply_plan(self.graph, self.library, plan)
        target = self.directory / "edited"
        receipt = snapshot_project(self.source, target, self.graph, scene_path="example-scene.json",
                                   derived_documents=result["derived_documents"])
        reopened = load_project(target)
        graph = SceneGraph.from_bundle(reopened, receipt["active_scene"]["path"], source_root=target)
        np.testing.assert_allclose(np.array(graph.evaluate_base("copy")["world_matrix"])[:3, 3], [1, 0, 3])
        source = next(n for n in graph.document()["nodes"] if n["id"] == "mesh")
        copied = next(n for n in graph.document()["nodes"] if n["id"] == "copy")
        self.assertEqual(source["asset"], copied["asset"])
        self.assertEqual((target / "triangle.obj").read_bytes(), before["triangle.obj"])
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, before)

    def test_world_direction_respects_rotated_parent(self):
        document = self.graph.document()
        root = next(n for n in document["nodes"] if n["id"] == "root")
        root["transform"]["rotation"] = axis_angle([0, 1, 0], np.pi/2).tolist()
        graph = SceneGraph(document, external_references={})
        before = np.array(graph.evaluate_base("mesh")["world_matrix"])
        apply_plan(graph, PrefabLibrary(), compile_description(graph, "move mesh 3 meters east"))
        after = np.array(graph.evaluate_base("mesh")["world_matrix"])
        np.testing.assert_allclose(after[:3, 3] - before[:3, 3], [3, 0, 0], atol=1e-10)

    def test_sequential_language_sees_prior_edits(self):
        plan = compile_description(self.graph, "move mesh 1 meter east; move mesh 2 meters east")
        apply_plan(self.graph, self.library, plan)
        self.assertAlmostEqual(self.graph.evaluate_base("mesh")["world_matrix"][0][3], 5.)
        self.assertEqual(self.graph.revision, 1)

    def test_ambiguous_name_requires_resolution(self):
        document = self.graph.document()
        document["nodes"][0]["name"] = "same"
        document["nodes"][1]["name"] = "same"
        graph = SceneGraph(document, external_references={})
        plan = compile_description(graph, 'hide "same"')
        self.assertEqual(plan["status"], "unresolved")
        self.assertEqual(plan["diagnostics"][0]["code"], "SCENE3D_LANGUAGE_TARGET")
        self.assertEqual(compile_description(graph, "hide mesh")["status"], "ready")

    def test_article_in_display_name_is_preserved_and_word_numbers_compile(self):
        document = self.graph.document()
        next(n for n in document["nodes"] if n["id"] == "mesh")["name"] = "The Well"
        graph = SceneGraph(document, external_references={})
        for text in ('move "The Well" two meters east', 'move the well two meters east'):
            plan = compile_description(graph, text)
            self.assertEqual(plan["status"], "ready", plan)
            self.assertEqual(plan["commands"][0]["parameters"]["identifier"], "mesh")
        self.assertEqual(compile_description(graph, 'hide "well"')["status"], "unresolved")

    def test_unresolved_statement_prevents_partial_application(self):
        before = self.graph.document()
        plan = compile_description(self.graph, "hide mesh; summon a dragon; move mesh 2 meters east")
        self.assertEqual(plan["status"], "unresolved")
        self.assertEqual(plan["diagnostics"][0]["statement"], 1)
        self.assertEqual(plan["diagnostics"][0]["text"], "summon a dragon")
        with self.assertRaises(RigError):
            apply_plan(self.graph, self.library, plan)
        self.assertEqual(self.graph.document(), before)

    def test_stale_scene_revision_and_hash_fail(self):
        plan = compile_description(self.graph, "hide mesh")
        self.graph.set_tags("mesh", ["changed"])
        with self.assertRaisesRegex(RigError, "changed after"):
            apply_plan(self.graph, self.library, plan)

    def test_rehashed_malicious_commands_do_not_authorize_new_behavior(self):
        plan = compile_description(self.graph, "hide mesh")
        plan["commands"] = [{"operation": "delete", "parameters": {"identifier": "mesh"}}]
        unsigned = {key: value for key, value in plan.items() if key != "sha256"}
        plan["sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                                  ensure_ascii=False).encode()).hexdigest()
        with self.assertRaisesRegex(RigError, "deterministic compilation"):
            apply_plan(self.graph, self.library, plan)
        self.assertTrue(any(n["id"] == "mesh" for n in self.graph.document()["nodes"]))

    def test_wrong_types_and_resource_limits_fail_clearly(self):
        for text in (None, [], "", "a"*16385, "😀"*5000, "hide mesh\0", "\ud800"):
            with self.subTest(text=repr(text)[:30]), self.assertRaises(RigError):
                compile_description(self.graph, text)
        with self.assertRaises(RigError):
            compile_description(self.graph, ";".join(["hide mesh"] * 129))
        for text in ("scale mesh by 0", "scale mesh by -2", "move mesh 999999999 meters east", "hide mesh and camera"):
            self.assertEqual(compile_description(self.graph, text)["status"], "unresolved")

    def test_safe_delete_preserves_externally_referenced_camera(self):
        plan = compile_description(self.graph, "delete camera")
        self.assertEqual(plan["status"], "unresolved")
        self.assertEqual(self.graph.revision, 0)

    def test_cli_actual_plan_apply_snapshot_and_failure(self):
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(root / "scripts/scene3d.py")]
        plan_file = self.directory / "plan.json"
        planned = subprocess.run(command + ["--output", str(plan_file), "plan", "--project", str(self.source),
                                 "--scene", "example-scene.json", "--text", "move mesh 4 meters east"], capture_output=True, text=True)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        target = self.directory / "cli-result"
        applied = subprocess.run(command + ["apply-plan", "--project", str(self.source), "--scene", "example-scene.json",
                                 "--file", str(plan_file), "--destination", str(target)], capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = json.loads(applied.stdout)
        bundle = load_project(target)
        graph = SceneGraph.from_bundle(bundle, result["active_scene"]["path"], source_root=target)
        self.assertEqual(graph.evaluate_base("mesh")["world_matrix"][0][3], 6.)
        again = subprocess.run(command + ["apply-plan", "--project", str(self.source), "--scene", "example-scene.json",
                               "--file", str(plan_file), "--destination", str(target)], capture_output=True, text=True)
        self.assertEqual(again.returncode, 2)
        self.assertTrue(load_project(target))

    def test_cli_typed_batch_rejects_stale_and_rolls_back_then_reopens(self):
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(root / "scripts/scene3d.py")]
        common = ["--project", str(self.source), "--scene", "example-scene.json"]
        state = subprocess.run(command + ["scene-state"] + common, capture_output=True, text=True)
        self.assertEqual(state.returncode, 0, state.stderr)
        state = json.loads(state.stdout)
        self.assertEqual(state["revision"], 0)
        batch = self.directory / "batch.json"
        target = self.directory / "batch-output"
        batch.write_text(json.dumps({"schema_version": 1, "commands": [
            {"operation": "rename", "parameters": {"identifier": "mesh", "name": "Renamed"}},
            {"operation": "delete", "parameters": {"identifier": "absent"}}
        ]}))
        def run(digest=state["scene_sha256"]):
            return subprocess.run(command + ["edit"] + common + ["--file", str(batch),
                "--expected-revision", "0", "--expected-sha256", digest,
                "--destination", str(target)], capture_output=True, text=True)
        stale = run("0" * 64)
        self.assertEqual(stale.returncode, 2)
        self.assertEqual(json.loads(stale.stderr)["error"]["code"], "SCENE3D_CONFLICT")
        invalid = run()
        self.assertEqual(invalid.returncode, 2)
        self.assertFalse(target.exists())
        self.assertEqual(SceneGraph.from_bundle(load_project(self.source), "example-scene.json",
                         source_root=self.source).digest, state["scene_sha256"])
        content = json.loads(batch.read_text())
        content["commands"].pop()
        batch.write_text(json.dumps(content))
        success = run()
        self.assertEqual(success.returncode, 0, success.stderr)
        receipt = json.loads(success.stdout)
        graph = SceneGraph.from_bundle(load_project(target), receipt["active_scene"]["path"], source_root=target)
        self.assertEqual(graph.revision, 1)
        self.assertEqual(next(n for n in graph.document()["nodes"] if n["id"] == "mesh")["name"], "Renamed")
        self.assertEqual((target / "triangle.obj").read_bytes(), (self.source / "triangle.obj").read_bytes())

    def test_cli_dry_run_does_not_publish_and_unresolved_is_nonzero(self):
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(root / "scripts/scene3d.py")]
        target = self.directory / "not-written"
        result = subprocess.run(command + ["revise", "--project", str(self.source), "--scene", "example-scene.json",
                                "--text", "hide mesh", "--destination", str(target), "--dry-run"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())
        result = subprocess.run(command + ["plan", "--project", str(self.source), "--scene", "example-scene.json",
                                "--text", "open('secret')"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "unresolved")


if __name__ == "__main__":
    unittest.main()
