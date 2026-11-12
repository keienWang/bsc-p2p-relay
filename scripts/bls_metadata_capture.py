#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded private stdout/stderr capture for strict metadata, not test streams."""
import json
import os
import re
import selectors
import subprocess
import time

from bls_stage0_inventory import require, sha
import scanner_redgreen_ci as process

MODE = "separate_stdout_stderr"
REASONS = {None, "timeout", "output_limit", "invalid_utf8", "capture_error",
           "termination_permission_error", "unreaped_process"}


def capture(command, cwd, env, attempt, label, budget, timeout):
    require(re.fullmatch(r"[a-z0-9-]+", label), "metadata_label")
    paths = {name: attempt / (label + "." + name + ".log") for name in ("stdout", "stderr")}
    status_path = attempt / (label + ".json")
    require(not any(p.exists() or p.is_symlink() for p in [*paths.values(), status_path]), "metadata_log_exists")
    allowance = min(process.COMMAND_BYTES, budget.remaining)
    deadline = min(budget.deadline, time.monotonic() + timeout)
    require(allowance > 0 and time.monotonic() < deadline, "metadata_budget_exhausted")
    data = {name: bytearray() for name in paths}
    total, reason, decoded = 0, None, None
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    selector = None
    try:
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map() and reason is None:
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                part = os.read(key.fileobj.fileno(), 65536)
                if not part:
                    selector.unregister(key.fileobj)
                    continue
                room = allowance - total
                saved = part[:room]
                data[key.data].extend(saved)
                total += len(saved)
                if len(part) > room:
                    reason = "output_limit"
                    break
        if reason is None:
            try:
                proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
        if reason is None:
            try:
                decoded = data["stdout"].decode("utf-8", errors="strict")
                data["stderr"].decode("utf-8", errors="strict")
            except UnicodeError:
                reason = "invalid_utf8"
    except BaseException:
        # Python-delivered interruption is failure, including when an exited
        # leader leaves descendants holding pipes. Uncatchable signals remain
        # the disposable runner / outer watchdog's cleanup boundary.
        reason = reason or "capture_error"
    finally:
        if reason is not None or proc.poll() is None:
            if process.terminate_group(proc):
                reason = reason or "termination_permission_error"
        if proc.poll() is None:
            reason = "unreaped_process"
        if selector is not None:
            try:
                selector.close()
            except Exception:
                reason = reason or "capture_error"
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except Exception:
                reason = reason or "capture_error"
        budget.remaining -= total
        status = {"returncode": proc.returncode, "reason": reason, "capture_mode": MODE}
        for name, path in paths.items():
            path.write_bytes(data[name])
            status[name + "_private"] = {"bytes": len(data[name]), "sha256": sha(data[name])}
        status_path.write_text(json.dumps(status, sort_keys=True) + "\n")
    require(reason is None, "metadata_capture_failed")
    return proc.returncode, decoded


def read_status(attempt, label):
    """Rebind each saved private prefix to its receipt, without returning text."""
    from bls_stage0_inventory import read_file
    status = json.loads(read_file(attempt, label + ".json"))
    require(isinstance(status, dict) and set(status) == {
        "returncode", "reason", "capture_mode", "stdout_private", "stderr_private"}, "metadata_status_shape")
    require(status["capture_mode"] == MODE and status["reason"] in REASONS, "metadata_status_shape")
    require(status["returncode"] is None or type(status["returncode"]) is int and -255 <= status["returncode"] <= 255,
            "metadata_status_code")
    total = 0
    for name in ("stdout", "stderr"):
        item = status[name + "_private"]
        require(isinstance(item, dict) and set(item) == {"bytes", "sha256"}, "metadata_stream_shape")
        raw = read_file(attempt, label + "." + name + ".log")
        require(type(item["bytes"]) is int and item["bytes"] == len(raw) and item["sha256"] == sha(raw), "metadata_stream_identity")
        total += len(raw)
    require(total <= process.COMMAND_BYTES, "metadata_stream_limit")
    return status
