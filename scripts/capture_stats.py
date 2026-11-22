#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture one validated loopback statistics snapshot into a new local file."""
import argparse
import contextlib
import errno
import http.client
import ipaddress
import json
import os
import pathlib
import signal
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from smoke import MAX_STATS_RESPONSE_BYTES, decode_stats, read_stats


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def loopback_base(address):
    parsed = urllib.parse.urlsplit("http://" + address)
    if (parsed.username is not None or parsed.password is not None or parsed.path
            or parsed.query or parsed.fragment or not parsed.hostname or "%" in address):
        raise ValueError("API must be a literal loopback IP and port, without URL components")
    host = ipaddress.ip_address(parsed.hostname)
    if not host.is_loopback or parsed.port is None or not 1 <= parsed.port <= 65535:
        raise ValueError("API must be a literal loopback IP and port")
    canonical = f"[{host}]" if host.version == 6 else str(host)
    return f"http://{canonical}:{parsed.port}"


def read_snapshot(path):
    """Read the same bounded v1 snapshot format for capture and comparison."""
    path = pathlib.Path(path)
    error = "snapshot input must be a regular file"
    # Refuse special paths before opening them. On POSIX, also avoid blocking
    # on a pipe if the directory entry changes between stat and open.
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(error)

    def open_saved_file(filename, flags):
        return os.open(filename, flags | getattr(os, "O_NONBLOCK", 0))

    with open(path, "rb", opener=open_saved_file) as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(error)
        payload = source.read(MAX_STATS_RESPONSE_BYTES + 1)
    if len(payload) > MAX_STATS_RESPONSE_BYTES:
        raise ValueError("snapshot exceeds response bound")
    return decode_stats(payload)


def require_same_instance(before, after):
    if before["instance_id"] != after["instance_id"]:
        raise ValueError("instance changed; start a new comparison baseline")
    if before["created_at"] != after["created_at"]:
        raise ValueError("instance creation metadata changed")


def check_capture_destination(target):
    """Reject unusable local paths before making a read-only HTTP request."""
    try:
        target.lstat()
    except FileNotFoundError:
        parent = target.parent
        if not stat.S_ISDIR(parent.stat().st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "snapshot destination parent is not a directory", str(parent))
        return
    # lstat also checks a dangling symlink, which would block os.link.
    raise FileExistsError(errno.EEXIST, "snapshot destination already exists", str(target))


def save_new_snapshot(path, value):
    """Publish a complete validated file without replacing an existing baseline."""
    target = pathlib.Path(path)
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".relay-stats-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        # Same-directory hard link is an atomic create-if-absent; unlike replace
        # it cannot overwrite a baseline created after a preflight exists check.
        os.link(temporary, target)
    finally:
        os.unlink(temporary)


def parse_fetch_deadline(value):
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0
    if not 0.001 <= seconds <= 60:  # Also rejects NaN and infinity.
        raise argparse.ArgumentTypeError("fetch deadline must be 0.001 to 60 finite seconds")
    return seconds


@contextlib.contextmanager
def fetch_time_limit(seconds):
    if seconds is None:
        yield
        return
    if not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")):
        raise ValueError("fetch deadline requires POSIX interval timers")
    alarm = signal.SIGALRM
    timer = signal.ITIMER_REAL
    if signal.getsignal(alarm) != signal.SIG_DFL or any(signal.getitimer(timer)):
        raise ValueError("cannot set fetch deadline while SIGALRM is already in use")

    def expired(_signum, _frame):
        raise TimeoutError(f"fetch deadline exceeded ({seconds:g} seconds)")

    # CLI main thread only. Refuse an occupied process timer rather than
    # displacing a caller's timer or handler.
    previous = signal.signal(alarm, expired)
    try:
        signal.setitimer(timer, seconds)
        yield
    finally:
        try:
            signal.setitimer(timer, 0)
        finally:
            signal.signal(alarm, previous)


def capture(address, destination, *, same_instance_as=None, fetch_deadline=None):
    base = loopback_base(address)
    baseline = None
    if same_instance_as is not None:
        try:
            baseline = read_snapshot(same_instance_as)
        except (OSError, ValueError, RecursionError) as error:
            raise ValueError(f"baseline snapshot: {error}") from error
    destination = pathlib.Path(destination)
    check_capture_destination(destination)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with fetch_time_limit(fetch_deadline):
        value = read_stats(opener, base)
    if baseline is not None:
        require_same_instance(baseline, value)
    save_new_snapshot(destination, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api", help="literal loopback IP:port, e.g. 127.0.0.1:8787 or [::1]:8787")
    parser.add_argument("output", help="new snapshot filename; existing files are never overwritten")
    parser.add_argument("--same-instance-as", metavar="BASELINE",
                        help="publish only if the returned instance matches this saved snapshot")
    parser.add_argument("--fetch-deadline", type=parse_fetch_deadline, metavar="SECONDS",
                        help="optional POSIX wall-clock limit for the fetch, 0.001 to 60 seconds")
    args = parser.parse_args(argv)
    try:
        capture(args.api, args.output, same_instance_as=args.same_instance_as,
                fetch_deadline=args.fetch_deadline)
    except (OSError, ValueError, AssertionError, RecursionError, http.client.HTTPException) as error:
        print("statistics capture failed: " + str(error), file=sys.stderr)
        return 1
    print("Saved validated statistics snapshot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
