import copy
import hashlib
import json
import shutil
import sqlite3
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


class CandidateV2Tests(unittest.TestCase):
    def _correction_order(self, root):
        bundle = root / "bundle"
        database = root / "workspace.sqlite3"
        operation_path = root / "flag.json"
        work_order_path = root / "work-order.json"
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
        operation_path.write_text(json.dumps({
            "kind": "flag",
            "draft_version": 0,
            "cue_id": "c0001",
            "categories": ["translation"],
            "author": "lead",
            "note": "請調整語氣",
        }, ensure_ascii=False), encoding="utf-8")
        run_cli(
            "workspace-apply",
            "--database",
            str(database),
            "--review-id",
            package["review_id"],
            "--operation",
            str(operation_path),
        )
        run_cli(
            "workspace-complete",
            "--database",
            str(database),
            "--review-id",
            package["review_id"],
            "--draft-version",
            "1",
        )
        run_cli(
            "work-pull",
            "--database",
            str(database),
            "--review-id",
            package["review_id"],
            "--out",
            str(work_order_path),
        )
        return database, package, json.loads(work_order_path.read_text(encoding="utf-8"))

    def _candidate(self, work_order):
        document = copy.deepcopy(work_order["document"])
        target = work_order["targets"][0]
        cue = next(row for row in document["cues"] if row["id"] == target["cue_ids"][0])
        cue["display_text"] = "OpenClaw 仍保留明確的人工審稿步驟"
        cue["speech_text"] = "OpenClaw 仍保留明確的人工審稿步驟。"
        framecue.recompute_draft_blocks(document)
        framecue.refresh_document_checksum(document)
        return {
            "schema": "framecue_candidate_revision_v2",
            "status": "ready_for_review",
            "request_id": work_order["request_id"],
            "workspace_id": work_order["workspace_id"],
            "operation": "content_correction_review",
            "base_revision": work_order["base_revision"],
            "base_draft_version": work_order["base_draft_version"],
            "base_checksum": work_order["base_checksum"],
            "document": document,
            "change_proposals": [{
                "proposal_id": "proposal-1",
                "range_id": target["range_id"],
                "before_checksum": target["before_checksum"],
                "replacement": {
                    "cues": [copy.deepcopy(next(row for row in document["cues"] if row["id"] == cue_id)) for cue_id in target["cue_ids"]],
                    "blocks": [copy.deepcopy(next(row for row in document["blocks"] if row["id"] == block_id)) for block_id in target["block_ids"]],
                },
            }],
        }

    def _two_proposal_order(self, root):
        fixture_root = root / "fixture"
        shutil.copytree(FIXTURE.parent, fixture_root)
        source_path = fixture_root / FIXTURE.name
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["blocks"] = [
            {
                "id": f"b000{index + 1}", "cue_ids": [cue["id"]],
                "start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "budget_ms": 1500,
                "source_text": cue["original_text"], "target_text": cue["text"],
                "speech_text": cue["speech_text"],
            }
            for index, cue in enumerate(source["cues"])
        ]
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        bundle, database = root / "bundle", root / "workspace.sqlite3"
        run_cli("build", "--input", str(source_path), "--out-dir", str(bundle))
        package = json.loads((bundle / "review_package.json").read_text(encoding="utf-8"))
        run_cli(
            "workspace-import", "--database", str(database),
            "--package", str(bundle / "review_package.json"), "--timing-profile", "synchronous_dub",
        )
        for version, cue_id in enumerate(("c0001", "c0002")):
            operation_path = root / f"flag-{cue_id}.json"
            operation_path.write_text(json.dumps({
                "kind": "flag", "draft_version": version, "cue_id": cue_id,
                "categories": ["translation"], "author": "lead", "note": f"請調整 {cue_id}",
            }, ensure_ascii=False), encoding="utf-8")
            run_cli(
                "workspace-apply", "--database", str(database),
                "--review-id", package["review_id"], "--operation", str(operation_path),
            )
        run_cli(
            "workspace-complete", "--database", str(database),
            "--review-id", package["review_id"], "--draft-version", "2",
        )
        order_path = root / "two-work-order.json"
        run_cli(
            "work-pull", "--database", str(database),
            "--review-id", package["review_id"], "--out", str(order_path),
        )
        return database, package, json.loads(order_path.read_text(encoding="utf-8"))

    def _two_proposal_candidate(self, work_order):
        document = copy.deepcopy(work_order["document"])
        for index, cue in enumerate(document["cues"], start=1):
            cue["display_text"] = f"已修正字幕 {index}"
            cue["speech_text"] = f"已修正字幕 {index}。"
        framecue.recompute_draft_blocks(document)
        framecue.refresh_document_checksum(document)
        proposals = []
        for index, target in enumerate(work_order["targets"], start=1):
            proposals.append({
                "proposal_id": f"proposal-{index}",
                "range_id": target["range_id"],
                "before_checksum": target["before_checksum"],
                "replacement": {
                    "cues": [copy.deepcopy(next(cue for cue in document["cues"] if cue["id"] == cue_id)) for cue_id in target["cue_ids"]],
                    "blocks": [copy.deepcopy(next(block for block in document["blocks"] if block["id"] == block_id)) for block_id in target["block_ids"]],
                },
            })
        candidate = self._candidate(work_order)
        candidate["document"] = document
        candidate["change_proposals"] = proposals
        return candidate

    def _submit(self, root, database, name, candidate):
        path = root / name
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        return run_cli("work-submit", "--database", str(database), "--candidate", str(path))

    def _decide(self, root, database, package, work_order, candidate, decisions, checksum=None):
        path = root / "decisions.json"
        path.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
        return run_cli(
            "candidate-decide",
            "--database", str(database),
            "--review-id", package["review_id"],
            "--request-id", work_order["request_id"],
            "--candidate-checksum", checksum or hashlib.sha256(framecue.canonical_json(candidate).encode("utf-8")).hexdigest(),
            "--decisions", str(path),
        )

    def test_accepting_every_content_proposal_creates_the_voice_work_order(self):
        with tempfile.TemporaryDirectory(prefix="framecue-candidate-v2-accept-") as temp:
            root = Path(temp)
            database, package, work_order = self._correction_order(root)
            candidate = self._candidate(work_order)
            self._submit(root, database, "candidate.json", candidate)

            summary = json.loads(self._decide(
                root, database, package, work_order, candidate,
                [{"proposal_id": "proposal-1", "decision": "accept"}],
            ).stdout)

            self.assertEqual(summary["stage"], "voice_realization_pending")
            self.assertEqual(summary["status"], "accepted")
            next_order_path = root / "voice-order.json"
            run_cli(
                "work-pull", "--database", str(database),
                "--review-id", package["review_id"], "--out", str(next_order_path),
            )
            next_order = json.loads(next_order_path.read_text(encoding="utf-8"))
            self.assertEqual(next_order["operation"], "realize_voice_timeline")
            self.assertEqual(next_order["document"]["cues"][0]["display_text"], "OpenClaw 仍保留明確的人工審稿步驟")

    def test_rejecting_one_proposal_reopens_only_that_range(self):
        with tempfile.TemporaryDirectory(prefix="framecue-candidate-v2-partial-") as temp:
            root = Path(temp)
            database, package, work_order = self._two_proposal_order(root)
            candidate = self._two_proposal_candidate(work_order)
            self._submit(root, database, "candidate.json", candidate)

            summary = json.loads(self._decide(
                root, database, package, work_order, candidate,
                [
                    {"proposal_id": "proposal-1", "decision": "accept"},
                    {"proposal_id": "proposal-2", "decision": "reject"},
                ],
            ).stdout)

            self.assertEqual(summary["stage"], "content_review")
            self.assertEqual(summary["status"], "changes_requested")
            snapshot = framecue.workspace_snapshot(database, package["review_id"], "test")
            self.assertEqual(snapshot["document"]["cues"][0]["display_text"], "已修正字幕 1")
            self.assertEqual(snapshot["document"]["cues"][1]["display_text"], work_order["document"]["cues"][1]["display_text"])
            self.assertEqual([issue["cue_ids"] for issue in snapshot["issues"]], [["c0002"]])

            run_cli(
                "workspace-complete", "--database", str(database),
                "--review-id", package["review_id"], "--draft-version", str(summary["draft_version"]),
            )
            next_order_path = root / "retry-order.json"
            run_cli(
                "work-pull", "--database", str(database),
                "--review-id", package["review_id"], "--out", str(next_order_path),
            )
            next_order = json.loads(next_order_path.read_text(encoding="utf-8"))
            self.assertEqual([target["cue_ids"] for target in next_order["targets"]], [["c0002"]])

            retry_candidate = self._candidate(next_order)
            self._submit(root, database, "retry-candidate.json", retry_candidate)
            retry_summary = json.loads(self._decide(
                root, database, package, next_order, retry_candidate,
                [{"proposal_id": "proposal-1", "decision": "reject"}],
            ).stdout)
            run_cli(
                "workspace-complete", "--database", str(database),
                "--review-id", package["review_id"], "--draft-version", str(retry_summary["draft_version"]),
            )
            repeated_order_path = root / "repeated-order.json"
            run_cli(
                "work-pull", "--database", str(database),
                "--review-id", package["review_id"], "--out", str(repeated_order_path),
            )
            repeated_order = json.loads(repeated_order_path.read_text(encoding="utf-8"))
            self.assertNotEqual(repeated_order["request_id"], next_order["request_id"])
            self.assertEqual(repeated_order["base_checksum"], next_order["base_checksum"])

    def test_candidate_decisions_fail_atomically_when_incomplete_stale_or_dependency_split(self):
        with tempfile.TemporaryDirectory(prefix="framecue-candidate-v2-atomic-") as temp:
            root = Path(temp)
            database, package, work_order = self._two_proposal_order(root)
            candidate = self._two_proposal_candidate(work_order)
            for proposal in candidate["change_proposals"]:
                proposal["dependencies"] = ["shared-meaning"]
            self._submit(root, database, "candidate.json", candidate)

            invalid = [
                ([{"proposal_id": "proposal-1", "decision": "accept"}], None),
                ([
                    {"proposal_id": "proposal-1", "decision": "accept"},
                    {"proposal_id": "proposal-1", "decision": "reject"},
                ], None),
                ([
                    {"proposal_id": "proposal-1", "decision": "accept"},
                    {"proposal_id": "unknown", "decision": "reject"},
                ], None),
                ([
                    {"proposal_id": "proposal-1", "decision": "accept"},
                    {"proposal_id": "proposal-2", "decision": "reject"},
                ], None),
                ([
                    {"proposal_id": "proposal-1", "decision": "accept"},
                    {"proposal_id": "proposal-2", "decision": "accept"},
                ], "0" * 64),
            ]
            for decisions, checksum in invalid:
                with self.assertRaises(subprocess.CalledProcessError):
                    self._decide(root, database, package, work_order, candidate, decisions, checksum)
                self.assertEqual(
                    framecue.workspace_snapshot(database, package["review_id"], "test")["stage"],
                    "content_candidate_review",
                )

            summary = json.loads(self._decide(
                root, database, package, work_order, candidate,
                [
                    {"proposal_id": "proposal-1", "decision": "accept"},
                    {"proposal_id": "proposal-2", "decision": "accept"},
                ],
            ).stdout)
            self.assertEqual(summary["stage"], "voice_realization_pending")

    def test_legacy_work_order_constraint_migration_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory(prefix="framecue-work-order-migration-") as temp:
            root = Path(temp)
            database, _, work_order = self._correction_order(root)
            connection = sqlite3.connect(database)
            connection.executescript("""
                PRAGMA foreign_keys = OFF;
                ALTER TABLE work_orders RENAME TO current_work_orders;
                CREATE TABLE work_orders (
                    work_order_id INTEGER PRIMARY KEY,
                    request_id TEXT UNIQUE NOT NULL,
                    review_id TEXT NOT NULL REFERENCES workspaces(review_id),
                    revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
                    base_revision TEXT NOT NULL,
                    base_checksum TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    candidate_json TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(review_id, base_revision, base_checksum, operation)
                );
                INSERT INTO work_orders SELECT * FROM current_work_orders;
                DROP TABLE current_work_orders;
            """)
            connection.close()

            migrated = framecue.open_workspace_database(database)
            rows = migrated.execute("SELECT request_id FROM work_orders").fetchall()
            migrated.close()
            self.assertEqual([row["request_id"] for row in rows], [work_order["request_id"]])

    def test_content_candidate_submit_marks_the_workspace_ready_for_human_decision(self):
        with tempfile.TemporaryDirectory(prefix="framecue-candidate-v2-happy-") as temp:
            root = Path(temp)
            database, package, work_order = self._correction_order(root)

            summary = json.loads(self._submit(root, database, "candidate.json", self._candidate(work_order)).stdout)

            self.assertEqual(summary, {
                "stage": "content_candidate_review",
                "status": "candidate_ready",
                "request_id": work_order["request_id"],
            })
            self.assertEqual(
                framecue.workspace_snapshot(database, package["review_id"], "test")["stage"],
                "content_candidate_review",
            )
            with self.assertRaises(subprocess.CalledProcessError):
                self._submit(root, database, "repeat.json", self._candidate(work_order))

    def test_content_candidate_rejects_out_of_range_or_immutable_changes_without_writing_state(self):
        with tempfile.TemporaryDirectory(prefix="framecue-candidate-v2-closed-") as temp:
            root = Path(temp)
            database, package, work_order = self._correction_order(root)
            candidate = self._candidate(work_order)

            outside_range = copy.deepcopy(candidate)
            outside_range["document"]["cues"][1]["display_text"] = "未標記範圍不能被修改"
            framecue.recompute_draft_blocks(outside_range["document"])
            framecue.refresh_document_checksum(outside_range["document"])
            with self.assertRaises(subprocess.CalledProcessError):
                self._submit(root, database, "outside.json", outside_range)

            immutable_field = copy.deepcopy(candidate)
            immutable_field["document"]["cues"][0]["source_text"] = "The source cannot change."
            framecue.refresh_document_checksum(immutable_field["document"])
            with self.assertRaises(subprocess.CalledProcessError):
                self._submit(root, database, "immutable.json", immutable_field)

            wrong_before = copy.deepcopy(candidate)
            wrong_before["change_proposals"][0]["before_checksum"] = "0" * 64
            with self.assertRaises(subprocess.CalledProcessError):
                self._submit(root, database, "stale.json", wrong_before)

            reordered = copy.deepcopy(candidate)
            reordered["document"]["cues"][0], reordered["document"]["cues"][1] = (
                reordered["document"]["cues"][1], reordered["document"]["cues"][0]
            )
            framecue.refresh_document_checksum(reordered["document"])
            with self.assertRaises(subprocess.CalledProcessError):
                self._submit(root, database, "reordered.json", reordered)

            forged_timing = copy.deepcopy(candidate)
            parent = forged_timing["document"]["cues"][0]
            left, right = copy.deepcopy(parent), copy.deepcopy(parent)
            boundary = (parent["source_start_ms"] + parent["source_end_ms"]) // 2
            for child, cue_id, start, end, text in (
                (left, "candidate-left", parent["source_start_ms"], boundary, "OpenClaw 仍保留"),
                (right, "candidate-right", boundary, parent["source_end_ms"], "明確的人工審稿步驟"),
            ):
                child.update({
                    "id": cue_id, "source_start_ms": start, "source_end_ms": end,
                    "output_start_ms": None, "output_end_ms": None, "timing_state": "provisional",
                    "source_text": parent["source_text"][:len(parent["source_text"]) // 2] if child is left else parent["source_text"][len(parent["source_text"]) // 2:],
                    "display_text": text, "speech_text": text, "origin_cue_ids": parent["origin_cue_ids"],
                    "lineage": {"operation": "split", "parent_cue_ids": [parent["id"]]},
                })
            left["output_start_ms"] = 123
            forged_timing["document"]["cues"][0:1] = [left, right]
            forged_timing["document"]["blocks"][0]["cue_ids"][0:1] = [left["id"], right["id"]]
            framecue.recompute_draft_blocks(forged_timing["document"])
            framecue.refresh_document_checksum(forged_timing["document"])
            forged_timing["change_proposals"][0]["replacement"] = {
                "cues": [left, right], "blocks": [forged_timing["document"]["blocks"][0]],
            }
            with self.assertRaises(subprocess.CalledProcessError):
                self._submit(root, database, "forged-timing.json", forged_timing)

            left["output_start_ms"] = None
            framecue.refresh_document_checksum(forged_timing["document"])
            summary = json.loads(self._submit(root, database, "valid-split.json", forged_timing).stdout)
            self.assertEqual(summary["stage"], "content_candidate_review")
            self.assertEqual(framecue.workspace_snapshot(database, package["review_id"], "test")["stage"], "content_candidate_review")


if __name__ == "__main__":
    unittest.main()
