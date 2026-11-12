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


JSON_FIELDS_BASE = "Name,ImportPath,Error,Dir,GoFiles,IgnoredGoFiles,IgnoredOtherFiles,CFiles,CgoFiles,CXXFiles,MFiles,HFiles,FFiles,SFiles,SwigFiles,SwigCXXFiles,SysoFiles,"
JSON_FIELDS_TEST = JSON_FIELDS_BASE + "TestGoFiles,XTestGoFiles,CompiledGoFiles,Export,DepOnly,Imports,ImportMap,TestImports,XTestImports,Module"
JSON_FIELDS_ORDINARY = JSON_FIELDS_BASE + "CompiledGoFiles,Export,DepOnly,Imports,ImportMap,Module"


def framework_diagnostic(line, name):
    # Observed call-site/command shapes under pinned x/tools v0.52.0.
    # Environment text is inspected only for shape, never returned/exported.
    if name not in {CANCEL, TRACE+"/ordinary", TRACE+"/tests"}:
        return False
    m = re.fullmatch(
        r"\s*invoke\.go:205: (?:starting|[0-9]+(?:\.[0-9]+)?(?:ns|µs|ms|s) for)"
        r" GOROOT=[^\r\n]* GOPATH=[^\r\n]+ GO111MODULE=(?:on|off)"
        r" GOPROXY=file://[^\r\n]+ go list (.+)", line)
    if not m:
        return False
    args = m.group(1)
    if args in {'-f "{{context.GOARCH}} {{context.Compiler}}" -- unsafe',
                '-e -f {{context.ReleaseTags}} -- unsafe'}:
        return True
    tests = name != TRACE+"/ordinary"
    fields = JSON_FIELDS_TEST if tests else JSON_FIELDS_ORDINARY
    prefix = ("-e -json="+fields+" -compiled=true -test="+str(tests).lower()
              +" -export=false -deps=true -find=false -buildvcs=false -pgo=off -- ")
    if not args.startswith(prefix):
        return False
    target = args[len(prefix):]
    suffix = "/entry/x" if name == CANCEL else "/entry/p"
    return target.startswith("/") and target.endswith(suffix) and not any(c.isspace() for c in target)


def classify(output, code, red):
    """Only the two specific assertion failures constitute expected red."""
    require(code == (1 if red else 0), "unexpected test-process exit")
    require(not any(s in output for s in (
        "panic:", "fatal error:", "test timed out", "WARNING: DATA RACE",
    )), "panic, timeout or race is not expected red")
    runs, ends, logs = set(), {}, {n: [] for n in NAMES}
    package_start, package_end = 0, []
    semantic = []
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
        if action != "output":
            semantic.append({"action": action, "test": name})
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
    assertions, diagnostics = {}, {}
    for name, chunks in logs.items():
        lines = "".join(chunks).splitlines()
        framework = [line for line in lines if framework_diagnostic(line, name)]
        diagnostics[name] = len(framework)
        located = [line.strip() for line in lines if re.match(r"^\s*\S+\.go:\d+: ", line) and not framework_diagnostic(line, name)]
        assertions[name] = []
        if red and name in markers:
            filename, message = markers[name]
            require(len(located) == 1 and re.fullmatch(
                re.escape(filename) + r":\d+: " + re.escape(message), located[0]),
                "missing/ambiguous exact failure assertion")
            assertions[name] = ["callgraph_error_not_propagated" if name == CANCEL else "test_only_trace_missing"]
        else:
            require(not located, "unexpected located test message")
    return {"expected_red": red, "outcomes": ends, "assertion_codes": assertions,
            "framework_diagnostic_counts": diagnostics, "semantic_events": semantic}


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
    (attempt/"identity-before.json").write_text(json.dumps({
        "upstream": base.UPSTREAM, "go": base.GO_VERSION,
        "test_hashes": test_hashes, "patched_source": receipt["executed_source"],
        "compiled_package_binaries": binaries,
    }, indent=2)+"\n")
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
SAFE_FORMAT = "allowlisted_semantics_not_raw_test2json"


def read_local(attempt, name):
    path = attempt/name
    if not path.exists():
        return None
    require(path.is_file() and not path.is_symlink(), "unsafe local evidence")
    require(path.stat().st_size <= COMMAND_BYTES, "oversized local evidence")
    return path.read_bytes()


def digest_map(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), "identity keys mismatch")
    require(all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in value.values()),
            "invalid identity hash")
    return {k: value[k] for k in sorted(keys)}


def safe_identity(value, after=False):
    require(value.get("upstream") == base.UPSTREAM and value.get("go") == base.GO_VERSION,
            "identity version mismatch")
    result = {"upstream": base.UPSTREAM, "go": base.GO_VERSION,
              "test_hashes": digest_map(value["test_hashes"], TEST_FILES),
              "patched_source": digest_map(value["patched_source"], base.CHANGED),
              "binaries": digest_map(value["compiled_package_binaries"], ("baseline", "patched"))}
    if after:
        post = digest_map(value["post_execution_binaries"], ("baseline", "patched"))
        require(post == result["binaries"], "post-execution binary mismatch")
        result["post_execution_binaries"] = post
    return result


def safe_status(attempt, label):
    raw = read_local(attempt, label+".json")
    if raw is None:
        return None
    value = json.loads(raw)
    code, reason = value["returncode"], value["reason"]
    require(code is None or type(code) is int and -255 <= code <= 255, "invalid exit code")
    require(reason in {None, "timeout", "output_limit", "termination_permission_error"}, "invalid process reason")
    result = {"returncode": code, "reason": reason}
    log = read_local(attempt, label+".log")
    require(log is not None, "status without local log")
    require(type(value.get("bytes")) is int and value["bytes"] == len(log)
            and value.get("sha256") == hashlib.sha256(log).hexdigest(), "local log/status digest mismatch")
    if log is not None:
        # Digest of the runtime-local input only. No claim that a separately
        # masked hosted transcript preserves these bytes.
        result.update(raw_local_bytes=len(log), raw_local_sha256=hashlib.sha256(log).hexdigest())
    return result


def semantic_evidence(attempt, comparison_passed):
    payload = {"schema_version": 2, "representation": SAFE_FORMAT,
               "upstream": base.UPSTREAM, "go": base.GO_VERSION,
               "compilation": {}, "tests": {}, "isolation": None,
               "identity_before": None, "identity_after": None}
    raw = read_local(attempt, "isolation.json")
    if raw is not None:
        v = json.loads(raw)
        caps = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        require(type(v["uid"]) is int and v["uid"] > 0 and v["groups"] == [], "invalid isolation")
        require(v["status"]["NoNewPrivs"] == "1" and all(int(v["status"][k], 16) == 0 for k in caps),
                "invalid capability evidence")
        require(v["network_namespace"] != v["host_network_namespace"]
                and v["verified_down_loopback_and_empty_routes"] is True, "invalid network evidence")
        payload["isolation"] = {"nonroot": True, "supplementary_groups": 0, "capabilities": "zero",
                                "no_new_privileges": True, "network_namespace_replaced": True,
                                "down_loopback_only": True, "routes_empty": True}
    for label, after in (("identity-before.json", False), ("result.json", True)):
        raw = read_local(attempt, label)
        if raw is not None:
            value = json.loads(raw)
            payload["identity_after" if after else "identity_before"] = safe_identity(value, after)
    for side in ("baseline", "patched"):
        status = safe_status(attempt, side+"-compile")
        payload["compilation"][side] = status
        status = safe_status(attempt, side+"-tests")
        payload["tests"][side] = {"execution": status, "classification": None}
        raw = read_local(attempt, side+"-tests.log")
        if raw is not None and status is not None:
            try:
                outcome = classify(raw.decode("utf-8", errors="strict"), status["returncode"], side == "baseline")
                payload["tests"][side]["classification"] = {"valid": True, **outcome}
            except (ValueError, KeyError, TypeError, UnicodeError):
                payload["tests"][side]["classification"] = {"valid": False, "error_code": "test_evidence_rejected"}
    if comparison_passed:
        require(payload["isolation"] is not None and payload["identity_before"] is not None
                and payload["identity_after"] is not None, "missing success identity evidence")
        for side in ("baseline", "patched"):
            c, t = payload["compilation"][side], payload["tests"][side]
            require(c is not None and c["returncode"] == 0 and c["reason"] is None, "compile not successful")
            require(t["execution"] is not None and t["execution"]["reason"] is None
                    and t["classification"] is not None and t["classification"]["valid"], "tests not verified")
        before, after = payload["identity_before"], payload["identity_after"]
        require(all(before[k] == after[k] for k in before), "before/after identity mismatch")
    return payload


def export_evidence(attempt, comparison_passed, sink=None, limit=EXPORT_LIMIT):
    if sink is None:
        sink = sys.stdout
    payload = semantic_evidence(attempt, comparison_passed)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    # This hash binds the sanitized canonical payload, NOT any raw log.
    envelope = {"evidence_format": SAFE_FORMAT, "sanitized_bytes": len(data),
                "sanitized_sha256": hashlib.sha256(data).hexdigest(), "payload": payload}
    record = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)+"\n"
    require(len(record.encode()) <= limit-2048, "sanitized evidence output limit")
    sink.write(record)
    sink.flush()
    return {"exported_bytes": len(record.encode()), "sanitized_sha256": envelope["sanitized_sha256"],
            "evidence_format": SAFE_FORMAT}


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
             "export": exported, "error_code": None if failure is None else "comparison_or_export_failed"}
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
    except Exception:
        print("FAIL: scanner comparison failed; see sanitized receipt", file=sys.stderr)
        sys.exit(1)
