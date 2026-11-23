# SPDX-License-Identifier: GPL-3.0-or-later
import json
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from probe_readiness import decode_readiness
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
                cases = ((200, json.dumps(ready).encode(), 0, ready),
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

            for name, status, payload, code, extra in (
                ("ready.json", 200, ready, 0, ()),
                ("below-threshold.json", 200, ready, 3, ("--min-ready-peers", "2")),
                ("unready.json", 503, zero, 3, ()),
                ("inconsistent.json", 503, ready, 1, ()),
                ("wrong-network.json", 200, ready, 1, ("--expect-network", "testnet")),
            ):
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

            self.assertEqual(paths, ["/readyz"] * 5)
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


if __name__ == "__main__":
    unittest.main()
