#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare two saved v1 statistics bodies offline; never fetch or submit data."""
import argparse
import json
import pathlib
import sys

from smoke import MAX_STATS_RESPONSE_BYTES, decode_stats
from capture_stats import save_new_snapshot


def read_snapshot(path):
    with pathlib.Path(path).open("rb") as source:
        payload = source.read(MAX_STATS_RESPONSE_BYTES + 1)
    if len(payload) > MAX_STATS_RESPONSE_BYTES:
        raise ValueError("snapshot exceeds response bound")
    return decode_stats(payload)


def compare_snapshots(before, after):
    """Inputs must have passed decode_stats. Deltas are not rates or an identity."""
    if before["instance_id"] != after["instance_id"]:
        raise ValueError("instance changed; start a new comparison baseline")
    if before["created_at"] != after["created_at"]:
        raise ValueError("instance creation metadata changed")
    groups = {}
    for group in ("submit", "http"):
        delta = {}
        for field, old in before[group].items():
            new = after[group][field]
            if new < old:
                raise ValueError("counter decreased; wrap, reset or sample ordering invalidates delta")
            delta[field] = new - old
        groups[group + "_delta"] = delta
    return {
        "source_schema_version": 1,
        "instance_id": before["instance_id"],
        "created_at": before["created_at"],
        "before_observed_at": before["observed_at"],
        "after_observed_at": after["observed_at"],
        **groups,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", help="earlier saved JSON response body")
    parser.add_argument("after", help="later saved JSON response body from the same endpoint/instance")
    parser.add_argument("--output", help="save a complete comparison to a new file; never overwrite")
    parser.add_argument("--require-unchanged", action="store_true",
                        help="exit 3 after reporting a valid comparison with nonzero deltas")
    args = parser.parse_args(argv)
    try:
        result = compare_snapshots(read_snapshot(args.before), read_snapshot(args.after))
        if args.output is not None:
            save_new_snapshot(args.output, result)
    except (OSError, ValueError, RecursionError) as error:
        print("statistics unavailable: " + str(error), file=sys.stderr)
        return 1
    if args.output is None:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        print("Saved validated statistics comparison.")
    if args.require_unchanged and any(
        count for group in ("submit_delta", "http_delta") for count in result[group].values()
    ):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
