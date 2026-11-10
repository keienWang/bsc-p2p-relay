#!/usr/bin/env python3
"""Record resolved Go modules without machine-local paths or credentials."""
import json
import subprocess

text = subprocess.check_output(["go", "list", "-m", "-json", "all"], text=True)
decoder = json.JSONDecoder()
modules = []
while text.strip():
    module, end = decoder.raw_decode(text.lstrip())
    text = text.lstrip()[end:]
    record = {key: module[key] for key in ("Path", "Version", "Sum", "Main") if key in module}
    if "Replace" in module:
        record["Replace"] = {key: module["Replace"][key] for key in ("Path", "Version", "Sum") if key in module["Replace"]}
    modules.append(record)
print(json.dumps({"format": "Go module inventory (not an SPDX/CycloneDX SBOM)", "modules": modules}, indent=2))
