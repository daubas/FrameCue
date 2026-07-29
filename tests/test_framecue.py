import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import framecue  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures"


class FrameCueV2Tests(unittest.TestCase):
    def setUp(self):
        if not (ROOT / "dist" / "index.html").is_file():
            self.fail("run npm run build before the FrameCue test suite")
        self.temp = tempfile.TemporaryDirectory(prefix="framecue-tests-")
        self.output = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build_fixture(self, name):
        fixture = FIXTURES / name
        source = framecue.read_json(fixture / "package.source.json")
        out_dir = self.output / name
        summary = framecue.build_package(source, fixture, out_dir)
        package = framecue.read_json(out_dir / "review_package.json")
        return out_dir, package, summary

    def test_builds_all_supported_workflows(self):
        expected = {
            "basic": "subtitle",
            "redraw": "redraw",
            "boundary": "boundary",
            "hyperframes": "hyperframes",
        }
        for fixture, workflow in expected.items():
            with self.subTest(fixture=fixture):
                out_dir, package, summary = self.build_fixture(fixture)
                self.assertEqual(summary["workflow"], workflow)
                self.assertEqual(package["schema_version"], framecue.PACKAGE_SCHEMA)
                self.assertTrue((out_dir / "index.html").is_file())
                self.assertTrue((out_dir / package["scenes"][0]["image"]).is_file())
                if workflow == "hyperframes":
                    self.assertTrue((out_dir / "assets/hyperframes/framecue-player.html").is_file())

    def test_result_requires_complete_approved_snapshot(self):
        out_dir, package, _ = self.build_fixture("basic")
        result = framecue.default_result(package, approved=True)
        summary = framecue.validate_result(result, package, require_approved=True)
        self.assertEqual(summary["status"], "approved")
        result["cues"].pop()
        with self.assertRaises(framecue.FrameCueError):
            framecue.validate_result(result, package, require_approved=True)
        self.assertTrue((out_dir / "review_package.json").is_file())

    def test_approved_result_rejects_cue_block_speech_divergence(self):
        _, package, _ = self.build_fixture("basic")
        result = framecue.default_result(package, approved=True)
        result["cues"][0]["text"] = "OpenClaw 改成另一段字幕"
        with self.assertRaisesRegex(framecue.FrameCueError, "does not match its cues"):
            framecue.validate_result(result, package, require_approved=True)

        result = framecue.default_result(package, approved=True)
        result["blocks"][0]["speech_text"] = "這是沒有同步的舊語音。"
        with self.assertRaisesRegex(framecue.FrameCueError, "speech_text does not match"):
            framecue.validate_result(result, package, require_approved=True)

    def test_no_block_result_keeps_cue_speech_text(self):
        out_dir, package, _ = self.build_fixture("redraw")
        package["blocks"] = []
        package["content_checksum"] = framecue.package_checksum(package)
        result = framecue.default_result(package, approved=True)
        self.assertEqual(result["cues"][0]["speech_text"], package["cues"][0]["speech_text"])
        self.assertEqual(framecue.validate_result(result, package, require_approved=True)["status"], "approved")
        self.assertTrue((out_dir / "review_package.json").is_file())

    def test_package_checksum_rejects_edits(self):
        _, package, _ = self.build_fixture("basic")
        package["cues"][0]["text"] = "tampered"
        with self.assertRaises(framecue.FrameCueError):
            framecue.validate_package(package, None, check_assets=False)

    def test_same_source_has_a_stable_checksum(self):
        fixture = FIXTURES / "basic"
        source = framecue.read_json(fixture / "package.source.json")
        first = self.output / "first"
        second = self.output / "second"
        framecue.build_package(source, fixture, first)
        framecue.build_package(source, fixture, second)
        first_package = framecue.read_json(first / "review_package.json")
        second_package = framecue.read_json(second / "review_package.json")
        self.assertEqual(first_package["content_checksum"], second_package["content_checksum"])

    def test_null_legacy_milliseconds_fall_back_to_seconds(self):
        self.assertEqual(framecue.source_ms({"start_ms": None, "start": 1.25}, "start_ms", "fixture"), 1250)

    def test_manifest_copies_two_immutable_bundles(self):
        basic_dir, _, _ = self.build_fixture("basic")
        redraw_dir, _, _ = self.build_fixture("redraw")
        manifest_dir = self.output / "manifest"
        summary = framecue.build_manifest([
            f"basic={basic_dir}",
            f"redraw={redraw_dir}",
        ], manifest_dir)
        self.assertEqual(summary["item_count"], 2)
        manifest = framecue.read_json(manifest_dir / "framecue_manifest.json")
        self.assertEqual(manifest["schema_version"], framecue.MANIFEST_SCHEMA)
        self.assertTrue((manifest_dir / "items/basic/review_package.json").is_file())

    def test_migrate_v1_creates_a_new_bundle(self):
        fixture = FIXTURES / "basic"
        legacy_root = self.output / "legacy"
        legacy_root.mkdir()
        shutil.copy2(fixture / "assets/scene.svg", legacy_root / "scene.svg")
        legacy = {
            "scenes": [{
                "id": 1,
                "start": 0,
                "end": 1,
                "image": "scene.svg",
                "compare_image": "scene.svg",
                "full_image": "scene.svg"
            }],
            "cues": [{
                "id": 1,
                "start": 0,
                "end": 1,
                "scene_id": 1,
                "text": "舊字幕",
                "speech_text": "舊字幕。",
                "original_text": "legacy subtitle"
            }]
        }
        legacy_path = legacy_root / "review_package.json"
        legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        source = framecue.migrate_v1(legacy_path, "", "legacy-fixture", "r1", "subtitle")
        out_dir = self.output / "migrated"
        framecue.build_package(source, legacy_root, out_dir)
        package = framecue.read_json(out_dir / "review_package.json")
        self.assertEqual(package["review_id"], "legacy-fixture")
        self.assertEqual(package["cues"][0]["id"], "c0001")
        self.assertTrue((out_dir / package["scenes"][0]["redraw"]["comparison_image"]).is_file())

    def test_migrate_v1_keeps_safe_string_ids(self):
        self.assertEqual(framecue.migrated_id("s", "scene_001", 1), "scene_001")
        self.assertEqual(framecue.migrated_id("c", "cue one", 1), "c-cue-one")


if __name__ == "__main__":
    unittest.main()
