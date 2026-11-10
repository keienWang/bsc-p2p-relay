#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Narrow offline documentation/source check, not a Go or Markdown parser.

No subprocesses, imports outside the standard library, or network requests.
Only the changed operator docs and their local link targets are inspected.
"""

import pathlib
import re
import sys
import urllib.parse


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCUMENTS = ("docs/OPERATIONS.md", "README.md", "docs/PRODUCTION_READINESS.md",
             "docs/SECURITY.md")


def table_names(doc, section, prefix=""):
    block = re.search(
        rf"<!-- operations-{section}:start -->(.*?)<!-- operations-{section}:end -->",
        doc, re.DOTALL,
    )
    if block is None:
        raise ValueError(f"missing operations-{section} table markers")
    rows = [line for line in block[1].splitlines() if line.startswith("|")]
    names = re.findall(rf"^\| `{re.escape(prefix)}([^`]+)` \|", block[1], re.MULTILINE)
    if (not names or len(names) != len(set(names)) or len(names) != len(rows) - 2
            or any(not re.fullmatch(r"[a-z][a-z0-9_-]*", name) for name in names)):
        raise ValueError(f"empty, duplicate or unsupported operations-{section} table rows")
    return set(names)


def json_fields(source, struct):
    block = re.search(rf"type {struct} struct \{{(.*?)^\}}", source, re.DOTALL | re.MULTILINE)
    if block is None:
        raise ValueError(f"missing Go struct {struct}")
    fields = []
    for line in block[1].splitlines():
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        declaration = re.fullmatch(r'\s*[A-Z]\w*\s+[^`]+`([^`]+)`\s*(?://.*)?', line)
        tag = re.fullmatch(r'json:"([^"]*)"', declaration[1]) if declaration else None
        if tag is None or not re.fullmatch(r"[a-z][a-z0-9_]*", tag[1]):
            raise ValueError(f"unsupported {struct} field/tag syntax; review checker: {line.strip()}")
        fields.append(tag[1])
    if not fields:
        raise ValueError(f"no JSON fields found in {struct}")
    if len(fields) != len(set(fields)):
        raise ValueError(f"duplicate JSON field names in {struct}")
    return set(fields)


def check_contracts(doc, cli, node, http, store):
    # This checker supports the current default import and direct flag calls,
    # not a general Go name resolver. Reject aliases/receivers instead of
    # silently overlooking registrations through flag.CommandLine or a saved
    # function value. A source refactor must update this narrow checker too.
    if cli.count('"flag"') != 1 or not re.search(r'^\s*"flag"\s*$', cli, re.MULTILINE):
        raise ValueError("unsupported flag import syntax; review checker")
    if re.search(r"\bflag\s*(?:\s*\.\s*)?/[/\*]", cli):
        raise ValueError("comments in flag references need manual review")
    references = list(re.finditer(r"\bflag\s*\.\s*(\w+)", cli))
    for reference in references:
        if not re.match(r"\s*\(", cli[reference.end():]):
            raise ValueError("unsupported flag reference or receiver; review checker")
    calls = re.findall(r"\bflag\s*\.\s*(\w+)\s*\(", cli)
    methods = set(calls)
    supported = {"StringVar", "BoolVar", "IntVar", "DurationVar", "Var", "String", "Parse"}
    if methods - supported:
        raise ValueError(f"unrecognized CLI flag methods; review checker: {sorted(methods - supported)}")
    declarations = re.findall(
        r'\bflag\s*\.\s*(?:StringVar|BoolVar|IntVar|DurationVar|Var)\s*\([^,]+,\s*"([^"]*)"\s*,', cli)
    declarations += re.findall(r'\bflag\s*\.\s*String\s*\(\s*"([^"]*)"\s*,', cli)
    if (len(declarations) != sum(method != "Parse" for method in calls)
            or any(not re.fullmatch(r"[a-z][a-z0-9_-]*", name) for name in declarations)):
        raise ValueError("unparsed or unsupported CLI flag declarations; review checker")
    if len(declarations) != len(set(declarations)):
        raise ValueError("duplicate CLI flag names in source")
    flags = set(declarations)
    expected = {"flags": flags, "submission": json_fields(node, "Submission"),
                "health": json_fields(node, "Health")}
    if not flags:
        raise ValueError("no CLI flag declarations found")
    errors = []
    for section, source_names in expected.items():
        documented = table_names(doc, section, "--" if section == "flags" else "")
        if documented != source_names:
            errors.append(f"{section}: undocumented={sorted(source_names - documented)}; "
                          f"not in source={sorted(documented - source_names)}")
    # Ignore only the function declaration, then account for every remaining
    # apiError reference. Literal errors may use numeric or net/http status
    # constants. The current dynamic forwarding call is explicitly recognized;
    # any other expression, alias or escaped Go string needs manual review.
    handler = re.sub(r"\bfunc\s+apiError\s*\(", "func _apiErrorDeclaration(", http)
    error_references = list(re.finditer(r"\bapiError\b", handler))
    for reference in error_references:
        if not re.match(r"\s*\(", handler[reference.end():]):
            raise ValueError("unsupported apiError reference; review checker")
    literal_errors = re.findall(
        r'\bapiError\s*\(\s*w\s*,\s*(?:\d+|http\.Status[A-Za-z]+)\s*,\s*"([^"\\]*)"\s*\)',
        handler,
    )
    forwarded_errors = re.findall(
        r"\bapiError\s*\(\s*w\s*,\s*status\s*,\s*err\.Error\(\)\s*\)", handler)
    if len(error_references) != len(literal_errors) + len(forwarded_errors):
        raise ValueError("unparsed apiError call; review checker")
    messages = set(literal_errors)
    admission_source = node + store
    if re.search(r"\bvar\s*(?:\(|/[/\*])", admission_source):
        raise ValueError("grouped/commented var declarations need manual review")
    admission_declarations = re.findall(r"\bvar\s+Err\w+\b", admission_source)
    admission_assignments = re.findall(r"\bErr\w+\s*:?=(?!=)", admission_source)
    admission_errors = re.findall(
        r'\bvar\s+Err\w+\s*=\s*errors\.New\("([^"\\]*)"\)', admission_source)
    if len(admission_declarations) != len(admission_errors) or len(admission_assignments) != len(admission_errors):
        raise ValueError("unparsed admission-error declaration or reassignment; review checker")
    messages.update(admission_errors)
    if not messages:
        raise ValueError("no application error strings found")
    for message in sorted(messages):
        if f"`{message}`" not in doc:
            errors.append(f"undocumented application error: {message}")
    return errors


def heading_anchors(markdown):
    """Enough GitHub-style heading slugs for this repository's simple headings."""
    counts = {}
    anchors = set()
    for heading in re.findall(r"^#{1,6} +(.+?)\s*#*\s*$", markdown, re.MULTILINE):
        slug = re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


def check_links(root, relative):
    document = root / relative
    errors = []
    for destination in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", document.read_text()):
        link = urllib.parse.urlsplit(destination)
        if link.scheme or link.netloc:
            continue  # Deliberately do not fetch or validate external destinations.
        target = (document.parent / urllib.parse.unquote(link.path)).resolve() if link.path else document
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            errors.append(f"{relative}: invalid local file link {destination}")
        elif link.fragment and urllib.parse.unquote(link.fragment) not in heading_anchors(target.read_text()):
            errors.append(f"{relative}: missing heading link {destination}")
    return errors


def main():
    try:
        read = lambda path: (ROOT / path).read_text()
        errors = check_contracts(read("docs/OPERATIONS.md"), read("cmd/bsc-relay/main.go"),
                                 read("internal/relay/node.go"), read("internal/relay/http.go"),
                                 read("internal/relay/store.go"))
        for document in DOCUMENTS:
            errors.extend(check_links(ROOT, document))
    except (OSError, ValueError) as error:
        errors = [str(error)]
    if errors:
        print("FAIL: operations documentation check", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("PASS: CLI/JSON field names, literal application errors, local doc links; no default-value, network or runtime checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
