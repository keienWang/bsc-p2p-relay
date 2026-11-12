#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict, non-exporting parser for the six-test BLS experiment.

Input is private test2json, never a public transcript. Output contains only
fixed semantic labels. Parsing evidence is not execution or race reproduction.
"""
import json
import math
import re

from bls_stage0_inventory import BLS, GateError, require

PACKAGE = BLS + "/cachepatchtests"
COLD = "TestCacheConcurrentFirstUse"
TESTS = (COLD, "TestCacheConcurrentDistinctAndMixedKeys", "TestCacheCopyIsolation",
         "TestCacheConcurrentReturnedKeyMutation", "TestCacheAggregationKeepsCachedInputs",
         "TestCacheRejectsInvalidEncodings")
CANDIDATE_PATTERN = "^(" + "|".join(TESTS) + ")$"
BASELINE_PATTERN = "^" + COLD + "$"
GORACE = "halt_on_error=0 exitcode=66 atexit_sleep_ms=0 history_size=1"
MAX_EVENTS = 32768
MAX_BYTES = 16 * 1024 * 1024


def target_race(block, source_root):
    """Both accesses must occur in ONE race block with the fixed source chains."""
    lines = block.splitlines()
    require(lines and lines[0] == "WARNING: DATA RACE", "race_block_header")
    starts = []
    for i, line in enumerate(lines):
        match = re.fullmatch(
            r"(Read|Write|Previous read|Previous write) at (0x[0-9a-f]+) by goroutine ([1-9][0-9]*):", line)
        if match:
            starts.append((i, match))
    require(len(starts) == 2, "race_access_count")
    current, previous = starts[0][1], starts[1][1]
    require(not current[1].startswith("Previous ") and previous[1].startswith("Previous "),
            "race_access_order")
    require(current[2] == previous[2] and current[3] != previous[3], "race_access_identity")
    accesses = {}
    for index, (start, match) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        section = lines[start + 1:end]
        # Creation stacks cannot satisfy the required memory-access call chain.
        for i, line in enumerate(section):
            if line.startswith("Goroutine "):
                section = section[:i]
                break
        frames = []
        for i, line in enumerate(section):
            if i + 1 >= len(section):
                break
            location = re.fullmatch(r"\s+(.+\.go):([1-9][0-9]*)(?: \+0x[0-9a-f]+)?", section[i + 1])
            if location:
                frames.append((line.strip(), location.group(1)))
        kind = match[1].split()[-1].lower()
        require(kind not in accesses, "race_access_kinds")
        method = "Get" if kind == "read" else "Add"
        cache = re.compile(re.escape(BLS + "/cache/nonblocking") +
                           r"\.\(\*LRU\[.{1,2048}\]\)\." + method + r"\(\)")
        expected = [
            lambda symbol, path: bool(cache.fullmatch(symbol)) and
                path == source_root + "/cache/nonblocking/lru.go",
            lambda symbol, path: symbol == BLS + "/crypto/bls/blst.publicKeyFromBytes()" and
                path == source_root + "/crypto/bls/blst/public_key.go",
            lambda symbol, path: symbol == BLS + "/crypto/bls/blst.PublicKeyFromBytes()" and
                path == source_root + "/crypto/bls/blst/public_key.go",
        ]
        # The first conflicting frame owns the memory access. Finding the cache
        # deeper in an unrelated access's call stack is not this reproduction.
        require(frames and expected[0](*frames[0]), "race_access_frame_mismatch")
        cursor = 1
        for matches in expected[1:]:
            while cursor < len(frames) and not matches(*frames[cursor]):
                cursor += 1
            require(cursor < len(frames), "race_target_chain_missing")
            cursor += 1
        accesses[kind] = "nonblocking_" + method + "_via_public_key_from_bytes"
    require(set(accesses) == {"read", "write"}, "race_access_kinds")
    return accesses


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_test_json_key")
        result[key] = value
    return result


def invalid_constant(_):
    raise GateError("nonfinite_test_json_number")


def classify(raw, exit_code, baseline, source_root):
    require(type(baseline) is bool, "invalid_baseline_mode")
    require(isinstance(source_root, str) and source_root.startswith("/") and
            not source_root.endswith("/") and not any(p in {".", "..", ""} for p in source_root.split("/")[1:]),
            "invalid_source_root")
    require(isinstance(raw, str), "test_output_type")
    try:
        size = len(raw.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise GateError("test_output_encoding") from None
    require(size <= MAX_BYTES, "test_output_limit")
    require(type(exit_code) is int and exit_code in (0, 1), "test_exit_rejected")
    require(not any(x in raw for x in ("panic:", "fatal error:", "test timed out", "runtime: out of memory")),
            "test_runtime_failure")
    expected = (COLD,) if baseline else TESTS
    events = raw.splitlines()
    require(0 < len(events) <= MAX_EVENTS, "test_event_count")
    started, terminal, active = False, None, None
    runs, outcomes, chunks = set(), {}, []
    owned_chunks = {name: [] for name in expected}
    package_chunks = []
    for line in events:
        try:
            event = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except GateError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise GateError("invalid_test_json") from None
        require(isinstance(event, dict) and set(event) <= {"Time", "Action", "Package", "Test", "Elapsed", "Output"},
                "test_event_shape")
        require(event.get("Package") == PACKAGE and terminal is None, "test_package_or_terminal")
        action, test = event.get("Action"), event.get("Test")
        require(action in {"start", "run", "output", "pass", "fail"}, "test_action_rejected")
        require((action == "output") == ("Output" in event), "test_output_field")
        if "Time" in event:
            require(isinstance(event["Time"], str), "test_time_shape")
        if "Elapsed" in event:
            elapsed = event["Elapsed"]
            require(type(elapsed) in (int, float) and (type(elapsed) is int or math.isfinite(elapsed)) and elapsed >= 0,
                    "test_elapsed_shape")
        if test is not None:
            require(test in expected and started, "unexpected_test")
            if action == "run":
                require(test not in runs and active is None, "duplicate_or_overlapping_test_run")
                runs.add(test)
                active = test
            elif action == "output":
                require(test in runs and test not in outcomes and active == test, "test_output_outside_run")
            else:
                require(action in {"pass", "fail"} and test in runs and test not in outcomes and active == test,
                        "test_terminal_rejected")
                outcomes[test] = action
                active = None
        elif action == "start":
            require(not started, "duplicate_package_start")
            started = True
        elif action in {"pass", "fail"}:
            require(started and active is None and set(outcomes) == set(expected), "incomplete_tests")
            terminal = action
        else:
            require(action == "output" and started, "package_event_rejected")
        if action == "output":
            require(isinstance(event.get("Output"), str), "test_output_shape")
            chunks.append(event["Output"])
            # Test2json's active test supplies ownership even for an unlabelled
            # runtime output event. Never merge different test bodies to infer
            # a valid marker or call chain.
            owner = test if test is not None else active
            if owner is not None:
                owned_chunks[owner].append(event["Output"])
            else:
                package_chunks.append(event["Output"])
    require(started and runs == set(expected) and set(outcomes) == set(expected) and terminal,
            "incomplete_tests")
    text = "".join(chunks)
    pattern = re.compile(r"==================\n(WARNING: DATA RACE\n.*?)==================\n", re.DOTALL)
    blocks = list(pattern.finditer(text))
    require(baseline or not blocks, "unexpected_race")
    races = [target_race(match.group(1), source_root) for match in blocks]
    remainder = pattern.sub("", text)
    require("WARNING: DATA RACE" not in remainder and "==================" not in remainder,
            "truncated_or_unframed_race")
    ordinary = remainder.splitlines()
    red = bool(races)
    require(exit_code == (1 if red else 0) and terminal == ("fail" if red else "pass"),
            "test_outcome_mismatch")
    require(outcomes == {name: "fail" if red else "pass" for name in expected}, "test_outcome_mismatch")
    race_markers = 0
    text_runs, text_ends, text_finals = [], [], []
    for line in ordinary:
        if re.fullmatch(r"\s+testing\.go:[1-9][0-9]*: race detected during execution of test", line):
            require(red, "unexpected_race_marker")
            race_markers += 1
        elif line in {"PASS", "FAIL"}:
            text_finals.append(line.lower())
        elif line == "":
            continue
        else:
            run = re.fullmatch(r"=== RUN   (Test[A-Za-z0-9_]+)", line)
            end = re.fullmatch(r"--- (PASS|FAIL): (Test[A-Za-z0-9_]+) \([0-9]+(?:\.[0-9]+)?s\)", line)
            if run:
                text_runs.append(run[1])
            elif end:
                text_ends.append((end[2], end[1].lower()))
            else:
                raise GateError("unrecognized_test_output")
    require((race_markers > 0) == red, "race_marker_mismatch")
    require(len(text_runs) == len(expected) and set(text_runs) == set(expected), "test_text_runs_mismatch")
    require(len(text_ends) == len(expected) and dict(text_ends) == outcomes, "test_text_outcomes_mismatch")
    require(text_finals == [terminal], "test_text_package_mismatch")
    require([line for line in "".join(package_chunks).splitlines() if line] == [terminal.upper()],
            "package_output_ownership")
    for name, owned in owned_chunks.items():
        local = pattern.sub("", "".join(owned)).splitlines()
        markers = [line for line in local if line.startswith("=== RUN") or line.startswith("--- ")]
        require(len(markers) == 2 and markers[0] == "=== RUN   " + name and
                re.fullmatch(r"--- " + outcomes[name].upper() + ": " + re.escape(name) +
                             r" \([0-9]+(?:\.[0-9]+)?s\)", markers[1]), "test_output_ownership")
    return {"classification": "target_cache_race" if red else "quiet_pass",
            "outcomes": outcomes, "package_outcome": terminal, "exit_code": exit_code,
            "target_race_blocks": len(races), "frame_classes": races,
            "coverage": "cold_first_use_only" if baseline else "six_blackbox_tests"}
