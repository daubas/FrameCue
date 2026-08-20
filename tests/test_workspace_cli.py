import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


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


class WorkspaceCliTests(unittest.TestCase):
    def test_workspace_serve_dispatches_server_lifecycle(self):
        server = unittest.mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt

        with patch.object(framecue, "make_workspace_server", return_value=server) as factory:
            args = framecue.parser().parse_args([
                "workspace-serve",
                "--database",
                "DB",
                "--dir",
                "BUNDLE",
                "--port",
                "8765",
            ])
            try:
                args.func(args)
            except KeyboardInterrupt:
                pass

        factory.assert_called_once_with("DB", "BUNDLE", 8765)
        server.serve_forever.assert_called_once_with()
        server.server_close.assert_called_once_with()

    def _pending_work_order(self, root):
        bundle = root / "bundle"
        database = root / "workspace.sqlite3"
        result_path = root / "review-result.json"
        work_order_path = root / "work-order.json"
        run_cli("build", "--input", str(FIXTURE), "--out-dir", str(bundle))
        package = json.loads((bundle / "review_package.json").read_text(encoding="utf-8"))
        result = {
            "schema_version": "framecue_review_result_v1",
            "review_id": package["review_id"],
            "revision": package["revision"],
            "package_checksum": package["content_checksum"],
            "viewer_version": package["viewer_version"],
            "status": "approved",
            "approved_at": "2026-08-20T00:00:00+00:00",
            "generated_at": "2026-08-20T00:00:00+00:00",
            "blocks": [
                {
                    "id": block["id"],
                    "target_text": block["target_text"],
                    "speech_text": block["speech_text"],
                    "action": "use_edit",
                    "instruction": "",
                    "approved": True,
                }
                for block in package["blocks"]
            ],
            "cues": [
                {
                    "id": cue["id"],
                    "text": cue["text"],
                    "speech_text": cue["speech_text"],
                    "action": "use_edit",
                    "instruction": "",
                }
                for cue in package["cues"]
            ],
        }
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        run_cli(
            "workspace-import",
            "--database",
            str(database),
            "--package",
            str(bundle / "review_package.json"),
            "--timing-profile",
            "synchronous_dub",
        )
        run_cli(
            "content-complete",
            "--database",
            str(database),
            "--review-id",
            package["review_id"],
            "--result",
            str(result_path),
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

    def _candidate(self, root, work_order):
        document = copy.deepcopy(work_order["document"])
        document["revision_kind"] = "voice_aligned"
        for cue in document["cues"]:
            cue["output_start_ms"] = cue["source_start_ms"]
            cue["output_end_ms"] = cue["source_end_ms"]
        self._refresh_checksum(document)
        evidence_dir = root / "evidence"
        evidence_dir.mkdir()

        def evidence(name, payload):
            path = evidence_dir / name
            path.write_bytes(payload)
            return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}

        def wav_evidence(name):
            path = evidence_dir / name
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\x00\x00")
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        block_audio = [
            {
                "block_id": block["id"],
                **wav_evidence(f"{block['id']}.wav"),
            }
            for block in document["blocks"]
        ]
        word_alignment = {
            "schema": "agenticdub_word_alignment_v1",
            "request_id": work_order["request_id"],
            "base_checksum": work_order["base_checksum"],
            "document_checksum": document["checksum"],
            "cues": [
                {
                    "id": cue["id"],
                    "start_ms": cue["output_start_ms"],
                    "end_ms": cue["output_end_ms"],
                }
                for cue in document["cues"]
            ],
        }
        timing_audit = {
            "schema": "agenticdub_timing_audit_v1",
            "request_id": work_order["request_id"],
            "base_checksum": work_order["base_checksum"],
            "document_checksum": document["checksum"],
            "timing_profile": document["timing_profile"],
            "status": "passed",
            "overlap_count": 0,
        }
        return {
            "schema": "framecue_candidate_revision_v1",
            "request_id": work_order["request_id"],
            "base_revision": work_order["base_revision"],
            "base_checksum": work_order["base_checksum"],
            "status": "ready_for_review",
            "document": document,
            "changed_cue_ids": [],
            "changed_block_ids": [],
            "validation": {
                "word_alignment_status": "passed",
                "timing_audit_status": "passed",
            },
            "assets": {
                "block_audio": block_audio,
                "word_alignment": evidence(
                    "word-alignment.json", json.dumps(word_alignment, sort_keys=True).encode("utf-8")
                ),
                "timing_audit": evidence(
                    "timing-audit.json", json.dumps(timing_audit, sort_keys=True).encode("utf-8")
                ),
            },
        }

    def _refresh_checksum(self, document):
        payload = copy.deepcopy(document)
        payload.pop("checksum", None)
        document["checksum"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _write_candidate(self, root, name, candidate):
        path = root / name
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _submit(self, database, candidate_path):
        return run_cli("work-submit", "--database", str(database), "--candidate", str(candidate_path))

    def _assert_rejected_then_accepted(self, root, database, candidate, rejected):
        with self.assertRaises(subprocess.CalledProcessError):
            self._submit(database, self._write_candidate(root, "rejected.json", rejected))
        summary = json.loads(self._submit(database, self._write_candidate(root, "candidate.json", candidate)).stdout)
        self.assertEqual(summary["stage"], "audiovisual_review")
        self.assertEqual(summary["status"], "candidate_ready")

    def test_content_completion_creates_checksum_bound_work_order(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-cli-") as temp:
            database, package, work_order = self._pending_work_order(Path(temp))
            self.assertEqual(work_order["schema"], "framecue_work_order_v1")
            self.assertEqual(work_order["base_checksum"], work_order["document"]["checksum"])
            self.assertEqual(work_order["document"]["source_checksum"], package["content_checksum"])
            self.assertEqual(work_order["base_revision"], package["revision"])
            self.assertEqual(work_order["timing_profile"], "synchronous_dub")
            self.assertEqual(work_order["document"]["timing_profile"], "synchronous_dub")
            candidate = self._candidate(Path(temp), work_order)
            summary = json.loads(self._submit(database, self._write_candidate(Path(temp), "candidate.json", candidate)).stdout)
            self.assertEqual(summary["stage"], "audiovisual_review")
            self.assertEqual(summary["status"], "candidate_ready")
            self.assertEqual(summary["request_id"], work_order["request_id"])

    def test_work_submit_rejects_immutable_document_changes(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-immutable-") as temp:
            root = Path(temp)
            database, _, work_order = self._pending_work_order(root)
            candidate = self._candidate(root, work_order)
            rejected = copy.deepcopy(candidate)
            rejected["document"]["source_checksum"] = "tampered"
            self._refresh_checksum(rejected["document"])
            self._assert_rejected_then_accepted(root, database, candidate, rejected)

    def test_work_submit_rejects_overlapping_output_ranges(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-overlap-") as temp:
            root = Path(temp)
            database, _, work_order = self._pending_work_order(root)
            candidate = self._candidate(root, work_order)
            rejected = copy.deepcopy(candidate)
            first, second = rejected["document"]["cues"][:2]
            second["output_start_ms"] = first["output_end_ms"] - 1
            self._refresh_checksum(rejected["document"])
            self._assert_rejected_then_accepted(root, database, candidate, rejected)

    def test_work_submit_rejects_unverified_evidence(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-evidence-") as temp:
            root = Path(temp)
            database, _, work_order = self._pending_work_order(root)
            candidate = self._candidate(root, work_order)
            rejected = copy.deepcopy(candidate)
            rejected["assets"]["word_alignment"]["sha256"] = "0" * 64
            self._assert_rejected_then_accepted(root, database, candidate, rejected)

    def test_work_submit_rejects_opaque_block_audio(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-opaque-audio-") as temp:
            root = Path(temp)
            database, _, work_order = self._pending_work_order(root)
            candidate = self._candidate(root, work_order)
            rejected = copy.deepcopy(candidate)
            audio = rejected["assets"]["block_audio"][0]
            payload = b"not a WAV"
            path = root / "opaque.wav"
            path.write_bytes(payload)
            audio["path"] = str(path)
            audio["sha256"] = hashlib.sha256(payload).hexdigest()
            self._assert_rejected_then_accepted(root, database, candidate, rejected)

    def test_work_submit_rejects_unbound_word_alignment(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-word-alignment-") as temp:
            root = Path(temp)
            database, _, work_order = self._pending_work_order(root)
            candidate = self._candidate(root, work_order)
            rejected = copy.deepcopy(candidate)
            alignment = json.loads(Path(rejected["assets"]["word_alignment"]["path"]).read_text(encoding="utf-8"))
            alignment["document_checksum"] = "0" * 64
            payload = json.dumps(alignment, sort_keys=True).encode("utf-8")
            path = root / "unbound-word-alignment.json"
            path.write_bytes(payload)
            rejected["assets"]["word_alignment"] = {
                "path": str(path),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            self._assert_rejected_then_accepted(root, database, candidate, rejected)

    def test_work_submit_rejects_unbound_timing_audit(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-timing-audit-") as temp:
            root = Path(temp)
            database, _, work_order = self._pending_work_order(root)
            candidate = self._candidate(root, work_order)
            rejected = copy.deepcopy(candidate)
            audit = json.loads(Path(rejected["assets"]["timing_audit"]["path"]).read_text(encoding="utf-8"))
            audit["timing_profile"] = "interpreter_lag"
            payload = json.dumps(audit, sort_keys=True).encode("utf-8")
            path = root / "unbound-timing-audit.json"
            path.write_bytes(payload)
            rejected["assets"]["timing_audit"] = {
                "path": str(path),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            self._assert_rejected_then_accepted(root, database, candidate, rejected)


if __name__ == "__main__":
    unittest.main()
