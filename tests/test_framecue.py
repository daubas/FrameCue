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

    def test_new_modes_build_from_carousel_and_markdown_inputs(self):
        cards = self.output / "cards"
        cards.mkdir()
        for name in ("slide-01.png", "slide-02.png", "contact-sheet-v1.png", "mobile-audit-390.png"):
            (cards / name).write_bytes(b"framecue-png")
        carousel_source = framecue.carousel_source(cards, "fixture-carousel")
        carousel_dir = self.output / "carousel"
        framecue.build_package(carousel_source, cards, carousel_dir)
        carousel = framecue.read_json(carousel_dir / "review_package.json")
        self.assertEqual(carousel["workflow"]["kind"], "image_carousel")
        self.assertEqual(len(carousel["cues"]), 2)
        self.assertTrue((carousel_dir / carousel["media"]["carousel"]["contact_sheet"]).is_file())

        article = self.output / "article.md"
        article.write_text(
            "---\nepisode: Ep01\n---\n\n# 黃仁勳在晶圓上寫了一句話\n\n請多做一點。\n\n- 算力\n- 電力\n\n---\n\n## 編輯備註（Gate B 自審）\n\n- 這裡不是審閱目標。\n",
            encoding="utf-8",
        )
        markdown_source = framecue.markdown_source(article, "fixture-markdown")
        markdown_dir = self.output / "markdown"
        framecue.build_package(markdown_source, article.parent, markdown_dir)
        markdown = framecue.read_json(markdown_dir / "review_package.json")
        self.assertEqual([cue["markdown"]["kind"] for cue in markdown["cues"]], ["heading", "paragraph", "list"])
        self.assertIn("episode: Ep01", markdown["media"]["markdown"]["frontmatter"])
        self.assertIn("編輯備註", markdown["media"]["markdown"]["editorial_notes"])
        self.assertNotIn("編輯備註", "\n".join(cue["text"] for cue in markdown["cues"]))

    def test_result_actions_are_restricted_by_workflow(self):
        _, package, _ = self.build_fixture("redraw")
        result = framecue.default_result(package, approved=True)
        result["cues"][0]["action"] = "replace_asset"
        with self.assertRaisesRegex(framecue.FrameCueError, "invalid for redraw"):
            framecue.validate_result(result, package, require_approved=True)

        package["workflow"]["kind"] = "image_carousel"
        package["content_checksum"] = framecue.package_checksum(package)
        result["package_checksum"] = package["content_checksum"]
        self.assertEqual(framecue.validate_result(result, package, require_approved=True)["status"], "approved")

        result["cues"][0]["action"] = "needs_source"
        with self.assertRaisesRegex(framecue.FrameCueError, "invalid for image_carousel"):
            framecue.validate_result(result, package, require_approved=True)

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

    def test_source_video_is_materialized(self):
        fixture = FIXTURES / "basic"
        source_video = self.output / "source.mp4"
        source_video.write_bytes(b"framecue-video-fixture")
        source = framecue.read_json(fixture / "package.source.json")
        source["media"] = {"video": {"source": str(source_video)}}
        out_dir = self.output / "video"

        framecue.build_package(source, fixture, out_dir)

        package = framecue.read_json(out_dir / "review_package.json")
        self.assertEqual(package["media"]["video"]["src"], "assets/video/source.mp4")
        self.assertEqual((out_dir / package["media"]["video"]["src"]).read_bytes(), source_video.read_bytes())
        captions = out_dir / package["media"]["video"]["captions"]
        self.assertTrue(captions.read_text(encoding="utf-8").startswith("WEBVTT\n\n"))
        framecue.validate_package(package, out_dir)

    def test_paper_edit_builds_multi_source_beats_and_requires_decisions(self):
        source_root = self.output / "paper-edit-input"
        source_root.mkdir()
        for name in ("beat-1.svg", "beat-2.svg"):
            (source_root / name).write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="100%" height="100%" fill="#123"/></svg>',
                encoding="utf-8",
            )
        (source_root / "lesson-a.mp4").write_bytes(b"paper-edit-video-a")
        (source_root / "lesson-b.mp4").write_bytes(b"paper-edit-video-b")
        source = {
            "review_id": "paper-edit-fixture",
            "revision": "r1",
            "workflow": {"kind": "paper_edit", "label": "Paper edit fixture"},
            "media": {"paper_edit": {"sources": [
                {"id": "lesson-a", "label": "Lesson A", "source": "lesson-a.mp4"},
                {"id": "lesson-b", "label": "Lesson B", "source": "lesson-b.mp4"},
            ]}},
            "scenes": [
                {"id": "s0001", "start_ms": 0, "end_ms": 3000, "image": "beat-1.svg"},
                {"id": "s0002", "start_ms": 3000, "end_ms": 5000, "image": "beat-2.svg"},
            ],
            "cues": [
                {
                    "id": "c0001", "start_ms": 0, "end_ms": 3000, "scene_id": "s0001", "text": "Beat one", "speech_text": "Beat one",
                    "beat": {
                        "teaching_purpose": "建立問題", "spoken_content_summary": "說明痛點", "duration_ms": 3000,
                        "narration_spine": {"source_id": "lesson-a", "start_ms": 1000, "end_ms": 2500},
                        "source_ranges": [
                            {"source_id": "lesson-a", "start_ms": 1000, "end_ms": 2500, "availability": "existing", "role": "evidence"},
                            {"source_id": "lesson-b", "start_ms": 0, "end_ms": 1200, "availability": "existing", "role": "reference-only"},
                        ],
                        "visual_slots": [
                            {"id": "ui", "description": "產品畫面", "availability": "existing", "role": "evidence", "source_id": "lesson-a", "start_ms": 1000, "end_ms": 2500},
                            {"id": "diagram", "description": "補充圖", "availability": "planned", "role": "illustration", "production_method": "動態圖卡", "duration": "3 秒", "acceptance": "手機上可讀"},
                        ],
                        "unresolved_work": ["補拍圖示"],
                    },
                },
                {
                    "id": "c0002", "start_ms": 3000, "end_ms": 5000, "scene_id": "s0002", "text": "Beat two", "speech_text": "Beat two",
                    "beat": {
                        "teaching_purpose": "交代下一步", "spoken_content_summary": "提出行動", "duration_ms": 2000,
                        "narration_spine": {"text": "以清楚的行動收尾", "production_method": "補錄旁白", "duration": "2 秒", "acceptance": "行動句清楚"},
                        "source_ranges": [],
                        "visual_slots": [{"id": "placeholder", "availability": "gap", "role": "illustration"}],
                        "unresolved_work": ["建立 placeholder"],
                    },
                },
            ],
            "blocks": [],
        }
        out_dir = self.output / "paper-edit"
        framecue.build_package(source, source_root, out_dir)
        package = framecue.read_json(out_dir / "review_package.json")
        self.assertEqual(package["workflow"]["kind"], "paper_edit")
        self.assertEqual([source["id"] for source in package["media"]["paper_edit"]["sources"]], ["lesson-a", "lesson-b"])
        self.assertTrue((out_dir / package["media"]["paper_edit"]["sources"][0]["src"]).is_file())
        self.assertNotIn("source", package["media"]["paper_edit"]["sources"][0])

        result = framecue.default_result(package, approved=True)
        self.assertEqual([cue["decision"] for cue in result["cues"]], ["approve", "approve"])
        self.assertEqual(framecue.validate_result(result, package, require_approved=True)["status"], "approved")

        result = framecue.default_result(package)
        result["cues"][0].update({"decision": "revise", "action": "rewrite"})
        with self.assertRaisesRegex(framecue.FrameCueError, "require a note"):
            framecue.validate_result(result, package)

        invalid = json.loads(json.dumps(package))
        invalid["cues"][0]["beat"]["visual_slots"][0]["availability"] = "planned"
        invalid["content_checksum"] = framecue.package_checksum(invalid)
        with self.assertRaisesRegex(framecue.FrameCueError, "evidence requires existing"):
            framecue.validate_package(invalid, out_dir)

        invalid = json.loads(json.dumps(package))
        invalid["cues"][0]["beat"]["visual_slots"][0] = {"availability": "existing", "role": "illustration"}
        invalid["content_checksum"] = framecue.package_checksum(invalid)
        with self.assertRaisesRegex(framecue.FrameCueError, "existing media needs"):
            framecue.validate_package(invalid, out_dir)

        invalid = json.loads(json.dumps(package))
        invalid["cues"][0]["beat"]["visual_slots"][1].update({"source_id": "lesson-a", "start_ms": 1000, "end_ms": 2500})
        invalid["content_checksum"] = framecue.package_checksum(invalid)
        with self.assertRaisesRegex(framecue.FrameCueError, "planned or gap media"):
            framecue.validate_package(invalid, out_dir)

        invalid = json.loads(json.dumps(package))
        invalid["cues"][0]["beat"]["visual_slots"][1]["acceptance"] = " "
        invalid["content_checksum"] = framecue.package_checksum(invalid)
        with self.assertRaisesRegex(framecue.FrameCueError, "acceptance must not be empty"):
            framecue.validate_package(invalid, out_dir)

        invalid = json.loads(json.dumps(package))
        invalid["cues"][1]["beat"]["narration_spine"]["acceptance"] = " "
        invalid["content_checksum"] = framecue.package_checksum(invalid)
        with self.assertRaisesRegex(framecue.FrameCueError, "narration_spine.acceptance must not be empty"):
            framecue.validate_package(invalid, out_dir)

        all_planned = {
            "review_id": "paper-edit-planned", "revision": "r1", "workflow": {"kind": "paper_edit"},
            "media": {"paper_edit": {"sources": []}},
            "scenes": [{"id": "s0001", "start_ms": 0, "end_ms": 1000, "image": "beat-1.svg"}],
            "cues": [{
                "id": "c0001", "start_ms": 0, "end_ms": 1000, "scene_id": "s0001", "text": "Planned Beat", "speech_text": "Planned Beat",
                "beat": {
                    "teaching_purpose": "規劃下一步", "spoken_content_summary": "先保留 placeholder", "narration_spine": "先說明目標",
                    "source_ranges": [], "visual_slots": [{"id": "placeholder", "availability": "planned", "role": "illustration"}],
                    "duration_ms": 1000, "unresolved_work": ["建立動畫"],
                },
            }],
            "blocks": [],
        }
        planned_dir = self.output / "paper-edit-planned"
        framecue.build_package(all_planned, source_root, planned_dir)
        planned_package = framecue.read_json(planned_dir / "review_package.json")
        self.assertEqual(planned_package["media"]["paper_edit"]["sources"], [])

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
