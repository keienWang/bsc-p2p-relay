# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import concurrent.futures
import contextlib
import io
import json
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import time
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

    def test_competing_publishers_expose_one_complete_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "stats.json"
            candidates = [dict(sample(), instance_id=identity * 32) for identity in ("a", "b")]
            for index, candidate in enumerate(candidates):
                candidate["http"]["concurrency_rejected_total"] = (1 << 64) - 1 - index

            # Make both writes reach the real atomic publication boundary after
            # staging; sequential duplicate checks cannot exercise this race.
            staged = threading.Barrier(3, timeout=5)
            release = threading.Event()
            real_link = capture_stats.os.link

            def publish(source, destination):
                staged.wait()
                if not release.wait(timeout=5):
                    raise TimeoutError("publication fixture did not release writers")
                real_link(source, destination)

            with mock.patch("capture_stats.os.link", side_effect=publish):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
                    futures = [workers.submit(capture_stats.save_new_snapshot, target, candidate)
                               for candidate in candidates]
                    try:
                        staged.wait()
                        self.assertFalse(target.exists(), "target appeared before publication")
                        self.assertCountEqual(
                            [json.loads(file.read_bytes()) for file in root.iterdir()], candidates)
                    finally:
                        release.set()
                    errors = [future.exception(timeout=5) for future in futures]

            winners = [index for index, error in enumerate(errors) if error is None]
            self.assertEqual(len(winners), 1, errors)
            self.assertIsInstance(errors[1 - winners[0]], FileExistsError)
            self.assertEqual(json.loads(target.read_bytes()), candidates[winners[0]])
            self.assertEqual(list(root.iterdir()), [target])

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

    def test_fetch_deadline_values_are_bounded_and_finite(self):
        for value in ("0", "-1", "0.0001", "61", "NaN", "inf", "-inf", "invalid"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                capture_stats.parse_fetch_deadline(value)
        self.assertEqual(capture_stats.parse_fetch_deadline("0.001"), 0.001)
        self.assertEqual(capture_stats.parse_fetch_deadline("60"), 60)

    def test_default_fetch_does_not_require_platform_timers(self):
        with mock.patch("capture_stats.signal", object()):
            with capture_stats.fetch_time_limit(None):
                pass
            with self.assertRaisesRegex(ValueError, "requires POSIX"), capture_stats.fetch_time_limit(1):
                self.fail("configured deadline was silently ignored")

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX interval timer required")
    def test_fetch_deadline_preserves_existing_alarm_and_idle_error(self):
        alarm, timer = signal.SIGALRM, signal.ITIMER_REAL
        self.assertEqual(signal.getsignal(alarm), signal.SIG_DFL)
        self.assertEqual(signal.getitimer(timer), (0, 0))
        handler = lambda *_: None
        previous = signal.signal(alarm, handler)
        try:
            with self.assertRaisesRegex(ValueError, "already in use"), capture_stats.fetch_time_limit(1):
                self.fail("existing alarm handler was replaced")
            self.assertIs(signal.getsignal(alarm), handler)
        finally:
            signal.signal(alarm, previous)
        signal.setitimer(timer, 30)
        try:
            with self.assertRaisesRegex(ValueError, "already in use"), capture_stats.fetch_time_limit(1):
                self.fail("existing alarm timer was replaced")
            self.assertGreater(signal.getitimer(timer)[0], 20)
        finally:
            signal.setitimer(timer, 0)
        with self.assertRaisesRegex(TimeoutError, "original socket idle timeout"), capture_stats.fetch_time_limit(1):
            raise TimeoutError("original socket idle timeout")
        self.assertEqual(signal.getsignal(alarm), signal.SIG_DFL)
        self.assertEqual(signal.getitimer(timer), (0, 0))

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX interval timer required")
    def test_optional_fetch_deadline_limits_a_valid_slow_response(self):
        body = json.dumps(sample()).encode()
        script = pathlib.Path(capture_stats.__file__).resolve()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                for key, value in (("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                                   ("X-Content-Type-Options", "nosniff"), ("Content-Length", str(len(body)))):
                    self.send_header(key, value)
                self.end_headers()
                try:
                    for offset in range(0, len(body), 32):
                        self.wfile.write(body[offset:offset + 32])
                        self.wfile.flush()
                        time.sleep(0.05)  # Every write is within the idle timeout.
                except (BrokenPipeError, ConnectionResetError):
                    return  # The deadline test closes this response early.

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory() as directory:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                address = "127.0.0.1:" + str(server.server_port)
                command = [sys.executable, str(script), address]
                good, bad = (pathlib.Path(directory) / name for name in ("before.json", "after.json"))
                allowed = subprocess.run(command + [str(good)], capture_output=True, text=True, timeout=8)
                self.assertEqual((allowed.returncode, allowed.stderr), (0, ""))
                self.assertEqual(json.loads(good.read_bytes()), sample())
                limited = subprocess.run(command + [str(bad), "--fetch-deadline", "0.25"],
                                         capture_output=True, text=True, timeout=8)
                self.assertEqual((limited.returncode, limited.stdout), (1, ""))
                self.assertIn("fetch deadline exceeded", limited.stderr)
                self.assertEqual(list(pathlib.Path(directory).iterdir()), [good])
            finally:
                server.shutdown()
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
