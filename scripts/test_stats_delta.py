# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import copy
import io
import json
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

    def test_missing_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = stats_delta.main([directory + "/missing", directory + "/other"])
            self.assertEqual((code, out.getvalue()), (1, ""))
            self.assertIn("statistics unavailable:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
