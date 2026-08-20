import json
import re
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
