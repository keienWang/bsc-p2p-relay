#!/usr/bin/env python3
"""Loopback-only process smoke test. Never submits a signed transaction."""
import json
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

binary = str(pathlib.Path(sys.argv[1]).resolve())
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
base = f"http://127.0.0.1:{port}"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with tempfile.TemporaryDirectory(prefix="relay-smoke-") as work:
    process = subprocess.Popen(
        [binary, "--no-discovery", "--p2p", "127.0.0.1:0", "--api", f"127.0.0.1:{port}"],
        cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError(process.stdout.read())
            try:
                with opener.open(base + "/healthz", timeout=1) as response:
                    health = json.load(response)
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            raise RuntimeError("health endpoint did not start")
        assert health["ready_peers"] == 0 and health["confirmation_tracking"] is False
        try:
            opener.open(base + "/readyz", timeout=2)
            raise AssertionError("readiness incorrectly succeeded with no peers")
        except urllib.error.HTTPError as error:
            assert error.code == 503
        request = urllib.request.Request(base + "/v1/transactions", data=b'{"raw_tx":"0x00"}', headers={"Content-Type": "application/json"})
        try:
            opener.open(request, timeout=2)
            raise AssertionError("malformed transaction accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 400
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        assert list(pathlib.Path(work).iterdir()) == [], "unexpected persistent data files"
        print("PASS: startup, health, readiness, malformed input, shutdown, no data files")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
