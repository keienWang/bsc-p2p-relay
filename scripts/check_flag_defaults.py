#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare runbook defaults with the built relay's offline Go flag help."""

import argparse
import json
import pathlib
import re
import subprocess
import sys

from check_operations_docs import ROOT, table_names


NAME = r"[a-z][a-z0-9_-]*"
HEADER = re.compile(rf"  -({NAME})(?: (\w+))?")
ROW = re.compile(rf"^\| `--({NAME})` \| (?:`([^`]+)`|(none)) \|", re.MULTILINE)
DEFAULT = re.compile(r" \(default (.+)\)\Z")


def documented_defaults(text):
    names = table_names(text, "flags", "--")
    block = text.split("<!-- operations-flags:start -->", 1)[1].split(
        "<!-- operations-flags:end -->", 1)[0]
    rows = ROW.findall(block)
    if len(rows) != len(names) or {name for name, _, _ in rows} != names:
        raise ValueError("unsupported operations flag default cell")
    return {name: literal if literal else none for name, literal, none in rows}


def help_defaults(text):
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Usage of ") or not lines[0].endswith(":"):
        raise ValueError("missing Go flag help header")
    flags = {}
    for line in lines[1:]:
        header = HEADER.fullmatch(line)
        if header:
            name, kind = header.groups()
            if name in flags:
                raise ValueError(f"duplicate flag in help: --{name}")
            flags[name] = (kind, [])
        elif flags and line.startswith("    \t"):
            flags[name][1].append(line[5:])
        else:
            raise ValueError("unexpected Go flag help layout")
    if not flags:
        raise ValueError("no flags found in help")
    defaults = {}
    for name, (kind, description) in flags.items():
        if not description or not description[-1]:
            raise ValueError(f"missing help description: --{name}")
        match = DEFAULT.search(description[-1])
        if kind is None:
            value = match[1] if match else "false"
            if value not in {"false", "true"}:
                raise ValueError(f"unsupported boolean default: --{name}")
        elif kind == "value" and name == "peer" and not match:
            # The repeatable enode option has an empty list by default.
            value = "none"
        elif match and kind in {"string", "int", "duration"}:
            value = json.loads(match[1]) if kind == "string" else match[1]
            if not isinstance(value, str):
                raise ValueError(f"unsupported help default: --{name}")
        else:
            raise ValueError(f"unsupported help default: --{name}")
        defaults[name] = value
    return defaults


def compare_defaults(document, usage):
    documented, compiled = documented_defaults(document), help_defaults(usage)
    missing, extra = sorted(compiled.keys() - documented), sorted(documented.keys() - compiled)
    if missing or extra:
        return [f"flag names differ: undocumented={missing}; not in binary={extra}"]
    return [
        f"--{name}: documented {documented[name]!r}, binary default {compiled[name]!r}"
        for name in sorted(compiled) if documented[name] != compiled[name]
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    args = parser.parse_args()
    try:
        # The invalid API address is an extra guard against reaching startup if
        # a future regression accidentally stops treating --help as terminal.
        result = subprocess.run(
            [str(args.binary.resolve()), "--api", "not-an-address", "--no-discovery",
             "--p2p", "127.0.0.1:0", "--help"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0 or result.stdout:
            raise ValueError("relay help did not exit successfully with stderr-only usage")
        errors = compare_defaults((ROOT / "docs/OPERATIONS.md").read_text(), result.stderr)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        errors = [str(error)]
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS: runbook CLI defaults match the built relay's offline --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
