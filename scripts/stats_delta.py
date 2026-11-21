#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare two saved v1 statistics bodies offline; never fetch or submit data."""
import argparse
import json
import sys

from capture_stats import read_snapshot, require_same_instance, save_new_snapshot
from smoke import STATS_HTTP_FIELDS, STATS_SUBMIT_FIELDS

SELECTABLE_COUNTERS = tuple(sorted(
    f"{group}.{field}"
    for group, fields in (("submit", STATS_SUBMIT_FIELDS), ("http", STATS_HTTP_FIELDS))
    for field in fields
))


def compare_snapshots(before, after):
    """Inputs must have passed decode_stats. Deltas are not rates or an identity."""
    require_same_instance(before, after)
    groups = {}
    for group in ("submit", "http"):
        delta = {}
        for field, old in before[group].items():
            new = after[group][field]
            if new < old:
                raise ValueError(f"counter decreased ({group}.{field}: {old} -> {new}); "
                                 "wrap, reset or sample ordering invalidates delta")
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
    guard = parser.add_mutually_exclusive_group()
    guard.add_argument("--require-unchanged", action="store_true",
                       help="exit 3 after reporting a valid comparison with any nonzero delta")
    guard.add_argument("--require-unchanged-counter", action="append", choices=SELECTABLE_COUNTERS,
                       metavar="GROUP.COUNTER", help="exit 3 if a selected counter increased; repeatable")
    args = parser.parse_args(argv)
    stage = "before snapshot"
    try:
        before = read_snapshot(args.before)
        stage = "after snapshot"
        after = read_snapshot(args.after)
        stage = "snapshot comparison"
        result = compare_snapshots(before, after)
        if args.output is not None:
            stage = "output report"
            save_new_snapshot(args.output, result)
    except (OSError, ValueError, RecursionError) as error:
        print(f"statistics unavailable: {stage}: {error}", file=sys.stderr)
        return 1
    if args.output is None:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        print("Saved validated statistics comparison.")
    if args.require_unchanged and any(
        count for group in ("submit_delta", "http_delta") for count in result[group].values()
    ):
        return 3
    for selector in args.require_unchanged_counter or ():
        group, field = selector.split(".")
        if result[group + "_delta"][field]:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
