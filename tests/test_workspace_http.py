import copy
import json
import hashlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import framecue


ROOT = Path(__file__).resolve().parents[1]
FRAMECUE = ROOT / "framecue.py"
FIXTURE = ROOT / "tests" / "fixtures" / "basic" / "package.source.json"


def run_cli(*arguments):
    return subprocess.run(
        [sys.executable, str(FRAMECUE), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def approved_result(package):
    timestamp = "2026-08-20T00:00:00+00:00"
    return {
        "schema_version": "framecue_review_result_v1",
        "review_id": package["review_id"],
        "revision": package["revision"],
        "package_checksum": package["content_checksum"],
        "viewer_version": package["viewer_version"],
        "status": "approved",
        "approved_at": timestamp,
        "generated_at": timestamp,
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


class WorkspaceHTTPTests(unittest.TestCase):
    def _workspace_server(self, root):
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
            f"{base}{path}",
            data=data,
            method=method,
            headers=request_headers,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, json.loads(response.read().decode("utf-8"))

    def _workspace_headers(self, base, snapshot, session_id):
        return {
            "Origin": base,
            "X-FrameCue-CSRF": snapshot["csrf_token"],
            "X-FrameCue-Session": session_id,
        }

    def _agent_token(self, database, package, *permissions, label="agent"):
        result = run_cli(
            "agent-token-create",
            "--database", str(database),
            "--label", label,
            "--workspace", package["review_id"],
            *sum((["--permission", permission] for permission in permissions), []),
        )
        return json.loads(result.stdout)

    def _pending_order_server(self, root):
        database, package, server, thread = self._workspace_server(root)
        summary = framecue.complete_workspace_round(database, package["review_id"], 0)
        return database, package, summary, server, thread

    def _correction_order_server(self, root):
        database, package, server, thread = self._workspace_server(root)
        framecue.apply_draft_operation(database, package["review_id"], {
            "kind": "flag", "draft_version": 0, "cue_id": "c0001",
            "categories": ["translation"], "author": "lead", "note": "please revise",
        })
        summary = framecue.complete_workspace_round(database, package["review_id"], 1)
        return database, package, summary, server, thread

    def _content_candidate(self, database, request_id):
        connection = framecue.open_workspace_database(database)
        row = connection.execute(
            "SELECT request_json FROM work_orders WHERE request_id = ?", (request_id,)
        ).fetchone()
        connection.close()
        order = json.loads(row["request_json"])
        document = copy.deepcopy(order["document"])
        target = order["targets"][0]
        cue = next(value for value in document["cues"] if value["id"] == target["cue_ids"][0])
        cue["display_text"] = "已修正字幕"
        cue["speech_text"] = "已修正字幕。"
        framecue.recompute_draft_blocks(document)
        framecue.refresh_document_checksum(document)
        return {
            "schema": "framecue_candidate_revision_v2",
            "status": "ready_for_review",
            "request_id": request_id,
            "workspace_id": order["workspace_id"],
            "operation": order["operation"],
            "base_revision": order["base_revision"],
            "base_draft_version": order["base_draft_version"],
            "base_checksum": order["base_checksum"],
            "document": document,
            "change_proposals": [{
                "proposal_id": "proposal-1",
                "range_id": target["range_id"],
                "before_checksum": target["before_checksum"],
                "replacement": {
                    "cues": [copy.deepcopy(next(value for value in document["cues"] if value["id"] == cue_id)) for cue_id in target["cue_ids"]],
                    "blocks": [copy.deepcopy(next(value for value in document["blocks"] if value["id"] == block_id)) for block_id in target["block_ids"]],
                },
            }],
        }

    def test_agent_token_cli_stores_only_hash_and_revocation_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="framecue-agent-token-") as temp:
            database, package, server, thread = self._workspace_server(Path(temp))
            try:
                token = self._agent_token(database, package, "list", "read", "claim")
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                row = connection.execute("SELECT * FROM agent_tokens WHERE token_id = ?", (token["token_id"],)).fetchone()
                work_orders_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'work_orders'"
                ).fetchone()["sql"]
                columns = {item["name"] for item in connection.execute("PRAGMA table_info(work_orders)")}
                connection.close()
                self.assertEqual(row["token_hash"], hashlib.sha256(token["token"].encode()).hexdigest())
                self.assertNotIn(token["token"], json.dumps(dict(row)))
                self.assertNotIn("UNIQUE(review_id, base_revision, base_checksum, operation)", work_orders_sql)
                self.assertTrue({"lease_owner_token_id", "lease_expires_at", "attempt_count", "last_error_json"} <= columns)

                for _ in range(2):
                    revoked = run_cli(
                        "agent-token-revoke", "--database", str(database), "--token-id", token["token_id"]
                    )
                    self.assertEqual(json.loads(revoked.stdout)["status"], "revoked")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_agent_list_and_read_enforce_bearer_permission_scope_and_revocation(self):
        with tempfile.TemporaryDirectory(prefix="framecue-agent-read-") as temp:
            root = Path(temp)
            database, package, summary, server, thread = self._pending_order_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                token = self._agent_token(database, package, "list", "read")
                auth = {"Authorization": f"Bearer {token['token']}"}
                _, _, listed = self._json_request(
                    base, f"/api/agent/work-orders?workspace_id={package['review_id']}", headers=auth
                )
                self.assertEqual([row["request_id"] for row in listed["work_orders"]], [summary["request_id"]])
                self.assertNotIn("token_hash", json.dumps(listed))
                self.assertNotIn("document", listed["work_orders"][0])
                self.assertNotIn("targets", listed["work_orders"][0])
                _, _, read = self._json_request(base, f"/api/agent/work-orders/{summary['request_id']}", headers=auth)
                self.assertEqual(read["request_id"], summary["request_id"])

                for headers, code in [({}, 401), ({"Authorization": "Bearer wrong"}, 401)]:
                    request = urllib.request.Request(
                        f"{base}/api/agent/work-orders?workspace_id={package['review_id']}", headers=headers
                    )
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(failure.exception.code, code)

                claim_only = self._agent_token(database, package, "claim", label="claim-only")
                request = urllib.request.Request(
                    f"{base}/api/agent/work-orders?workspace_id={package['review_id']}",
                    headers={"Authorization": f"Bearer {claim_only['token']}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                outside = run_cli(
                    "agent-token-create", "--database", str(database), "--label", "outside",
                    "--workspace", "another-workspace", "--permission", "read",
                )
                outside_token = json.loads(outside.stdout)["token"]
                request = urllib.request.Request(
                    f"{base}/api/agent/work-orders/{summary['request_id']}",
                    headers={"Authorization": f"Bearer {outside_token}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                run_cli("agent-token-revoke", "--database", str(database), "--token-id", token["token_id"])
                request = urllib.request.Request(
                    f"{base}/api/agent/work-orders/{summary['request_id']}", headers=auth
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(failure.exception.code, 401)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_agent_claim_is_atomic_renewable_and_expired_claim_returns_to_pending(self):
        with tempfile.TemporaryDirectory(prefix="framecue-agent-claim-") as temp:
            root = Path(temp)
            database, package, summary, server, thread = self._pending_order_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                first = self._agent_token(database, package, "claim", "read", label="first")
                second = self._agent_token(database, package, "claim", "read", label="second")
                third = self._agent_token(database, package, "claim", "read", label="third")

                def claim(token):
                    return self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/claim", method="POST",
                        headers={"Authorization": f"Bearer {token['token']}"},
                    )[2]

                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:00:00+00:00"):
                    claimed = claim(first)
                self.assertEqual(claimed["status"], "processing")
                self.assertEqual(claimed["attempt_count"], 1)
                self.assertEqual(claimed["lease_expires_at"], "2026-08-20T00:05:00+00:00")
                self.assertNotIn("document", claimed)

                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:01:00+00:00"):
                    renewed = claim(first)
                self.assertEqual(renewed["attempt_count"], 1)
                self.assertEqual(renewed["lease_expires_at"], "2026-08-20T00:06:00+00:00")

                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:02:00+00:00"):
                    request = urllib.request.Request(
                        f"{base}/api/agent/work-orders/{summary['request_id']}/claim", data=b"", method="POST",
                        headers={"Authorization": f"Bearer {second['token']}"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(failure.exception.code, 409)

                run_cli(
                    "agent-token-revoke", "--database", str(database), "--token-id", first["token_id"]
                )
                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:02:00+00:00"):
                    reclaimed_after_revoke = claim(second)
                self.assertEqual(reclaimed_after_revoke["attempt_count"], 2)
                self.assertEqual(reclaimed_after_revoke["lease_owner_token_id"], second["token_id"])

                pull = root / "processing.json"
                with self.assertRaises(subprocess.CalledProcessError):
                    run_cli(
                        "work-pull", "--database", str(database), "--review-id", package["review_id"],
                        "--out", str(pull),
                    )

                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:08:00+00:00"):
                    reclaimed = claim(third)
                self.assertEqual(reclaimed["attempt_count"], 3)
                self.assertEqual(reclaimed["lease_owner_token_id"], third["token_id"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_agent_owner_can_submit_valid_content_candidate_but_other_and_expired_leases_cannot(self):
        with tempfile.TemporaryDirectory(prefix="framecue-agent-submit-") as temp:
            root = Path(temp)
            database, package, summary, server, thread = self._correction_order_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                owner = self._agent_token(database, package, "claim", "submit", label="owner")
                other = self._agent_token(database, package, "submit", label="other")
                owner_auth = {"Authorization": f"Bearer {owner['token']}"}
                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:00:00+00:00"):
                    self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/claim",
                        method="POST", headers=owner_auth,
                    )
                candidate = self._content_candidate(database, summary["request_id"])
                candidate_path = root / "candidate.json"
                candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(subprocess.CalledProcessError):
                    run_cli("work-submit", "--database", str(database), "--candidate", str(candidate_path))

                wrong_permission = self._agent_token(database, package, "read", label="reader")
                request = urllib.request.Request(
                    f"{base}/api/agent/work-orders/{summary['request_id']}/submit",
                    data=json.dumps(candidate).encode(), method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {wrong_permission['token']}",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:01:00+00:00"):
                    request = urllib.request.Request(
                        f"{base}/api/agent/work-orders/{summary['request_id']}/submit",
                        data=json.dumps(candidate).encode(), method="POST",
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {other['token']}"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(failure.exception.code, 409)

                    _, _, submitted = self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/submit",
                        method="POST", value=candidate, headers=owner_auth,
                    )
                self.assertEqual(submitted["status"], "candidate_ready")
                self.assertEqual(submitted["stage"], "content_candidate_review")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_agent_fail_and_retry_replace_request_id_atomically(self):
        with tempfile.TemporaryDirectory(prefix="framecue-agent-retry-") as temp:
            root = Path(temp)
            database, package, summary, server, thread = self._correction_order_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                worker = self._agent_token(database, package, "claim", "fail", "retry")
                auth = {"Authorization": f"Bearer {worker['token']}"}
                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:00:00+00:00"):
                    self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/claim",
                        method="POST", headers=auth,
                    )
                    _, _, failed = self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/fail",
                        method="POST",
                        value={"category": "provider", "message": "temporary outage", "retryable": True},
                        headers=auth,
                    )
                expected_error = {"category": "provider", "message": "temporary outage", "retryable": True}
                self.assertEqual(failed, {
                    "request_id": summary["request_id"], "status": "failed", "last_error": expected_error,
                })
                connection = framecue.open_workspace_database(database)
                failed_row = connection.execute("SELECT * FROM work_orders").fetchone()
                connection.close()
                self.assertEqual(failed_row["last_error_json"], framecue.canonical_json(expected_error))
                self.assertIsNone(failed_row["lease_owner_token_id"])

                outside = json.loads(run_cli(
                    "agent-token-create", "--database", str(database), "--label", "outside",
                    "--workspace", "different", "--permission", "retry",
                ).stdout)
                request = urllib.request.Request(
                    f"{base}/api/agent/work-orders/{summary['request_id']}/retry",
                    data=b"{}", method="POST",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {outside['token']}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                _, _, retried = self._json_request(
                    base, f"/api/agent/work-orders/{summary['request_id']}/retry",
                    method="POST", value={}, headers=auth,
                )
                self.assertEqual(retried["status"], "pending")
                self.assertNotEqual(retried["request_id"], summary["request_id"])
                connection = framecue.open_workspace_database(database)
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM work_orders WHERE request_id = ?", (summary["request_id"],)
                ).fetchone())
                row = connection.execute("SELECT * FROM work_orders").fetchone()
                connection.close()
                self.assertEqual(json.loads(row["request_json"])["request_id"], retried["request_id"])
                self.assertEqual(row["attempt_count"], 1)
                self.assertIsNone(row["last_error_json"])
                self.assertIsNone(row["candidate_json"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_agent_fail_retry_reject_nonowner_expired_invalid_nonretryable_and_wrong_permission(self):
        def post_expect(base, path, token, value, code):
            request = urllib.request.Request(
                f"{base}{path}", data=json.dumps(value).encode(), method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            )
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(failure.exception.code, code)

        with tempfile.TemporaryDirectory(prefix="framecue-agent-fail-closed-") as temp:
            root = Path(temp)
            database, package, summary, server, thread = self._correction_order_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                owner = self._agent_token(database, package, "claim", "fail", "retry")
                other = self._agent_token(database, package, "fail", label="other")
                wrong = self._agent_token(database, package, "read", label="reader")
                owner_auth = {"Authorization": f"Bearer {owner['token']}"}
                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:00:00+00:00"):
                    self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/claim",
                        method="POST", headers=owner_auth,
                    )
                    post_expect(
                        base, f"/api/agent/work-orders/{summary['request_id']}/retry",
                        owner["token"], {}, 409,
                    )
                    post_expect(
                        base, f"/api/agent/work-orders/{summary['request_id']}/fail",
                        other["token"], {"category": "x", "message": "x", "retryable": True}, 409,
                    )
                    post_expect(
                        base, f"/api/agent/work-orders/{summary['request_id']}/fail",
                        wrong["token"], {"category": "x", "message": "x", "retryable": True}, 403,
                    )
                    for invalid in (
                        {"category": "", "message": "x", "retryable": True},
                        {"category": "x" * 65, "message": "x", "retryable": True},
                        {"category": "x", "message": "", "retryable": True},
                        {"category": "x", "message": "x" * 2001, "retryable": True},
                        {"category": "x", "message": "x", "retryable": 1},
                    ):
                        post_expect(
                            base, f"/api/agent/work-orders/{summary['request_id']}/fail",
                            owner["token"], invalid, 409,
                        )
                    self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/fail",
                        method="POST",
                        value={"category": "validation", "message": "bad input", "retryable": False},
                        headers=owner_auth,
                    )
                post_expect(
                    base, f"/api/agent/work-orders/{summary['request_id']}/retry",
                    owner["token"], {}, 409,
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

        with tempfile.TemporaryDirectory(prefix="framecue-agent-fail-expired-") as temp:
            root = Path(temp)
            database, package, summary, server, thread = self._correction_order_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                owner = self._agent_token(database, package, "claim", "fail")
                auth = {"Authorization": f"Bearer {owner['token']}"}
                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:00:00+00:00"):
                    self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/claim",
                        method="POST", headers=auth,
                    )
                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:06:00+00:00"):
                    post_expect(
                        base, f"/api/agent/work-orders/{summary['request_id']}/fail",
                        owner["token"], {"category": "x", "message": "x", "retryable": True}, 409,
                    )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

        with tempfile.TemporaryDirectory(prefix="framecue-agent-submit-expired-") as temp:
            root = Path(temp)
            database, package, summary, server, thread = self._correction_order_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                owner = self._agent_token(database, package, "claim", "submit")
                auth = {"Authorization": f"Bearer {owner['token']}"}
                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:00:00+00:00"):
                    self._json_request(
                        base, f"/api/agent/work-orders/{summary['request_id']}/claim",
                        method="POST", headers=auth,
                    )
                candidate = self._content_candidate(database, summary["request_id"])
                with mock.patch("framecue.agent_utc_now", return_value="2026-08-20T00:06:00+00:00"):
                    request = urllib.request.Request(
                        f"{base}/api/agent/work-orders/{summary['request_id']}/submit",
                        data=json.dumps(candidate).encode(), method="POST",
                        headers={"Content-Type": "application/json", **auth},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(failure.exception.code, 409)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_workspace_v2_snapshot_operations_and_sse_only_publish_reload_versions(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-v2-http-") as temp:
            root = Path(temp)
            _, package, server, thread = self._workspace_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, headers, snapshot = self._json_request(
                    base,
                    "/api/workspace/snapshot",
                    headers={"X-FrameCue-Display-Name": "Alice"},
                )
                self.assertEqual(snapshot["schema"], "framecue_workspace_snapshot_v2")
                self.assertEqual(snapshot["workspace_id"], package["review_id"])
                self.assertEqual(snapshot["stage"], "content_review")
                self.assertEqual(snapshot["draft_version"], 0)
                self.assertEqual(snapshot["issues"], [])
                self.assertEqual(snapshot["direct_edit_count"], 0)
                self.assertEqual(snapshot["document"]["schema"], "framecue_subtitle_document_v2")
                self.assertTrue(snapshot["csrf_token"])
                self.assertTrue(snapshot["session_id"])
                self.assertEqual(snapshot["display_name"], "Alice")
                self.assertEqual(snapshot["lead_session_id"], snapshot["session_id"])
                self.assertIn("framecue_session=", headers["Set-Cookie"])

                session_id = snapshot["session_id"]
                operation_headers = self._workspace_headers(base, snapshot, session_id)
                legacy_completion = urllib.request.Request(
                    f"{base}/api/content-complete",
                    data=json.dumps(approved_result(package), ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", **operation_headers},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(legacy_completion, timeout=5)
                self.assertEqual(failure.exception.code, 409)

                _, _, changed = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "edit",
                        "draft_version": 0,
                        "cue_id": "c0001",
                        "display_text": "已由伺服器保存的字幕",
                    },
                    headers=operation_headers,
                )
                self.assertEqual(changed["draft_version"], 1)
                self.assertEqual(changed["document"]["cues"][0]["display_text"], "已由伺服器保存的字幕")

                _, _, flagged = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "flag",
                        "draft_version": 1,
                        "cue_ids": ["c0001"],
                        "categories": ["other"],
                        "author": "Alice",
                    },
                    headers=operation_headers,
                )
                self.assertEqual(len(flagged["issues"]), 1)
                _, _, unflagged = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "flag",
                        "draft_version": 2,
                        "cue_ids": ["c0001"],
                        "categories": ["other"],
                        "author": "Alice",
                        "enabled": False,
                    },
                    headers=operation_headers,
                )
                self.assertEqual(unflagged["issues"], [])

                events = urllib.request.Request(
                    f"{base}/api/workspace/events",
                    headers={
                        "X-FrameCue-Session": session_id,
                        "Last-Event-ID": "0",
                    },
                )
                with urllib.request.urlopen(events, timeout=5) as response:
                    event_body = response.read().decode("utf-8")
                    self.assertTrue(response.headers["Content-Type"].startswith("text/event-stream"))
                self.assertIn("event: snapshot", event_body)
                self.assertIn("data: {\"version\":", event_body)
                self.assertNotIn("document", event_body)
                self.assertNotIn("cues", event_body)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_workspace_collaboration_blocks_locked_or_dirty_completion_and_requires_the_lead(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-collaboration-") as temp:
            root = Path(temp)
            _, _, server, thread = self._workspace_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, alice = self._json_request(
                    base,
                    "/api/workspace/snapshot",
                    headers={"X-FrameCue-Display-Name": "Alice"},
                )
                _, _, bob = self._json_request(
                    base,
                    "/api/workspace/snapshot",
                    headers={"X-FrameCue-Display-Name": "Bob"},
                )
                alice_headers = self._workspace_headers(base, alice, alice["session_id"])
                bob_headers = self._workspace_headers(base, bob, bob["session_id"])

                self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "lock", "draft_version": 0, "cue_ids": ["c0001"]},
                    headers=alice_headers,
                )
                locked_edit = urllib.request.Request(
                    f"{base}/api/workspace/operation",
                    data=json.dumps({
                        "kind": "edit",
                        "draft_version": 0,
                        "cue_id": "c0001",
                        "display_text": "Bob 不可以覆寫鎖定中的 Cue",
                    }, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", **bob_headers},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(locked_edit, timeout=5)
                self.assertEqual(failure.exception.code, 409)

                _, _, changed = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "edit",
                        "draft_version": 0,
                        "cue_id": "c0002",
                        "display_text": "Bob 可以編輯未鎖定的 Cue",
                    },
                    headers=bob_headers,
                )
                self.assertEqual(changed["draft_version"], 1)

                not_lead = urllib.request.Request(
                    f"{base}/api/workspace/complete",
                    data=json.dumps({"draft_version": 1}).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", **bob_headers},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(not_lead, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "dirty", "dirty": True},
                    headers=alice_headers,
                )
                blocked = urllib.request.Request(
                    f"{base}/api/workspace/complete",
                    data=json.dumps({"draft_version": 1}).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", **alice_headers},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(blocked, timeout=5)
                self.assertEqual(failure.exception.code, 409)

                self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "unlock", "draft_version": 1, "cue_ids": ["c0001"]},
                    headers=alice_headers,
                )
                self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "dirty", "dirty": False},
                    headers=alice_headers,
                )
                _, _, completed = self._json_request(
                    base,
                    "/api/workspace/complete",
                    method="POST",
                    value={"draft_version": 1},
                    headers=alice_headers,
                )
                self.assertEqual(completed["stage"], "content_agent_review_pending")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_workspace_posts_reject_unknown_sessions_until_snapshot_registers_again(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-session-restart-") as temp:
            root = Path(temp)
            database, _, server, thread = self._workspace_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, first = self._json_request(base, "/api/workspace/snapshot")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            bundle = root / "bundle"
            restarted = framecue.make_workspace_server(database, bundle, port=0)
            restarted_thread = threading.Thread(target=restarted.serve_forever, daemon=True)
            restarted_thread.start()
            try:
                base = f"http://127.0.0.1:{restarted.server_address[1]}"
                _, _, bootstrap = self._json_request(base, "/api/workspace")
                stale = urllib.request.Request(
                    f"{base}/api/workspace/operation",
                    data=json.dumps({"kind": "dirty", "dirty": True}).encode("utf-8"),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                        "X-FrameCue-CSRF": bootstrap["csrf_token"],
                        "X-FrameCue-Session": first["session_id"],
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(stale, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                _, _, registered = self._json_request(
                    base,
                    "/api/workspace/snapshot",
                    headers={"X-FrameCue-Session": first["session_id"]},
                )
                self.assertNotEqual(registered["session_id"], first["session_id"])
            finally:
                restarted.shutdown()
                restarted_thread.join(timeout=5)
                restarted.server_close()

    def test_workspace_presence_and_lead_transfer_are_version_bound(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-lead-") as temp:
            root = Path(temp)
            _, _, server, thread = self._workspace_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                _, _, alice = self._json_request(
                    base,
                    "/api/workspace/snapshot",
                    headers={"X-FrameCue-Display-Name": "Alice"},
                )
                _, _, bob = self._json_request(
                    base,
                    "/api/workspace/snapshot",
                    headers={"X-FrameCue-Display-Name": "Bob"},
                )
                alice_headers = self._workspace_headers(base, alice, alice["session_id"])
                bob_headers = self._workspace_headers(base, bob, bob["session_id"])

                _, _, present = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={"kind": "presence", "selected_cue_id": "c0001"},
                    headers=alice_headers,
                )
                alice_presence = next(item for item in present["participants"] if item["session_id"] == alice["session_id"])
                self.assertEqual(alice_presence["selected_cue_id"], "c0001")

                _, _, transferred = self._json_request(
                    base,
                    "/api/workspace/operation",
                    method="POST",
                    value={
                        "kind": "lead",
                        "draft_version": 0,
                        "expected_lead_session_id": alice["session_id"],
                        "new_lead_session_id": bob["session_id"],
                    },
                    headers=alice_headers,
                )
                self.assertEqual(transferred["lead_session_id"], bob["session_id"])

                old_lead = urllib.request.Request(
                    f"{base}/api/workspace/complete",
                    data=json.dumps({"draft_version": 0}).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json", **alice_headers},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(old_lead, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                _, _, completed = self._json_request(
                    base,
                    "/api/workspace/complete",
                    method="POST",
                    value={"draft_version": 0},
                    headers=bob_headers,
                )
                self.assertEqual(completed["stage"], "voice_realization_pending")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_workspace_locks_renew_for_fifteen_seconds_and_then_expire(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-lock-ttl-") as temp:
            root = Path(temp)
            _, _, server, thread = self._workspace_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with mock.patch.object(framecue.time, "monotonic", return_value=100):
                    _, _, alice = self._json_request(base, "/api/workspace/snapshot")
                    _, _, bob = self._json_request(base, "/api/workspace/snapshot")
                    alice_headers = self._workspace_headers(base, alice, alice["session_id"])
                    bob_headers = self._workspace_headers(base, bob, bob["session_id"])
                    self._json_request(
                        base,
                        "/api/workspace/operation",
                        method="POST",
                        value={"kind": "lock", "draft_version": 0, "cue_ids": ["c0001"]},
                        headers=alice_headers,
                    )

                with mock.patch.object(framecue.time, "monotonic", return_value=104):
                    self._json_request(
                        base,
                        "/api/workspace/operation",
                        method="POST",
                        value={"kind": "lock", "draft_version": 0, "cue_ids": ["c0001"]},
                        headers=alice_headers,
                    )

                with mock.patch.object(framecue.time, "monotonic", return_value=116):
                    still_locked = urllib.request.Request(
                        f"{base}/api/workspace/operation",
                        data=json.dumps({
                            "kind": "edit",
                            "draft_version": 0,
                            "cue_id": "c0001",
                            "display_text": "renewed lock still owns this Cue",
                        }).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json", **bob_headers},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        urllib.request.urlopen(still_locked, timeout=5)
                    self.assertEqual(failure.exception.code, 409)

                with mock.patch.object(framecue.time, "monotonic", return_value=120):
                    _, _, changed = self._json_request(
                        base,
                        "/api/workspace/operation",
                        method="POST",
                        value={
                            "kind": "edit",
                            "draft_version": 0,
                            "cue_id": "c0001",
                            "display_text": "expired lock no longer blocks this Cue",
                        },
                        headers=bob_headers,
                    )
                self.assertEqual(changed["draft_version"], 1)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_workspace_timeout_clears_disconnected_dirty_state_before_lead_takeover(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-timeout-") as temp:
            root = Path(temp)
            _, _, server, thread = self._workspace_server(root)
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with mock.patch.object(framecue.time, "monotonic", return_value=100):
                    _, _, alice = self._json_request(base, "/api/workspace/snapshot")
                    _, _, bob = self._json_request(base, "/api/workspace/snapshot")
                    alice_headers = self._workspace_headers(base, alice, alice["session_id"])
                    bob_headers = self._workspace_headers(base, bob, bob["session_id"])
                    self._json_request(
                        base,
                        "/api/workspace/operation",
                        method="POST",
                        value={"kind": "dirty", "dirty": True},
                        headers=alice_headers,
                    )

                with mock.patch.object(framecue.time, "monotonic", return_value=119):
                    self._json_request(
                        base,
                        "/api/workspace/snapshot",
                        headers={"X-FrameCue-Session": bob["session_id"]},
                    )

                with mock.patch.object(framecue.time, "monotonic", return_value=121):
                    _, _, lead = self._json_request(
                        base,
                        "/api/workspace/operation",
                        method="POST",
                        value={
                            "kind": "lead",
                            "draft_version": 0,
                            "expected_lead_session_id": alice["session_id"],
                            "new_lead_session_id": bob["session_id"],
                        },
                        headers=bob_headers,
                    )
                    self.assertEqual(lead["lead_session_id"], bob["session_id"])
                    _, _, completed = self._json_request(
                        base,
                        "/api/workspace/complete",
                        method="POST",
                        value={"draft_version": 0},
                        headers=bob_headers,
                    )
                self.assertEqual(completed["stage"], "voice_realization_pending")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_server_serves_current_viewer_without_mutating_bundle(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-viewer-") as temp:
            root = Path(temp)
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

            current_index = (ROOT / "dist" / "index.html").read_bytes()
            script_src = re.search(
                rb'<script[^>]+src="([^"]+)"',
                current_index,
            ).group(1).decode("utf-8")
            script_path = script_src[2:] if script_src.startswith("./") else script_src
            sentinel = b"legacy FrameCue viewer"
            (bundle / "index.html").write_bytes(sentinel)

            server = framecue.make_workspace_server(database, bundle, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), current_index)

                with urllib.request.urlopen(f"{base}/{script_path}", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            self.assertEqual((bundle / "index.html").read_bytes(), sentinel)

    def test_static_asset_supports_single_byte_range(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-range-") as temp:
            root = Path(temp)
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
            asset = b"0123456789"
            (bundle / "range-fixture.bin").write_bytes(asset)

            server = framecue.make_workspace_server(database, bundle, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/range-fixture.bin"
                request = urllib.request.Request(url, headers={"Range": "bytes=2-5"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read()
                    self.assertEqual(response.status, 206)
                    self.assertEqual(body, asset[2:6])
                    self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
                    self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                    self.assertEqual(response.headers["Content-Length"], "4")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_content_completion_requires_csrf_and_creates_pending_work_order(self):
        with tempfile.TemporaryDirectory(prefix="framecue-workspace-http-") as temp:
            root = Path(temp)
            bundle = root / "bundle"
            database = root / "workspace.sqlite3"
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

            server = framecue.make_workspace_server(database, bundle, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/api/workspace", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    bootstrap = json.loads(response.read().decode("utf-8"))
                self.assertEqual(bootstrap["mode"], "server")
                self.assertEqual(bootstrap["workspace_id"], package["review_id"])
                self.assertEqual(bootstrap["stage"], "content_review")
                self.assertEqual(bootstrap["endpoint"], "/api/content-complete")
                self.assertIsInstance(bootstrap["csrf"], str)
                self.assertTrue(bootstrap["csrf"])

                body = json.dumps(approved_result(package), ensure_ascii=False).encode("utf-8")
                missing_csrf = urllib.request.Request(
                    f"{base}{bootstrap['endpoint']}",
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json", "Origin": base},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(missing_csrf, timeout=5)
                self.assertEqual(failure.exception.code, 403)

                request = urllib.request.Request(
                    f"{base}{bootstrap['endpoint']}",
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                        "X-FrameCue-CSRF": bootstrap["csrf"],
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    summary = json.loads(response.read().decode("utf-8"))
                self.assertEqual(summary["stage"], "voice_realization_pending")

                repeated_result = json.loads(body.decode("utf-8"))
                repeated_result["generated_at"] = "2026-08-20T00:00:01+00:00"
                repeated_request = urllib.request.Request(
                    f"{base}{bootstrap['endpoint']}",
                    data=json.dumps(repeated_result, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                        "X-FrameCue-CSRF": bootstrap["csrf"],
                    },
                )
                with urllib.request.urlopen(repeated_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    repeated = json.loads(response.read().decode("utf-8"))
                self.assertEqual(repeated, summary)

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
                self.assertEqual(work_order["workspace_id"], package["review_id"])
                self.assertEqual(work_order["operation"], "realize_voice_timeline")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
