# SPDX-License-Identifier: GPL-3.0-or-later
import json
import pathlib
import queue
import subprocess
import sys
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
                status, payload = responses.get_nowait()
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
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
