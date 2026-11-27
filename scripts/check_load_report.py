#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that a successful isolated load run produced readable local evidence."""
import argparse
import json
import os
import pathlib
import stat
import sys

MAX_REPORT_BYTES = 4 * 1024 * 1024
REVISIONS = {"soak": "http-fail-closed-v2", "unique": "unique-capacity-v1"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("non-JSON number")


def read_report(path):
    error = "required report.json is missing, empty, or not a regular non-symlink file"
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(error)

    def open_report(filename, flags):
        return os.open(filename, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))

    with open(path, "rb", opener=open_report) as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(error)
        data = source.read(MAX_REPORT_BYTES + 1)
    if not data:
        raise ValueError(error)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("required report.json exceeds the 4 MiB limit")
    return json.loads(data.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)


def validate_report(report, harness):
    if (type(report) is not dict or report.get("harness_revision") != REVISIONS[harness]
            or report.get("synthetic_only") is not True):
        raise ValueError("required report.json does not identify the expected synthetic harness")
    if harness == "soak":
        if any(not isinstance(report.get(key), dict) or not report[key]
               for key in ("occupancy_900", "occupancy_1024")):
            raise ValueError("required report.json lacks a soak occupancy result")
    elif report.get("passed") is not True:
        raise ValueError("required report.json does not record a successful unique-capacity result")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=tuple(REVISIONS))
    parser.add_argument("report", type=pathlib.Path)
    args = parser.parse_args()
    try:
        validate_report(read_report(args.report), args.harness)
    except FileNotFoundError:
        print("ERROR: required report.json is missing, empty, or not a regular non-symlink file", file=sys.stderr)
        return 1
    except (OSError, ValueError, RecursionError) as error:
        print(f"ERROR: invalid load report: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
