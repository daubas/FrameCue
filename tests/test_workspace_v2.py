import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRAMECUE = ROOT / "framecue.py"
FIXTURE = ROOT / "tests" / "fixtures" / "basic" / "package.source.json"
sys.path.insert(0, str(ROOT))
import framecue


def run_cli(*arguments):
    return subprocess.run(
        [sys.executable, str(FRAMECUE), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


class WorkspaceV2Tests(unittest.TestCase):
    def _workspace(self, root):
        bundle = root / "bundle"
        database = root / "workspace.sqlite3"
        run_cli("build", "--input", str(FIXTURE), "--out-dir", str(bundle))
        package = json.loads((bundle / "review_package.json").read_text(encoding="utf-8"))
        run_cli(
            "workspace-import",
            "--database",
            str(database),
            "--package",
            str(bundle / "review_package.json"),
            "--timing-profile",
            "synchronous_dub",
        )
        return database, package

    def _apply(self, root, database, review_id, name, operation):
        path = root / name
        path.write_text(json.dumps(operation, ensure_ascii=False), encoding="utf-8")
        return run_cli(
            "workspace-apply",
            "--database",
            str(database),
            "--review-id",
            review_id,
            "--operation",
            str(path),
        )

    def _complete(self, database, review_id, draft_version):
        return run_cli(
            "workspace-complete",
            "--database",
            str(database),
            "--review-id",
            review_id,
            "--draft-version",
            str(draft_version),
        )

    def test_edit_updates_the_complete_draft_and_stale_version_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-edit-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            changed = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "edit.json",
                {
                    "kind": "edit",
                    "draft_version": 0,
                    "cue_id": "c0001",
                    "display_text": "OpenClaw 讓人工審稿保持明確",
                },
            ).stdout)

            self.assertEqual(changed["stage"], "content_review")
            self.assertEqual(changed["draft_version"], 1)
            self.assertEqual(changed["document"]["schema"], "framecue_subtitle_document_v2")
            self.assertEqual(changed["document"]["cues"][0]["display_text"], "OpenClaw 讓人工審稿保持明確")
            self.assertEqual(changed["document"]["cues"][0]["speech_text"], "OpenClaw 讓人工審稿保持明確")
            self.assertIn("OpenClaw 讓人工審稿保持明確", changed["document"]["blocks"][0]["target_text"])

            with self.assertRaises(subprocess.CalledProcessError):
                self._apply(
                    root,
                    database,
                    package["review_id"],
                    "stale.json",
                    {
                        "kind": "edit",
                        "draft_version": 0,
                        "cue_id": "c0002",
                        "display_text": "這筆更新不應寫入",
                    },
                )

            current = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "current.json",
                {
                    "kind": "edit",
                    "draft_version": 1,
                    "cue_id": "c0002",
                    "display_text": "審稿者確認的是完整內容版本",
                },
            ).stdout)
            self.assertEqual(current["draft_version"], 2)
            self.assertEqual(current["document"]["cues"][0]["display_text"], "OpenClaw 讓人工審稿保持明確")
            self.assertEqual(current["document"]["cues"][1]["display_text"], "審稿者確認的是完整內容版本")

    def test_split_creates_provisional_cues_with_parent_lineage(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-split-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            with self.assertRaises(subprocess.CalledProcessError):
                self._apply(
                    root,
                    database,
                    package["review_id"],
                    "trusted-words.json",
                    {
                        "kind": "split",
                        "draft_version": 0,
                        "cue_id": "c0001",
                        "cursor": 10,
                        "word_timestamps": [],
                    },
                )
            split = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "split.json",
                {
                    "kind": "split",
                    "draft_version": 0,
                    "cue_id": "c0001",
                    "cursor": 10,
                },
            ).stdout)

            self.assertEqual(split["draft_version"], 1)
            first, second = split["document"]["cues"][:2]
            self.assertNotEqual(first["id"], "c0001")
            self.assertNotEqual(second["id"], "c0001")
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(first["origin_cue_ids"], ["c0001"])
            self.assertEqual(second["origin_cue_ids"], ["c0001"])
            self.assertEqual(first["lineage"], {"operation": "split", "parent_cue_ids": ["c0001"]})
            self.assertEqual(second["lineage"], {"operation": "split", "parent_cue_ids": ["c0001"]})
            self.assertEqual(first["source_start_ms"], 0)
            self.assertEqual(second["source_end_ms"], 1500)
            self.assertEqual(first["source_end_ms"], second["source_start_ms"])
            self.assertEqual(first["timing_state"], "provisional")
            self.assertEqual(second["timing_state"], "provisional")
            self.assertEqual(first["speech_text"], first["display_text"])
            self.assertEqual(second["speech_text"], second["display_text"])
            self.assertEqual(split["document"]["blocks"][0]["cue_ids"][:2], [first["id"], second["id"]])

    def test_split_preserves_separate_speech_text_as_two_parts(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-split-speech-") as temp:
            root = Path(temp)
            bundle = root / "bundle"
            database = root / "workspace.sqlite3"
            run_cli("build", "--input", str(FIXTURE), "--out-dir", str(bundle))
            package_path = bundle / "review_package.json"
            package = json.loads(package_path.read_text(encoding="utf-8"))
            package["cues"][0]["speech_text"] = "OpenClaw 仍要人工確認流程"
            package["content_checksum"] = framecue.package_checksum(package)
            package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
            run_cli(
                "workspace-import",
                "--database",
                str(database),
                "--package",
                str(package_path),
                "--timing-profile",
                "synchronous_dub",
            )
            split = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "split.json",
                {"kind": "split", "draft_version": 0, "cue_id": "c0001", "cursor": 10},
            ).stdout)
            first, second = split["document"]["cues"][:2]
            self.assertFalse(first["speech_linked"])
            self.assertFalse(second["speech_linked"])
            self.assertEqual((first["speech_text"], second["speech_text"]), ("OpenClaw", "仍要人工確認流程"))

    def test_merge_replaces_same_block_neighbours_with_combined_lineage(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-merge-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            split = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "split.json",
                {"kind": "split", "draft_version": 0, "cue_id": "c0001", "cursor": 10},
            ).stdout)
            left, right = split["document"]["cues"][:2]
            self.assertEqual(
                " ".join((left["source_text"], right["source_text"])),
                package["cues"][0]["original_text"],
            )
            merged = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "merge.json",
                {
                    "kind": "merge",
                    "draft_version": 1,
                    "cue_id": left["id"],
                    "adjacent_cue_id": right["id"],
                },
            ).stdout)

            cue = merged["document"]["cues"][0]
            self.assertEqual(merged["draft_version"], 2)
            self.assertNotIn(cue["id"], {left["id"], right["id"]})
            self.assertEqual(cue["origin_cue_ids"], ["c0001"])
            self.assertEqual(cue["lineage"], {
                "operation": "merge",
                "parent_cue_ids": [left["id"], right["id"]],
            })
            self.assertEqual((cue["source_start_ms"], cue["source_end_ms"]), (0, 1500))
            self.assertEqual(cue["source_text"], package["cues"][0]["original_text"])
            self.assertEqual(cue["timing_state"], "provisional")
            self.assertEqual(merged["document"]["blocks"][0]["cue_ids"][0], cue["id"])

    def test_completion_tracks_edits_through_split_and_merge(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-lineage-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            self._apply(root, database, package["review_id"], "flag.json", {
                "kind": "flag", "draft_version": 0, "cue_id": "c0001",
                "categories": ["translation"], "author": "lead",
            })
            self._apply(root, database, package["review_id"], "edit.json", {
                "kind": "edit", "draft_version": 1, "cue_id": "c0001",
                "display_text": "OpenClaw 審查流程已更新",
            })
            split = json.loads(self._apply(root, database, package["review_id"], "split.json", {
                "kind": "split", "draft_version": 2, "cue_id": "c0001", "cursor": 8,
            }).stdout)
            left, right = split["document"]["cues"][:2]
            merged = json.loads(self._apply(root, database, package["review_id"], "merge.json", {
                "kind": "merge", "draft_version": 3, "cue_id": left["id"],
                "adjacent_cue_id": right["id"],
            }).stdout)

            completed = json.loads(self._complete(database, package["review_id"], 4).stdout)
            self.assertEqual(completed["stage"], "content_agent_review_pending")
            work_order_path = root / "work-order.json"
            run_cli(
                "work-pull", "--database", str(database), "--review-id", package["review_id"],
                "--out", str(work_order_path),
            )
            work_order = json.loads(work_order_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {cue_id for target in work_order["targets"] for cue_id in target["cue_ids"]},
                {merged["document"]["cues"][0]["id"]},
            )

    def test_merge_rejects_cues_from_different_semantic_blocks(self):
        package = json.loads(FIXTURE.read_text(encoding="utf-8"))
        package["content_checksum"] = framecue.package_checksum(package)
        document = framecue.workspace_draft_document(package, "synchronous_dub")
        first_block = document["blocks"][0]
        first_block["cue_ids"] = ["c0001"]
        document["blocks"].append({**first_block, "id": "b0002", "cue_ids": ["c0002"]})
        document["cues"][0]["block_id"] = "b0001"
        document["cues"][1]["block_id"] = "b0002"

        with self.assertRaises(framecue.FrameCueError):
            framecue.apply_draft_merge(document, {
                "kind": "merge",
                "cue_id": "c0001",
                "adjacent_cue_id": "c0002",
            })

    def test_duplicate_flags_merge_by_range_and_category_without_losing_authors(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-flag-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            first = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "flag-one.json",
                {
                    "kind": "flag",
                    "draft_version": 0,
                    "cue_ids": ["c0001", "c0002"],
                    "categories": ["translation", "terminology"],
                    "author": "lead",
                    "note": "術語需要確認",
                },
            ).stdout)
            range_ids = {issue["category"]: issue["range_id"] for issue in first["issues"]}
            self.assertEqual(first["draft_version"], 1)
            self.assertEqual(first["direct_edit_count"], 0)
            self.assertEqual(set(range_ids), {"translation", "terminology"})

            repeated = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "flag-two.json",
                {
                    "kind": "flag",
                    "draft_version": 1,
                    "cue_ids": ["c0001", "c0002"],
                    "categories": ["translation", "terminology"],
                    "author": "peer",
                    "note": "請由 agent 一併處理",
                },
            ).stdout)
            self.assertEqual(repeated["draft_version"], 2)
            self.assertEqual(len(repeated["issues"]), 2)
            for issue in repeated["issues"]:
                self.assertEqual(issue["range_id"], range_ids[issue["category"]])
                self.assertEqual(issue["authors"], ["lead", "peer"])
                self.assertEqual(issue["notes"], ["術語需要確認", "請由 agent 一併處理"])

            removed = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "unflag.json",
                {
                    "kind": "flag",
                    "draft_version": 2,
                    "cue_ids": ["c0001", "c0002"],
                    "categories": ["translation"],
                    "author": "lead",
                    "enabled": False,
                },
            ).stdout)
            self.assertEqual(removed["draft_version"], 3)
            translation = next(issue for issue in removed["issues"] if issue["category"] == "translation")
            self.assertEqual(translation["authors"], ["peer"])
            self.assertEqual({issue["category"] for issue in removed["issues"]}, {"translation", "terminology"})

            cleared = json.loads(self._apply(
                root,
                database,
                package["review_id"],
                "unflag-last-author.json",
                {
                    "kind": "flag",
                    "draft_version": 3,
                    "cue_ids": ["c0001", "c0002"],
                    "categories": ["translation"],
                    "author": "peer",
                    "enabled": False,
                },
            ).stdout)
            self.assertEqual([issue["category"] for issue in cleared["issues"]], ["terminology"])

    def test_clean_completion_creates_one_content_revision_and_voice_order(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-clean-complete-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            completed = json.loads(self._complete(database, package["review_id"], 0).stdout)
            self.assertEqual(completed["stage"], "voice_realization_pending")
            self.assertEqual(completed["operation"], "realize_voice_timeline")
            self.assertEqual(completed["draft_version"], 0)

            work_order_path = root / "work-order.json"
            run_cli(
                "work-pull",
                "--database",
                str(database),
                "--review-id",
                package["review_id"],
                "--out",
                str(work_order_path),
            )
            work_order = json.loads(work_order_path.read_text(encoding="utf-8"))
            self.assertEqual(work_order["schema"], "framecue_work_order_v2")
            self.assertEqual(work_order["operation"], "realize_voice_timeline")
            self.assertEqual(work_order["base_draft_version"], 0)
            self.assertEqual(work_order["document"]["revision_kind"], "content")

            repeated = json.loads(self._complete(database, package["review_id"], 0).stdout)
            self.assertEqual(repeated, completed)

    def test_changed_round_freezes_snapshot_and_creates_only_one_correction_order(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-correction-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            self._apply(
                root,
                database,
                package["review_id"],
                "edit.json",
                {
                    "kind": "edit",
                    "draft_version": 0,
                    "cue_id": "c0001",
                    "display_text": "OpenClaw 保留人工審稿",
                },
            )
            self._apply(
                root,
                database,
                package["review_id"],
                "flag.json",
                {
                    "kind": "flag",
                    "draft_version": 1,
                    "cue_id": "c0002",
                    "categories": ["translation"],
                    "author": "lead",
                    "note": "請調整語氣",
                },
            )
            completed = json.loads(self._complete(database, package["review_id"], 2).stdout)
            self.assertEqual(completed["stage"], "content_agent_review_pending")
            self.assertEqual(completed["operation"], "content_correction_review")

            work_order_path = root / "work-order.json"
            run_cli(
                "work-pull",
                "--database",
                str(database),
                "--review-id",
                package["review_id"],
                "--out",
                str(work_order_path),
            )
            work_order = json.loads(work_order_path.read_text(encoding="utf-8"))
            self.assertEqual(work_order["schema"], "framecue_work_order_v2")
            self.assertEqual(work_order["operation"], "content_correction_review")
            self.assertEqual(work_order["base_draft_version"], 2)
            self.assertEqual(work_order["document"]["revision_kind"], "draft_snapshot")
            self.assertEqual(work_order["document"]["cues"][0]["display_text"], "OpenClaw 保留人工審稿")
            self.assertEqual({cue_id for target in work_order["targets"] for cue_id in target["cue_ids"]}, {"c0001", "c0002"})
            self.assertTrue(all(target["range_id"].startswith("range-") for target in work_order["targets"]))

            with self.assertRaises(subprocess.CalledProcessError):
                self._apply(
                    root,
                    database,
                    package["review_id"],
                    "blocked-edit.json",
                    {
                        "kind": "edit",
                        "draft_version": 2,
                        "cue_id": "c0001",
                        "display_text": "這筆修改必須被拒絕",
                    },
                )
            repeated = json.loads(self._complete(database, package["review_id"], 2).stdout)
            self.assertEqual(repeated, completed)

    def test_edit_and_flag_on_one_range_produce_one_authoritative_target(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-one-target-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            self._apply(
                root,
                database,
                package["review_id"],
                "edit.json",
                {
                    "kind": "edit",
                    "draft_version": 0,
                    "cue_id": "c0001",
                    "display_text": "OpenClaw 保留人工審稿",
                },
            )
            self._apply(
                root,
                database,
                package["review_id"],
                "flag.json",
                {
                    "kind": "flag",
                    "draft_version": 1,
                    "cue_id": "c0001",
                    "categories": ["translation"],
                    "author": "lead",
                    "note": "語氣仍需調整",
                },
            )
            self._complete(database, package["review_id"], 2)
            work_order_path = root / "work-order.json"
            run_cli(
                "work-pull",
                "--database",
                str(database),
                "--review-id",
                package["review_id"],
                "--out",
                str(work_order_path),
            )
            targets = json.loads(work_order_path.read_text(encoding="utf-8"))["targets"]
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]["cue_ids"], ["c0001"])
            self.assertTrue(targets[0]["context"]["direct_edit"])
            self.assertEqual(targets[0]["context"]["categories"], ["translation"])
            self.assertEqual(targets[0]["context"]["notes"], ["語氣仍需調整"])


if __name__ == "__main__":
    unittest.main()
