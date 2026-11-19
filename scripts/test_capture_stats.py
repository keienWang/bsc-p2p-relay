# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import json
import pathlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import capture_stats
from test_stats_delta import sample


class CaptureStatsTests(unittest.TestCase):
    def test_literal_loopback_only(self):
        self.assertEqual(capture_stats.loopback_base("127.0.0.1:8787"), "http://127.0.0.1:8787")
        self.assertEqual(capture_stats.loopback_base("[::1]:8787"), "http://[::1]:8787")
        for address in ("localhost:8787", "example.com:80", "192.0.2.1:80", "0.0.0.0:80", "[::]:80",
                        "127.0.0.1", "127.0.0.1:0", "127.0.0.1:65536", "user@127.0.0.1:80",
                        "127.0.0.1:80/", "127.0.0.1:80?q=1", "127.0.0.1:80#x", "[::1%lo]:80",
                        "https://127.0.0.1:80"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                capture_stats.loopback_base(address)

    def test_atomic_new_file_and_baseline_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "stats.json"
            capture_stats.save_new_snapshot(target, sample())
            original = target.read_bytes()
            self.assertEqual(json.loads(original), sample())
            with self.assertRaises(FileExistsError):
                capture_stats.save_new_snapshot(target, {"different": True})
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [target])

    def test_publication_failure_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("capture_stats.os.link", side_effect=OSError("fixture publication failure")):
                with self.assertRaises(OSError):
                    capture_stats.save_new_snapshot(pathlib.Path(directory) / "stats.json", sample())
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_actual_loopback_capture_and_redirect_refusal(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.server.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                    self.end_headers()
                    return
                body = json.dumps(sample()).encode() if self.server.valid else b"{}"
                self.send_response(200)
                for key, value in (("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                                   ("X-Content-Type-Options", "nosniff"), ("Content-Length", str(len(body)))):
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.redirect, server.valid = False, True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}):
                target = pathlib.Path(directory) / "stats.json"
                address = "127.0.0.1:" + str(server.server_port)
                capture_stats.capture(address, target)
                self.assertEqual(json.loads(target.read_bytes()), sample())
                server.redirect = True
                with self.assertRaises(OSError):
                    capture_stats.capture(address, pathlib.Path(directory) / "redirect.json")
                self.assertNotIn("/must-not-follow", requests)
                server.redirect, server.valid = False, False
                with self.assertRaises(ValueError):
                    capture_stats.capture(address, pathlib.Path(directory) / "invalid.json")
                self.assertEqual(list(pathlib.Path(directory).iterdir()), [target])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_failure_has_no_success_stdout_or_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "stats.json"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = capture_stats.main(["example.com:80", str(target)])
            self.assertEqual((code, out.getvalue()), (1, ""))
            self.assertIn("statistics capture failed:", err.getvalue())
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
