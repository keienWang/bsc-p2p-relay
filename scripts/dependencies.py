#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Record resolved Go modules without machine-local paths or credentials."""
import argparse
import json
import subprocess
import sys

from local_json import destination_path, existing_output_mode, publish_json


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
            replacement = module["Replace"]
            if (not isinstance(replacement, dict) or not isinstance(replacement.get("Path"), str)
                    or not replacement["Path"]):
                raise ValueError("go list returned a replacement without a path")
            record["Replace"] = {key: replacement[key] for key in ("Path", "Version", "Sum") if key in replacement}
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
