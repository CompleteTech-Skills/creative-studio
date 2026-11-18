"""Portable admission evidence from real Pillow shot and scene renders."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from test_production_render import fixture_project, track
from production_render import render_scene, render_shot
from silent_video import source_from_production


class SilentVideoProductionAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = fixture_project(self.root / "project")
        self.fps = {"numerator": 24000, "denominator": 1001}
        self.bundle["project"]["render"].update(fps=self.fps, background="#f1ece6ff")

    def assert_admitted(self, destination, rendered, count):
        owned_files = ["render-manifest.json", "audio.wav"]
        if rendered["kind"] == "scene-render":
            for shot in ("first", "second"):
                owned_files.extend(f"shots/{shot}/{name}" for name in ("render-manifest.json", "audio.wav"))
        original = {name: (destination / name).read_bytes() for name in owned_files}
        source = source_from_production(destination)
        self.assertEqual(source["origin"], "production2d")
        self.assertEqual(source["fps"], self.fps)
        self.assertEqual((source["width"], source["height"]), (48, 32))
        self.assertEqual(source["alpha"], "opaque")
        self.assertEqual(len(rendered["frames"]), count)
        expected = []
        for index, record in enumerate(rendered["frames"]):
            relative = f"frames/{index:06d}.png"
            data = (destination / relative).read_bytes()
            self.assertEqual(record["path"], relative)
            self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
            expected.append({"index": index, "path": relative, "bytes": len(data), "sha256": record["sha256"]})
        self.assertEqual(source["frames"], expected)
        self.assertEqual({name: (destination / name).read_bytes() for name in owned_files}, original)
        return source

    def test_complete_shot_admits_ordered_frames_and_preserves_manifest_and_wav(self):
        destination = self.root / "shot"
        rendered = render_shot(self.bundle, "first", destination)
        self.assert_admitted(destination, rendered, 6)

    def test_complete_scene_admits_frames_in_cut_order_and_preserves_shot_outputs(self):
        first_scene = self.bundle["scenes"][0]
        second_shot = self.bundle["shots"]["second"]
        second_shot["camera"]["position"] = track((0, [4, 0]))
        first_scene["shots"].append(second_shot)
        self.bundle["scenes"] = [first_scene]
        destination = self.root / "scene"
        rendered = render_scene(self.bundle, "scene-one", destination)
        source = self.assert_admitted(destination, rendered, 12)
        for offset, shot in ((0, "first"), (6, "second")):
            first_frame = destination / "shots" / shot / "frames" / "000000.png"
            self.assertEqual(source["frames"][offset]["sha256"], hashlib.sha256(first_frame.read_bytes()).hexdigest())
        self.assertNotEqual(source["frames"][0]["sha256"], source["frames"][6]["sha256"])

    def test_incomplete_shot_is_rejected_and_checkpoint_can_be_resumed(self):
        destination = self.root / "partial"
        render_shot(self.bundle, "first", destination, max_frames=2)
        checkpoint = destination / "render-manifest.json"
        original = checkpoint.read_bytes()
        with self.assertRaisesRegex(ValueError, "render is incomplete"):
            source_from_production(destination)
        self.assertEqual(checkpoint.read_bytes(), original)
        rendered = render_shot(self.bundle, "first", destination)
        self.assertEqual(rendered["operation"]["reused_frames"], 2)
        self.assert_admitted(destination, rendered, 6)

    def test_complete_shot_rejects_malformed_manifest_or_corrupt_media(self):
        destination = self.root / "damaged"
        render_shot(self.bundle, "first", destination)
        for relative, diagnostic in (
            ("render-manifest.json", "render manifest is corrupt"),
            ("frames/000001.png", "rendered frame missing or changed"),
            ("audio.wav", "mixed audio is missing or changed"),
        ):
            with self.subTest(relative=relative):
                path = destination / relative
                original = path.read_bytes()
                path.write_bytes(original[:8])
                with self.assertRaisesRegex(ValueError, diagnostic):
                    source_from_production(destination)
                self.assertEqual(path.read_bytes(), original[:8])
                path.write_bytes(original)

    def test_transparent_production_frame_requires_explicit_compositing(self):
        self.bundle["project"]["render"]["background"] = "#00000000"
        destination = self.root / "transparent"
        render_shot(self.bundle, "first", destination)
        with self.assertRaisesRegex(ValueError, "opaque profile rejects nonopaque source alpha"):
            source_from_production(destination)
