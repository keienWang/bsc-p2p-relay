# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import json
import pathlib
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from probe_readiness import decode_readiness, main
from smoke import MAX_STATS_RESPONSE_BYTES


def health(**changes):
    return dict(dict(network="mainnet", chain_id="56", ready_peers=0, connected_peers=0,
                     cached_transactions=0, cached_bytes=0, written_messages=0, write_failures=0,
                     confirmation_tracking=False), **changes)


class ProbeReadinessTest(unittest.TestCase):
    def test_contract_rejects_non_health_and_inconsistent_status(self):
        cases = ((health(), 200), (health(ready_peers=1, connected_peers=1), 503),
                 (health(ready_peers=1, connected_peers=0), 200),
                 (health(connected_peers=201), 503), (health(ready_peers=False), 503),
                 (health(chain_id=56), 503), (health(network="testnet"), 503),
                 (health(confirmation_tracking=0), 503), (health(cached_bytes=-1), 503),
                 (health(written_messages=1 << 64), 503), ({"error": "unavailable"}, 503),
                 (health(extra=0), 503))
        for value, status in cases:
            with self.subTest(value=value, status=status), self.assertRaises(ValueError):
                decode_readiness(json.dumps(value), status)
        self.assertEqual(decode_readiness(json.dumps(health(network="testnet", chain_id="97")), 503)["chain_id"], "97")

    def test_cache_counts_must_describe_the_same_entries(self):
        for count, size in ((0, 1), (1, 0), (2, 1)):
            with self.subTest(count=count, size=size), self.assertRaisesRegex(
                    ValueError, "cache count and bytes disagree"):
                decode_readiness(json.dumps(health(cached_transactions=count, cached_bytes=size)), 503)
        for count, size in ((0, 0), (1, 1), (2, 256)):
            with self.subTest(count=count, size=size):
                sample = health(cached_transactions=count, cached_bytes=size)
                self.assertEqual(decode_readiness(json.dumps(sample), 503), sample)

    @unittest.skipUnless(hasattr(signal, "setitimer"), "readiness deadlines require POSIX")
    def test_opt_in_retries_only_refused_loopback_connections(self):
        ready = health(ready_peers=1, connected_peers=1)
        payload = json.dumps(ready).encode()
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        with server, tempfile.TemporaryDirectory() as directory:
            server.server_bind()  # Keep the port reserved but do not listen yet.
            api = f"127.0.0.1:{server.server_port}"
            baseline = [api, "--attempts", "2", "--total-deadline", "2"]
            script = pathlib.Path(__file__).resolve().with_name("probe_readiness.py")

            def run(*args):
                return subprocess.run([sys.executable, str(script), *args],
                                      capture_output=True, text=True, timeout=5, check=False)

            default = run(*baseline)
            self.assertEqual((default.returncode, default.stdout), (1, ""))
            self.assertIn("Connection refused", default.stderr)
            for invalid in ((api,), (api, "--attempts", "2", "--fetch-deadline", "1")):
                result = run(*invalid, "--retry-connection-refused")
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn("--attempts", result.stderr)
                self.assertIn("--total-deadline", result.stderr)

            output = pathlib.Path(directory) / "observation.json"
            exhausted = run(*baseline, "--retry-connection-refused", "--output", str(output))
            self.assertEqual((exhausted.returncode, exhausted.stdout), (1, ""))
            self.assertIn("Connection refused", exhausted.stderr)
            self.assertFalse(output.exists())

            budget = run(api, "--attempts", "3", "--total-deadline", "0.06", "--retry-connection-refused")
            self.assertEqual((budget.returncode, budget.stdout), (1, ""))
            self.assertIn("total deadline exceeded", budget.stderr)

            thread = threading.Thread(target=server.serve_forever, daemon=True)

            def start_listening(_seconds):
                self.assertFalse(output.exists())
                server.server_activate()
                thread.start()

            stdout, stderr = io.StringIO(), io.StringIO()
            try:
                with mock.patch("probe_readiness.time.sleep", side_effect=start_listening) as sleep:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = main(baseline + ["--retry-connection-refused", "--output", str(output)])
                sleep.assert_called_once()
                self.assertEqual((code, stdout.getvalue(), stderr.getvalue(), paths), (0, "", "", ["/readyz"]))
                self.assertEqual(json.loads(output.read_bytes()), ready)
            finally:
                if thread.is_alive():
                    server.shutdown()
                    thread.join(timeout=5)

    def test_loopback_cli_distinguishes_unready_from_unavailable(self):
        responses = queue.Queue()
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                status, payload, *on_request = responses.get_nowait()
                for callback in on_request:
                    callback()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The bounded reader can reject before consuming the body.

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                command = [sys.executable, str(pathlib.Path(__file__).resolve().with_name("probe_readiness.py")),
                           "127.0.0.1:" + str(server.server_port)]
                ready = health(ready_peers=1, connected_peers=2, written_messages=(1 << 64) - 1)
                zero = health(connected_peers=2)
                occupied = dict(ready, cached_transactions=2, cached_bytes=256)
                incoherent = dict(ready, cached_transactions=0, cached_bytes=1)
                cases = ((200, json.dumps(occupied).encode(), 0, occupied),
                         (200, json.dumps(incoherent).encode(), 1, None),
                         (503, json.dumps(zero).encode(), 3, zero),
                         (503, b'{"error":"unavailable"}', 1, None),
                         (503, json.dumps(ready).encode(), 1, None),
                         (404, json.dumps(zero).encode(), 1, None),
                         (503, b" " * MAX_STATS_RESPONSE_BYTES + b"{}", 1, None))
                for status, body, code, expected in cases:
                    with self.subTest(status=status, code=code, size=len(body)):
                        responses.put((status, body))
                        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, code, result.stderr)
                        if expected is None:
                            self.assertEqual(result.stdout, "")
                            self.assertIn("readiness unavailable:", result.stderr)
                        else:
                            self.assertEqual(result.stderr, "")
                            self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(paths, ["/readyz"] * len(cases))
                paths.clear()
                testnet_ready = dict(ready, network="testnet", chain_id="97")
                for status, payload, network, exit_code in (
                    (200, testnet_ready, "testnet", 0),
                    (200, testnet_ready, "mainnet", 1),
                    (503, zero, "mainnet", 3),
                    (503, zero, "testnet", 1),
                ):
                    with self.subTest(expected_network=network, actual_network=payload["network"], status=status):
                        responses.put((status, json.dumps(payload).encode()))
                        result = subprocess.run(command + ["--expect-network", network],
                                                capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, exit_code, result.stderr)
                        if exit_code == 1:
                            self.assertEqual(result.stdout, "")
                            self.assertIn(f"expected {network}, reported {payload['network']}", result.stderr)
                        else:
                            self.assertEqual(result.stderr, "")
                            self.assertEqual(json.loads(result.stdout), payload)
                invalid = subprocess.run(command + ["--expect-network", "devnet"],
                                         capture_output=True, text=True, timeout=5)
                self.assertEqual((invalid.returncode, invalid.stdout), (2, ""))
                self.assertEqual(paths, ["/readyz"] * 4)  # Invalid argument sends no GET.
                paths.clear()
                option = "--min-ready-peers"
                two = dict(ready, ready_peers=2)
                maximum = dict(ready, ready_peers=200, connected_peers=200)
                thresholds = (
                    (200, ready, 2, 3, ()),
                    (200, two, 2, 0, ()),
                    (200, maximum, 200, 0, ()),
                    (503, zero, 2, 3, ()),
                    (200, zero, 2, 1, ()),
                    (200, {"error": "unavailable"}, 2, 1, ()),
                    (200, testnet_ready, 2, 1, ("--expect-network", "mainnet")),
                )
                for status, payload, minimum, exit_code, extra in thresholds:
                    with self.subTest(status=status, minimum=minimum, exit_code=exit_code):
                        responses.put((status, json.dumps(payload).encode()))
                        result = subprocess.run(command + [option, str(minimum), *extra],
                                                capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, exit_code, result.stderr)
                        if exit_code == 1:
                            self.assertEqual(result.stdout, "")
                            self.assertIn("readiness unavailable:", result.stderr)
                        else:
                            self.assertEqual(result.stderr, "")
                            self.assertEqual(json.loads(result.stdout), payload)
                for bad in ("0", "-1", "201", "1.5", "unknown"):
                    with self.subTest(minimum=bad):
                        result = subprocess.run(command + [option, bad],
                                                capture_output=True, text=True, timeout=5)
                        self.assertEqual((result.returncode, result.stdout), (2, ""))
                        self.assertIn("integer from 1 to 200", result.stderr)
                self.assertEqual(paths, ["/readyz"] * len(thresholds))
                self.assert_saved_observations(command, responses, paths, ready, zero)
                if hasattr(signal, "setitimer"):
                    self.assert_retry_observations(command, responses, paths, ready, zero)
                    self.assert_total_deadline(command, responses, paths, ready, zero)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def assert_saved_observations(self, command, responses, paths, ready, zero):
        paths.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def run(destination, *extra):
                return subprocess.run(command + ["--output", destination, *extra], cwd=root,
                                      capture_output=True, text=True, timeout=5, check=False)

            cases = (
                ("ready.json", 200, ready, 0, ()),
                ("below-threshold.json", 200, ready, 3, ("--min-ready-peers", "2")),
                ("unready.json", 503, zero, 3, ()),
                ("inconsistent.json", 503, ready, 1, ()),
                ("wrong-network.json", 200, ready, 1, ("--expect-network", "testnet")),
                ("phantom-bytes.json", 200, dict(ready, cached_transactions=0, cached_bytes=1), 1, ()),
                ("too-few-bytes.json", 503, dict(zero, cached_transactions=2, cached_bytes=1), 1, ()),
                ("nonempty.json", 200, dict(ready, cached_transactions=2, cached_bytes=256), 0, ()),
            )
            for name, status, payload, code, extra in cases:
                with self.subTest(name=name):
                    responses.put((status, json.dumps(payload).encode()))
                    result = run(name, *extra)
                    self.assertEqual((result.returncode, result.stdout), (code, ""), result.stderr)
                    path = root / name
                    if code == 1:
                        self.assertFalse(path.exists())
                        self.assertIn("readiness unavailable:", result.stderr)
                    else:
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(json.loads(path.read_bytes()), payload)

            self.assertEqual(paths, ["/readyz"] * len(cases))
            paths.clear()
            previous = (root / "ready.json").read_bytes()
            for path in ("ready.json", "missing/record.json"):
                with self.subTest(path=path):
                    rejected = run(path)
                    self.assertEqual((rejected.returncode, rejected.stdout), (1, ""))
                    self.assertIn("readiness unavailable:", rejected.stderr)
            self.assertEqual((root / "ready.json").read_bytes(), previous)
            self.assertEqual(paths, [])

            raced = root / "race.json"
            responses.put((200, json.dumps(ready).encode(), lambda: raced.write_bytes(b"concurrent result")))
            rejected = run(raced.name)
            self.assertEqual((rejected.returncode, rejected.stdout), (1, ""))
            self.assertIn("readiness unavailable:", rejected.stderr)
            self.assertEqual(raced.read_bytes(), b"concurrent result")
            self.assertEqual(list(root.glob(".relay-stats-*")), [])
            self.assertEqual(paths, ["/readyz"])

    def assert_retry_observations(self, command, responses, paths, ready, zero):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            enough = dict(ready, ready_peers=2)
            later_unready = dict(zero, connected_peers=3)
            other_network = dict(enough, network="testnet", chain_id="97")

            def run(*options):
                return subprocess.run(command + list(options), cwd=root, capture_output=True,
                                      text=True, timeout=5, check=False)

            def add(status, payload, *callbacks):
                responses.put((status, json.dumps(payload).encode(), *callbacks))

            paths.clear()
            invalid = run("--attempts", "2", "--output", "no-deadline.json")
            self.assertEqual((invalid.returncode, invalid.stdout, paths), (2, "", []))
            self.assertIn("--fetch-deadline", invalid.stderr)
            self.assertFalse((root / "no-deadline.json").exists())
            for tries in ("0", "21", "1.5"):
                invalid = run("--attempts", tries, "--fetch-deadline", "1")
                self.assertEqual((invalid.returncode, invalid.stdout, paths), (2, "", []))

            add(200, enough)
            satisfied = run("--attempts", "3", "--fetch-deadline", "1", "--min-ready-peers", "2")
            self.assertEqual((satisfied.returncode, satisfied.stderr, paths), (0, "", ["/readyz"]))
            self.assertEqual(json.loads(satisfied.stdout), enough)

            for final, code, name in (
                ((200, enough), 0, "recovered.json"),
                ((503, later_unready), 3, "still-unready.json"),
                ((503, {"error": "not readiness"}), 1, "malformed.json"),
                ((200, other_network), 1, "wrong-network.json"),
                ((200, other_network), 1, "changed-network.json"),
                ((404, zero), 1, "unavailable.json"),
            ):
                with self.subTest(name=name):
                    paths.clear()
                    target = root / name
                    published_early = []
                    # For the recovery case HTTP 200 is valid but below the
                    # local threshold. The others really begin with HTTP 503.
                    if code == 0:
                        add(200, ready)
                    else:
                        add(503, zero)
                    add(*final, lambda: published_early.append(target.exists()))
                    network = () if name == "changed-network.json" else ("--expect-network", "mainnet")
                    result = run("--attempts", "2" if code != 1 else "3", "--fetch-deadline", "1",
                                 "--min-ready-peers", "2", "--output", name, *network,
                                 "--total-deadline", "2", "--retry-connection-refused")
                    self.assertEqual((result.returncode, result.stdout), (code, ""), result.stderr)
                    self.assertEqual((paths, published_early), (["/readyz"] * 2, [False]))
                    if code == 1:
                        self.assertIn("readiness unavailable:", result.stderr)
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(json.loads(target.read_bytes()), final[1])


    def assert_total_deadline(self, command, responses, paths, ready, zero):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def run(*options):
                paths.clear()
                return subprocess.run(command + ["--attempts", "2", *options], cwd=root,
                                      capture_output=True, text=True, timeout=5, check=False)

            def add(status, value, *callbacks):
                responses.put((status, json.dumps(value).encode(), *callbacks))

            invalid = run("--total-deadline", "NaN")
            self.assertEqual((invalid.returncode, invalid.stdout, paths), (2, "", []))
            self.assertIn("total deadline must be 0.001 to 60 finite seconds", invalid.stderr)

            add(503, zero)
            add(503, zero)
            unready = run("--total-deadline", "2")
            self.assertEqual((unready.returncode, unready.stderr, paths), (3, "", ["/readyz"] * 2))
            self.assertEqual(json.loads(unready.stdout), zero)

            add(503, zero)
            backoff = run("--total-deadline", "0.2", "--output", "backoff.json")
            self.assertEqual((backoff.returncode, backoff.stdout, paths), (1, "", ["/readyz"]))
            self.assertIn("readiness total deadline exceeded", backoff.stderr)
            self.assertFalse((root / "backoff.json").exists())

            add(200, ready, lambda: time.sleep(0.45))
            per_fetch = run("--fetch-deadline", "0.2", "--total-deadline", "2")
            self.assertEqual((per_fetch.returncode, per_fetch.stdout, paths), (1, "", ["/readyz"]))
            self.assertIn("fetch deadline exceeded", per_fetch.stderr)
            self.assertNotIn("total deadline exceeded", per_fetch.stderr)

            # Both input replies fit a per-fetch limit of 1.5 s; the same
            # pair does not fit 1.25 s once the intervening 250 ms counts.
            for extra, want in (((), 0), (("--total-deadline", "1.25"), 1)):
                with self.subTest(extra=extra):
                    name = f"budget-{want}.json"
                    target = root / name
                    add(503, zero)
                    add(200, ready, lambda: time.sleep(1.1))
                    result = run("--fetch-deadline", "1.5", "--output", name, *extra)
                    self.assertEqual((result.returncode, result.stdout, paths), (want, "", ["/readyz"] * 2))
                    if want:
                        self.assertIn("readiness total deadline exceeded", result.stderr)
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(json.loads(target.read_bytes()), ready)


if __name__ == "__main__":
    unittest.main()
