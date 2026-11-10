#!/usr/bin/env python3
"""Loopback-only process smoke test. Never submits a signed transaction."""
import datetime
import json
import re
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MAX_STATS_RESPONSE_BYTES = 16 * 1024

STATS_SUBMIT_FIELDS = {
    "accepted_new_cache_lifetime_total", "accepted_cached_retry_total",
    "rejected_validation_total", "rejected_no_peers_total", "rejected_cache_full_total",
    "rejected_queue_unavailable_total", "rejected_admission_expired_total", "other_total",
}
STATS_METADATA_FIELDS = {"schema_version", "instance_id", "created_at", "observed_at", "submit", "http"}


def unique_object(pairs):
    """Reject duplicate JSON keys in this smoke test, including nested objects."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate statistics field")
        value[key] = item
    return value


def decode_stats(payload):
    """Validate this test's frozen v1 response; failures are never filled with zero."""
    value = json.loads(payload, object_pairs_hook=unique_object)
    if type(value) is not dict or set(value) != STATS_METADATA_FIELDS:
        raise ValueError("unexpected statistics object fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported statistics schema")
    if type(value["instance_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", value["instance_id"]):
        raise ValueError("invalid instance identity")
    for field in ("created_at", "observed_at"):
        stamp = value[field]
        if type(stamp) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z", stamp):
            raise ValueError("invalid UTC statistics timestamp")
        parsed = datetime.datetime.fromisoformat(stamp[:-1] + "+00:00")
        if parsed == datetime.datetime.min.replace(tzinfo=datetime.timezone.utc):
            raise ValueError("uninitialized statistics timestamp")
    # Wall time can go backward; do not require observed_at >= created_at.
    for group, fields in (("submit", STATS_SUBMIT_FIELDS), ("http", {"concurrency_rejected_total"})):
        counters = value[group]
        if type(counters) is not dict or set(counters) != fields:
            raise ValueError("unexpected statistics counter fields")
        if any(type(count) is not int or not 0 <= count <= (1 << 64) - 1 for count in counters.values()):
            raise ValueError("statistics counters must be uint64 integers")
    return value


def read_stats(opener, base):
    with opener.open(base + "/v1/stats", timeout=2) as response:
        if (response.status != 200 or response.headers.get("Content-Type") != "application/json"
                or response.headers.get("Cache-Control") != "no-store"
                or response.headers.get("X-Content-Type-Options") != "nosniff"):
            raise AssertionError("invalid statistics HTTP response")
        # This smoke supports a single decimal Content-Length or an EOF-delimited
        # response, not arbitrary transfer codings. Reject TE, including TE+CL,
        # instead of silently trusting one of two conflicting framing signals.
        if response.headers.get_all("Transfer-Encoding", []):
            raise ValueError("unsupported or conflicting statistics Transfer-Encoding")
        lengths = response.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            raise ValueError("duplicate statistics Content-Length")
        expected_length = None
        if lengths:
            declaration = lengths[0].strip(" \t")
            if not re.fullmatch(r"[0-9]+", declaration):
                raise ValueError("invalid statistics Content-Length")
            expected_length = int(declaration)
            if expected_length > MAX_STATS_RESPONSE_BYTES:
                raise ValueError("statistics Content-Length exceeds response bound")
        # HTTPResponse.read(amt) can return an early EOF without IncompleteRead.
        # Bounded reading is retained, with an explicit declared-length check.
        payload = response.read(MAX_STATS_RESPONSE_BYTES + 1)
        if len(payload) > MAX_STATS_RESPONSE_BYTES:
            raise AssertionError("unexpectedly large statistics response")
        if expected_length is not None and len(payload) != expected_length:
            raise ValueError("incomplete statistics Content-Length body")
        return decode_stats(payload)


def assert_stats_counts(value, validations):
    expected = dict.fromkeys(STATS_SUBMIT_FIELDS, 0)
    expected["rejected_validation_total"] = validations
    assert value["submit"] == expected, "unexpected Submit outcomes"
    assert value["http"] == {"concurrency_rejected_total": 0}, "unexpected HTTP refusal"


def main(binary):
    binary = str(pathlib.Path(binary).resolve())
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
            initial_stats = read_stats(opener, base)
            assert_stats_counts(initial_stats, 0)
            repeated_stats = read_stats(opener, base)
            assert_stats_counts(repeated_stats, 0)
            assert repeated_stats["instance_id"] == initial_stats["instance_id"]
            assert repeated_stats["created_at"] == initial_stats["created_at"]
            request = urllib.request.Request(base + "/v1/transactions", data=b'{"raw_tx":"0x00"}', headers={"Content-Type": "application/json"})
            try:
                opener.open(request, timeout=2)
                raise AssertionError("malformed transaction accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 400
            after_rejection = read_stats(opener, base)
            assert_stats_counts(after_rejection, 1)
            assert after_rejection["instance_id"] == initial_stats["instance_id"]
            assert after_rejection["created_at"] == initial_stats["created_at"]
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=10) == 0
            assert list(pathlib.Path(work).iterdir()) == [], "unexpected persistent data files"
            print("PASS: startup, health, readiness, stats schema/identity/headers/exact validation increment, malformed input, shutdown, no data files")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    main(sys.argv[1])
