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


def parse_counter_budget(value):
    selector, separator, digits = value.partition("=")
    if not separator or selector not in SELECTABLE_COUNTERS:
        raise argparse.ArgumentTypeError("expected an input counter as GROUP.COUNTER=COUNT")
    if not digits.isascii() or not digits.isdecimal():
        raise argparse.ArgumentTypeError("counter limit must be an ASCII decimal uint64")
    digits = digits.lstrip("0") or "0"
    if len(digits) > 20 or int(digits) > (1 << 64) - 1:
        raise argparse.ArgumentTypeError("counter limit must be an ASCII decimal uint64")
    group, field = selector.split(".")
    return group, field, int(digits)


def observed_time_key(stamp):
    """Sort validated v1 UTC timestamps without dropping nanoseconds."""
    seconds, _, fraction = stamp[:-1].partition(".")
    return seconds, fraction.ljust(9, "0")


def compare_snapshots(before, after, *, require_forward_time=False):
    """Inputs must have passed decode_stats. Deltas are not rates or an identity."""
    require_same_instance(before, after)
    if require_forward_time and observed_time_key(after["observed_at"]) <= observed_time_key(before["observed_at"]):
        raise ValueError("after observed_at must be strictly later than before observed_at")
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
    parser.add_argument("--require-forward-time", action="store_true",
                        help="reject equal or decreasing UTC observation timestamps")
    guard = parser.add_mutually_exclusive_group()
    guard.add_argument("--require-unchanged", action="store_true",
                       help="exit 3 after reporting a valid comparison with any nonzero delta")
    guard.add_argument("--require-unchanged-counter", action="append", choices=SELECTABLE_COUNTERS,
                       metavar="GROUP.COUNTER", help="exit 3 if a selected counter increased; repeatable")
    guard.add_argument("--max-counter-increase", action="append", type=parse_counter_budget,
                       metavar="GROUP.COUNTER=COUNT",
                       help="exit 3 if a selected counter delta exceeds its limit; repeatable")
    args = parser.parse_args(argv)
    budgets = {}
    for group, field, limit in args.max_counter_increase or ():
        key = group, field
        if key in budgets:
            parser.error(f"duplicate --max-counter-increase counter: {group}.{field}")
        budgets[key] = limit
    stage = "before snapshot"
    try:
        before = read_snapshot(args.before)
        stage = "after snapshot"
        after = read_snapshot(args.after)
        stage = "snapshot comparison"
        result = compare_snapshots(before, after, require_forward_time=args.require_forward_time)
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
    if any(result[group + "_delta"][field] > limit for (group, field), limit in budgets.items()):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
