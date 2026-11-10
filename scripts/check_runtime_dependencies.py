#!/usr/bin/env python3
"""Fail if obsolete DTLS v2 re-enters the compiled relay dependency graph."""
import subprocess

packages = subprocess.check_output(["go", "list", "-deps", "-buildvcs=false", "./cmd/bsc-relay"], text=True).splitlines()
obsolete = [p for p in packages if p == "github.com/pion/dtls/v2" or p.startswith("github.com/pion/dtls/v2/")]
if obsolete:
    raise SystemExit("Obsolete DTLS v2 packages in relay build:\n" + "\n".join(obsolete))
print("PASS: compiled relay dependencies exclude DTLS v2")
