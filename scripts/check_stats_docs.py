#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline check of fixed stats JSON field documentation and local links.

This narrow source check is not a Go parser, JSON type validator or runtime test.
"""
import pathlib
import re
import sys

from check_operations_docs import check_links, json_fields, table_names

ROOT = pathlib.Path(__file__).resolve().parents[1]


def check_contract(doc, source, http_source):
    errors = []
    marked = doc.replace("<!-- stats-", "<!-- operations-stats-")
    for section, struct in (("metadata", "Stats"), ("submit", "SubmitStats"),
                            ("http", "HTTPStats")):
        documented = table_names(marked, "stats-" + section)
        actual = json_fields(source, struct)
        if documented != actual:
            errors.append(f"{struct}: undocumented={sorted(actual - documented)}; "
                          f"not in source={sorted(documented - actual)}")
    versions = re.findall(r"\bSchemaVersion\s*:\s*([^,\n}]+)", source)
    if versions != ["1"]:
        errors.append("v1 must have exactly one literal SchemaVersion: 1 assignment")
    if 'case "/v1/stats":' not in http_source or "GET /v1/stats" not in doc:
        errors.append("missing documented /v1/stats route")
    return errors


def main():
    try:
        errors = check_contract((ROOT / "docs/OBSERVABILITY.md").read_text(),
                                (ROOT / "internal/relay/stats.go").read_text(),
                                (ROOT / "internal/relay/http.go").read_text())
        errors.extend(check_links(ROOT, "docs/OBSERVABILITY.md"))
    except (OSError, ValueError) as error:
        errors = [str(error)]
    if errors:
        print("FAIL: stats documentation check", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("PASS: stats JSON field names, literal v1 version/route, local links; no runtime/type checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
