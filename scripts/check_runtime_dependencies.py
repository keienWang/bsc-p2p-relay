#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reject obsolete compiled packages and the unsupported split OpenPGP module."""
import subprocess

RELAY_MODULE = "github.com/keienWang/bsc-p2p-relay"
FORBIDDEN_PACKAGES = ("github.com/pion/dtls/v2", "golang.org/x/crypto/openpgp")
FORBIDDEN_MODULES = ("golang.org/x/crypto/openpgp",)


def forbidden_packages(packages):
    return [package for package in packages if any(
        package.split(" [", 1)[0] == prefix or package.split(" [", 1)[0].startswith(prefix + "/")
        for prefix in FORBIDDEN_PACKAGES)]


def forbidden_modules(modules):
    # DTLS v2 remains in BSC's unused complete upstream module graph. Its compiled
    # packages are forbidden above. Do not add the unsupported OpenPGP split
    # module even as a graph-only dependency to clear a parent-module advisory.
    return [module for module in modules if module in FORBIDDEN_MODULES]


def main():
    packages = subprocess.check_output(
        ["go", "list", "-deps", "-buildvcs=false", "./cmd/bsc-relay"], text=True).splitlines()
    if RELAY_MODULE + "/cmd/bsc-relay" not in packages:
        raise SystemExit("Runtime dependency graph does not contain the expected relay command")
    test_packages = subprocess.check_output(
        ["go", "list", "-deps", "-test", "-buildvcs=false", "./..."], text=True).splitlines()
    if RELAY_MODULE + "/internal/relay.test" not in test_packages:
        raise SystemExit("Test dependency graph does not contain the expected relay test binary")
    obsolete = forbidden_packages(sorted(set(packages + test_packages)))
    if obsolete:
        raise SystemExit("Forbidden packages in relay/default test graph:\n" + "\n".join(obsolete))
    modules = subprocess.check_output(
        ["go", "list", "-m", "-f", "{{.Path}}", "all"], text=True).splitlines()
    if RELAY_MODULE not in modules:
        raise SystemExit("Module dependency graph does not contain the expected relay module")
    unsupported = forbidden_modules(modules)
    if unsupported:
        raise SystemExit("Unsupported modules in dependency graph:\n" + "\n".join(unsupported))
    print("PASS: no DTLS v2/OpenPGP in relay/test package graphs or split OpenPGP module")


if __name__ == "__main__":
    main()
