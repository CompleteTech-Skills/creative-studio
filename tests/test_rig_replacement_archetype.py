"""Exercise template replacement through native correction and loopback HTTP."""
from copy import deepcopy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import numpy as np
from rig_corrections import apply_rig_corrections
from rig_errors import RigError, atomic_json, object_hash, timestamp
from rig_gltf import export_geometry
from rig_http import RigServer
from rig_jobs import create_run, save_rig
from rig_models import Geometry, Mesh, load_rig, rig_signature
from rig_operations import mutate
from rig_package import package_rig, restore_rig
from rig_retarget import build_retarget_profile, retarget_pose
from rig_skeleton import evaluate_fk, neutral_pose
from rig_skinning import evaluate_deformation
from rig_states import rig_capabilities
from rig_templates import ROOT as TEMPLATE_ROOT, load_rig_template


class ReplacementArchetypeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rig-541-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        # Eight actual vertices and twelve triangles, not a manifest-only rig.
        points = np.array([[-.4, 0, -.9], [.4, 0, -.9], [.4, 1.2, -.9], [-.4, 1.2, -.9],
                           [-.4, 0, .9], [.4, 0, .9], [.4, 1.2, .9], [-.4, 1.2, .9]])
        triangles = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
                              [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2],
                              [0, 4, 7], [0, 7, 3], [1, 2, 6], [1, 6, 5]])
        self.source = self.directory / "synthetic.glb"
        export_geometry(Geometry([Mesh("body", points, triangles)], [dict(name="fixture")]),
                        self.source, provenance={"fixture": True})
        self.source_bytes = self.source.read_bytes()
        self.server = None

    def native_rig(self, name="native", template="custom"):
        rig = create_run(self.source, self.directory / name, identifier=name, fixture=True)
        result = mutate(rig.root, "auto", {"template": template})
        self.assertEqual(result["status"], "succeeded")
        return load_rig(rig.root)

    def correction(self, rig, *templates):
        return dict(schema_version=1, kind="hatch-3d-rig-correction",
                    rig_id=rig.manifest["id"], rig_version=rig.manifest["version"],
                    expected_sha256=rig_signature(rig.manifest), author="synthetic oracle",
                    reason="Issue 541 template replacement regression", created_at=timestamp(),
                    edits=[dict(operation="template", target="skeleton", value={"template": item})
                           for item in templates])

    def files(self, root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
                if p.is_file() and p.name not in ("job.json", ".rig.lock")}

    def retained_files(self, rig):
        files = self.files(rig.root)
        return {name: data for name, data in files.items()
                if name.startswith(("source/", "geometry/", "rig/revisions/", "rig/corrections/"))
                or name == "identity.json"}

    def assert_retained(self, root, files):
        for name, data in files.items():
            with self.subTest(retained=name):
                self.assertEqual((root / name).read_bytes(), data)

    def request(self, method, path, body=None):
        if self.server is None:
            self.server = RigServer(self.directory / "http-runs", self.directory)
            thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            thread.start()
            def stop():
                self.server.shutdown()
                self.server.server_close()
                thread.join(timeout=3)
            self.addCleanup(stop)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        try:
            connection.request(method, path, None if body is None else json.dumps(body),
                               {"Content-Type": "application/json",
                                "Authorization": "Bearer " + self.server.token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def http_rig(self):
        code, value = self.request("POST", "/v1/rig-jobs",
                                   {"model": self.source.name, "id": "http", "fixture": True})
        self.assertEqual(code, 200, value)
        self.assertTrue(value["fixture"])
        code, value = self.request("POST", "/v1/rig-jobs/http/generate", {"template": "custom"})
        self.assertEqual(code, 200, value)
        return load_rig(self.server.root / "http")

    def assert_quadruped(self, rig, stage):
        expected = load_rig_template("quadruped")
        with self.subTest(stage=stage, property="skeleton"):
            self.assertEqual(len(rig.skeleton["bones"]), 27)
            self.assertEqual({b["id"]: b["role"] for b in rig.skeleton["bones"]},
                             {b["id"]: b["role"] for b in expected["bones"]})
            self.assertTrue(all(np.isfinite(matrix).all() for matrix in evaluate_fk(rig.skeleton).values()))
        with self.subTest(stage=stage, property="archetype"):
            self.assertEqual(rig.manifest["archetype"], "quadruped")
        with self.subTest(stage=stage, property="capability"):
            self.assertIn("quadrupedal", rig_capabilities(rig))
        with self.subTest(stage=stage, property="original-input"):
            self.assertTrue(rig.manifest["source"]["fixture"])
            self.assertEqual((rig.root / rig.manifest["source"]["path"]).read_bytes(), self.source_bytes)
            self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def assert_retarget(self, target, stage):
        reference_name = "reference-" + stage
        source = self.native_rig(reference_name, "quadruped")
        with self.subTest(stage=stage, property="strict-retarget"):
            # No partial=True: matching admitted quadrupeds need no cross-species opt-in.
            profile = build_retarget_profile(source, target)
            self.assertEqual(len(profile["mapping"]), 27)
            self.assertEqual(profile["omissions"], [])
            pose = retarget_pose(source, target, neutral_pose(source.skeleton), profile)
            self.assertTrue(all(np.isfinite(m).all() for m in evaluate_fk(target.skeleton, pose).values()))

    def roundtrip(self, rig, correction, retained):
        receipt = "rig/corrections/" + object_hash(correction) + ".json"
        self.assertEqual(json.loads((rig.root / receipt).read_bytes()), correction)
        self.assert_retained(rig.root, retained)
        self.assertFalse(rig.weights)
        self.assertFalse(rig.clips)
        self.assert_quadruped(rig, "returned")
        loaded = load_rig(rig.root)
        self.assert_quadruped(loaded, "reloaded")
        self.assert_retarget(loaded, "reloaded")
        # Regenerate actual binding and evaluate actual native deformation.
        result = mutate(rig.root, "weights", {})
        self.assertEqual(result["status"], "succeeded")
        bound = load_rig(rig.root)
        for mesh in evaluate_deformation(bound).values():
            self.assertTrue(np.isfinite(mesh["positions"]).all())
        archive = self.directory / "package.zip"
        package_rig(rig.root, archive)
        restored_root = self.directory / "restored"
        result = restore_rig(archive, restored_root)
        self.assertTrue(result["verified"])
        restored = load_rig(restored_root)
        self.assertEqual(restored.manifest, bound.manifest)
        self.assertEqual((restored_root / receipt).read_bytes(), (rig.root / receipt).read_bytes())
        self.assert_retained(restored_root, retained)
        self.assert_quadruped(restored, "restored")
        self.assert_retarget(restored, "restored")
        for mesh in evaluate_deformation(restored).values():
            self.assertTrue(np.isfinite(mesh["positions"]).all())

    def add_prior_correction(self, rig):
        correction = self.correction(rig)
        correction["edits"] = [dict(operation="expression", target="unused", value=None)]
        updated, _ = apply_rig_corrections(rig, correction)
        self.assertTrue(list((rig.root / "rig/revisions").glob("*.json")))
        return updated

    def test_native_nonpersistent_replacement_matches_admitted_template(self):
        original = self.native_rig()
        before = self.files(original.root)
        manifest = deepcopy(original.manifest)
        result, _ = apply_rig_corrections(original, self.correction(original, "quadruped"), persist=False)
        self.assertEqual(original.manifest, manifest)
        self.assertEqual(self.files(original.root), before)
        self.assert_quadruped(result, "nonpersistent")
        self.assert_retarget(result, "nonpersistent")

    def test_native_save_reload_package_restore_and_downstream_selection(self):
        original = self.add_prior_correction(self.native_rig())
        retained = self.retained_files(original)
        correction = self.correction(original, "quadruped")
        result, report = apply_rig_corrections(original, correction)
        self.assertTrue(report["source_preserved"])
        self.roundtrip(result, correction, retained)

    def test_http_save_reload_package_restore_and_downstream_selection(self):
        original = self.add_prior_correction(self.http_rig())
        retained = self.retained_files(original)
        correction = self.correction(original, "quadruped")
        code, response = self.request("PATCH", "/v1/rigs/http", correction)
        self.assertEqual(code, 200, response)
        self.assertEqual(response["status"], "succeeded")
        code, manifest = self.request("GET", "/v1/rigs/http")
        self.assertEqual(code, 200, manifest)
        with self.subTest(stage="http-get"):
            self.assertEqual(manifest["archetype"], "quadruped")
        self.roundtrip(load_rig(original.root), correction, retained)

    def test_independent_uncertainty_survives_native_and_http_replacement(self):
        for route in ("native", "http"):
            with self.subTest(route=route):
                rig = self.native_rig() if route == "native" else self.http_rig()
                independent = dict(element=rig.geometry.meshes[0].id, code="RIG_SURFACE_UNCERTAIN",
                                   message="Synthetic material interpretation needs separate review.",
                                   evidence=dict(source="import-metadata", reference="synthetic independent evidence",
                                                 confidence=.35), resolution="pending")
                rig.manifest["uncertainties"].append(deepcopy(independent))
                save_rig(rig, operation="synthetic-independent-evidence")
                correction = self.correction(rig, "quadruped")
                if route == "native":
                    result, _ = apply_rig_corrections(rig, correction)
                else:
                    code, response = self.request("PATCH", "/v1/rigs/http", correction)
                    self.assertEqual(code, 200, response)
                    result = load_rig(rig.root)
                self.assertIn(independent, result.manifest["uncertainties"])
                self.assertIn(independent, load_rig(result.root).manifest["uncertainties"])

    def test_native_custom_file_and_object_use_declared_identity(self):
        for mode in ("file", "object"):
            with self.subTest(mode=mode):
                rig = self.native_rig("custom-" + mode)
                template = load_rig_template("custom")
                template["id"] = "authored-creature"
                path = self.directory / "unrelated-filename.json"
                path.write_text(json.dumps(template), encoding="utf-8")
                chosen = str(path) if mode == "file" else template
                result, _ = apply_rig_corrections(rig, self.correction(rig, chosen))
                self.assertEqual([b["role"] for b in result.skeleton["bones"]], ["root"])
                self.assertEqual(result.manifest["archetype"], "authored-creature")
                self.assertEqual(load_rig(result.root).manifest["archetype"], "authored-creature")

    def test_refit_preserves_same_code_reviews_from_other_owners(self):
        for route in ("native", "http"):
            with self.subTest(route=route):
                original = self.native_rig() if route == "native" else self.http_rig()
                previous_fit = deepcopy(original.manifest["uncertainties"][0])
                unrelated = [
                    dict(element=element, code="RIG_ANATOMY_UNCERTAIN",
                         message=f"Independent review of {element} remains pending.",
                         evidence=dict(source="import-metadata", reference="Independent authored note",
                                       confidence=.35), resolution="pending")
                    for element in ("body", "pelvis")
                ]
                original.manifest["uncertainties"].extend(deepcopy(unrelated))
                save_rig(original, operation="synthetic-independent-anatomy")
                correction = self.correction(original, "quadruped")
                correction["edits"][0]["value"]["landmarks"] = {
                    "root": dict(head=[0, 0, 0], tail=[0, .3, 0], approved=True),
                    "pelvis": dict(head=[0, .5, 0], tail=[0, .7, 0], confidence=.8,
                                   approved=True),
                }
                if route == "native":
                    result, _ = apply_rig_corrections(original, correction)
                    manifests = [result.manifest, load_rig(result.root).manifest]
                else:
                    status, response = self.request("PATCH", "/v1/rigs/http", correction)
                    self.assertEqual(status, 200, response)
                    status, returned = self.request("GET", "/v1/rigs/http")
                    self.assertEqual(status, 200, returned)
                    manifests = [returned, load_rig(original.root).manifest]
                for manifest in manifests:
                    observations = manifest["uncertainties"]
                    self.assertNotIn(previous_fit, observations)
                    for review in unrelated:
                        with self.subTest(element=review["element"]):
                            self.assertIn(review, observations)
                    fitted_pelvis = [item["resolution"] for item in observations
                                     if item["element"] == "pelvis"
                                     and item["evidence"]["source"] == "human-landmark"]
                    self.assertEqual(fitted_pelvis, ["pending"])

    def refit_duplicate_observations(self, *, legacy):
        original = self.native_rig()
        fitted_observation = deepcopy(original.manifest["uncertainties"][0])
        original.manifest["uncertainties"].append(deepcopy(fitted_observation))
        save_rig(original, operation="synthetic-legacy-duplicate-anatomy")
        original = load_rig(original.root)
        self.assertEqual(original.manifest["anatomy_uncertainty_bindings"],
                         [object_hash(fitted_observation), None])
        if legacy:
            # Older manifests have neither a persisted digest nor a native
            # occurrence owner for either of these byte-identical records.
            manifest = deepcopy(original.manifest)
            manifest.pop("anatomy_uncertainty_bindings")
            atomic_json(original.root / "rig.json", manifest)
            original = load_rig(original.root)
        correction = self.correction(original, "custom", "quadruped")
        correction["edits"][1]["value"]["landmarks"] = {
            "root": dict(head=[0, 0, 0], tail=[0, .3, 0], approved=True),
        }
        result, _ = apply_rig_corrections(original, correction)
        observations = load_rig(result.root).manifest["uncertainties"]
        return observations, fitted_observation

    def test_refit_retains_duplicate_previous_observations_when_ownership_is_ambiguous(self):
        observations, fitted_observation = self.refit_duplicate_observations(legacy=True)
        self.assertEqual(observations.count(fitted_observation), 2)

    def test_refit_retires_known_occurrence_and_preserves_its_opaque_copy(self):
        observations, fitted_observation = self.refit_duplicate_observations(legacy=False)
        self.assertEqual(observations.count(fitted_observation), 1)

    def seed_legacy_reviews(self, route, count):
        rig = self.native_rig() if route == "native" else self.http_rig()
        preview, _ = apply_rig_corrections(rig, self.correction(rig, "quadruped"), persist=False)
        pelvis = next(item for item in preview.manifest["uncertainties"] if item["element"] == "pelvis")
        independent = [deepcopy(pelvis)]
        for index in range(count - 1):
            independent.append(dict(pelvis, message=f"Synthetic independent observation {index}",
                                    resolution=("accepted", "corrected", "pending")[index % 3]))
        # Simulate a pre-binding manifest. An earlier reviewer independently
        # recorded the exact value a future quadruped fit will generate.
        legacy = deepcopy(rig.manifest)
        legacy.pop("anatomy_uncertainty_bindings", None)
        legacy["uncertainties"] = deepcopy(independent)
        atomic_json(rig.root / "rig.json", legacy)
        return load_rig(rig.root), independent

    def apply_and_reload(self, route, rig, correction):
        if route == "native":
            result, _ = apply_rig_corrections(rig, correction)
            returned = result.manifest
        else:
            status, response = self.request("PATCH", "/v1/rigs/http", correction)
            self.assertEqual(status, 200, response)
            status, returned = self.request("GET", "/v1/rigs/http")
            self.assertEqual(status, 200, returned)
        loaded = load_rig(rig.root)
        self.assertEqual(loaded.manifest, returned)
        return loaded

    def test_low_confidence_landmark_requires_a_lasting_edit_to_its_bound_bone(self):
        def pelvis_records(rig):
            anatomy = [item for item in rig.manifest["uncertainties"]
                       if item["code"] == "RIG_ANATOMY_UNCERTAIN"]
            bindings = rig.manifest["anatomy_uncertainty_bindings"]
            self.assertEqual(len(anatomy), len(bindings))
            return [(item, binding) for item, binding in zip(anatomy, bindings)
                    if item["element"] == "pelvis"]

        for route in ("native", "http"):
            with self.subTest(route=route):
                rig = self.native_rig() if route == "native" else self.http_rig()
                self.assertNotIn("pelvis", [bone["id"] for bone in rig.skeleton["bones"]])
                landmark = dict(head=[0, .5, 0], tail=[0, .7, 0], approved=True, confidence=.8)
                correction = self.correction(rig, "quadruped")
                correction["edits"][0]["value"]["landmarks"] = {"pelvis": landmark}
                rig = self.apply_and_reload(route, rig, correction)
                bone = next(b for b in rig.skeleton["bones"] if b["id"] == "pelvis")
                self.assertEqual((bone["head"], bone["tail"]), (landmark["head"], landmark["tail"]))
                (observation, binding), = pelvis_records(rig)
                self.assertEqual(binding, object_hash(observation))
                self.assertEqual(observation["resolution"], "pending")
                self.assertEqual(observation["evidence"]["confidence"], .8)
                self.assertEqual(observation["evidence"]["source"], "human-landmark")

                # A second author records the very same observation. Only the
                # fit-owned occurrence can be resolved by moving the pelvis.
                rig.manifest["uncertainties"].append(deepcopy(observation))
                save_rig(rig, operation="synthetic-independent-landmark-observation")
                rig = load_rig(rig.root)

                # The last fit restores the active anatomy after a transient fit.
                refit = self.correction(rig, "quadruped", "quadruped")
                refit["edits"][0]["value"]["landmarks"] = {"pelvis": dict(landmark, tail=[0, .8, 0])}
                refit["edits"][1]["value"]["landmarks"] = {"pelvis": landmark}
                rig = self.apply_and_reload(route, rig, refit)
                pending_pair = [(observation, None), (observation, object_hash(observation))]
                self.assertEqual(pelvis_records(rig), pending_pair)

                move = dict(operation="bone-update", target="pelvis", value={"tail": [0, .8, 0]})
                correction = self.correction(rig)
                correction["edits"] = [move, dict(move, value={"tail": landmark["tail"]})]
                rig = self.apply_and_reload(route, rig, correction)
                bone = next(b for b in rig.skeleton["bones"] if b["id"] == "pelvis")
                self.assertEqual(bone["tail"], landmark["tail"])
                self.assertEqual(pelvis_records(rig), pending_pair)

                correction = self.correction(rig)
                correction["edits"] = [move]
                rig = self.apply_and_reload(route, rig, correction)
                bone = next(b for b in rig.skeleton["bones"] if b["id"] == "pelvis")
                self.assertEqual(bone["tail"], move["value"]["tail"])
                resolved = dict(observation, resolution="corrected")
                self.assertEqual(pelvis_records(rig), [(observation, None), (resolved, object_hash(resolved))])

    def test_repeated_refits_at_exact_limit_keep_independent_and_owned_twins(self):
        for route in ("native", "http"):
            with self.subTest(route=route):
                rig, independent = self.seed_legacy_reviews(route, 997)
                original = self.retained_files(rig)
                for generation in range(3):
                    with self.subTest(generation=generation):
                        rig = self.apply_and_reload(route, rig, self.correction(rig, "quadruped"))
                        records = rig.manifest["uncertainties"]
                        fitted = records[len(independent):]
                        self.assertEqual(len(records), 1024)
                        self.assertEqual(len(fitted), 27)
                        self.assertEqual(records[:997], independent)
                        self.assertEqual(records.count(independent[0]), 2)
                        self.assertEqual(rig.manifest["anatomy_uncertainty_bindings"],
                                         [None] * 997 + [object_hash(item) for item in fitted])
                        self.assert_retained(rig.root, original)
                # Only the fit-owned occurrence follows the newly renamed bone.
                correction = self.correction(rig)
                correction["edits"] = [dict(operation="bone-update", target="pelvis",
                                            value={"id": "pelvis-renamed"})]
                rig = self.apply_and_reload(route, rig, correction)
                records = rig.manifest["uncertainties"]
                self.assertEqual(records[:997], independent)
                self.assertEqual(records.count(independent[0]), 1)
                self.assertIn(dict(independent[0], element="pelvis-renamed"), records[997:])
                self.assertEqual(rig.manifest["anatomy_uncertainty_bindings"],
                                 [None] * 997 + [object_hash(item) for item in records[997:]])
                self.assert_retained(rig.root, original)

    def test_over_limit_refit_refuses_without_mutating_protected_files(self):
        for route in ("native", "http"):
            with self.subTest(route=route):
                rig, independent = self.seed_legacy_reviews(route, 998)
                before = self.files(rig.root)
                correction = self.correction(rig, "quadruped")
                if route == "native":
                    with self.assertRaises(RigError) as caught:
                        apply_rig_corrections(rig, correction)
                    error = caught.exception.report
                else:
                    status, response = self.request("PATCH", "/v1/rigs/http", correction)
                    self.assertEqual(status, 400, response)
                    error = response["error"]
                self.assertEqual(error["code"], "RIG_LIMIT_UNCERTAINTY")
                self.assertEqual((error["expected"], error["observed"]), (1024, 1025))
                self.assertEqual(load_rig(rig.root).manifest["uncertainties"], independent)
                self.assertEqual(self.files(rig.root), before)

    def test_last_admitted_template_determines_identity(self):
        rig = self.native_rig()
        result, _ = apply_rig_corrections(rig, self.correction(rig, "quadruped", "humanoid"))
        self.assertEqual({b["role"] for b in result.skeleton["bones"]},
                         {b["role"] for b in load_rig_template("humanoid")["bones"]})
        self.assertEqual(result.manifest["archetype"], "humanoid")

    def test_failed_native_admission_preserves_active_rig_and_all_files(self):
        rig = self.add_prior_correction(self.native_rig())
        before = self.files(rig.root)
        manifest = deepcopy(rig.manifest)
        # A prior successful edit in the same transaction must not escape on failure.
        correction = self.correction(rig, "quadruped", "missing-synthetic-template")
        for persist in (False, True):
            with self.subTest(persist=persist):
                with self.assertRaises(RigError) as error:
                    apply_rig_corrections(rig, correction, persist=persist)
                self.assertEqual(error.exception.report["code"], "RIG_TEMPLATE")
                self.assertEqual(rig.manifest, manifest)
                self.assertEqual(load_rig(rig.root).manifest, manifest)
                self.assertEqual(self.files(rig.root), before)

    def test_http_rejects_noninstalled_templates_without_changing_active_rig(self):
        rig = self.add_prior_correction(self.http_rig())
        before = self.files(rig.root)
        manifest = deepcopy(rig.manifest)
        template = load_rig_template("custom")
        path = self.directory / "local-custom.json"
        path.write_text(json.dumps(template), encoding="utf-8")
        for value in (template, str(path), str(TEMPLATE_ROOT / "quadruped.json"),
                      "quadruped.json", "missing-synthetic-template"):
            with self.subTest(template_type=type(value).__name__, value=str(value)[:60]):
                code, response = self.request("PATCH", "/v1/rigs/http", self.correction(rig, "quadruped", value))
                self.assertEqual(code, 400, response)
                self.assertEqual(response["error"]["code"], "RIG_API_TEMPLATE")
                self.assertEqual(load_rig(rig.root).manifest, manifest)
                self.assertEqual(self.files(rig.root), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
