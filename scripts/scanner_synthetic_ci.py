#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental, explicit-test-only scanner harness; never invokes govulncheck.

prepare may fetch ordinary pinned Go modules, but never runs candidate code.
run enters a disposable Linux network/PID namespace before compilation/tests.
There is deliberately no online fallback or broad upstream test selection.
"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

UPSTREAM = "e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5"
GO_VERSION = "go1.26.11"
BUNDLE = Path(__file__).resolve().parents[1] / "research/scanner-synthetic"
PATCHES = (
    "0001-propagate-callgraph-error.patch",
    "0002-preserve-package-instances-and-roots.patch",
    "0003-check-source-ownership-and-module-identities.patch",
    "0004-test-only-symbol-control-and-derivative-identity.patch",
    "0005-pin-generic-fixture-language.patch",
)
CHANGED = (
    "internal/vulncheck/source.go", "internal/vulncheck/packages.go",
    "internal/vulncheck/callgraph_error_test.go",
    "internal/vulncheck/package_instances_test.go",
    "internal/vulncheck/source_identity.go", "internal/vulncheck/source_identity_test.go",
    "internal/vulncheck/test_only_symbol_trace_test.go", "internal/scan/run.go",
    "internal/scan/derivative_identity.go", "internal/scan/derivative_identity_test.go",
)
TESTS = {
    "./internal/vulncheck": (
        "TestCallgraphCancellationPropagates", "TestPackageInstancesRetainTestOnlyImports",
        "TestLoadedTestRootsKeepIdentity", "TestSourceTypesAndImportedFindingsKeepVariant",
        "TestSourceIdentityConflictsFailClosed", "TestSourceSamePathReplacementAndEffectiveConflict",
        "TestSourceGenericOriginIdentity", "TestBinaryPathContractWithoutSourceIndex",
        "TestTestOnlySymbolTraceControl",
    ),
    "./internal/scan": ("TestExperimentalScannerIdentity", "TestExperimentalPreparedScannerIdentity"),
}
MAX_OUTPUT = 16 * 1024 * 1024


def require(ok, message):
    if not ok:
        raise ValueError(message)


def file_bytes(root, relative):
    p = Path(relative)
    require(not p.is_absolute() and ".." not in p.parts, "unsafe relative path")
    candidate = root / p
    require(candidate.is_file() and not candidate.is_symlink(), "missing/symlink input: " + relative)
    require(candidate.resolve().is_relative_to(root.resolve()), "input escapes root")
    return candidate.read_bytes()


def hashes(root, paths):
    return {p: hashlib.sha256(file_bytes(root, p)).hexdigest() for p in paths}


def run(command, *, cwd, env):
    # Fixed-size streaming reads bound log memory. The outer namespace watchdog
    # owns the whole test phase; exceeding this output cap is a hard failure.
    with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT) as proc:
        output = bytearray()
        while True:
            part = proc.stdout.read(65536)
            if not part:
                break
            if len(output) + len(part) > MAX_OUTPUT:
                proc.kill()
                proc.wait()
                raise ValueError("command output limit exceeded")
            output.extend(part)
        code = proc.wait()
    text = output.decode("utf-8", errors="strict")
    print(text, end="", flush=True)
    require(code == 0, "command failed with exit " + str(code) + ": " + command[0])
    return text


def clean_env(go, state):
    return {
        "PATH": str(Path(go).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
        "GOCACHE": str(state / "build-cache"), "GOMODCACHE": str(state / "module-cache"),
        "GOTOOLCHAIN": "local", "GOENV": "off", "GOTELEMETRY": "off", "GOWORK": "off",
        "GOFLAGS": "-buildvcs=false -p=1", "GOMAXPROCS": "2",
        "CGO_ENABLED": "1", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        # No real vulnerability DB, even if an accidental CLI is added later.
        "GOVULNDB": "file://" + str(state / "NO_VULNERABILITY_DATABASE"),
    }


def verify_bundle():
    manifest = json.loads(file_bytes(BUNDLE, "MANIFEST.json"))
    require(manifest["upstream_commit"] == UPSTREAM, "bundle revision mismatch")
    expected = {"UPSTREAM.json", "LICENSE", "PATENTS", "LICENSE.CONTRIBUTIONS", "README.md"} | {"patches/" + p for p in PATCHES}
    require({e["path"] for e in manifest["files"]} == expected, "unexpected bundle files")
    require(len(manifest["files"]) == len(expected), "duplicate bundle entries")
    for entry in manifest["files"]:
        data = file_bytes(BUNDLE, entry["path"])
        require(len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"],
                "bundle hash mismatch: " + entry["path"])
    return hashlib.sha256(file_bytes(BUNDLE, "MANIFEST.json")).hexdigest()


def normalize_sources(source, state, go, env):
    # Preserve exact pre-format bytes before the strict, non-simplifying formatter.
    before_bytes = {p: file_bytes(source, p) for p in CHANGED}
    before = {p: hashlib.sha256(b).hexdigest() for p, b in before_bytes.items()}
    for p, data in before_bytes.items():
        saved = state / "before-gofmt" / p
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(data)
    (state / "before-gofmt.json").write_text(json.dumps(before, indent=2) + "\n")
    gofmt = str(Path(go).with_name("gofmt"))
    run([gofmt, "-w", *CHANGED], cwd=source, env=env)
    require(not run([gofmt, "-l", *CHANGED], cwd=source, env=env).strip(), "formatter verification failed")
    after_bytes = {p: file_bytes(source, p) for p in CHANGED}
    diff = b"".join(b"".join(difflib.diff_bytes(
        difflib.unified_diff, before_bytes[p].splitlines(keepends=True),
        after_bytes[p].splitlines(keepends=True),
        fromfile=("before/" + p).encode(), tofile=("after/" + p).encode())) for p in CHANGED)
    (state / "format.diff").write_bytes(diff)
    print(diff.decode("utf-8", errors="strict"), end="", flush=True)
    return before, {p: hashlib.sha256(b).hexdigest() for p, b in after_bytes.items()}


def prepare(source, state):
    require(sys.platform == "linux" and os.geteuid() != 0, "prepare requires an ordinary Linux user")
    require(not state.exists(), "state directory already exists; do not overwrite an earlier attempt")
    go = shutil.which("go")
    require(go is not None, "Go is required")
    go = str(Path(go).resolve())
    manifest_hash = verify_bundle()
    state.mkdir(mode=0o700)
    for name in ("home", "tmp", "build-cache", "module-cache"):
        (state / name).mkdir(mode=0o700)
    env = clean_env(go, state)
    require(run([go, "env", "GOVERSION"], cwd=source, env=env).strip() == GO_VERSION, "wrong Go version")
    require(run(["git", "rev-parse", "HEAD"], cwd=source, env=env).strip() == UPSTREAM, "wrong upstream commit")
    require(not run(["git", "status", "--porcelain"], cwd=source, env=env).strip(), "upstream must be clean")
    refs = json.loads(file_bytes(BUNDLE, "UPSTREAM.json"))
    require(refs["upstream_commit"] == UPSTREAM, "reference revision mismatch")
    for entry in refs["references"]:
        data = file_bytes(source, entry["path"])
        require(len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"],
                "upstream reference mismatch: " + entry["path"])
    # The only online operation: download modules pinned by this unmodified
    # official go.mod/go.sum. This is not a scanner or candidate test execution.
    run([go, "mod", "download"], cwd=source, env=env)
    run([go, "mod", "verify"], cwd=source, env=env)
    run(["git", "diff", "--exit-code", "--", "go.mod", "go.sum"], cwd=source, env=env)
    for patch in PATCHES:
        p = str(BUNDLE / "patches" / patch)
        run(["git", "apply", "--check", p], cwd=source, env=env)
        run(["git", "apply", p], cwd=source, env=env)
    tracked = run(["git", "diff", "--name-only"], cwd=source, env=env).splitlines()
    added = run(["git", "ls-files", "--others"], cwd=source, env=env).splitlines()
    require(set(tracked + added) == set(CHANGED), "unexpected changed-source scope")
    before, after = normalize_sources(source, state, go, env)
    receipt = {"upstream_commit": UPSTREAM, "bundle_manifest_sha256": manifest_hash,
               "source": str(source), "go": go, "go_version": GO_VERSION,
               "uid": os.getuid(), "gid": os.getgid(), "before_gofmt": before,
               "executed_source": after, "prepared": True}
    (state / "prepared.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"before_gofmt": before, "executed_source": after}, sort_keys=True))


def isolated_command(source, state, receipt, host_netns):
    env = clean_env(receipt["go"], state)
    env.update(GOPROXY="off", GOSUMDB="off", SCANNER_HOST_NETNS=host_netns)
    return ["sudo", "-n", "timeout", "--signal=TERM", "--kill-after=10s", "360s",
            "unshare", "--net", "--pid", "--fork", "--mount-proc", "--kill-child=KILL",
            "setpriv", "--reuid=" + str(receipt["uid"]), "--regid=" + str(receipt["gid"]),
            "--clear-groups", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
            "--no-new-privs", "env", "-i", *[k + "=" + v for k, v in sorted(env.items())],
            sys.executable, str(Path(__file__).resolve()), "inner", str(source), str(state)]


def verify_test_events(output, expected):
    passed, finished = set(), False
    for line in output.splitlines():
        event = json.loads(line)
        action, name = event.get("Action"), event.get("Test")
        require(action not in ("fail", "skip"), "failed or skipped selected test")
        if action == "pass" and name:
            passed.add(name)
        if action == "pass" and not name:
            finished = True
    require(finished and set(expected) <= passed, "missing selected test PASS or package completion")
    return sorted(set(expected) & passed)


def verify_isolation(status, current_netns, host_netns, links, routes4, routes6, uid, groups):
    require(uid != 0 and not groups, "test process retained root or supplementary groups")
    require(status.get("NoNewPrivs") == "1", "no-new-privs missing")
    for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        require(field in status and int(status[field], 16) == 0, "capabilities retained: " + field)
    require(host_netns and current_netns != host_netns, "network namespace was not replaced")
    require(len(links) == 1 and links[0].get("ifname") == "lo" and "UP" not in links[0].get("flags", []),
            "unexpected network interface")
    require(not routes4 and not routes6, "network namespace has routes")


def verify_changed_scope(source, env):
    require(run(["git", "rev-parse", "HEAD"], cwd=source, env=env).strip() == UPSTREAM, "upstream HEAD changed")
    tracked = run(["git", "diff", "--no-ext-diff", "--name-only", "HEAD"], cwd=source, env=env).splitlines()
    added = run(["git", "ls-files", "--others"], cwd=source, env=env).splitlines()
    require(set(tracked + added) == set(CHANGED), "source changes outside the patch allowlist")


def read_receipt(source, state):
    receipt = json.loads((state / "prepared.json").read_text())
    require(receipt["prepared"] is True and receipt["source"] == str(source), "preparation mismatch")
    require(receipt["upstream_commit"] == UPSTREAM and receipt["go_version"] == GO_VERSION, "wrong preparation")
    require(receipt["bundle_manifest_sha256"] == verify_bundle(), "bundle changed after preparation")
    require(receipt["executed_source"] == hashes(source, CHANGED), "source changed after formatting")
    require(receipt["uid"] != 0, "root test execution forbidden")
    return receipt


def selected_test_command(go, package, names):
    # Only the outer build is read-only; generated packagestest modules may write
    # their own sums using their local file proxy inside the network namespace.
    pattern = "^(" + "|".join(names) + ")$"
    return [go, "test", "-mod=readonly", "-json", "-race", "-count=1",
            "-timeout=150s", "-run", pattern, package]


def inner(source, state):
    receipt = read_receipt(source, state)
    env = dict(os.environ)
    require(env.get("GOPROXY") == "off" and env.get("GOSUMDB") == "off" and env.get("GOTOOLCHAIN") == "local",
            "offline toolchain flags missing")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    status = {k: v.strip() for k, v in status.items()}
    def ip(*args):
        return json.loads(subprocess.check_output(["ip", "-j", *args], text=True, env=env))
    verify_isolation(status, os.readlink("/proc/self/ns/net"), env.get("SCANNER_HOST_NETNS"),
                     ip("link"), ip("-4", "route", "show", "table", "all"),
                     ip("-6", "route", "show", "table", "all"), os.geteuid(), os.getgroups())
    require(os.geteuid() == receipt["uid"] and os.getegid() == receipt["gid"], "test user mismatch")
    require(run([receipt["go"], "env", "GOVERSION"], cwd=source, env=env).strip() == GO_VERSION, "wrong inner Go")
    results = {}
    for package, names in TESTS.items():
        output = run(selected_test_command(receipt["go"], package, names),
                     cwd=source, env=env)
        results[package] = verify_test_events(output, names)
    require(receipt["executed_source"] == hashes(source, CHANGED), "tests changed source")
    verify_changed_scope(source, env)
    result = {"passed": True, "network_namespace_replaced": True, "capabilities": "zero",
              "selected_test_passes": results, "executed_source": receipt["executed_source"]}
    (state / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


def execute(source, state):
    receipt = read_receipt(source, state)
    verify_changed_scope(source, clean_env(receipt["go"], state))
    require(not (state / "started.json").exists(), "attempt already started; preserve it and prepare a fresh state")
    require(not (state / "result.json").exists(), "result already exists; do not silently repeat an attempt")
    for tool in ("sudo", "timeout", "unshare", "setpriv", "ip"):
        require(shutil.which(tool), "required isolation tool unavailable: " + tool)
    cmd = isolated_command(source, state, receipt, os.readlink("/proc/self/ns/net"))
    with (state / "started.json").open("x") as f:
        json.dump({"upstream_commit": UPSTREAM, "bundle_manifest_sha256": receipt["bundle_manifest_sha256"]}, f)
    try:
        run(cmd, cwd=source, env=dict(os.environ))
        result = json.loads((state / "result.json").read_text())
        require(result.get("passed") is True, "missing successful inner result")
    except BaseException:
        (state / "outer-result.json").write_text('{"passed": false}\n')
        raise
    (state / "outer-result.json").write_text('{"passed": true}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "run", "inner"))
    parser.add_argument("source", type=Path)
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    require(not args.source.is_symlink() and not args.state.is_symlink(), "symlink roots forbidden")
    source, state = args.source.resolve(), args.state.resolve()
    require(source.is_dir() and source != state and not state.is_relative_to(source), "invalid source/state roots")
    {"prepare": prepare, "run": execute, "inner": inner}[args.phase](source, state)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        sys.exit(1)
