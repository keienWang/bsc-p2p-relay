#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture one validated loopback statistics snapshot into a new local file."""
import argparse
import http.client
import ipaddress
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from smoke import read_stats


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


def capture(address, destination):
    base = loopback_base(address)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    value = read_stats(opener, base)
    save_new_snapshot(destination, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api", help="literal loopback IP:port, e.g. 127.0.0.1:8787 or [::1]:8787")
    parser.add_argument("output", help="new snapshot filename; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        capture(args.api, args.output)
    except (OSError, ValueError, AssertionError, RecursionError, http.client.HTTPException) as error:
        print("statistics capture failed: " + str(error), file=sys.stderr)
        return 1
    print("Saved validated statistics snapshot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
