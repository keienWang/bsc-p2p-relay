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
import urllib.request

from smoke import MAX_STATS_RESPONSE_BYTES, decode_stats, read_stats


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def loopback_base(address):
    # Parse the literal argument before constructing a URL. A URL parser can
    # silently strip control characters or accept empty query/fragment syntax.
    error = "API must be a literal loopback IP and port"
    host_text, separator, port_text = address.rpartition(":")
    if not separator or "%" in host_text or not port_text.isascii() or not port_text.isdecimal():
        raise ValueError(error)
    digits = port_text.lstrip("0")
    if not digits or len(digits) > 5 or (port := int(digits)) > 65535:
        raise ValueError(error)
    try:
        if host_text.startswith("[") and host_text.endswith("]"):
            host = ipaddress.IPv6Address(host_text[1:-1])
        else:
            host = ipaddress.IPv4Address(host_text)
    except ipaddress.AddressValueError as exc:
        raise ValueError(error) from exc
    if not host.is_loopback:
        raise ValueError(error)
    canonical = f"[{host}]" if host.version == 6 else str(host)
    return f"http://{canonical}:{port}"


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


def observed_time_key(stamp):
    """Sort validated v1 UTC timestamps without dropping nanoseconds."""
    seconds, _, fraction = stamp[:-1].partition(".")
    return seconds, fraction.ljust(9, "0")


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


def parse_deadline(value, label):
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0
    if not 0.001 <= seconds <= 60:  # Also rejects NaN and infinity.
        raise argparse.ArgumentTypeError(f"{label} must be 0.001 to 60 finite seconds")
    return seconds


def parse_fetch_deadline(value):
    return parse_deadline(value, "fetch deadline")


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


def capture(address, destination, *, same_instance_as=None, fetch_deadline=None,
            require_forward_time=False):
    if require_forward_time and same_instance_as is None:
        raise ValueError("forward observation time requires a baseline snapshot")
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
        if (require_forward_time
                and observed_time_key(value["observed_at"]) <= observed_time_key(baseline["observed_at"])):
            raise ValueError("captured observed_at must be strictly later than baseline observed_at")
    save_new_snapshot(destination, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api", help="literal loopback IP:port, e.g. 127.0.0.1:8787 or [::1]:8787")
    parser.add_argument("output", help="new snapshot filename; existing files are never overwritten")
    parser.add_argument("--same-instance-as", metavar="BASELINE",
                        help="publish only if the returned instance matches this saved snapshot")
    parser.add_argument("--require-forward-time", action="store_true",
                        help="require observed_at to advance relative to --same-instance-as")
    parser.add_argument("--fetch-deadline", type=parse_fetch_deadline, metavar="SECONDS",
                        help="optional POSIX wall-clock limit for the fetch, 0.001 to 60 seconds")
    args = parser.parse_args(argv)
    if args.require_forward_time and args.same_instance_as is None:
        parser.error("--require-forward-time requires --same-instance-as")
    try:
        capture(args.api, args.output, same_instance_as=args.same_instance_as,
                fetch_deadline=args.fetch_deadline, require_forward_time=args.require_forward_time)
    except (OSError, ValueError, AssertionError, RecursionError, http.client.HTTPException) as error:
        print("statistics capture failed: " + str(error), file=sys.stderr)
        return 1
    print("Saved validated statistics snapshot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
