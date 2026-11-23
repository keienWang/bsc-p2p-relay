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
