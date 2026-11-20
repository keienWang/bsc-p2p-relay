#!/usr/bin/env python3
"""Loopback-only process smoke test. Never submits a signed transaction."""
import datetime
import errno
import json
import os
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


def check_cli_rejections(binary, work):
    """Test argument errors without reaching even loopback Node startup.

    The invalid API address comes first so the prior, uncorrected binary also
    stops before New/Start rather than ignoring trailing isolation flags.
    """
    expected = "unexpected positional arguments; use --help for supported flags"
    cases = (
        ("unrecognized-subcommand",),
        ("unrecognized-subcommand", "--network", "testnet"),
        ("--network", "testnet", "unexpected-value"),
        ("--no-discovery", "false", "--network", "testnet"),
        ("--", "--network", "testnet"),
        ("-", "--network", "testnet"),
    )
    for suffix in cases:
        command = [binary, "--api", "not-an-address", "--no-discovery",
                   "--p2p", "127.0.0.1:0", *suffix]
        result = subprocess.run(command, cwd=work, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=5, check=False)
        if (result.returncode != 1 or expected not in result.stdout
                or "experimental relay started" in result.stdout):
            raise AssertionError("CLI positional arguments did not fail before startup")
    print("PASS: six CLI positional-argument rejection cases, before Node startup")


def main(binary):
    binary = str(pathlib.Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix="relay-cli-smoke-") as work:
        check_cli_rejections(binary, work)
        if list(pathlib.Path(work).iterdir()):
            raise AssertionError("CLI rejection left persistent data files")
    check_observation(binary, observe_only=False)
    check_observation(binary, observe_only=True)
    check_observation(binary, observe_only=True, ipv6=True)


def check_observation_rejects_before_body(host, api, port):
    """A wire-level client sends headers only and waits for the first response."""
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.sendall(
            f"POST /v1/transactions HTTP/1.1\r\nHost: {api}\r\n"
            "Content-Type: application/json\r\nContent-Length: 16\r\n"
            "Expect: 100-continue\r\nConnection: close\r\n\r\n".encode("ascii")
        )
        with connection.makefile("rb") as response:
            status_line = response.readline(256)
    if not re.fullmatch(rb"HTTP/1\.1 403 [^\r\n]+\r\n", status_line):
        raise AssertionError(f"expected observation 403 before any body bytes, got {status_line!r}")


def ipv6_loopback_available():
    try:
        with socket.socket(socket.AF_INET6) as listener:
            listener.bind(("::1", 0))
    except OSError as error:
        if error.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL):
            return False
        raise
    return True


STARTUP_API = re.compile(
    rb"^[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} INFO "
    rb"experimental relay started; no chain/state verification or confirmations "
    rb'network=mainnet api=([^ \r\n]+) observe_only=(true|false) ')
MAX_STARTUP_LOG_BYTES = 32 * 1024
STARTUP_TIMEOUT_SECONDS = 5


def startup_endpoint(log, host, observe_only):
    for line in log.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue  # Do not interpret a partially written log record.
        match = STARTUP_API.match(line)
        if match is None:
            continue
        address, mode = (value.decode("ascii") for value in match.groups())
        authority = f"[{host}]" if host == "::1" else host
        port = re.fullmatch(re.escape(authority) + r":([0-9]{1,5})", address)
        if port is None or not 1 <= int(port[1]) <= 65535:
            raise ValueError("startup log did not identify the expected loopback endpoint")
        if mode != str(observe_only).lower():
            raise ValueError("startup log reported the wrong observation mode")
        return address, int(port[1])
    return None


def wait_startup_endpoint(process, log, host, observe_only):
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while True:
        output = os.pread(log.fileno(), MAX_STARTUP_LOG_BYTES + 1, 0)
        if len(output) > MAX_STARTUP_LOG_BYTES:
            raise RuntimeError("relay exceeded startup log budget")
        if process.poll() is not None:
            raise RuntimeError("relay exited before smoke observation: " + output.decode(errors="replace"))
        if endpoint := startup_endpoint(output, host, observe_only):
            return endpoint
        if time.monotonic() >= deadline:
            raise RuntimeError("relay did not report a bound API: " + output.decode(errors="replace"))
        time.sleep(0.05)


def check_observation(binary, observe_only, ipv6=False):
    if ipv6 and not ipv6_loopback_available():
        print("SKIP: IPv6 loopback is not available on this host")
        return
    host = "::1" if ipv6 else "127.0.0.1"
    bind = "[::1]:0" if ipv6 else "127.0.0.1:0"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="relay-smoke-") as work, tempfile.TemporaryFile() as log:
        flags = ["--observe-only"] if observe_only else []
        process = subprocess.Popen(
            [binary, "--no-discovery", "--p2p", "127.0.0.1:0", "--api", bind, *flags],
            cwd=work, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            api, port = wait_startup_endpoint(process, log, host, observe_only)
            base = f"http://{api}"
            with opener.open(base + "/healthz", timeout=2) as response:
                health = json.load(response)
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
            body = b"not JSON" if observe_only else b'{"raw_tx":"0x00"}'
            request = urllib.request.Request(base + "/v1/transactions", data=body, headers={"Content-Type": "application/json"})
            try:
                opener.open(request, timeout=2)
                raise AssertionError("malformed transaction accepted")
            except urllib.error.HTTPError as error:
                assert error.code == (403 if observe_only else 400)
                if observe_only:
                    assert "submission is disabled" in json.load(error)["error"]
            if observe_only:
                check_observation_rejects_before_body(host, api, port)
            after_rejection = read_stats(opener, base)
            assert_stats_counts(after_rejection, 0 if observe_only else 1)
            assert after_rejection["instance_id"] == initial_stats["instance_id"]
            assert after_rejection["created_at"] == initial_stats["created_at"]
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=10) == 0
            assert list(pathlib.Path(work).iterdir()) == [], "unexpected persistent data files"
            if observe_only:
                print(f"PASS: observation-only health/readiness/stats and HTTP 403 before upload on {host}")
            else:
                print("PASS: startup, health, readiness, stats schema/identity/headers/exact validation increment, malformed input, shutdown, no data files")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    main(sys.argv[1])
