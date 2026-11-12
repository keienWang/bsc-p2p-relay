# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic evidence parser contracts. No Go/native or network execution."""
import json
import unittest

import bls_cache_race_evidence as evidence

ROOT = "/private/baseline-prysm"


def race_block(read_method="Get", read_root=ROOT):
    chunks = ["WARNING: DATA RACE\n"]
    for kind, method, root in (("Read", read_method, read_root), ("Previous write", "Add", ROOT)):
        chunks.append(kind + " at 0xc000001000 by goroutine " + ("12" if kind == "Read" else "13") + ":\n")
        for symbol, filename in (
            (evidence.BLS + "/cache/nonblocking.(*LRU[go.shape.[48]uint8,go.shape.interface {}])." + method + "()", "cache/nonblocking/lru.go"),
            (evidence.BLS + "/crypto/bls/blst.publicKeyFromBytes()", "crypto/bls/blst/public_key.go"),
            (evidence.BLS + "/crypto/bls/blst.PublicKeyFromBytes()", "crypto/bls/blst/public_key.go"),
        ):
            chunks.append("  " + symbol + "\n      " + root + "/" + filename + ":42 +0x123\n")
    chunks.append("Goroutine 12 (running) created at:\n  private.creation()\n      /private/x.go:1 +0x1\n")
    return "".join(chunks)


def stream(baseline=False, race=None):
    events = [{"Action": "start", "Package": evidence.PACKAGE}]
    def add(action, test=None, output=None):
        event = {"Action": action, "Package": evidence.PACKAGE}
        if test:
            event["Test"] = test
        if output is not None:
            event["Output"] = output
        events.append(event)
    for name in ((evidence.COLD,) if baseline else evidence.TESTS):
        add("run", name)
        add("output", name, "=== RUN   " + name + "\n")
        if race:
            add("output", name, "==================\n" + race + "==================\n")
            add("output", name, "    testing.go:1617: race detected during execution of test\n")
        outcome = "fail" if race else "pass"
        add("output", name, "--- " + outcome.upper() + ": " + name + " (0.01s)\n")
        add(outcome, name)
    add("output", output="FAIL\n" if race else "PASS\n")
    add("fail" if race else "pass")
    return "\n".join(json.dumps(event) for event in events)


class EvidenceContracts(unittest.TestCase):
    def test_six_candidate_tests_and_cold_only_baseline(self):
        green = evidence.classify(stream(), 0, False, ROOT)
        quiet = evidence.classify(stream(True), 0, True, ROOT)
        self.assertEqual(set(green["outcomes"]), set(evidence.TESTS))
        self.assertEqual(set(quiet["outcomes"]), {evidence.COLD})
        self.assertEqual(quiet["classification"], "quiet_pass")

    def test_target_red_has_only_safe_semantics(self):
        result = evidence.classify(stream(True, race_block()), 1, True, ROOT)
        self.assertEqual(result["classification"], "target_cache_race")
        self.assertEqual(result["target_race_blocks"], 1)
        self.assertNotIn("/private", json.dumps(result))
        self.assertNotIn("0xc", json.dumps(result))

    def test_multiple_complete_target_blocks_are_allowed(self):
        block = race_block() + "==================\n==================\n" + race_block()
        result = evidence.classify(stream(True, block), 1, True, ROOT)
        self.assertEqual(result["target_race_blocks"], 2)

    def test_wrong_chain_or_source_is_not_red(self):
        for block in (race_block("Add"), race_block(read_root="/unselected/source"),
                      race_block().replace(".publicKeyFromBytes()", ".unrelated()"),
                      race_block().replace(".PublicKeyFromBytes()", ".unrelated()")):
            with self.subTest(block=block), self.assertRaises(evidence.GateError):
                evidence.classify(stream(True, block), 1, True, ROOT)

    def test_unrelated_second_race_cannot_hide_after_target(self):
        block = race_block() + "==================\n==================\n" + race_block("Add")
        with self.assertRaises(evidence.GateError):
            evidence.classify(stream(True, block), 1, True, ROOT)

    def test_creation_stack_cannot_supply_access_chain(self):
        block = race_block().replace("  " + evidence.BLS + "/crypto/bls/blst.publicKeyFromBytes()",
                                     "Goroutine 99 (running) created at:\n  " + evidence.BLS + "/crypto/bls/blst.publicKeyFromBytes()", 1)
        with self.assertRaises(evidence.GateError):
            evidence.classify(stream(True, block), 1, True, ROOT)

    def test_candidate_never_accepts_race(self):
        with self.assertRaises(evidence.GateError):
            evidence.classify(stream(False, race_block()), 1, False, ROOT)

    def test_compile_oom_timeout_signal_and_unknown_output_are_not_red(self):
        for raw, code in (("compiler failure /private", 1), (stream(True), -9),
                          (stream(True), 137), (stream(True).replace("=== RUN", "panic: === RUN"), 1),
                          (stream(True).replace("=== RUN", "test timed out === RUN"), 1),
                          (stream(True).replace("=== RUN", "runtime: out of memory === RUN"), 1),
                          (stream(True, race_block()).replace("testing.go:1617: race detected during execution of test",
                                                             "fixture.go:99: unrelated assertion"), 1)):
            with self.subTest(code=code), self.assertRaises(evidence.GateError):
                evidence.classify(raw, code, True, ROOT)

    def test_incomplete_skip_unknown_duplicate_and_after_terminal_rejected(self):
        good = stream()
        events = [json.loads(line) for line in good.splitlines()]
        changes = [events[:-1], [events[0], events[-1]], events + [events[-1]],
                   events[:2] + [events[1]] + events[2:]]
        for action in ("skip", "pause", "cont"):
            changed = [dict(event) for event in events]
            changed[1]["Action"] = action
            changes.append(changed)
        for changed in changes:
            with self.subTest(), self.assertRaises(evidence.GateError):
                evidence.classify("\n".join(json.dumps(e) for e in changed), 0, False, ROOT)
        with self.assertRaises(evidence.GateError):
            evidence.classify(good.replace(evidence.TESTS[-1], "TestUnknown"), 0, False, ROOT)

    def test_exit_mismatch_empty_and_raw_extra_fields_rejected(self):
        for raw, code in ((stream(), 1), ("", 0),
                          (stream().replace('"Action": "start"', '"Action": "start", "Dir": "/private"'), 0)):
            with self.assertRaises(evidence.GateError):
                evidence.classify(raw, code, False, ROOT)

    def test_truncated_or_orphan_warning_rejected(self):
        original = stream(True, race_block())
        for text in (original.replace('==================\\n', '', 1),
                     original.replace('==================\\n', '', 2)):
            with self.assertRaises(evidence.GateError):
                evidence.classify(text, 1, True, ROOT)

    def test_pinned_go_previous_lowercase_and_both_conflict_orders(self):
        # Go go1.26.11/src/runtime/race/output_test.go, blob
        # 0d5c9096f0ccf17002ed058968e3f27dcd6980f2, uses `Previous write`.
        block = race_block()
        self.assertIn("Previous write", block)
        swapped = block.replace("Read at", "Write at").replace("Previous write at", "Previous read at")
        swapped = swapped.replace(".Get()", ".TEMP()").replace(".Add()", ".Get()").replace(".TEMP()", ".Add()")
        for report in (block, swapped):
            result = evidence.classify(stream(True, report), 1, True, ROOT)
            self.assertEqual(result["classification"], "target_cache_race")

    def test_same_goroutine_different_address_or_wrong_previous_order_rejected(self):
        original = race_block()
        for report in (original.replace("goroutine 13:", "goroutine 12:"),
                       original.replace("Previous write at 0xc000001000", "Previous write at 0xc000002000"),
                       original.replace("Previous write at", "Write at"),
                       original.replace("Read at", "Previous read at")):
            with self.subTest(), self.assertRaises(evidence.GateError):
                evidence.classify(stream(True, report), 1, True, ROOT)

    def test_text_and_events_must_both_agree_and_be_complete(self):
        good = stream()
        events = [json.loads(line) for line in good.splitlines()]
        cases = [good.replace("--- PASS:", "--- FAIL:"),
                 good.replace("PASS\\n", "FAIL\\n"),
                 "\n".join(json.dumps(e) for e in events if e["Action"] != "output"),
                 good.replace("=== RUN   " + evidence.COLD, "=== RUN   TestUnknown"),
                 good.replace("PASS\\n", "PASS\\nPASS\\n")]
        for raw in cases:
            with self.subTest(), self.assertRaises(evidence.GateError):
                evidence.classify(raw, 0, False, ROOT)

    def test_duplicate_keys_nonfinite_and_unexpected_field_types_rejected(self):
        good = stream()
        for addition in ('"Action": "fail", ', '"Elapsed": NaN, ', '"Elapsed": Infinity, ',
                         '"Elapsed": -1, ', '"Elapsed": true, ', '"Elapsed": "bad", ',
                         '"Time": [], ', '"Output": "hidden", '):
            raw = good.replace('"Action": "start"', addition + '"Action": "start"', 1)
            with self.subTest(addition=addition), self.assertRaises(evidence.GateError):
                evidence.classify(raw, 0, False, ROOT)

    def test_overlapping_runs_modes_and_bad_source_roots_rejected(self):
        events = [json.loads(line) for line in stream().splitlines()]
        events.insert(2, {"Action": "run", "Package": evidence.PACKAGE, "Test": evidence.TESTS[1]})
        with self.assertRaises(evidence.GateError):
            evidence.classify("\n".join(map(json.dumps, events)), 0, False, ROOT)
        for mode, root in (("baseline", ROOT), (1, ROOT), (False, "relative"),
                           (False, ROOT + "/"), (False, "/private/../baseline")):
            with self.subTest(), self.assertRaises(evidence.GateError):
                evidence.classify(stream(), 0, mode, root)

    def test_output_chunking_does_not_change_classification(self):
        events = [json.loads(line) for line in stream(True, race_block()).splitlines()]
        divided = []
        for event in events:
            if event["Action"] == "output":
                for char in event["Output"]:
                    divided.append(dict(event, Output=char))
            else:
                divided.append(event)
        result = evidence.classify("\n".join(map(json.dumps, divided)), 1, True, ROOT)
        self.assertEqual(result["target_race_blocks"], 1)

    def test_first_conflict_frame_must_own_the_cache_access(self):
        block = race_block().replace("by goroutine 12:\n", "by goroutine 12:\n  unrelated.top()\n      /unrelated/other.go:99 +0x1\n")
        with self.assertRaises(evidence.GateError):
            evidence.classify(stream(True, block), 1, True, ROOT)

    def test_output_test_ownership_and_order_are_not_global_sets(self):
        events = [json.loads(line) for line in stream().splitlines()]
        first, second = evidence.TESTS[:2]
        for event in events:
            if event["Action"] == "output":
                event["Output"] = event["Output"].replace(first, "TEMP").replace(second, first).replace("TEMP", second)
        with self.assertRaises(evidence.GateError):
            evidence.classify("\n".join(map(json.dumps, events)), 0, False, ROOT)
        events = [json.loads(line) for line in stream().splitlines()]
        events[2]["Output"], events[3]["Output"] = events[3]["Output"], events[2]["Output"]
        with self.assertRaises(evidence.GateError):
            evidence.classify("\n".join(map(json.dumps, events)), 0, False, ROOT)

    def test_unlabelled_runtime_chunks_still_belong_to_active_test(self):
        events = [json.loads(line) for line in stream(True, race_block()).splitlines()]
        for event in events:
            if event["Action"] == "output" and "WARNING: DATA RACE" in event["Output"]:
                del event["Test"]
        result = evidence.classify("\n".join(map(json.dumps, events)), 1, True, ROOT)
        self.assertEqual(result["target_race_blocks"], 1)


if __name__ == "__main__":
    unittest.main()
