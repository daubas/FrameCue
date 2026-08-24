import copy
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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

    def _workspace_with_adjacent_blocks(self, root):
        bundle = root / "bundle"
        database = root / "workspace.sqlite3"
        run_cli("build", "--input", str(FIXTURE), "--out-dir", str(bundle))
        package_path = bundle / "review_package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["blocks"] = [{
            "id": f"b{index:04d}",
            "cue_ids": [cue["id"]],
            "start_ms": cue["start_ms"],
            "end_ms": cue["end_ms"],
            "budget_ms": cue["end_ms"] - cue["start_ms"],
            "source_text": cue["original_text"],
            "target_text": cue["text"],
            "speech_text": cue["speech_text"],
            "legacy_block_id": f"legacy-b{index:04d}",
            "legacy_source_cue_ids": [index],
        } for index, cue in enumerate(package["cues"], 1)]
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
        return database, package

    def _workspace_with_block_layout(self, root, block_cue_ids):
        bundle = root / "bundle"
        database = root / "workspace.sqlite3"
        run_cli("build", "--input", str(FIXTURE), "--out-dir", str(bundle))
        package_path = bundle / "review_package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        cue_ids = [cue_id for group in block_cue_ids for cue_id in group]
        package["cues"] = [{
            "id": cue_id,
            "start_ms": index * 750,
            "end_ms": (index + 1) * 750,
            "scene_id": "s0001",
            "original_text": f"Source Cue {index + 1}.",
            "text": f"字幕 {index + 1}",
            "speech_text": f"字幕 {index + 1}。",
        } for index, cue_id in enumerate(cue_ids)]
        cue_by_id = {cue["id"]: cue for cue in package["cues"]}
        package["blocks"] = [{
            "id": f"b{index:04d}",
            "cue_ids": cue_ids,
            "start_ms": cue_by_id[cue_ids[0]]["start_ms"],
            "end_ms": cue_by_id[cue_ids[-1]]["end_ms"],
            "budget_ms": cue_by_id[cue_ids[-1]]["end_ms"] - cue_by_id[cue_ids[0]]["start_ms"],
            "source_text": " ".join(cue_by_id[cue_id]["original_text"] for cue_id in cue_ids),
            "target_text": " ".join(cue_by_id[cue_id]["text"] for cue_id in cue_ids),
            "speech_text": " ".join(cue_by_id[cue_id]["speech_text"] for cue_id in cue_ids),
            "legacy_block_id": f"legacy-b{index:04d}",
            "provenance": {"source": f"block-{index}"},
        } for index, cue_ids in enumerate(block_cue_ids, 1)]
        package["content_checksum"] = framecue.package_checksum(package)
        package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        run_cli(
            "workspace-import",
            "--database", str(database),
            "--package", str(package_path),
            "--timing-profile", "synchronous_dub",
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

    def test_merge_across_adjacent_semantic_blocks_recomposes_content_and_lineage(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-cross-block-merge-") as temp:
            root = Path(temp)
            database, package = self._workspace_with_adjacent_blocks(root)
            merged = json.loads(self._apply(root, database, package["review_id"], "merge.json", {
                "kind": "merge",
                "draft_version": 0,
                "cue_id": "c0001",
                "adjacent_cue_id": "c0002",
            }).stdout)

            document = merged["document"]
            cue = document["cues"][0]
            block = document["blocks"][0]
            self.assertEqual(merged["stage"], "content_review")
            self.assertEqual(merged["draft_version"], 1)
            self.assertEqual(len(document["cues"]), 1)
            self.assertEqual(len(document["blocks"]), 1)
            self.assertEqual(cue["lineage"], {
                "operation": "merge",
                "parent_cue_ids": ["c0001", "c0002"],
            })
            self.assertEqual(cue["origin_cue_ids"], ["c0001", "c0002"])
            self.assertEqual((cue["source_start_ms"], cue["source_end_ms"]), (0, 3000))
            self.assertEqual(cue["source_text"], " ".join(source["original_text"] for source in package["cues"]))
            self.assertNotIn(block["id"], {"b0001", "b0002"})
            self.assertNotIn("legacy_block_id", block)
            self.assertNotIn("legacy_source_cue_ids", block)
            self.assertEqual(block["cue_ids"], [cue["id"]])
            self.assertEqual(block["lineage"]["operation"], "merge")
            self.assertEqual(block["lineage"]["parent_block_ids"], ["b0001", "b0002"])
            self.assertEqual(block["lineage"]["parent_blocks"], [{
                "id": "b0001",
                "lineage": None,
                "extensions": {"legacy_block_id": "legacy-b0001", "legacy_source_cue_ids": [1]},
            }, {
                "id": "b0002",
                "lineage": None,
                "extensions": {"legacy_block_id": "legacy-b0002", "legacy_source_cue_ids": [2]},
            }])
            self.assertEqual(cue["block_id"], block["id"])
            self.assertEqual((block["start_ms"], block["end_ms"], block["budget_ms"]), (0, 3000, 3000))
            self.assertEqual(block["source_text"], cue["source_text"])
            self.assertEqual(block["target_text"], cue["display_text"])
            self.assertEqual(block["speech_text"], cue["speech_text"])
            self.assertEqual(document["checksum"], framecue.document_checksum(document))
            self.assertEqual(merged["direct_edit_count"], 1)
            connection = framecue.open_workspace_database(database)
            try:
                draft = framecue.draft_row(connection, framecue.workspace_row(connection, package["review_id"]))
            finally:
                connection.close()
            change = draft["direct_changes"][0]
            self.assertEqual(change["parent_cue_ids"], ["c0001", "c0002"])
            self.assertEqual(change["parent_block_ids"], ["b0001", "b0002"])
            self.assertEqual(change["result_block_ids"], [block["id"]])
            self.assertRegex(change["before_checksum"], r"^[0-9a-f]{64}$")
            self.assertRegex(change["after_checksum"], r"^[0-9a-f]{64}$")

    def test_merge_accepts_reverse_argument_order_for_merge_previous(self):
        package = json.loads(FIXTURE.read_text(encoding="utf-8"))
        package["content_checksum"] = framecue.package_checksum(package)
        document = framecue.workspace_draft_document(package, "synchronous_dub")

        result = framecue.apply_draft_merge(document, {
            "kind": "merge",
            "cue_id": "c0002",
            "adjacent_cue_id": "c0001",
        })

        merged = document["cues"][0]
        self.assertEqual(result["kind"], "merge")
        self.assertEqual(result["cue_ids"], [merged["id"]])
        self.assertEqual(result["parent_cue_ids"], ["c0001", "c0002"])
        self.assertEqual(result["parent_block_ids"], [document["blocks"][0]["id"]])
        self.assertEqual(result["result_block_ids"], [document["blocks"][0]["id"]])
        self.assertRegex(result["before_checksum"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["after_checksum"], r"^[0-9a-f]{64}$")
        self.assertEqual(merged["lineage"]["parent_cue_ids"], ["c0001", "c0002"])
        self.assertEqual(document["blocks"][0]["cue_ids"], [merged["id"]])
        self.assertEqual(document["checksum"], framecue.document_checksum(document))

    def test_cross_block_merge_audit_hashes_every_changed_sibling_cue(self):
        package = json.loads(FIXTURE.read_text(encoding="utf-8"))
        package["content_checksum"] = framecue.package_checksum(package)
        document = framecue.workspace_draft_document(package, "synchronous_dub")
        first_children = framecue.apply_draft_split(document, {
            "kind": "split", "cue_id": "c0001", "cursor": 4,
        })["cue_ids"]
        second_children = framecue.apply_draft_split(document, {
            "kind": "split", "cue_id": "c0002", "cursor": 4,
        })["cue_ids"]
        parent_blocks = copy.deepcopy(document["blocks"])
        parent_cue_ids = {cue_id for block in parent_blocks for cue_id in block["cue_ids"]}
        expected_before = hashlib.sha256(framecue.canonical_json({
            "cues": [copy.deepcopy(cue) for cue in document["cues"] if cue["id"] in parent_cue_ids],
            "blocks": parent_blocks,
        }).encode("utf-8")).hexdigest()

        result = framecue.apply_draft_merge(document, {
            "kind": "merge",
            "cue_id": first_children[-1],
            "adjacent_cue_id": second_children[0],
        })

        result_block = document["blocks"][0]
        result_cue_ids = set(result_block["cue_ids"])
        expected_after = hashlib.sha256(framecue.canonical_json({
            "cues": [cue for cue in document["cues"] if cue["id"] in result_cue_ids],
            "blocks": [result_block],
        }).encode("utf-8")).hexdigest()
        self.assertEqual(len(parent_cue_ids), 4)
        self.assertEqual(len(result_cue_ids), 3)
        self.assertEqual(result["before_checksum"], expected_before)
        self.assertEqual(result["after_checksum"], expected_after)

    def test_block_merge_preserves_multi_cue_projection_audit_and_completion_target(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-block-merge-") as temp:
            root = Path(temp)
            database, package = self._workspace_with_block_layout(root, [
                ["c0001", "c0002"], ["c0003", "c0004"],
            ])
            before = framecue.workspace_draft_document(package, "synchronous_dub")
            merged = json.loads(self._apply(root, database, package["review_id"], "block-merge.json", {
                "kind": "block_merge",
                "draft_version": 0,
                "block_id": "b0001",
                "adjacent_block_id": "b0002",
            }).stdout)

            document = merged["document"]
            block = document["blocks"][0]
            cue_ids = ["c0001", "c0002", "c0003", "c0004"]
            self.assertEqual(merged["draft_version"], 1)
            self.assertEqual([cue["id"] for cue in document["cues"]], cue_ids)
            self.assertEqual(block["cue_ids"], cue_ids)
            self.assertNotIn(block["id"], {"b0001", "b0002"})
            self.assertTrue(all(cue["block_id"] == block["id"] for cue in document["cues"]))
            self.assertEqual(block["target_text"], "字幕 1 字幕 2 字幕 3 字幕 4")
            self.assertEqual(block["speech_text"], "字幕 1。 字幕 2。 字幕 3。 字幕 4。")
            self.assertEqual(block["lineage"], {
                "operation": "block_merge",
                "parent_block_ids": ["b0001", "b0002"],
                "parent_blocks": [{
                    "id": "b0001", "lineage": None,
                    "extensions": {"legacy_block_id": "legacy-b0001", "provenance": {"source": "block-1"}},
                }, {
                    "id": "b0002", "lineage": None,
                    "extensions": {"legacy_block_id": "legacy-b0002", "provenance": {"source": "block-2"}},
                }],
            })
            self.assertEqual(document["checksum"], framecue.document_checksum(document))
            connection = framecue.open_workspace_database(database)
            try:
                draft = framecue.draft_row(connection, framecue.workspace_row(connection, package["review_id"]))
            finally:
                connection.close()
            change = draft["direct_changes"][0]
            self.assertEqual(change["kind"], "block_merge")
            self.assertEqual(change["scope"], "block")
            self.assertEqual(change["cue_ids"], cue_ids)
            self.assertEqual(change["parent_block_ids"], ["b0001", "b0002"])
            self.assertEqual(change["result_block_ids"], [block["id"]])
            self.assertEqual(change["before_checksum"], framecue.draft_projection_checksum(before, cue_ids, before["blocks"]))
            self.assertEqual(change["after_checksum"], framecue.draft_projection_checksum(document, cue_ids, [block]))

            completed = json.loads(self._complete(database, package["review_id"], 1).stdout)
            self.assertEqual(completed["operation"], "realize_voice_timeline")
            work_order_path = root / "work-order.json"
            run_cli("work-pull", "--database", str(database), "--review-id", package["review_id"], "--out", str(work_order_path))
            target = json.loads(work_order_path.read_text(encoding="utf-8"))["targets"][0]
            self.assertEqual(target["cue_ids"], cue_ids)
            self.assertEqual(target["block_ids"], [block["id"]])
            self.assertFalse(target["context"]["direct_edit"])
            self.assertNotIn("direct_changes", target["context"])

    def test_block_split_keeps_cues_and_records_parent_projection(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-block-split-") as temp:
            root = Path(temp)
            database, package = self._workspace_with_block_layout(root, [["c0001", "c0002", "c0003", "c0004"]])
            before = framecue.workspace_draft_document(package, "synchronous_dub")
            split = json.loads(self._apply(root, database, package["review_id"], "block-split.json", {
                "kind": "block_split",
                "draft_version": 0,
                "block_id": "b0001",
                "cue_id": "c0003",
            }).stdout)

            document = split["document"]
            left, right = document["blocks"]
            cue_ids = ["c0001", "c0002", "c0003", "c0004"]
            self.assertEqual([cue["id"] for cue in document["cues"]], cue_ids)
            self.assertEqual(left["cue_ids"], ["c0001", "c0002"])
            self.assertEqual(right["cue_ids"], ["c0003", "c0004"])
            self.assertNotIn(left["id"], {"b0001", right["id"]})
            self.assertNotEqual(right["id"], "b0001")
            self.assertEqual([cue["block_id"] for cue in document["cues"]], [left["id"], left["id"], right["id"], right["id"]])
            for block, child_ids in ((left, ["c0001", "c0002"]), (right, ["c0003", "c0004"])):
                self.assertEqual(block["lineage"], {
                    "operation": "block_split",
                    "parent_block_ids": ["b0001"],
                    "parent_blocks": [{
                        "id": "b0001", "lineage": None,
                        "extensions": {"legacy_block_id": "legacy-b0001", "provenance": {"source": "block-1"}},
                    }],
                    "split_at_cue_id": "c0003",
                })
                self.assertEqual(block["cue_ids"], child_ids)
            self.assertEqual(document["checksum"], framecue.document_checksum(document))
            connection = framecue.open_workspace_database(database)
            try:
                draft = framecue.draft_row(connection, framecue.workspace_row(connection, package["review_id"]))
            finally:
                connection.close()
            change = draft["direct_changes"][0]
            self.assertEqual(change["kind"], "block_split")
            self.assertEqual(change["cue_ids"], cue_ids)
            self.assertEqual(change["parent_block_ids"], ["b0001"])
            self.assertEqual(change["result_block_ids"], [left["id"], right["id"]])
            self.assertEqual(change["before_checksum"], framecue.draft_projection_checksum(before, cue_ids, before["blocks"]))
            self.assertEqual(change["after_checksum"], framecue.draft_projection_checksum(document, cue_ids, [left, right]))

    def test_block_operations_keep_existing_issue_and_direct_change_references(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-block-references-") as temp:
            root = Path(temp)
            database, package = self._workspace_with_block_layout(root, [
                ["c0001", "c0002"], ["c0003", "c0004"],
            ])
            self._apply(root, database, package["review_id"], "edit.json", {
                "kind": "edit", "draft_version": 0, "cue_id": "c0001", "display_text": "已直接修改",
            })
            self._apply(root, database, package["review_id"], "flag.json", {
                "kind": "flag", "draft_version": 1, "cue_id": "c0004",
                "categories": ["segmentation"], "author": "lead",
            })
            merged = json.loads(self._apply(root, database, package["review_id"], "block-merge.json", {
                "kind": "block_merge", "draft_version": 2,
                "block_id": "b0001", "adjacent_block_id": "b0002",
            }).stdout)
            self.assertEqual(merged["issues"][0]["cue_ids"], ["c0004"])
            connection = framecue.open_workspace_database(database)
            try:
                draft = framecue.draft_row(connection, framecue.workspace_row(connection, package["review_id"]))
            finally:
                connection.close()
            self.assertEqual([change["kind"] for change in draft["direct_changes"]], ["edit", "block_merge"])

    def test_block_operations_are_versioned_and_stage_gated(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-block-stage-") as temp:
            root = Path(temp)
            database, package = self._workspace_with_block_layout(root, [["c0001", "c0002", "c0003"]])
            applied = json.loads(self._apply(root, database, package["review_id"], "block-split.json", {
                "kind": "block_split", "draft_version": 0, "block_id": "b0001", "cue_id": "c0002",
            }).stdout)
            before_stale = copy.deepcopy(applied["document"])
            with self.assertRaises(subprocess.CalledProcessError):
                self._apply(root, database, package["review_id"], "stale-block-split.json", {
                    "kind": "block_split", "draft_version": 0,
                    "block_id": applied["document"]["blocks"][0]["id"], "cue_id": "c0002",
                })
            connection = framecue.open_workspace_database(database)
            try:
                current = framecue.draft_row(connection, framecue.workspace_row(connection, package["review_id"]))
            finally:
                connection.close()
            self.assertEqual(current["document"], before_stale)
            self._complete(database, package["review_id"], 1)
            with self.assertRaises(subprocess.CalledProcessError):
                self._apply(root, database, package["review_id"], "blocked-block-merge.json", {
                    "kind": "block_merge", "draft_version": 1,
                    "block_id": applied["document"]["blocks"][0]["id"],
                    "adjacent_block_id": applied["document"]["blocks"][1]["id"],
                })

    def test_block_operations_reject_reverse_non_adjacent_and_invalid_requests_before_mutation(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-block-reject-") as temp:
            root = Path(temp)
            _, package = self._workspace_with_block_layout(root, [
                ["c0001"], ["c0002"], ["c0003", "c0004"],
            ])
            document = framecue.workspace_draft_document(package, "synchronous_dub")
            before = copy.deepcopy(document)
            for operation, error in ((
                {"kind": "block_merge", "block_id": "b0002", "adjacent_block_id": "b0001"},
                "document order",
            ), (
                {"kind": "block_merge", "block_id": "b0001", "adjacent_block_id": "b0003"},
                "document order",
            ), (
                {"kind": "block_split", "block_id": "b0003", "cue_id": "c0003"},
                "must not be the first",
            ), (
                {"kind": "block_split", "block_id": "b0003", "cue_id": "c9999"},
                "was not found",
            ), (
                {"kind": "block_merge", "block_id": "b0001", "adjacent_block_id": "b9999"},
                "was not found",
            )):
                with self.assertRaisesRegex(framecue.FrameCueError, error):
                    if operation["kind"] == "block_merge":
                        framecue.apply_draft_block_merge(document, operation)
                    else:
                        framecue.apply_draft_block_split(document, operation)
                self.assertEqual(document, before)

    def test_merge_rejects_non_adjacent_semantic_blocks_without_mutating(self):
        package = json.loads(FIXTURE.read_text(encoding="utf-8"))
        third_cue = {
            "id": "c0003",
            "start_ms": 3000,
            "end_ms": 4500,
            "scene_id": "s0001",
            "original_text": "A later source Cue.",
            "text": "稍後的字幕",
            "speech_text": "稍後的字幕。",
        }
        package["cues"].append(third_cue)

        def block(block_id, cue):
            return {
                "id": block_id,
                "cue_ids": [cue["id"]],
                "start_ms": cue["start_ms"],
                "end_ms": cue["end_ms"],
                "budget_ms": cue["end_ms"] - cue["start_ms"],
                "source_text": cue["original_text"],
                "target_text": cue["text"],
                "speech_text": cue["speech_text"],
            }

        package["blocks"] = [
            block("b0001", package["cues"][0]),
            block("b0002", third_cue),
            block("b0003", package["cues"][1]),
        ]
        package["content_checksum"] = framecue.package_checksum(package)
        document = framecue.workspace_draft_document(package, "synchronous_dub")
        before = copy.deepcopy(document)

        with self.assertRaisesRegex(framecue.FrameCueError, "adjacent Semantic Block"):
            framecue.apply_draft_merge(document, {
                "kind": "merge",
                "cue_id": "c0001",
                "adjacent_cue_id": "c0002",
            })
        self.assertEqual(document, before)

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

    def test_human_edits_complete_as_authoritative_content_without_flags(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-human-edit-complete-") as temp:
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
                    "display_text": "採用人工確認後的字幕",
                },
            )

            completed = json.loads(self._complete(database, package["review_id"], 1).stdout)
            self.assertEqual(completed["stage"], "voice_realization_pending")
            self.assertEqual(completed["operation"], "realize_voice_timeline")

            work_order_path = root / "work-order.json"
            run_cli(
                "work-pull",
                "--database", str(database),
                "--review-id", package["review_id"],
                "--out", str(work_order_path),
            )
            work_order = json.loads(work_order_path.read_text(encoding="utf-8"))
            self.assertEqual(work_order["document"]["revision_kind"], "content")
            self.assertEqual(work_order["document"]["cues"][0]["display_text"], "採用人工確認後的字幕")

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
            self.assertEqual({cue_id for target in work_order["targets"] for cue_id in target["cue_ids"]}, {"c0002"})
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

    def test_pending_content_round_can_be_reopened_without_losing_edits(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-reopen-") as temp:
            root = Path(temp)
            database, package = self._workspace(root)
            self._apply(root, database, package["review_id"], "edit.json", {
                "kind": "edit", "draft_version": 0, "cue_id": "c0001",
                "display_text": "保留這筆人工修改",
            })
            self._apply(root, database, package["review_id"], "flag.json", {
                "kind": "flag", "draft_version": 1, "cue_id": "c0002",
                "categories": ["translation"], "author": "lead", "note": "仍需處理",
            })
            completed = json.loads(self._complete(database, package["review_id"], 2).stdout)

            reopened = json.loads(run_cli(
                "workspace-reopen", "--database", str(database),
                "--review-id", package["review_id"],
            ).stdout)

            self.assertEqual(completed["stage"], "content_agent_review_pending")
            self.assertEqual(reopened["stage"], "content_review")
            self.assertEqual(reopened["draft_version"], 2)
            self.assertEqual(reopened["cancelled_request_id"], completed["request_id"])
            changed = json.loads(self._apply(root, database, package["review_id"], "edit-2.json", {
                "kind": "edit", "draft_version": 2, "cue_id": "c0002",
                "display_text": "補上完成後才發現的問題",
            }).stdout)
            self.assertEqual(changed["draft_version"], 3)
            self.assertEqual(changed["document"]["cues"][0]["display_text"], "保留這筆人工修改")

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
            self.assertFalse(targets[0]["context"]["direct_edit"])
            self.assertEqual(targets[0]["context"]["categories"], ["translation"])
            self.assertEqual(targets[0]["context"]["notes"], ["語氣仍需調整"])
            self.assertNotIn("direct_changes", targets[0]["context"])


class SuggestionJobTests(unittest.TestCase):
    def _workspace_server(self, root, separate_blocks=False):
        bundle = root / "bundle"
        database = root / "workspace.sqlite3"
        run_cli("build", "--input", str(FIXTURE), "--out-dir", str(bundle))
        package_path = bundle / "review_package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        if separate_blocks:
            package["blocks"] = [{
                "id": f"b{index:04d}",
                "cue_ids": [cue["id"]],
                "start_ms": cue["start_ms"],
                "end_ms": cue["end_ms"],
                "budget_ms": cue["end_ms"] - cue["start_ms"],
                "source_text": cue["original_text"],
                "target_text": cue["text"],
                "speech_text": cue["speech_text"],
                "legacy_block_id": f"legacy-b{index:04d}",
                "legacy_source_cue_ids": [index],
            } for index, cue in enumerate(package["cues"], 1)]
            package["content_checksum"] = framecue.package_checksum(package)
            package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        run_cli(
            "workspace-import",
            "--database", str(database),
            "--package", str(package_path),
            "--timing-profile", "synchronous_dub",
        )
        server = framecue.make_workspace_server(database, bundle, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return database, package, server, thread

    def _json_request(self, base, path, *, method="GET", value=None, headers=None):
        request_headers = dict(headers or {})
        data = None
        if value is not None:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            f"{base}{path}", data=data, method=method, headers=request_headers,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, json.loads(response.read().decode("utf-8"))

    def _workspace_headers(self, base, snapshot):
        return {
            "Origin": base,
            "X-FrameCue-CSRF": snapshot["csrf_token"],
            "Cookie": f"framecue_session={snapshot['session_id']}",
        }

    def _agent_token(self, database, package, *permissions, label="agent"):
        arguments = [
            "agent-token-create",
            "--database", str(database),
            "--label", label,
            "--workspace", package["review_id"],
        ]
        for permission in permissions:
            arguments.extend(["--permission", permission])
        return json.loads(run_cli(*arguments).stdout)

    def _flag(self, base, snapshot, *, draft_version, note, cue_id="c0001", author="Reviewer", enabled=True):
        value = {
            "kind": "flag",
            "draft_version": draft_version,
            "cue_id": cue_id,
            "categories": ["translation"],
            "author": author,
            "note": note,
        }
        if not enabled:
            value["enabled"] = False
        return self._json_request(
            base,
            "/api/workspace/operation",
            method="POST",
            value=value,
            headers=self._workspace_headers(base, snapshot),
        )[2]

    def test_suggestion_job_lifecycle_keeps_draft_unchanged_until_reviewer_applies(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-lifecycle-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                flagged = self._flag(base, initial, draft_version=0, note="請改成自然語氣")
                self.assertEqual(flagged["draft_version"], 1)
                self.assertEqual(len(flagged["suggestions"]), 1)
                job = flagged["suggestions"][0]
                self.assertEqual(
                    {key: job[key] for key in (
                        "status", "cue_ids", "category", "note", "base_draft_version", "base_checksum",
                    )},
                    {
                        "status": "queued",
                        "cue_ids": ["c0001"],
                        "category": "translation",
                        "note": "請改成自然語氣",
                        "base_draft_version": 1,
                        "base_checksum": flagged["document"]["checksum"],
                    },
                )
                self.assertIsNone(job["proposal"])
                self.assertIsNone(job["error"])
                before_cues = copy.deepcopy(flagged["document"]["cues"])

                _, _, workspace = self._json_request(base, "/api/workspace")
                self.assertEqual(workspace["suggestions"][0]["job_id"], job["job_id"])

                token = self._agent_token(database, package, "list", "read", "claim", "submit", "fail")
                auth = {"Authorization": f"Bearer {token['token']}"}
                _, _, listed = self._json_request(
                    base, f"/api/agent/suggestions?workspace_id={package['review_id']}", headers=auth,
                )
                self.assertEqual([item["job_id"] for item in listed["suggestions"]], [job["job_id"]])
                self.assertNotIn("context", listed["suggestions"][0])
                _, _, read = self._json_request(
                    base, f"/api/agent/suggestions/{job['job_id']}", headers=auth,
                )
                self.assertEqual(read["context"]["cue"], before_cues[0])
                self.assertEqual(read["context"]["block"]["id"], before_cues[0]["block_id"])
                event_request = (
                    "GET /api/workspace/events HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{server.server_address[1]}\r\n"
                    f"Cookie: framecue_session={initial['session_id']}\r\n"
                    f"Last-Event-ID: {flagged['snapshot_version']}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                with socket.create_connection(server.server_address, timeout=5) as client:
                    client.settimeout(1)
                    client.sendall(event_request)
                    event_body = client.recv(4096)
                    self.assertIn(b"Content-Type: text/event-stream", event_body)
                    _, _, claimed = self._json_request(
                        base, f"/api/agent/suggestions/{job['job_id']}/claim", method="POST", headers=auth,
                    )
                    while b"event: snapshot" not in event_body:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        event_body += chunk
                self.assertIn(b"event: snapshot", event_body)
                self.assertEqual(claimed["status"], "processing")
                self.assertEqual(claimed["attempt_count"], 1)

                proposal = {
                    "kind": "replace_text",
                    "cue_id": "c0001",
                    "text": "請用更自然的中文表達",
                    "explanation": "保留原意，調整語氣。",
                }
                _, _, submitted = self._json_request(
                    base,
                    f"/api/agent/suggestions/{job['job_id']}/submit",
                    method="POST",
                    value={"proposal": proposal},
                    headers=auth,
                )
                self.assertEqual(submitted, {
                    "job_id": job["job_id"], "status": "ready", "proposal": proposal,
                })

                _, _, ready = self._json_request(base, "/api/workspace/snapshot")
                ready_job = next(item for item in ready["suggestions"] if item["job_id"] == job["job_id"])
                self.assertEqual(ready_job["status"], "ready")
                self.assertEqual(ready_job["proposal"], proposal)
                self.assertEqual(ready["document"]["cues"], before_cues)

                _, _, applied = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "suggestion_apply", "job_id": job["job_id"], "draft_version": 1},
                    headers=self._workspace_headers(base, initial),
                )
                self.assertEqual(applied["draft_version"], 2)
                self.assertEqual(applied["document"]["cues"][0]["display_text"], proposal["text"])
                self.assertEqual(applied["suggestions"], [])
                _, _, applied_job = self._json_request(
                    base, f"/api/agent/suggestions/{job['job_id']}", headers=auth,
                )
                self.assertEqual(applied_job["status"], "applied")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_suggestion_auth_failure_and_stale_apply_do_not_overwrite_human_edit(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-stale-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                first = self._flag(base, initial, draft_version=0, note="第一次請求")
                first_job = first["suggestions"][0]
                owner = self._agent_token(database, package, "claim", "submit", "fail")
                reader = self._agent_token(database, package, "read", label="reader")
                other = self._agent_token(database, package, "claim", label="other")
                denied = urllib.request.Request(
                    f"{base}/api/agent/suggestions/{first_job['job_id']}/claim",
                    data=b"",
                    method="POST",
                    headers={"Authorization": f"Bearer {reader['token']}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(denied, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                owner_auth = {"Authorization": f"Bearer {owner['token']}"}
                self._json_request(
                    base, f"/api/agent/suggestions/{first_job['job_id']}/claim", method="POST", headers=owner_auth,
                )
                other_claim = urllib.request.Request(
                    f"{base}/api/agent/suggestions/{first_job['job_id']}/claim",
                    data=b"",
                    method="POST",
                    headers={"Authorization": f"Bearer {other['token']}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(other_claim, timeout=5)
                self.assertEqual(failure.exception.code, 409)
                _, _, first_edit = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "edit",
                        "draft_version": 1,
                        "cue_id": "c0001",
                        "display_text": "審稿者的前一版",
                    },
                    headers=self._workspace_headers(base, initial),
                )
                self.assertEqual(first_edit["draft_version"], 2)
                stale_claim = urllib.request.Request(
                    f"{base}/api/agent/suggestions/{first_job['job_id']}/claim",
                    data=b"",
                    method="POST",
                    headers=owner_auth,
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(stale_claim, timeout=5)
                self.assertEqual(failure.exception.code, 409)
                _, _, stale_first = self._json_request(base, "/api/workspace/snapshot")
                self.assertEqual(stale_first["suggestions"][0]["status"], "stale")
                _, _, failed = self._json_request(
                    base,
                    f"/api/agent/suggestions/{first_job['job_id']}/fail",
                    method="POST",
                    value={"category": "provider", "message": "temporary failure", "retryable": True},
                    headers=owner_auth,
                )
                self.assertEqual(failed["status"], "failed")

                second = self._flag(base, initial, draft_version=2, note="重新請求")
                second_job = next(item for item in second["suggestions"] if item["job_id"] != first_job["job_id"])
                self._json_request(
                    base, f"/api/agent/suggestions/{second_job['job_id']}/claim", method="POST", headers=owner_auth,
                )
                proposal = {
                    "kind": "replace_text",
                    "cue_id": "c0001",
                    "text": "Agent 建議版本",
                    "explanation": "語氣調整。",
                }
                self._json_request(
                    base,
                    f"/api/agent/suggestions/{second_job['job_id']}/submit",
                    method="POST",
                    value={"proposal": proposal},
                    headers=owner_auth,
                )
                _, _, edited = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "edit",
                        "draft_version": 3,
                        "cue_id": "c0001",
                        "display_text": "審稿者的較新版本",
                    },
                    headers=self._workspace_headers(base, initial),
                )
                self.assertEqual(edited["draft_version"], 4)

                stale = urllib.request.Request(
                    f"{base}/api/workspace/operation",
                    data=json.dumps({
                        "kind": "suggestion_apply", "job_id": second_job["job_id"], "draft_version": 4,
                    }).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", **self._workspace_headers(base, initial)},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(stale, timeout=5)
                self.assertEqual(failure.exception.code, 409)

                _, _, after_stale = self._json_request(base, "/api/workspace/snapshot")
                self.assertEqual(after_stale["document"]["cues"][0]["display_text"], "審稿者的較新版本")
                stale_job = next(item for item in after_stale["suggestions"] if item["job_id"] == second_job["job_id"])
                self.assertEqual(stale_job["status"], "stale")

                _, _, ignored = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "suggestion_ignore", "job_id": second_job["job_id"], "draft_version": 4},
                    headers=self._workspace_headers(base, initial),
                )
                self.assertEqual(ignored["draft_version"], 4)
                self.assertEqual(ignored["document"]["cues"][0]["display_text"], "審稿者的較新版本")
                self.assertEqual(ignored["suggestions"], [])
                _, _, ignored_job = self._json_request(
                    base, f"/api/agent/suggestions/{second_job['job_id']}",
                    headers={"Authorization": f"Bearer {reader['token']}"},
                )
                self.assertEqual(ignored_job["status"], "ignored")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_unflag_ignores_active_suggestions_without_showing_a_card(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-unflag-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                flagged = self._flag(base, initial, draft_version=0, note="取消前的請求")
                job = flagged["suggestions"][0]
                _, _, cancelled = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "flag",
                        "draft_version": 1,
                        "cue_id": "c0001",
                        "categories": ["translation"],
                        "author": "Reviewer",
                        "enabled": False,
                    },
                    headers=self._workspace_headers(base, initial),
                )
                self.assertEqual(cancelled["draft_version"], 2)
                self.assertEqual(cancelled["suggestions"], [])

                token = self._agent_token(database, package, "list", "read")
                auth = {"Authorization": f"Bearer {token['token']}"}
                _, _, listed = self._json_request(
                    base, f"/api/agent/suggestions?workspace_id={package['review_id']}", headers=auth,
                )
                self.assertEqual(listed["suggestions"], [])
                _, _, ignored = self._json_request(
                    base, f"/api/agent/suggestions/{job['job_id']}", headers=auth,
                )
                self.assertEqual(ignored["status"], "ignored")
                self.assertIsNone(ignored["lease_owner_token_id"])
                self.assertIsNone(ignored["lease_expires_at"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_unflag_keeps_a_shared_suggestion_until_the_last_reviewer_removes_it(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-shared-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                first = self._flag(
                    base, initial, draft_version=0, note="A 的請求", author="Reviewer A",
                )
                job = first["suggestions"][0]
                joined = self._flag(
                    base, initial, draft_version=1, note="  A 的請求  ", author="Reviewer B",
                )
                self.assertEqual(joined["draft_version"], 2)
                self.assertEqual([item["job_id"] for item in joined["suggestions"]], [job["job_id"]])
                self.assertEqual(joined["suggestions"][0]["status"], "queued")
                self.assertEqual(joined["issues"][0]["authors"], ["Reviewer A", "Reviewer B"])

                after_first_unflag = self._flag(
                    base, initial, draft_version=2, note="", author="Reviewer A", enabled=False,
                )
                self.assertEqual(after_first_unflag["draft_version"], 3)
                self.assertEqual(
                    [item["job_id"] for item in after_first_unflag["suggestions"]], [job["job_id"]],
                )
                self.assertEqual(after_first_unflag["issues"][0]["authors"], ["Reviewer B"])

                token = self._agent_token(database, package, "list", "read", "claim")
                auth = {"Authorization": f"Bearer {token['token']}"}
                self._json_request(
                    base, f"/api/agent/suggestions/{job['job_id']}/claim", method="POST", headers=auth,
                )
                last_unflag = self._flag(
                    base, initial, draft_version=3, note="", author="Reviewer B", enabled=False,
                )
                self.assertEqual(last_unflag["draft_version"], 4)
                self.assertEqual(last_unflag["suggestions"], [])
                _, _, ignored = self._json_request(
                    base, f"/api/agent/suggestions/{job['job_id']}", headers=auth,
                )
                self.assertEqual(ignored["status"], "ignored")
                self.assertIsNone(ignored["lease_owner_token_id"])
                self.assertIsNone(ignored["lease_expires_at"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_reflag_dedupes_normalized_note_but_replaces_changed_instruction(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-note-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                first = self._flag(base, initial, draft_version=0, note="  請保留術語  ")
                first_job = first["suggestions"][0]
                self.assertEqual(first_job["note"], "請保留術語")

                same_note = self._flag(base, initial, draft_version=1, note="\t請保留術語\n")
                self.assertEqual([item["job_id"] for item in same_note["suggestions"]], [first_job["job_id"]])
                self.assertEqual(same_note["suggestions"][0]["note"], "請保留術語")
                token = self._agent_token(database, package, "claim", "list", "read")
                auth = {"Authorization": f"Bearer {token['token']}"}
                self._json_request(
                    base, f"/api/agent/suggestions/{first_job['job_id']}/claim", method="POST", headers=auth,
                )

                changed_note = self._flag(base, initial, draft_version=2, note="請改成正式語氣")
                self.assertEqual(len(changed_note["suggestions"]), 1)
                changed_job = changed_note["suggestions"][0]
                self.assertNotEqual(changed_job["job_id"], first_job["job_id"])
                self.assertEqual(changed_job["status"], "queued")
                self.assertEqual(changed_job["note"], "請改成正式語氣")

                _, _, first_detail = self._json_request(
                    base, f"/api/agent/suggestions/{first_job['job_id']}", headers=auth,
                )
                self.assertEqual(first_detail["status"], "ignored")
                self.assertIsNone(first_detail["lease_owner_token_id"])
                self.assertIsNone(first_detail["lease_expires_at"])
                _, _, listed = self._json_request(
                    base, f"/api/agent/suggestions?workspace_id={package['review_id']}", headers=auth,
                )
                self.assertEqual([item["job_id"] for item in listed["suggestions"]], [changed_job["job_id"]])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_blank_note_remains_latest_after_ignoring_a_ready_suggestion(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-blank-note-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                self._flag(base, initial, draft_version=0, note="第一版提示")
                blank = self._flag(base, initial, draft_version=1, note=" \t ")
                blank_job = blank["suggestions"][0]
                self.assertEqual(blank_job["note"], "")
                self.assertEqual(blank["issues"][0]["notes"], ["第一版提示", ""])

                same_blank = self._flag(base, initial, draft_version=2, note="")
                self.assertEqual([item["job_id"] for item in same_blank["suggestions"]], [blank_job["job_id"]])
                self.assertEqual(same_blank["issues"][0]["notes"], ["第一版提示", ""])

                token = self._agent_token(database, package, "claim", "submit", "read")
                auth = {"Authorization": f"Bearer {token['token']}"}
                self._json_request(
                    base, f"/api/agent/suggestions/{blank_job['job_id']}/claim", method="POST", headers=auth,
                )
                _, _, ready = self._json_request(
                    base,
                    f"/api/agent/suggestions/{blank_job['job_id']}/submit",
                    method="POST",
                    value={"proposal": {
                        "kind": "replace_text",
                        "cue_id": "c0001",
                        "text": "Agent 建議文字",
                        "explanation": "可供審稿者選擇。",
                    }},
                    headers=auth,
                )
                self.assertEqual(ready["status"], "ready")
                _, _, ignored = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "suggestion_ignore", "job_id": blank_job["job_id"], "draft_version": 3},
                    headers=self._workspace_headers(base, initial),
                )
                self.assertEqual(ignored["suggestions"], [])

                _, _, reloaded = self._json_request(base, "/api/workspace/snapshot")
                issue = next(item for item in reloaded["issues"] if item["category"] == "translation")
                self.assertEqual(issue["notes"][-1], "")
                _, _, detail = self._json_request(
                    base, f"/api/agent/suggestions/{blank_job['job_id']}", headers=auth,
                )
                self.assertEqual(detail["status"], "ignored")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_suggestion_stales_when_split_or_merge_removes_its_cue(self):
        operations = [
            {"kind": "split", "cue_id": "c0001", "cursor": 10},
            {"kind": "merge", "cue_id": "c0001", "adjacent_cue_id": "c0002"},
        ]
        for operation in operations:
            with self.subTest(kind=operation["kind"]):
                with tempfile.TemporaryDirectory(prefix="framecue-suggestion-structure-") as temp:
                    database, package, server, thread = self._workspace_server(Path(temp))
                    try:
                        base = f"http://127.0.0.1:{server.server_address[1]}"
                        _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                        job = self._flag(base, initial, draft_version=0, note="結構調整前的建議")["suggestions"][0]
                        _, _, changed = self._json_request(
                            base,
                            "/api/workspace/operation",
                            method="POST",
                            value={**operation, "draft_version": 1},
                            headers=self._workspace_headers(base, initial),
                        )
                        self.assertEqual(changed["draft_version"], 2)
                        stale = next(item for item in changed["suggestions"] if item["job_id"] == job["job_id"])
                        self.assertEqual(stale["status"], "stale")

                        token = self._agent_token(database, package, "read")
                        _, _, detail = self._json_request(
                            base,
                            f"/api/agent/suggestions/{job['job_id']}",
                            headers={"Authorization": f"Bearer {token['token']}"},
                        )
                        self.assertEqual(detail["status"], "stale")
                        self.assertEqual(detail["cue_ids"], ["c0001"])
                    finally:
                        server.shutdown()
                        thread.join(timeout=5)
                        server.server_close()

    def test_ready_suggestion_blocks_a_structural_change_that_would_stale_it(self):
        with tempfile.TemporaryDirectory(prefix="framecue-ready-suggestion-structure-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                job = self._flag(base, initial, draft_version=0, note="先完成 Agent 建議")["suggestions"][0]
                token = self._agent_token(database, package, "claim", "submit")
                auth = {"Authorization": f"Bearer {token['token']}"}
                self._json_request(
                    base, f"/api/agent/suggestions/{job['job_id']}/claim", method="POST", headers=auth,
                )
                self._json_request(
                    base,
                    f"/api/agent/suggestions/{job['job_id']}/submit",
                    method="POST",
                    value={"proposal": {
                        "kind": "replace_text",
                        "cue_id": "c0001",
                        "text": "Agent 已完成的建議",
                        "explanation": "等待審稿者決定。",
                    }},
                    headers=auth,
                )

                request = urllib.request.Request(
                    f"{base}/api/workspace/operation",
                    data=json.dumps({
                        "kind": "split", "cue_id": "c0001", "cursor": 10, "draft_version": 1,
                    }).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", **self._workspace_headers(base, initial)},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(failure.exception.code, 409)
                self.assertIn("resolve ready Agent suggestions", failure.exception.read().decode("utf-8"))

                _, _, unchanged = self._json_request(base, "/api/workspace/snapshot")
                self.assertEqual(unchanged["draft_version"], 1)
                self.assertEqual(unchanged["document"]["cues"][0]["id"], "c0001")
                self.assertEqual(unchanged["suggestions"][0]["status"], "ready")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_suggestion_context_freshness_is_scoped_to_its_cue_block(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-context-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp), separate_blocks=True)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                first = self._flag(
                    base, initial, draft_version=0, note="第一個 block", author="Reviewer A",
                )
                first_job = first["suggestions"][0]
                _, _, unrelated = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "edit",
                        "draft_version": 1,
                        "cue_id": "c0002",
                        "display_text": "另一個 block 的人工修改",
                    },
                    headers=self._workspace_headers(base, initial),
                )
                self.assertEqual(unrelated["draft_version"], 2)
                self.assertEqual(unrelated["suggestions"][0]["status"], "queued")
                token = self._agent_token(database, package, "claim")
                _, _, claimed = self._json_request(
                    base,
                    f"/api/agent/suggestions/{first_job['job_id']}/claim",
                    method="POST",
                    headers={"Authorization": f"Bearer {token['token']}"},
                )
                self.assertEqual(claimed["status"], "processing")

                second = self._flag(
                    base, initial, draft_version=2, note="第二個 block", cue_id="c0002", author="Reviewer B",
                )
                jobs = {item["job_id"]: item for item in second["suggestions"]}
                self.assertEqual(len(jobs), 2)
                self.assertEqual(jobs[first_job["job_id"]]["status"], "processing")
                second_job = next(item for item in second["suggestions"] if item["job_id"] != first_job["job_id"])
                self.assertEqual(second_job["status"], "queued")

                _, _, same_context = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "edit",
                        "draft_version": 3,
                        "cue_id": "c0001",
                        "display_text": "第一個 block 的人工修改",
                    },
                    headers=self._workspace_headers(base, initial),
                )
                statuses = {item["job_id"]: item["status"] for item in same_context["suggestions"]}
                self.assertEqual(statuses[first_job["job_id"]], "stale")
                self.assertEqual(statuses[second_job["job_id"]], "queued")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_agent_submit_rejects_blank_and_overlong_proposals(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-proposal-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                job = self._flag(base, initial, draft_version=0, note="請提出建議")["suggestions"][0]
                token = self._agent_token(database, package, "claim", "submit", "read")
                auth = {"Authorization": f"Bearer {token['token']}"}
                self._json_request(
                    base, f"/api/agent/suggestions/{job['job_id']}/claim", method="POST", headers=auth,
                )
                invalid_proposals = [
                    {"kind": "replace_text", "cue_id": "c0001", "text": " \t ", "explanation": "理由"},
                    {"kind": "replace_text", "cue_id": "c0001", "text": "建議", "explanation": " \n "},
                    {"kind": "replace_text", "cue_id": "c0001", "text": "x" * 10001, "explanation": "理由"},
                    {"kind": "replace_text", "cue_id": "c0001", "text": "建議", "explanation": "x" * 2001},
                ]
                for proposal in invalid_proposals:
                    request = urllib.request.Request(
                        f"{base}/api/agent/suggestions/{job['job_id']}/submit",
                        data=json.dumps({"proposal": proposal}, ensure_ascii=False).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json", **auth},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(failure.exception.code, 409)

                _, _, submitted = self._json_request(
                    base,
                    f"/api/agent/suggestions/{job['job_id']}/submit",
                    method="POST",
                    value={"proposal": {
                        "kind": "replace_text",
                        "cue_id": "c0001",
                        "text": "  修剪後文字  ",
                        "explanation": "  修剪後理由  ",
                    }},
                    headers=auth,
                )
                self.assertEqual(submitted["proposal"]["text"], "修剪後文字")
                self.assertEqual(submitted["proposal"]["explanation"], "修剪後理由")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_reflag_exposes_only_the_latest_suggestion_for_a_cue_category(self):
        with tempfile.TemporaryDirectory(prefix="framecue-suggestion-reflag-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                first = self._flag(base, initial, draft_version=0, note="第一個請求")
                first_job = first["suggestions"][0]
                self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "edit",
                        "draft_version": 1,
                        "cue_id": "c0001",
                        "display_text": "讓第一個 job 過期",
                    },
                    headers=self._workspace_headers(base, initial),
                )
                second = self._flag(base, initial, draft_version=2, note="第二個請求")
                self.assertEqual(len(second["suggestions"]), 1)
                second_job = second["suggestions"][0]
                self.assertNotEqual(second_job["job_id"], first_job["job_id"])
                self.assertEqual(second_job["status"], "queued")

                token = self._agent_token(database, package, "claim", "fail", "list", "read")
                auth = {"Authorization": f"Bearer {token['token']}"}
                _, _, first_detail = self._json_request(
                    base, f"/api/agent/suggestions/{first_job['job_id']}", headers=auth,
                )
                self.assertEqual(first_detail["status"], "ignored")
                self._json_request(
                    base, f"/api/agent/suggestions/{second_job['job_id']}/claim", method="POST", headers=auth,
                )
                self._json_request(
                    base,
                    f"/api/agent/suggestions/{second_job['job_id']}/fail",
                    method="POST",
                    value={"category": "provider", "message": "failed", "retryable": True},
                    headers=auth,
                )
                third = self._flag(base, initial, draft_version=3, note="第三個請求")
                self.assertEqual(len(third["suggestions"]), 1)
                third_job = third["suggestions"][0]
                self.assertNotIn(second_job["job_id"], [job["job_id"] for job in third["suggestions"]])
                self.assertEqual(third_job["status"], "queued")
                _, _, listed = self._json_request(
                    base, f"/api/agent/suggestions?workspace_id={package['review_id']}", headers=auth,
                )
                self.assertEqual([job["job_id"] for job in listed["suggestions"]], [third_job["job_id"]])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_delete_cue_repairs_block_issues_and_stales_its_suggestion(self):
        with tempfile.TemporaryDirectory(prefix="framecue-delete-cue-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp), separate_blocks=True)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, initial = self._json_request(base, "/api/workspace/snapshot")
                flagged = self._flag(base, initial, draft_version=0, note="刪除前")
                job_id = flagged["suggestions"][0]["job_id"]

                _, _, deleted = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "delete", "draft_version": 1, "cue_id": "c0001"},
                    headers=self._workspace_headers(base, initial),
                )

                self.assertNotIn("c0001", [cue["id"] for cue in deleted["document"]["cues"]])
                self.assertTrue(all("c0001" not in block["cue_ids"] for block in deleted["document"]["blocks"]))
                self.assertTrue(all("c0001" not in issue["cue_ids"] for issue in deleted["issues"]))
                stale = next(job for job in deleted["suggestions"] if job["job_id"] == job_id)
                self.assertEqual(stale["status"], "stale")

                connection = sqlite3.connect(database)
                try:
                    row = connection.execute(
                        "SELECT direct_changes_json FROM workspace_drafts WHERE review_id = ?",
                        (package["review_id"],),
                    ).fetchone()
                finally:
                    connection.close()
                changes = json.loads(row[0])
                self.assertEqual(changes[-1]["kind"], "delete")
                self.assertEqual(changes[-1]["parent_cue_ids"], ["c0001"])
                self.assertTrue(changes[-1]["cue_ids"])

                _, _, completed = self._json_request(
                    base,
                    "/api/workspace/complete",
                    method="POST",
                    value={"draft_version": 2},
                    headers=self._workspace_headers(base, initial),
                )
                self.assertEqual(completed["operation"], "realize_voice_timeline")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
