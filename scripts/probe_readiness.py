#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check one local Relay readiness response without submitting anything."""
import argparse
import http.client
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

from capture_stats import (NoRedirect, check_capture_destination, fetch_time_limit,
                           loopback_base, parse_fetch_deadline, save_new_snapshot)
from smoke import read_observation_body, unique_object

CHAIN_IDS = {"mainnet": "56", "testnet": "97"}
MAX_PEERS = 200
MAX_ATTEMPTS = 20
RETRY_DELAY_SECONDS = 0.25
OCCUPANCY_FIELDS = {"cached_transactions", "cached_bytes"}
PEER_FIELDS = {"ready_peers", "connected_peers"}
WRITE_FIELDS = {"written_messages", "write_failures"}
HEALTH_FIELDS = {"network", "chain_id", "confirmation_tracking"} | OCCUPANCY_FIELDS | PEER_FIELDS | WRITE_FIELDS


def decode_readiness(payload, status):
    value = json.loads(payload, object_pairs_hook=unique_object)
    if type(value) is not dict or set(value) != HEALTH_FIELDS:
        raise ValueError("unexpected readiness object fields")
    network = value["network"]
    if type(network) is not str or network not in CHAIN_IDS or value["chain_id"] != CHAIN_IDS[network]:
        raise ValueError("invalid readiness network/chain identity")
    if value["confirmation_tracking"] is not False:
        raise ValueError("invalid readiness confirmation_tracking flag")
    for fields, maximum in ((PEER_FIELDS, MAX_PEERS), (OCCUPANCY_FIELDS, (1 << 63) - 1),
                            (WRITE_FIELDS, (1 << 64) - 1)):
        if any(type(value[key]) is not int or not 0 <= value[key] <= maximum for key in fields):
            raise ValueError("invalid readiness peer, occupancy, or write count")
    ready = value["ready_peers"]
    if ready > value["connected_peers"] or status != (200 if ready else 503):
        raise ValueError("readiness status and peer counts disagree")
    return value


def probe(address, fetch_deadline=None):
    base = loopback_base(address)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with fetch_time_limit(fetch_deadline):
        try:
            response = opener.open(base + "/readyz", timeout=2)
        except urllib.error.HTTPError as error:
            if error.code != 503:
                error.close()
                raise
            response = error  # 503 can be a valid readiness observation.
        with response:
            if response.status not in (200, 503):
                raise ValueError(f"unexpected readiness HTTP status {response.status}")
            return decode_readiness(read_observation_body(response, "readiness"), response.status)


def parse_min_ready_peers(value):
    try:
        count = int(value)
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_PEERS:
        raise argparse.ArgumentTypeError(f"minimum ready peers must be an integer from 1 to {MAX_PEERS}")
    return count


def parse_attempts(value):
    try:
        count = int(value)
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_ATTEMPTS:
        raise argparse.ArgumentTypeError(f"attempts must be an integer from 1 to {MAX_ATTEMPTS}")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api", help="literal loopback IP:port, e.g. 127.0.0.1:8787 or [::1]:8787")
    parser.add_argument("--expect-network", choices=tuple(CHAIN_IDS),
                        help="reject observations from a different configured network")
    parser.add_argument("--min-ready-peers", type=parse_min_ready_peers, default=1, metavar="COUNT",
                        help=f"minimum ready peers for exit 0 (1 to {MAX_PEERS}, default 1)")
    parser.add_argument("--fetch-deadline", type=parse_fetch_deadline, metavar="SECONDS",
                        help="optional POSIX wall-clock limit for the fetch, 0.001 to 60 seconds")
    parser.add_argument("--attempts", type=parse_attempts, default=1, metavar="COUNT",
                        help=f"at most {MAX_ATTEMPTS} GETs; more than one requires --fetch-deadline")
    parser.add_argument("--output", type=pathlib.Path, metavar="FILE",
                        help="save the validated observation to a new file instead of stdout")
    args = parser.parse_args(argv)
    if args.attempts > 1 and args.fetch_deadline is None:
        parser.error("--attempts above 1 requires --fetch-deadline")
    try:
        if args.output is not None:
            check_capture_destination(args.output)
        expected_network = args.expect_network
        for attempt in range(1, args.attempts + 1):
            result = probe(args.api, args.fetch_deadline)
            if expected_network is None:
                expected_network = result["network"]
            elif result["network"] != expected_network:
                raise ValueError(f"network mismatch: expected {expected_network}, reported {result['network']}")
            if result["ready_peers"] >= args.min_ready_peers or attempt == args.attempts:
                break
            time.sleep(RETRY_DELAY_SECONDS)
        if args.output is not None:
            save_new_snapshot(args.output, result)
    except (OSError, ValueError, AssertionError, RecursionError, http.client.HTTPException) as error:
        print(f"readiness unavailable: {error}", file=sys.stderr)
        return 1
    if args.output is None:
        print(json.dumps(result, sort_keys=True))
    return 0 if result["ready_peers"] >= args.min_ready_peers else 3


if __name__ == "__main__":
    sys.exit(main())
