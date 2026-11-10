#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate a CycloneDX 1.6 SBOM for an exact committed Go application tree."""
import argparse
import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import urllib.parse

REPOSITORY = "https://github.com/keienWang/bsc-p2p-relay"
GENERATOR = "github.com/CycloneDX/cyclonedx-gomod/cmd/cyclonedx-gomod@v1.9.0"
MODULE = "github.com/keienWang/bsc-p2p-relay"
ADAPTER = "github.com/pion/stun/v2"


def build_environment(environ):
    """Do not let workspace, overlay, modfile or persisted Go settings change source."""
    if any(flag != "-buildvcs=false" for flag in shlex.split(environ.get("GOFLAGS", ""))):
        raise ValueError("unset GOFLAGS: custom build flags are not supported for provenance generation")
    env = dict(environ)
    env.update(GOENV="off", GOWORK="off", GOFLAGS="-buildvcs=false -mod=readonly", GO111MODULE="on")
    # Flags affecting source selection must be explicit future features rather
    # than silently inherited from the caller. Target GOOS/GOARCH/CGO_ENABLED
    # remain supported and are recorded by the generator.
    if env.get("GOEXPERIMENT"):
        raise ValueError("unset GOEXPERIMENT: experimental source selection is not supported")
    return env


def verify_origin(origin):
    if origin.removesuffix(".git").rstrip("/") != REPOSITORY:
        raise ValueError("source origin must identify the public bsc-p2p-relay repository via HTTPS")


def verify_modules(root, adapter, checkout):
    """Only this committed, in-tree adapter may receive project-owned identity."""
    checkout = checkout.resolve()
    if root.get("Path") != MODULE or not root.get("Main") or pathlib.Path(root.get("Dir", "")).resolve() != checkout:
        raise ValueError("selected commit does not contain the expected root module")
    replacement = adapter.get("Replace", {})
    expected = checkout / "compat/stunv2"
    if (adapter.get("Path") != ADAPTER or replacement.get("Path") != "./compat/stunv2"
            or replacement.get("Version") or pathlib.Path(replacement.get("Dir", "")).resolve() != expected
            or pathlib.Path(replacement.get("GoMod", "")).resolve() != expected / "go.mod"):
        raise ValueError("selected commit does not use the committed local STUN adapter")
    if not expected.is_dir() or expected.is_symlink():
        raise ValueError("adapter must be an in-tree directory")


def verify_checkout(checkout, env):
    tracked = subprocess.check_output(["git", "-C", str(checkout), "ls-files", "--stage"], text=True)
    if any(line.startswith(("120000 ", "160000 ")) for line in tracked.splitlines()):
        raise ValueError("symlinks and submodules are not supported in a provenance source tree")
    info = json.loads(subprocess.check_output(["go", "mod", "edit", "-json"], cwd=checkout, env=env, text=True))
    if info.get("Module", {}).get("Path") != MODULE:
        raise ValueError("selected commit has an unexpected root module")
    for replacement in info.get("Replace", []):
        if not replacement["New"].get("Version") and replacement != {"Old": {"Path": ADAPTER}, "New": {"Path": "./compat/stunv2"}}:
            raise ValueError("unexpected local module replacement")
    root = json.loads(subprocess.check_output(["go", "list", "-m", "-json"], cwd=checkout, env=env, text=True))
    adapter = json.loads(subprocess.check_output(["go", "list", "-m", "-json", ADAPTER], cwd=checkout, env=env, text=True))
    verify_modules(root, adapter, checkout)


def validate_output(bom):
    """Fail before writing if identity links dangle or private locations leak."""
    components = [bom["metadata"]["component"]] + bom.get("components", [])
    refs = [component["bom-ref"] for component in components]
    if len(set(refs)) != len(refs):
        raise ValueError("duplicate component reference")
    refs = set(refs)
    dependencies = bom.get("dependencies", [])
    if len({item["ref"] for item in dependencies}) != len(dependencies):
        raise ValueError("duplicate dependency reference")
    for item in dependencies:
        if item["ref"] not in refs or any(ref not in refs for ref in item.get("dependsOn", [])):
            raise ValueError("dangling dependency reference")

    def visit(value):
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            decoded = urllib.parse.unquote(value)
            if re.search(r"(?:^|[=\s])(?:/|[A-Za-z]:[\\/]|\\\\)", decoded) or any(
                    path in decoded for path in ("/workspace/", "/tmp/", "/home/", "/Users/", "file://")):
                raise ValueError("local filesystem location in generated manifest")
            if "://" in decoded:
                url = urllib.parse.urlsplit(decoded)
                if url.scheme not in ("http", "https") or url.username is not None or url.password is not None:
                    raise ValueError("non-public or credential-bearing URL in generated manifest")
                if url.query or url.fragment:
                    raise ValueError("URL query/fragment not supported in generated references")
    visit(bom)


def normalize(bom, commit):
    """Identify project-owned code accurately instead of guessing licenses."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("expected an exact Git commit SHA")
    if bom["metadata"]["component"].get("name") != MODULE:
        raise ValueError("generator returned an unexpected root identity")
    if sum(component.get("name") == ADAPTER for component in bom.get("components", [])) != 1:
        raise ValueError("generator must report exactly one verified STUN adapter")
    bom["metadata"]["component"]["licenses"] = [{"license": {"id": "GPL-3.0-or-later"}}]
    bom["metadata"].setdefault("properties", []).extend([
        {"name": "bsc-relay:source-commit", "value": commit},
        {"name": "bsc-relay:source-repository", "value": REPOSITORY},
        {"name": "bsc-relay:scope", "value": "Go application dependencies; excludes OS/native packages and test-only packages"},
    ])
    for component in bom.get("components", []):
        if component.get("name") != ADAPTER:
            continue
        # This is our local adapter, not Pion's obsolete upstream implementation.
        old_ref = component["bom-ref"]
        new_ref = "pkg:generic/bsc-p2p-relay-stunv2-adapter@" + commit
        component.update({
            "bom-ref": new_ref,
            "name": "bsc-p2p-relay STUN v2 compatibility adapter",
            "version": commit,
            "purl": new_ref,
            "description": "Project-owned adapter delegating BSC's required STUN API to maintained STUN v3; not upstream STUN v2 code.",
            "licenses": [{"license": {"id": "GPL-3.0-or-later"}}],
            "externalReferences": [{"type": "vcs", "url": f"{REPOSITORY}/tree/{commit}/compat/stunv2"}],
        })
        component.setdefault("properties", []).append({"name": "bsc-relay:go-import-path", "value": "github.com/pion/stun/v2"})
        for dependency in bom.get("dependencies", []):
            if dependency.get("ref") == old_ref:
                dependency["ref"] = new_ref
            dependency["dependsOn"] = [new_ref if ref == old_ref else ref for ref in dependency.get("dependsOn", [])]
    return bom


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=".", help="local Git checkout containing the committed source")
    parser.add_argument("--commit", default="HEAD", help="exact revision to describe (default HEAD)")
    parser.add_argument("--output", default="docs/SBOM.cdx.json")
    args = parser.parse_args()
    source = pathlib.Path(args.source).resolve()
    output = pathlib.Path(args.output).resolve()
    env = build_environment(os.environ)
    verify_origin(subprocess.check_output(["git", "-C", str(source), "remote", "get-url", "origin"], text=True).strip())
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "--verify", args.commit + "^{commit}"], text=True).strip()
    # The tool's Git library does not reliably read linked worktrees. Materialize
    # precisely the selected commit in a disposable ordinary clone, never edits.
    with tempfile.TemporaryDirectory(prefix="bsc-relay-sbom-") as temp:
        checkout = pathlib.Path(temp) / "source"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(source), str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", "--detach", commit], check=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin", REPOSITORY], check=True)
        verify_checkout(checkout, env)
        raw = pathlib.Path(temp) / "raw.json"
        # Build the generator for the host, then let it inspect the requested
        # target. Running `go run` with GOARCH=arm64 on amd64 would instead try
        # to execute a cross-compiled generator.
        host = json.loads(subprocess.check_output(["go", "env", "-json", "GOHOSTOS", "GOHOSTARCH"], env=env, text=True))
        tool_env = dict(env, GOOS=host["GOHOSTOS"], GOARCH=host["GOHOSTARCH"], CGO_ENABLED="0", GOBIN=str(pathlib.Path(temp) / "bin"))
        for option in ("GOAMD64", "GOARM", "GOARM64", "GO386", "GOMIPS", "GOMIPS64", "GOPPC64", "GORISCV64", "GOWASM"):
            tool_env.pop(option, None)
        subprocess.run(["go", "install", GENERATOR], cwd=temp, env=tool_env, check=True)
        executable = pathlib.Path(tool_env["GOBIN"]) / ("cyclonedx-gomod.exe" if host["GOHOSTOS"] == "windows" else "cyclonedx-gomod")
        subprocess.run([str(executable), "app", "-json", "-std", "-main", "cmd/bsc-relay", "-output", str(raw), "."], cwd=checkout, env=env, check=True)
        if subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"], text=True).strip():
            raise ValueError("generator changed the selected source tree")
        bom = normalize(json.loads(raw.read_text()), commit)
        validate_output(bom)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bom, indent=2) + "\n")
    print(f"Generated CycloneDX {bom['specVersion']} for source {commit}: {output}")


if __name__ == "__main__":
    main()
