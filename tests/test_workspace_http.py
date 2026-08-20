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
