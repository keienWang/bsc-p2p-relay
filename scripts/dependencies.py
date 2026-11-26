#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Record resolved Go modules without machine-local paths or credentials."""
import argparse
import json
import subprocess
import sys

from local_json import destination_path, existing_output_mode, publish_json


def portable_replacement(replacement):
    if not isinstance(replacement, dict) or not isinstance(replacement.get("Path"), str) or not replacement["Path"]:
        raise ValueError("go list returned a replacement without a path")
    target = replacement["Path"]
    if not replacement.get("Version"):
        # Go uses a filesystem path when the replacement has no version.
        # Keep the adapter's portable spelling but never persist an absolute
        # directory, a parent traversal, or a platform-specific path.
        parts = target[2:].split("/") if target.startswith("./") else []
        if not parts or any(part in {"", ".", ".."} for part in parts) or "\\" in target or ":" in target:
            raise ValueError("local replacement must use a portable ./path within the checkout")
    return {key: replacement[key] for key in ("Path", "Version", "Sum") if key in replacement}


def inventory(text):
    decoder = json.JSONDecoder()
    modules = []
    while text := text.lstrip():
        module, end = decoder.raw_decode(text)
        text = text[end:]
        if not isinstance(module, dict) or not isinstance(module.get("Path"), str) or not module["Path"]:
            raise ValueError("go list returned a module without a path")
        record = {key: module[key] for key in ("Path", "Version", "Sum", "Main") if key in module}
        if "Replace" in module:
            record["Replace"] = portable_replacement(module["Replace"])
        modules.append(record)
    if not modules:
        raise ValueError("go list returned no modules")
    return {"format": "Go module inventory (not an SPDX/CycloneDX SBOM)", "modules": modules}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="atomically replace an inventory file instead of writing stdout")
    args = parser.parse_args()
    try:
        output = destination_path(args.output) if args.output is not None else None
        if output is not None:
            existing_output_mode(output, "module inventory")
        text = subprocess.check_output(["go", "list", "-m", "-json", "all"], text=True)
        value = inventory(text)
        if output is None:
            print(json.dumps(value, indent=2))
        else:
            publish_json(output, value, label="module inventory", prefix=".relay-dependencies-")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"dependency inventory unavailable: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
