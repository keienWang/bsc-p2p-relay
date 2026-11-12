#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private comparison harness. No downloads, DB access, or workflow changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

import scanner_synthetic_ci as base

PACKAGE = "golang.org/x/vuln/internal/vulncheck"
CANCEL = "TestCallgraphCancellationPropagates"
TRACE = "TestTestOnlySymbolTraceControl"
NAMES = (CANCEL, TRACE, TRACE + "/ordinary", TRACE + "/tests")
TEST_FILES = (
    "internal/vulncheck/callgraph_error_test.go",
    "internal/vulncheck/test_only_symbol_trace_test.go",
)
PATTERN = "^(" + CANCEL + "|" + TRACE + ")$"
TOTAL_SECONDS = 480
TOTAL_BYTES = 32 * 1024 * 1024
COMMAND_BYTES = 16 * 1024 * 1024
require = base.require


def classify(output, code, red):
    """Only the two specific assertion failures constitute expected red."""
    require(code == (1 if red else 0), "unexpected test-process exit")
    require(not any(s in output for s in (
        "panic:", "fatal error:", "test timed out", "WARNING: DATA RACE",
    )), "panic, timeout or race is not expected red")
    runs, ends, logs = set(), {}, {n: [] for n in NAMES}
    package_start, package_end = 0, []
    events = output.splitlines()
    require(bool(events), "empty event stream")
    for line in events:
        event = json.loads(line)
        require(isinstance(event, dict), "invalid event")
        require(event.get("Package") == PACKAGE, "unexpected package")
        action, name = event.get("Action"), event.get("Test")
        require(action in {"start", "run", "output", "pass", "fail"}, "unexpected action")
        require(not package_end, "event after package terminal")
        if name is not None:
            require(name in NAMES, "unselected test event")
            require(package_start == 1, "test before package start")
            if action == "run":
                require(name not in runs, "duplicate test run")
                if "/" in name:
                    require(TRACE in runs and TRACE not in ends, "subtest outside active parent")
                runs.add(name)
            elif action == "output":
                require(name in runs and name not in ends, "output outside active test")
                require(isinstance(event.get("Output"), str), "invalid output")
                logs[name].append(event["Output"])
            else:
                require(action in {"pass", "fail"} and name in runs and name not in ends,
                        "invalid test terminal")
                ends[name] = action
        elif action == "start":
            require(package_start == 0, "duplicate package start")
            package_start = 1
        elif action in {"pass", "fail"}:
            require(package_start == 1 and set(ends) == set(NAMES), "incomplete tests")
            package_end.append(action)
        else:
            require(action == "output" and isinstance(event.get("Output"), str),
                    "invalid package event")
    require(runs == set(NAMES) and set(ends) == set(NAMES), "missing exact tests")
    expected = {n: "pass" for n in NAMES}
    if red:
        expected.update({CANCEL: "fail", TRACE: "fail", TRACE + "/tests": "fail"})
    require(ends == expected and package_end == ["fail" if red else "pass"],
            "unexpected test outcomes")
    markers = {
        CANCEL: ("callgraph_error_test.go",
                 "source returned result=<nil> error=<nil>; want nil, context.Canceled"),
        TRACE + "/tests": ("test_only_symbol_trace_test.go",
                          "no ordered TestOnlyTarget -> wrapper -> Target witness"),
    }
    # An assertion line is identified by its Go source location. Do not accept
    # arbitrary t.Fatal/log output alongside a matching marker.
    assertions = {}
    for name, chunks in logs.items():
        lines = "".join(chunks).splitlines()
        located = [s.strip() for s in lines if re.match(r"^\s*\S+\.go:\d+: ", s)]
        assertions[name] = located
        if red and name in markers:
            filename, message = markers[name]
            require(len(located) == 1 and re.fullmatch(
                re.escape(filename) + r":\d+: " + re.escape(message), located[0]),
                "missing/ambiguous exact failure assertion")
        else:
            require(not located, "unexpected located test message")
    return {"expected_red": red, "outcomes": ends, "assertions": assertions}


class Budget:
    def __init__(self, seconds=TOTAL_SECONDS, limit=TOTAL_BYTES):
        self.deadline = time.monotonic() + seconds
        self.remaining = limit


def terminate_group(proc):
    # Always send both signals: the leader may have exited while a descendant
    # still holds the pipe or ignores TERM. Only the direct child is reaped
    # here; the outer PID namespace owns remaining descendant lifecycle.
    denied = False
    for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            denied = True
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    return denied


def capture(command, cwd, env, attempt, label, budget, timeout):
    """Bound time/output; preserve prefix and terminal status even on failure."""
    log = attempt / (label + ".log")
    require(not log.exists(), "refusing log overwrite")
    allowance = min(COMMAND_BYTES, budget.remaining)
    deadline = min(budget.deadline, time.monotonic() + timeout)
    require(allowance > 0 and deadline > time.monotonic(), "global budget exhausted")
    data = bytearray()
    reason = None
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            ready = selector.select(min(0.1, max(0, deadline-time.monotonic())))
            for key, _ in ready:
                part = os.read(key.fileobj.fileno(), 65536)
                if not part:
                    selector.unregister(key.fileobj)
                    continue
                room = allowance-len(data)
                data.extend(part[:room])
                if len(part) > room:
                    reason = "output_limit"
                    break
            if reason:
                break
        if reason is None:
            try:
                proc.wait(timeout=max(0.001, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
    finally:
        selector.close()
        if reason is not None or proc.poll() is None:
            if terminate_group(proc):
                reason = reason or "termination_permission_error"
        proc.stdout.close()
        budget.remaining -= len(data)
        log.write_bytes(data)
        status = {"command": command, "returncode": proc.returncode, "reason": reason,
                  "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        (attempt / (label + ".json")).write_text(json.dumps(status, indent=2)+"\n")
    require(proc.poll() is not None, "process could not be reaped within kill budget")
    require(reason is None, "command exceeded " + str(reason))
    return proc.returncode, data.decode("utf-8", errors="strict")


def checked(command, cwd, env, attempt, label, budget, timeout=60):
    code, output = capture(command, cwd, env, attempt, label, budget, timeout)
    require(code == 0, "command failed: " + label)
    return output


def binary_hash(binary):
    require(binary.is_file() and not binary.is_symlink(), "missing/non-regular test binary")
    with binary.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inner(source, prepared, attempt):
    receipt = base.read_receipt(source, prepared)
    env = dict(os.environ)
    require(all(env.get(k) == v for k, v in {
        "GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local",
        "GOENV": "off", "GOWORK": "off", "GOTELEMETRY": "off",
    }.items()), "offline flags missing")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    status = {k: v.strip() for k, v in status.items()}
    budget = Budget()
    def cmd(args, label, cwd=source, timeout=60):
        return checked(args, cwd, env, attempt, label, budget, timeout)
    def ip(args, label):
        return json.loads(cmd(["ip", "-j", *args], label))
    base.verify_isolation(status, os.readlink("/proc/self/ns/net"), env.get("SCANNER_HOST_NETNS"),
                          ip(["link"], "links"), ip(["-4", "route", "show", "table", "all"], "routes4"),
                          ip(["-6", "route", "show", "table", "all"], "routes6"), os.geteuid(), os.getgroups())
    require(os.geteuid() == receipt["uid"] and os.getegid() == receipt["gid"], "identity mismatch")
    (attempt/"isolation.json").write_text(json.dumps({
        "uid": os.geteuid(), "gid": os.getegid(), "groups": os.getgroups(),
        "network_namespace": os.readlink("/proc/self/ns/net"),
        "host_network_namespace": env.get("SCANNER_HOST_NETNS"),
        "status": {k: status[k] for k in ("NoNewPrivs", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")},
        "verified_down_loopback_and_empty_routes": True,
    }, indent=2)+"\n")
    require(cmd([receipt["go"], "env", "GOVERSION"], "go-version").strip() == base.GO_VERSION, "Go mismatch")
    # The donor's tracked tree must still be the fixed official commit; only
    # the already allowlisted patch changes may be present.
    require(cmd(["git", "rev-parse", "HEAD"], "source-head").strip() == base.UPSTREAM, "source HEAD mismatch")
    tracked = cmd(["git", "diff", "--no-ext-diff", "--name-only", "HEAD"], "source-tracked").splitlines()
    added = cmd(["git", "ls-files", "--others"], "source-added").splitlines()
    require(set(tracked + added) == set(base.CHANGED), "patched source scope mismatch")
    baseline = attempt / "baseline"
    # Local clone only, no remote fetch; no hardlinks or reference alternates.
    cmd(["git", "clone", "--no-hardlinks", "--", str(source), str(baseline)], "clone")
    require(cmd(["git", "rev-parse", "HEAD"], "baseline-head", baseline).strip() == base.UPSTREAM, "baseline HEAD mismatch")
    require(not cmd(["git", "status", "--porcelain"], "baseline-clean", baseline).strip(), "baseline not pristine")
    test_hashes = base.hashes(source, TEST_FILES)
    for relative in TEST_FILES:
        dest = baseline / relative
        require(not dest.exists(), "baseline already contains injected test")
        dest.write_bytes(base.file_bytes(source, relative))
    require(base.hashes(baseline, TEST_FILES) == test_hashes, "different tests between trees")
    results, binaries = {}, {}
    for name, tree in (("baseline", baseline), ("patched", source)):
        binary = attempt / (name + ".test")
        cmd([receipt["go"], "test", "-mod=readonly", "-c", "-race", "-o", str(binary),
             "./internal/vulncheck"], name + "-compile", tree, 240)
        binaries[name] = binary_hash(binary)
    # Neither side runs unless BOTH exact package test binaries compiled.
    for name, tree in (("baseline", baseline), ("patched", source)):
        command = [receipt["go"], "tool", "test2json", "-t", "-p", PACKAGE,
                   str(attempt/(name+".test")), "-test.v=test2json",
                   "-test.count=1", "-test.timeout=150s", "-test.run="+PATTERN]
        code, output = capture(command, tree, env, attempt, name+"-tests", budget, 165)
        results[name] = classify(output, code, red=(name == "baseline"))
    after_binaries = {name: binary_hash(attempt/(name+".test")) for name in binaries}
    require(after_binaries == binaries, "test binary identity changed after execution")
    for name, tree in (("baseline", baseline), ("patched", source)):
        require(cmd(["git", "rev-parse", "HEAD"], name+"-final-head", tree).strip() == base.UPSTREAM,
                "final upstream HEAD mismatch: " + name)
    require(base.hashes(source, base.CHANGED) == receipt["executed_source"], "patched files changed")
    require(base.hashes(baseline, TEST_FILES) == test_hashes, "baseline test files changed")
    final_tracked = cmd(["git", "diff", "--no-ext-diff", "--name-only", "HEAD"], "patched-final-tracked").splitlines()
    final_added = cmd(["git", "ls-files", "--others"], "patched-final-added").splitlines()
    require(set(final_tracked + final_added) == set(base.CHANGED), "patched final scope changed")
    require(not cmd(["git", "diff", "--exit-code", "HEAD"], "baseline-final", baseline).strip(), "baseline changed")
    require(set(cmd(["git", "ls-files", "--others"], "baseline-added-final", baseline).splitlines()) == set(TEST_FILES),
            "baseline added extra files")
    result = {"passed": True, "upstream": base.UPSTREAM, "go": base.GO_VERSION,
              "test_hashes": test_hashes, "patched_source": receipt["executed_source"],
              "compiled_package_binaries": binaries, "post_execution_binaries": after_binaries, "comparison": results,
              "network_namespace_replaced": True}
    (attempt/"result.json").write_text(json.dumps(result, indent=2)+"\n")


EXPORT_LIMIT = 32 * 1024 * 1024
# Named artifacts only. Never walk or export the cloned upstream source tree.
EXPORT_FILES = (
    "isolation.json", "result.json",
    "baseline-compile.json", "patched-compile.json",
    "baseline-tests.json", "patched-tests.json",
    "baseline-tests.log", "patched-tests.log",
    "baseline-compile.log", "patched-compile.log",
    "links.log", "routes4.log", "routes6.log", "go-version.log",
    "outer.json", "outer.log",
)
REQUIRED_EXPORT = set(EXPORT_FILES[:8])


def export_evidence(attempt, comparison_passed, sink=None, limit=EXPORT_LIMIT):
    """Exact UTF-8 content in JSON records; truncation is explicit, never green."""
    if sink is None:
        sink = sys.stdout
    used, incomplete = 0, []
    for name in EXPORT_FILES:
        path = attempt/name
        if not path.exists():
            if comparison_passed and name in REQUIRED_EXPORT:
                incomplete.append(name)
            continue
        require(path.is_file() and not path.is_symlink(), "unsafe evidence file: "+name)
        require(path.stat().st_size <= COMMAND_BYTES, "oversized evidence file: "+name)
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict")
        digest = hashlib.sha256(raw).hexdigest()
        def encode(n):
            prefix = text[:n]
            return json.dumps({"evidence_file": name, "original_bytes": len(raw),
                               "sha256": digest, "complete": n == len(text),
                               "text": prefix}, ensure_ascii=True)+"\n"
        remaining = max(0, limit-2048-used)  # reserve final outer receipt and bounded error line
        whole = encode(len(text))
        if len(whole.encode()) <= remaining:
            record = whole
        else:
            incomplete.append(name)
            low, high = 0, len(text)
            if len(encode(0).encode()) > remaining:
                continue
            while low < high:
                mid = (low+high+1)//2
                if len(encode(mid).encode()) <= remaining:
                    low = mid
                else:
                    high = mid-1
            record = encode(low)
        sink.write(record)
        sink.flush()
        used += len(record.encode())
    require(not comparison_passed or not incomplete, "successful evidence missing or truncated: "+",".join(incomplete))
    return {"exported_bytes": used, "incomplete_files": incomplete}


def execute(source, prepared, attempt):
    require(not attempt.exists(), "fresh attempt directory required")
    receipt = base.read_receipt(source, prepared)
    require(os.getuid() == receipt["uid"] and os.getgid() == receipt["gid"], "caller mismatch")
    attempt.mkdir(mode=0o700)
    (attempt/"started.json").write_text(json.dumps({"source": str(source), "prepared": str(prepared)})+"\n")
    command = base.isolated_command(source, prepared, receipt, os.readlink("/proc/self/ns/net"))
    require(command[5] == "360s", "unexpected isolation command shape")
    command[5] = str(TOTAL_SECONDS)+"s"
    command[-4:] = [str(Path(__file__).resolve()), "inner", str(source), str(prepared), str(attempt)]
    failure = None
    try:
        checked(command, source, dict(os.environ), attempt, "outer", Budget(510), 500)
        require(json.loads((attempt/"result.json").read_text()).get("passed") is True, "missing inner success")
    except BaseException as exc:
        failure = exc
    comparison_passed = failure is None
    exported = None
    try:
        exported = export_evidence(attempt, comparison_passed)
    except BaseException as exc:
        failure = failure or exc
    final = {"passed": failure is None, "comparison_passed": comparison_passed,
             "export": exported, "error": None if failure is None else str(failure)[:300]}
    (attempt/"outer-result.json").write_text(json.dumps(final)+"\n")
    # This final receipt distinguishes an inner comparison success from an
    # export failure. The exporter reserves 2048 bytes for this record and any bounded error line.
    record = json.dumps({"outer_result": final})+"\n"
    require(len(record.encode()) <= 1024, "outer receipt overflow")
    print(record, end="", flush=True)
    if failure is not None:
        raise failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("run", "inner"))
    for name in ("source", "prepared", "attempt"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    paths = [args.source, args.prepared, args.attempt]
    require(not any(p.is_symlink() for p in paths), "symlink roots forbidden")
    source, prepared, attempt = [p.resolve() for p in paths]
    require(source.is_dir() and prepared.is_dir(), "missing prepared inputs")
    require(not attempt.is_relative_to(source) and not source.is_relative_to(attempt)
            and not attempt.is_relative_to(prepared) and not prepared.is_relative_to(attempt),
            "overlapping attempt/input roots")
    {"run": execute, "inner": inner}[args.phase](source, prepared, attempt)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print("FAIL: "+str(exc)[:300], file=sys.stderr)
        sys.exit(1)
