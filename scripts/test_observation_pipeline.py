# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise installed-script boundaries, not imported main() shortcuts."""
import copy
import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_stats_delta import sample

SCRIPTS = pathlib.Path(__file__).resolve().parent


class ObservationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = pathlib.Path(self.directory.name)
        self.responses = queue.Queue()
        responses = self.responses

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/v1/stats":
                    self.send_error(404)
                    return
                try:
                    payload = responses.get_nowait()
                except queue.Empty:
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.address = "127.0.0.1:" + str(self.server.server_port)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def run_cli(self, script, *args):
        # Execute from outside the source tree, with no custom Python module path.
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                              cwd=self.work, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10, check=False)

    def capture(self, filename, value):
        self.responses.put(json.dumps(value).encode())
        result = self.run_cli("capture_stats.py", self.address, filename)
        self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout)
        self.assertEqual(json.loads((self.work / filename).read_bytes()), value)

    def test_capture_to_delta_preserves_exact_integers_and_nanoseconds(self):
        before = sample()
        after = copy.deepcopy(before)
        for index, field in enumerate(sorted(before["submit"])):
            before["submit"][field] = (1 << 63) + index
            after["submit"][field] = before["submit"][field] + index + 1
        after["http"]["concurrency_rejected_total"] = (1 << 64) - 1
        after["observed_at"] = "2026-11-20T00:02:03.987654321Z"
        self.capture("before.json", before)
        self.capture("after.json", after)
        result = self.run_cli("stats_delta.py", "before.json", "after.json")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        delta = json.loads(result.stdout)
        self.assertEqual(delta, dict(
            source_schema_version=1, instance_id=before["instance_id"],
            created_at=before["created_at"], before_observed_at=before["observed_at"],
            after_observed_at=after["observed_at"],
            submit_delta={field: index + 1 for index, field in enumerate(sorted(before["submit"]))},
            http_delta={"concurrency_rejected_total": (1 << 64) - 1}))
        self.assertEqual(sorted(path.name for path in self.work.iterdir()), ["after.json", "before.json"])

    def test_captured_restart_and_reset_do_not_report_zero_success(self):
        before = sample()
        before["http"]["concurrency_rejected_total"] = 10
        self.capture("before.json", before)
        for name, changed, message in (
            ("restart.json", dict(before, instance_id="b" * 32), "instance changed"),
            ("reset.json", sample(), "counter decreased"),
        ):
            with self.subTest(name=name):
                self.capture(name, changed)
                result = self.run_cli("stats_delta.py", "before.json", name)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn(message, result.stderr)

    def test_duplicate_capture_preserves_baseline_and_cli_fails(self):
        self.capture("before.json", sample())
        original = (self.work / "before.json").read_bytes()
        self.responses.put(json.dumps(dict(sample(), instance_id="b" * 32)).encode())
        result = self.run_cli("capture_stats.py", self.address, "before.json")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("statistics capture failed:", result.stderr)
        self.assertEqual((self.work / "before.json").read_bytes(), original)
        self.assertEqual([path.name for path in self.work.iterdir()], ["before.json"])


if __name__ == "__main__":
    unittest.main()
