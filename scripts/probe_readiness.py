#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check one local Relay readiness response without submitting anything."""
import argparse
import http.client
import json
import sys
import urllib.error
import urllib.request

from capture_stats import NoRedirect, fetch_time_limit, loopback_base, parse_fetch_deadline
from smoke import read_observation_body, unique_object

CHAIN_IDS = {"mainnet": "56", "testnet": "97"}
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
    for fields, maximum in ((PEER_FIELDS, 200), (OCCUPANCY_FIELDS, (1 << 63) - 1),
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api", help="literal loopback IP:port, e.g. 127.0.0.1:8787 or [::1]:8787")
    parser.add_argument("--expect-network", choices=tuple(CHAIN_IDS),
                        help="reject observations from a different configured network")
    parser.add_argument("--fetch-deadline", type=parse_fetch_deadline, metavar="SECONDS",
                        help="optional POSIX wall-clock limit for the fetch, 0.001 to 60 seconds")
    args = parser.parse_args(argv)
    try:
        result = probe(args.api, args.fetch_deadline)
        if args.expect_network is not None and result["network"] != args.expect_network:
            raise ValueError(f"network mismatch: expected {args.expect_network}, reported {result['network']}")
    except (OSError, ValueError, AssertionError, RecursionError, http.client.HTTPException) as error:
        print(f"readiness unavailable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready_peers"] else 3


if __name__ == "__main__":
    sys.exit(main())
