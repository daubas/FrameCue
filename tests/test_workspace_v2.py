import copy
import hashlib
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

            self._complete(database, package["review_id"], 1)
            work_order_path = root / "work-order.json"
            run_cli("work-pull", "--database", str(database), "--review-id", package["review_id"], "--out", str(work_order_path))
            target = json.loads(work_order_path.read_text(encoding="utf-8"))["targets"][0]
            self.assertEqual(target["cue_ids"], cue_ids)
            self.assertEqual(target["block_ids"], [block["id"]])
            self.assertEqual(target["context"]["allowed_operation_scope"], "cue")
            self.assertEqual(target["context"]["direct_changes"], [change])

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
