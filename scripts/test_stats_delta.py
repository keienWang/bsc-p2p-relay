# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import copy
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import stats_delta
from smoke import STATS_SUBMIT_FIELDS, MAX_STATS_RESPONSE_BYTES


def sample():
    return dict(schema_version=1, instance_id="a" * 32,
                created_at="2026-11-19T00:00:00.123456789Z",
                observed_at="2026-11-19T00:01:00Z",
                submit=dict.fromkeys(STATS_SUBMIT_FIELDS, 0),
                http={"concurrency_rejected_total": 0})


class StatsDeltaTests(unittest.TestCase):
    def invoke(self, before, after):
        with tempfile.TemporaryDirectory() as directory:
            paths = [pathlib.Path(directory) / name for name in ("before.json", "after.json")]
            for path, value in zip(paths, (before, after)):
                path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = stats_delta.main([str(path) for path in paths])
            return code, out.getvalue(), err.getvalue()

    def test_exact_large_integer_deltas_and_separate_groups(self):
        before = sample()
        after = copy.deepcopy(before)
        for i, field in enumerate(sorted(STATS_SUBMIT_FIELDS)):
            before["submit"][field] = (1 << 63) + i
            after["submit"][field] = before["submit"][field] + i + 1
        after["http"]["concurrency_rejected_total"] = (1 << 64) - 1
        code, out, err = self.invoke(before, after)
        self.assertEqual((code, err), (0, ""))
        value = json.loads(out)
        self.assertEqual(value["http_delta"], {"concurrency_rejected_total": (1 << 64) - 1})
        for i, field in enumerate(sorted(STATS_SUBMIT_FIELDS)):
            self.assertEqual(value["submit_delta"][field], i + 1)
        self.assertNotIn("rate", value)
        self.assertNotIn("total", value)

    def test_unchanged_and_backward_wall_clock_are_not_rates(self):
        before, after = sample(), sample()
        after["observed_at"] = "2026-11-18T23:59:00Z"
        code, out, err = self.invoke(before, after)
        self.assertEqual((code, err), (0, ""))
        value = json.loads(out)
        self.assertEqual(set(value["submit_delta"].values()), {0})
        self.assertEqual(value["after_observed_at"], after["observed_at"])

    def test_cli_forward_time_guard_uses_exact_normalized_nanoseconds(self):
        command = [sys.executable, str(pathlib.Path(stats_delta.__file__).resolve())]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            before, after = sample(), sample()
            before["observed_at"] = "2026-11-19T23:59:59.123456781Z"
            after["observed_at"] = "2026-11-19T23:59:59.123456782Z"

            def run(*args):
                for filename, value in (("before.json", before), ("after.json", after)):
                    (root / filename).write_text(json.dumps(value), encoding="utf-8")
                return subprocess.run(command + ["before.json", "after.json", *args], cwd=root,
                                      capture_output=True, text=True, timeout=5, check=False)

            option = "--require-forward-time"
            for first, second in (
                (before["observed_at"], after["observed_at"]),
                ("2026-11-19T23:59:59.999999999Z", "2026-11-20T00:00:00Z"),
                ("2026-11-20T00:00:00Z", "2026-11-20T00:00:00.000000001Z"),
            ):
                with self.subTest(first=first, second=second):
                    before["observed_at"], after["observed_at"] = first, second
                    result = run(option, "--require-unchanged")
                    self.assertEqual((result.returncode, result.stderr), (0, ""))
                    self.assertEqual(json.loads(result.stdout)["after_observed_at"], second)
            for first, second in (
                ("2026-11-19T23:59:59.123456782Z", "2026-11-19T23:59:59.123456781Z"),
                ("2026-11-20T00:00:00Z", "2026-11-19T23:59:59.999999999Z"),
                ("2026-11-20T00:00:00Z", "2026-11-20T00:00:00.000000000Z"),
                ("2026-11-20T00:00:00.1Z", "2026-11-20T00:00:00.100000000Z"),
            ):
                with self.subTest(first=first, second=second):
                    before["observed_at"], after["observed_at"] = first, second
                    self.assertEqual(run().returncode, 0)
                    refused = run(option, "--output", "refused.json")
                    self.assertEqual((refused.returncode, refused.stdout), (1, ""))
                    self.assertIn("after observed_at must be strictly later", refused.stderr)
                    self.assertFalse((root / "refused.json").exists())
            before["observed_at"] = "2026-11-20T00:00:00Z"
            after["observed_at"] = "2026-11-20T00:00:00.000000001Z"
            count = "concurrency_rejected_total"
            after["http"][count] = 1
            growth = run(option, "--require-unchanged")
            self.assertEqual((growth.returncode, growth.stderr), (3, ""))
            self.assertEqual(json.loads(growth.stdout)["http_delta"][count], 1)
            before["http"][count] = 2
            decrease = run(option)
            self.assertEqual((decrease.returncode, decrease.stdout), (1, ""))
            self.assertIn("counter decreased", decrease.stderr)

    def test_minimum_recorded_gap_cli_is_exact_inclusive_and_preserves_counter_guard(self):
        command = [sys.executable, str(pathlib.Path(stats_delta.__file__).resolve())]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            before, after = sample(), sample()
            before["observed_at"] = "2026-11-19T23:59:59.999000001Z"
            after["observed_at"] = "2026-11-20T00:00:00Z"
            option = "--min-observation-gap-ms"

            def run(*args, input_name="before.json"):
                for filename, value in (("before.json", before), ("after.json", after)):
                    (root / filename).write_text(json.dumps(value), encoding="utf-8")
                return subprocess.run(command + [input_name, "after.json", *args], cwd=root,
                                      capture_output=True, text=True, timeout=5, check=False)

            baseline = run("--require-forward-time", "--require-unchanged")
            self.assertEqual((baseline.returncode, baseline.stderr), (0, ""))
            short = run(option, "1", "--require-unchanged", "--output", "no-report.json")
            self.assertEqual((short.returncode, short.stdout), (1, ""))
            self.assertIn("observation gap is below 1 ms", short.stderr)
            self.assertFalse((root / "no-report.json").exists())

            after["observed_at"] = "2026-11-20T00:00:00.000000001Z"
            equal = run(option, "1", "--require-forward-time", "--require-unchanged")
            ordinary = run()
            self.assertEqual((equal.returncode, equal.stderr, equal.stdout), (0, "", ordinary.stdout))
            after["http"]["concurrency_rejected_total"] = 1
            increase = run(option, "1", "--require-unchanged", "--output", "report.json")
            self.assertEqual((increase.returncode, increase.stderr), (3, ""))
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["http_delta"], {"concurrency_rejected_total": 1})

            # A later wall timestamp is required even without the older option.
            after["observed_at"] = "2026-11-19T23:59:59Z"
            backwards = run(option, "1")
            self.assertEqual((backwards.returncode, backwards.stdout), (1, ""))
            self.assertIn("observation gap is below 1 ms", backwards.stderr)
            for value in ("0", "-1", "1.5", "１", "+1", str(1 << 64), "9" * 5000):
                with self.subTest(value=value[:30]):
                    bad = run(option, value, input_name="missing.json")
                    self.assertEqual((bad.returncode, bad.stdout), (2, ""))
                    self.assertIn("positive ASCII decimal uint64", bad.stderr)
                    self.assertNotIn("before snapshot:", bad.stderr)

    def test_maximum_recorded_gap_cli_rejects_stale_and_reversed_snapshots(self):
        command = [sys.executable, "-O", str(pathlib.Path(stats_delta.__file__).resolve())]
        max_option, min_option = "--max-observation-gap-ms", "--min-observation-gap-ms"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            before, after = sample(), sample()
            before["observed_at"] = "2026-11-19T23:59:59.999000001Z"

            def run(*args, second="after.json"):
                for filename, value in (("before.json", before), ("after.json", after)):
                    (root / filename).write_text(json.dumps(value), encoding="utf-8")
                return subprocess.run(command + ["before.json", second, *args], cwd=root,
                                      capture_output=True, text=True, timeout=5, check=False)

            for timestamp, expected_gap in (
                ("2026-11-20T00:00:00.000000002Z", "1000001"),
                ("2026-11-20T01:00:00Z", "3600000999999"),
            ):
                with self.subTest(timestamp=timestamp):
                    after["observed_at"] = timestamp
                    self.assertEqual(run(min_option, "1", "--require-unchanged").returncode, 0)
                    stale = run(max_option, "1", "--require-unchanged", "--output", "stale.json")
                    self.assertEqual((stale.returncode, stale.stdout), (1, ""))
                    self.assertIn(f"above 1 ms (recorded {expected_gap} ns)", stale.stderr)
                    self.assertFalse((root / "stale.json").exists())

            after["observed_at"] = "2026-11-20T00:00:00.000000001Z"
            ordinary = run()
            exact = run(min_option, "1", max_option, "1", "--require-forward-time")
            self.assertEqual(exact.returncode, 0, exact.stderr)
            self.assertEqual((exact.stderr, exact.stdout), ("", ordinary.stdout))
            after["http"]["concurrency_rejected_total"] = 1
            growth = run(max_option, "1", "--require-unchanged", "--output", "valid.json")
            self.assertEqual((growth.returncode, growth.stderr), (3, ""))
            report = json.loads((root / "valid.json").read_text(encoding="utf-8"))
            self.assertEqual(report["http_delta"], {"concurrency_rejected_total": 1})

            after["observed_at"] = before["observed_at"]
            self.assertEqual(run(max_option, "1").returncode, 0)
            after["observed_at"] = "2026-11-19T23:59:59.999000000Z"
            self.assertEqual(run().returncode, 0)
            backwards = run(max_option, "1")
            self.assertEqual((backwards.returncode, backwards.stdout), (1, ""))
            self.assertIn("observation gap is negative (recorded -1 ns)", backwards.stderr)
            inverted = run(min_option, "2", max_option, "1", second="missing.json")
            self.assertEqual((inverted.returncode, inverted.stdout), (2, ""))
            self.assertIn("minimum observation gap cannot exceed maximum", inverted.stderr)
            self.assertNotIn("snapshot:", inverted.stderr)
            for bad in ("0", "-1", "+1", "1.5", "１", str(1 << 64), "9" * 5000):
                with self.subTest(bad=bad[:20]):
                    invalid = run(max_option, bad, second="missing.json")
                    self.assertEqual((invalid.returncode, invalid.stdout), (2, ""))
                    self.assertIn("maximum observation gap must be a positive ASCII decimal uint64", invalid.stderr)
                    self.assertNotIn("snapshot:", invalid.stderr)

    def test_restart_metadata_change_and_every_counter_decrease_fail_closed(self):
        cases = []
        changed = sample()
        changed["instance_id"] = "b" * 32
        cases.append((sample(), changed))
        changed = sample()
        changed["created_at"] = "2026-11-19T00:00:01Z"
        cases.append((sample(), changed))
        for group in ("submit", "http"):
            for field in sample()[group]:
                before = sample()
                before[group][field] = (1 << 64) - 1
                cases.append((before, sample()))
        for before, after in cases:
            with self.subTest(before=before, after=after):
                code, out, err = self.invoke(before, after)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("statistics unavailable:", err)

    def test_invalid_input_never_becomes_zero_or_partial_output(self):
        invalid = [b"", b"{", b"null", b"[]", b"\xff", b" " * (MAX_STATS_RESPONSE_BYTES + 1)]
        for field, value in (("schema_version", 2), ("instance_id", "invalid"), ("created_at", "not a date")):
            changed = sample()
            changed[field] = value
            invalid.append(changed)
        for value in (True, -1, 1.5, 1 << 64, "0", None):
            changed = sample()
            changed["http"]["concurrency_rejected_total"] = value
            invalid.append(changed)
        missing = sample()
        missing["submit"].pop(next(iter(STATS_SUBMIT_FIELDS)))
        invalid.append(missing)
        invalid.append(json.dumps(sample()).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1').encode())
        for bad in invalid:
            for before, after in ((bad, sample()), (sample(), bad)):
                with self.subTest(bad=bad):
                    code, out, err = self.invoke(before, after)
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn("statistics unavailable:", err)

    def test_cli_errors_identify_snapshot_stage_counter_or_output(self):
        command = [sys.executable, str(pathlib.Path(stats_delta.__file__).resolve())]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            good, bad, later, output = [root / name for name in
                                        ("good.json", "bad.json", "later.json", "output.json")]
            before, after = sample(), sample()
            before["http"]["concurrency_rejected_total"] = (1 << 64) - 1
            good.write_text(json.dumps(before), encoding="utf-8")
            later.write_text(json.dumps(after), encoding="utf-8")
            bad.write_text("{", encoding="utf-8")
            cases = ((bad, good, "before snapshot:"), (good, bad, "after snapshot:"),
                     (good, later, "http.concurrency_rejected_total: 18446744073709551615 -> 0"))
            for first, second, message in cases:
                with self.subTest(message=message):
                    result = subprocess.run(command + [str(first), str(second), "--output", str(output)],
                                            capture_output=True, text=True, timeout=5, check=False)
                    self.assertEqual((result.returncode, result.stdout), (1, ""))
                    self.assertIn(message, result.stderr)
                    self.assertFalse(output.exists())
            output.write_bytes(b"keep the prior report")
            result = subprocess.run(command + [str(later), str(good), "--output", str(output)],
                                    capture_output=True, text=True, timeout=5, check=False)
            self.assertEqual((result.returncode, result.stdout), (1, ""))
            self.assertIn("output report:", result.stderr)
            self.assertEqual(output.read_bytes(), b"keep the prior report")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO fixture required")
    def test_saved_input_rejects_nonregular_paths_without_waiting_for_bytes(self):
        command = [sys.executable, str(pathlib.Path(stats_delta.__file__).resolve())]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            saved = root / "saved.json"
            saved.write_text(json.dumps(sample()), encoding="utf-8")
            pipe = root / "not-a-snapshot"
            os.mkfifo(pipe)
            output = root / "output.json"
            for path in (root, pipe):
                with self.subTest(path=path):
                    result = subprocess.run(command + [str(saved), str(path), "--output", str(output)],
                                            capture_output=True, text=True, timeout=5, check=False)
                    self.assertEqual((result.returncode, result.stdout), (1, ""))
                    self.assertIn("after snapshot: snapshot input must be a regular file", result.stderr)
                    self.assertFalse(output.exists())
            link = root / "linked.json"
            link.symlink_to(saved.name)
            result = subprocess.run(command + [str(saved), str(link)],
                                    capture_output=True, text=True, timeout=5, check=False)
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assertEqual(json.loads(result.stdout)["instance_id"], sample()["instance_id"])

    def test_missing_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = stats_delta.main([directory + "/missing", directory + "/other"])
            self.assertEqual((code, out.getvalue()), (1, ""))
            self.assertIn("statistics unavailable:", err.getvalue())

    def test_cli_explains_only_exceeded_counter_limits_without_changing_the_report(self):
        command = [sys.executable, str(pathlib.Path(stats_delta.__file__).resolve())]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            before, after = sample(), sample()
            http = "http.concurrency_rejected_total"
            accepted = "submit.accepted_new_cache_lifetime_total"
            other = "submit.other_total"
            big = (1 << 64) - 1
            after["http"]["concurrency_rejected_total"] = 3
            after["submit"]["accepted_new_cache_lifetime_total"] = big
            after["submit"]["other_total"] = 1
            for name, value in (("before.json", before), ("after.json", after)):
                (root / name).write_text(json.dumps(value), encoding="utf-8")

            def run(*args):
                return subprocess.run(command + ["before.json", "after.json", *args], cwd=root,
                                      capture_output=True, text=True, timeout=5, check=False)

            def message(selector, delta, limit):
                return f"counter guard exceeded: {selector} delta={delta}, limit={limit}\n"

            baseline = run("--require-unchanged")
            self.assertEqual((baseline.returncode, baseline.stderr), (3, ""))
            explained = run("--explain-guard", "--require-unchanged")
            self.assertEqual((explained.returncode, explained.stdout), (3, baseline.stdout))
            self.assertEqual(explained.stderr,
                             message(http, 3, 0) + message(accepted, big, 0) + message(other, 1, 0))
            selected = run("--explain-guard", "--require-unchanged-counter", accepted,
                           "--require-unchanged-counter", accepted, "--output", "comparison.json")
            self.assertEqual((selected.returncode, selected.stderr), (3, message(accepted, big, 0)))
            self.assertEqual((root / "comparison.json").read_text(), baseline.stdout)
            budgets = run("--explain-guard", "--max-counter-increase", accepted + f"={big}",
                          "--max-counter-increase", http + "=2")
            self.assertEqual((budgets.returncode, budgets.stderr), (3, message(http, 3, 2)))
            allowed = run("--explain-guard", "--max-counter-increase", http + "=3")
            self.assertEqual((allowed.returncode, allowed.stderr), (0, ""))
            usage = run("--explain-guard")
            self.assertEqual((usage.returncode, usage.stdout), (2, ""))
            self.assertIn("--explain-guard requires a counter guard", usage.stderr)

    def test_cli_can_require_only_selected_counters_to_stay_unchanged(self):
        command = [sys.executable, str(pathlib.Path(stats_delta.__file__).resolve())]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            before, after = sample(), sample()
            after["submit"]["accepted_new_cache_lifetime_total"] = (1 << 64) - 1
            (root / "before.json").write_text(json.dumps(before), encoding="utf-8")
            (root / "after.json").write_text(json.dumps(after), encoding="utf-8")

            def run(*args, first="before.json", second="after.json"):
                return subprocess.run(command + [first, second, *args], cwd=root,
                                      capture_output=True, text=True, timeout=5, check=False)

            selector = "--require-unchanged-counter"
            http, unchanged = "http.concurrency_rejected_total", "submit.other_total"
            changed = "submit.accepted_new_cache_lifetime_total"
            ordinary = run()
            stable = run(selector, http, selector, unchanged)
            increased = run(selector, http, selector, changed)
            self.assertEqual((stable.returncode, increased.returncode), (0, 3))
            self.assertEqual((stable.stderr, increased.stderr), ("", ""))
            self.assertEqual((stable.stdout, increased.stdout), (ordinary.stdout, ordinary.stdout))
            self.assertEqual(json.loads(stable.stdout)["submit_delta"][changed.split(".")[1]], (1 << 64) - 1)

            saved = run(selector, changed, "--output", "delta.json")
            self.assertEqual((saved.returncode, saved.stderr), (3, ""))
            self.assertEqual((root / "delta.json").read_text(), ordinary.stdout)
            collision = run(selector, changed, "--output", "delta.json")
            self.assertEqual((collision.returncode, collision.stdout), (1, ""))
            self.assertEqual((root / "delta.json").read_text(), ordinary.stdout)

            # Even a non-selected counter decrease invalidates the comparison.
            reversed_order = run(selector, http, first="after.json", second="before.json")
            self.assertEqual((reversed_order.returncode, reversed_order.stdout), (1, ""))
            self.assertIn("counter decreased", reversed_order.stderr)
            for invalid in ((selector, "http.typo"), (selector, http, "--require-unchanged")):
                with self.subTest(args=invalid):
                    bad_args = run(*invalid, first="missing.json")
                    self.assertEqual((bad_args.returncode, bad_args.stdout), (2, ""))
                    self.assertNotIn("before snapshot:", bad_args.stderr)

    def test_cli_caps_selected_increases_without_changing_report_semantics(self):
        command = [sys.executable, str(pathlib.Path(stats_delta.__file__).resolve())]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            before, after = sample(), sample()
            after["observed_at"] = "2026-11-19T00:01:00.000000001Z"
            http, validation = "concurrency_rejected_total", "rejected_validation_total"
            accepted = "accepted_new_cache_lifetime_total"
            after["http"][http] = (1 << 64) - 1
            after["submit"].update({accepted: 1, validation: 2})
            for filename, payload in (("before.json", before), ("after.json", after)):
                (root / filename).write_text(json.dumps(payload), encoding="utf-8")

            def run(*args, first="before.json", second="after.json"):
                return subprocess.run(command + [first, second, *args], cwd=root,
                                      capture_output=True, text=True, timeout=5, check=False)

            option = "--max-counter-increase"
            zero_budget = "submit.rejected_queue_unavailable_total=0"
            flags = (option, zero_budget, option, f"http.{http}={after['http'][http]}")
            ordinary = run()
            equal = run(*flags, option, f"submit.{validation}=2", "--require-forward-time")
            exceeded = run(*flags, option, f"submit.{validation}=1")
            self.assertEqual((equal.returncode, exceeded.returncode), (0, 3))
            self.assertEqual((equal.stderr, exceeded.stderr), ("", ""))
            self.assertEqual((equal.stdout, exceeded.stdout), (ordinary.stdout, ordinary.stdout))
            report = json.loads(equal.stdout)
            self.assertEqual(report["http_delta"][http], (1 << 64) - 1)
            self.assertEqual(report["submit_delta"][accepted], 1)  # No budget on this field.
            above_exact_uint64_limit = run(option, f"http.{http}={(1 << 64) - 2}")
            self.assertEqual((above_exact_uint64_limit.returncode, above_exact_uint64_limit.stdout),
                             (3, ordinary.stdout))

            saved = run(option, f"submit.{accepted}=0", "--output", "delta.json")
            self.assertEqual((saved.returncode, saved.stderr), (3, ""))
            self.assertEqual((root / "delta.json").read_text(), ordinary.stdout)
            collision = run(option, f"submit.{accepted}=0", "--output", "delta.json")
            self.assertEqual((collision.returncode, collision.stdout), (1, ""))
            self.assertEqual((root / "delta.json").read_text(), ordinary.stdout)

            reversed_order = run(option, zero_budget, "--output", "reversed.json",
                                 first="after.json", second="before.json")
            self.assertEqual((reversed_order.returncode, reversed_order.stdout), (1, ""))
            self.assertIn("counter decreased", reversed_order.stderr)
            self.assertFalse((root / "reversed.json").exists())

            key = f"http.{http}"
            invalid_flags = [
                (option, value) for value in (
                    key, key + "=", key + "=1=2", "http.typo=1", "http_delta." + http + "=1",
                    key + "=-1", key + "=+1", key + "= 1", key + "=1.0", key + "=1_0",
                    key + "=١", key + f"={1 << 64}", key + "=" + "9" * 5000,
                )
            ]
            invalid_flags.extend((
                (option, key + "=1", option, key + "=2"),
                (option, key + "=1", "--require-unchanged"),
                (option, key + "=1", "--require-unchanged-counter", key),
            ))
            for invalid in invalid_flags:
                with self.subTest(args=str(invalid)[:128]):
                    result = run(*invalid, "--output", "invalid.json", first="missing.json")
                    self.assertEqual((result.returncode, result.stdout), (2, ""))
                    self.assertNotIn("before snapshot:", result.stderr)
                    self.assertFalse((root / "invalid.json").exists())


if __name__ == "__main__":
    unittest.main()
