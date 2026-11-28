#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare two saved v1 statistics bodies offline; never fetch or submit data."""
import argparse
import datetime
import json
import sys

from capture_stats import (observed_time_key, read_snapshot, require_same_instance,
                           save_new_snapshot)
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
    return selector, parse_uint64(digits, "counter limit must be an ASCII decimal uint64")


def parse_uint64(digits, message):
    if not digits.isascii() or not digits.isdecimal():
        raise argparse.ArgumentTypeError(message)
    digits = digits.lstrip("0") or "0"
    if len(digits) > 20 or (value := int(digits)) > (1 << 64) - 1:
        raise argparse.ArgumentTypeError(message)
    return value


def parse_gap_ms(digits, limit):
    message = f"{limit} observation gap must be a positive ASCII decimal uint64"
    value = parse_uint64(digits, message)
    if not value:
        raise argparse.ArgumentTypeError(message)
    return value


def observed_gap_ns(before, after):
    before_seconds, before_fraction = observed_time_key(before)
    after_seconds, after_fraction = observed_time_key(after)
    # Inputs are already validated as UTC. Subtract whole seconds as a
    # timedelta and handle fractions separately to preserve all nine digits.
    gap = datetime.datetime.fromisoformat(after_seconds) - datetime.datetime.fromisoformat(before_seconds)
    return (gap.days * 86400 + gap.seconds) * 1_000_000_000 + int(after_fraction) - int(before_fraction)


def compare_snapshots(before, after, *, require_forward_time=False, min_gap_ms=None, max_gap_ms=None):
    """Inputs must have passed decode_stats. Deltas are not rates or an identity."""
    require_same_instance(before, after)
    if require_forward_time and observed_time_key(after["observed_at"]) <= observed_time_key(before["observed_at"]):
        raise ValueError("after observed_at must be strictly later than before observed_at")
    if min_gap_ms is not None or max_gap_ms is not None:
        gap_ns = observed_gap_ns(before["observed_at"], after["observed_at"])
        if min_gap_ms is not None and gap_ns < min_gap_ms * 1_000_000:
            raise ValueError(f"observation gap is below {min_gap_ms} ms (recorded {gap_ns} ns)")
        if max_gap_ms is not None:
            if gap_ns < 0:
                raise ValueError(f"observation gap is negative (recorded {gap_ns} ns)")
            if gap_ns > max_gap_ms * 1_000_000:
                raise ValueError(f"observation gap is above {max_gap_ms} ms (recorded {gap_ns} ns)")
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


def collect_limits(parser, values, flag):
    limits = {}
    for selector, count in values or ():
        if selector in limits:
            parser.error(f"duplicate {flag} counter: {selector}")
        limits[selector] = count
    return limits


def counter_limits(result, limits):
    for selector, limit in sorted(limits.items()):
        group, field = selector.split(".")
        yield selector, result[group + "_delta"][field], limit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", help="earlier saved JSON response body")
    parser.add_argument("after", help="later saved JSON response body from the same endpoint/instance")
    parser.add_argument("--output", help="save a complete comparison to a new file; never overwrite")
    parser.add_argument("--require-forward-time", action="store_true",
                        help="reject equal or decreasing UTC observation timestamps")
    parser.add_argument("--min-observation-gap-ms", type=lambda value: parse_gap_ms(value, "minimum"),
                        metavar="MILLISECONDS",
                        help="require the recorded UTC observation gap to be at least this many milliseconds")
    parser.add_argument("--max-observation-gap-ms", type=lambda value: parse_gap_ms(value, "maximum"),
                        metavar="MILLISECONDS",
                        help="require a nonnegative recorded UTC observation gap at most this many milliseconds")
    guard = parser.add_mutually_exclusive_group()
    guard.add_argument("--require-unchanged", action="store_true",
                       help="exit 3 after reporting a valid comparison with any nonzero delta")
    guard.add_argument("--require-unchanged-counter", action="append", choices=SELECTABLE_COUNTERS,
                       metavar="GROUP.COUNTER", help="exit 3 if a selected counter increased; repeatable")
    guard.add_argument("--max-counter-increase", action="append", type=parse_counter_budget,
                       metavar="GROUP.COUNTER=COUNT",
                       help="exit 3 if a selected counter delta exceeds its limit; repeatable")
    parser.add_argument("--min-counter-increase", action="append", type=parse_counter_budget,
                        metavar="GROUP.COUNTER=COUNT",
                        help="exit 3 if a selected counter delta is below its minimum; repeatable")
    parser.add_argument("--explain-guard", action="store_true",
                        help="on exit 3, log each failed counter limit to stderr without changing the report")
    args = parser.parse_args(argv)
    if (args.min_observation_gap_ms is not None and args.max_observation_gap_ms is not None
            and args.min_observation_gap_ms > args.max_observation_gap_ms):
        parser.error("minimum observation gap cannot exceed maximum")
    minimums = collect_limits(parser, args.min_counter_increase, "--min-counter-increase")
    if args.require_unchanged:
        maximums = dict.fromkeys(SELECTABLE_COUNTERS, 0)
    elif args.require_unchanged_counter:
        maximums = dict.fromkeys(args.require_unchanged_counter, 0)
    else:
        maximums = collect_limits(parser, args.max_counter_increase, "--max-counter-increase")
    if args.explain_guard and not (minimums or maximums):
        parser.error("--explain-guard requires a counter guard")
    for selector, minimum in minimums.items():
        if selector in maximums and minimum > maximums[selector]:
            parser.error(f"minimum counter increase exceeds maximum: {selector}")
    stage = "before snapshot"
    try:
        before = read_snapshot(args.before)
        stage = "after snapshot"
        after = read_snapshot(args.after)
        stage = "snapshot comparison"
        result = compare_snapshots(before, after, require_forward_time=args.require_forward_time,
                                   min_gap_ms=args.min_observation_gap_ms, max_gap_ms=args.max_observation_gap_ms)
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
    exceeded = [(name, delta, limit) for name, delta, limit in counter_limits(result, maximums)
                if delta > limit]
    missed = [(name, delta, limit) for name, delta, limit in counter_limits(result, minimums)
              if delta < limit]
    if args.explain_guard:
        for selector, delta, limit in exceeded:
            print(f"counter guard exceeded: {selector} delta={delta}, limit={limit}", file=sys.stderr)
        for selector, delta, limit in missed:
            print(f"counter guard below minimum: {selector} delta={delta}, minimum={limit}", file=sys.stderr)
    return 3 if exceeded or missed else 0


if __name__ == "__main__":
    sys.exit(main())
