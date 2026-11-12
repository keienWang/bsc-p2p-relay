# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Python processes and mock failures. No Go, namespaces or network."""
import io
import contextlib
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import bls_metadata_capture as capture
import bls_stage0_inventory as base
import bls_cache_race_ci as runner


class MetadataCaptureContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        (self.state / "work").mkdir()
        self.logs = self.state / "logs"
        self.logs.mkdir()

    def args(self, stdout=b'{"valid":true}\n', stderr=b'PRIVATE_DIAGNOSTIC\n', extra=""):
        return [sys.executable, "-c", "import sys,time,os; sys.stdout.buffer.write(" + repr(stdout) +
                ");sys.stdout.flush();sys.stderr.buffer.write(" + repr(stderr) + ");sys.stderr.flush();" + extra]

    def collect(self, args, label="probe", budget=None, timeout=5):
        return capture.capture(args, self.state / "work", {}, self.logs, label,
                               budget or runner.process.Budget(10), timeout)

    def test_valid_json_and_stderr_diagnostics_remain_separate(self):
        code, output = self.collect(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(base.stream_json(output), [{"valid": True}])
        self.assertNotIn("PRIVATE_DIAGNOSTIC", output)
        status = capture.read_status(self.logs, "probe")
        self.assertEqual(status["stdout_private"]["sha256"], base.sha(b'{"valid":true}\n'))
        self.assertEqual(status["stderr_private"]["sha256"], base.sha(b'PRIVATE_DIAGNOSTIC\n'))
        self.assertNotIn("PRIVATE_DIAGNOSTIC", json.dumps(status))
        self.assertEqual((self.logs / "probe.stderr.log").read_bytes(), b'PRIVATE_DIAGNOSTIC\n')

    def test_runner_metadata_receipt_and_safe_projection(self):
        output, receipt = runner.command(self.state, "prepare-baseline-graph", self.args(), {}, runner.process.Budget(5))
        self.assertEqual(base.stream_json(output), [{"valid": True}])
        self.assertEqual(receipt["capture_mode"], capture.MODE)
        progress = json.loads((self.state / "progress.json").read_text())
        safe = runner.export_progress(progress, None)
        self.assertNotIn("PRIVATE_DIAGNOSTIC", json.dumps(safe))
        self.assertNotIn(str(self.state), json.dumps(safe))
        self.assertEqual(safe["last_command"]["stderr_private"], receipt["stderr_private"])
        with self.assertRaises(base.GateError):
            runner.export_receipt(receipt)  # Cannot silently use metadata shape for test/compile receipts.
        for change in (lambda v: v["stdout_private"].update(raw="PRIVATE_DIAGNOSTIC"),
                       lambda v: v["stderr_private"].update(bytes=True),
                       lambda v: v.update(capture_mode="merged_unchecked")):
            value = json.loads(json.dumps(receipt))
            change(value)
            with self.subTest(), self.assertRaises(base.GateError):
                runner.export_receipt(value, separate=True)

    def test_stderr_flood_hits_combined_limit_and_retains_only_prefix(self):
        budget = runner.process.Budget(5, 256)
        with mock.patch.object(runner.process, "COMMAND_BYTES", 128), self.assertRaises(base.GateError):
            self.collect(self.args(b'{}', b'x' * 65536, "time.sleep(10)"), budget=budget)
        status = capture.read_status(self.logs, "probe")
        self.assertEqual(status["reason"], "output_limit")
        total = sum(status[n + "_private"]["bytes"] for n in ("stdout", "stderr"))
        self.assertEqual(total, 128)
        self.assertEqual(budget.remaining, 128)
        self.assertLessEqual((self.logs / "probe.stderr.log").stat().st_size, 128)

    def test_both_streams_share_remaining_total_budget_across_commands(self):
        budget = runner.process.Budget(5, 64)
        self.collect(self.args(b'x' * 20, b'y' * 20), label="first", budget=budget)
        self.assertEqual(budget.remaining, 24)
        with self.assertRaises(base.GateError):
            self.collect(self.args(b'x' * 20, b'y' * 20), label="second", budget=budget)
        status = capture.read_status(self.logs, "second")
        self.assertEqual(status["reason"], "output_limit")
        self.assertEqual(budget.remaining, 0)
        with mock.patch.object(capture.subprocess, "Popen") as popen, self.assertRaises(base.GateError):
            self.collect(self.args(), label="third", budget=budget)
        popen.assert_not_called()

    def test_nonzero_exit_is_not_metadata_success(self):
        with self.assertRaisesRegex(base.GateError, "^command_failed_prepare-baseline-graph$"):
            runner.command(self.state, "prepare-baseline-graph", self.args(extra="sys.exit(7)"), {}, runner.process.Budget(5))
        status = capture.read_status(self.logs, "prepare-baseline-graph")
        self.assertEqual(status["returncode"], 7)
        self.assertEqual(status["reason"], None)
        safe = runner.export_progress(json.loads((self.state / "progress.json").read_text()), None)
        self.assertEqual(safe["last_command"]["exit_code"], 7)
        self.assertNotIn("PRIVATE_DIAGNOSTIC", json.dumps(safe))

    def test_invalid_json_still_fails_without_filtering_or_ignored_tail(self):
        for index, raw in enumerate((b'{} trailing', b'go: diagnostic\n{}', b'{"broken":')):
            code, output = self.collect(self.args(raw), label="bad-json-" + str(index))
            self.assertEqual(code, 0)
            with self.assertRaisesRegex(base.GateError, "invalid_json"):
                base.stream_json(output)

    def test_invalid_utf8_in_either_stream_is_failure(self):
        for index, (stdout, stderr) in enumerate(((b'\xff', b'valid'), (b'{}', b'\xff'))):
            label = "bad-utf8-" + str(index)
            with self.subTest(), self.assertRaises(base.GateError):
                self.collect(self.args(stdout, stderr), label=label)
            self.assertEqual(capture.read_status(self.logs, label)["reason"], "invalid_utf8")

    def test_timeout_reaps_group_and_does_not_accept_valid_prefix(self):
        with mock.patch.object(runner.process, "terminate_group", wraps=runner.process.terminate_group) as stop:
            with self.assertRaises(base.GateError):
                self.collect(self.args(extra="time.sleep(10)"), timeout=0.1)
        self.assertEqual(capture.read_status(self.logs, "probe")["reason"], "timeout")
        self.assertTrue(stop.called)
        self.assertIsNotNone(stop.call_args.args[0].poll())

    def assert_descendant_capture_cleanup(self, disable_capture_cleanup=False):
        pidfile = self.state / "owned-child.pid"
        script = ("import pathlib,subprocess,sys; "
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                  "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); print('{}',flush=True)")
        argv = [sys.executable, "-c", script, str(pidfile)]
        real_cleanup = runner.process.terminate_group
        proc = subprocess.Popen(argv, cwd=self.state / "work", env={}, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        try:
            # Readiness is a separate bounded prerequisite, not a timeout pass.
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertTrue(pidfile.is_file())
            pid = int(pidfile.read_text())
            self.assertTrue(select.select([proc.stdout], [], [], 1)[0])
            mutation = (mock.patch.object(runner.process, "terminate_group", return_value=False)
                        if disable_capture_cleanup else contextlib.nullcontext())
            with mutation, mock.patch.object(capture.subprocess, "Popen", return_value=proc) as start, self.assertRaises(base.GateError):
                self.collect(argv, timeout=0.2)
            start.assert_called_once()
            status = capture.read_status(self.logs, "probe")
            self.assertEqual(status["reason"], "timeout")
            def live():
                try:
                    return (Path("/proc") / str(pid) / "stat").read_text().rpartition(") ")[2].split()[0] != "Z"
                except (FileNotFoundError, ProcessLookupError):
                    return False
            deadline = time.monotonic() + 1
            while live() and time.monotonic() < deadline:
                time.sleep(0.01)
            # This assertion precedes the test's fallback cleanup, so a broken
            # capture cannot pass merely because finally kills its child.
            self.assertFalse(live(), "capture left owned descendant alive")
            self.assertIn((self.logs / "probe.stdout.log").read_bytes(), (b'', b'{}\n'))
        finally:
            # Real cleanup even for the deliberate no-op mutation or failed
            # preparation. It never supplies the preceding success evidence.
            real_cleanup(proc)
            proc.stdout.close()
            proc.stderr.close()

    def test_timeout_kills_descendant_even_after_leader_exit(self):
        self.assert_descendant_capture_cleanup()

    def test_cleanup_contract_detects_noop_capture_before_real_finalizer(self):
        with self.assertRaisesRegex(AssertionError, "capture left owned descendant alive"):
            self.assert_descendant_capture_cleanup(disable_capture_cleanup=True)

    def test_test_and_compile_capture_paths_are_not_changed(self):
        for label in ("baseline-compile", "candidate-compile", "test2json-compile", "baseline-test-1", "candidate-tests"):
            with self.subTest(label=label), \
                 mock.patch.object(capture, "capture", side_effect=AssertionError("metadata path used for tests")), \
                 mock.patch.object(runner.process, "capture", return_value=(0, "mock compiler/test text")) as old:
                _, receipt = runner.command(self.state, label, ["not-executed"], {}, runner.process.Budget(2))
            old.assert_called_once()
            self.assertNotIn("capture_mode", receipt)
            self.assertIn("raw_private_sha256", receipt)

    def test_expired_deadline_never_starts_child(self):
        budget = runner.process.Budget(0)
        with mock.patch.object(capture.subprocess, "Popen") as popen, self.assertRaises(base.GateError):
            self.collect(self.args(), budget=budget)
        popen.assert_not_called()

    def test_selector_registration_failure_still_closes_and_reaps_child(self):
        selector = mock.Mock()
        selector.register.side_effect = RuntimeError("PRIVATE_DIAGNOSTIC")
        created = []
        real_popen = subprocess.Popen
        def start(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            created.append(proc)
            return proc
        with mock.patch.object(capture.selectors, "DefaultSelector", return_value=selector), \
             mock.patch.object(capture.subprocess, "Popen", side_effect=start), self.assertRaises(base.GateError):
            self.collect(self.args(extra="time.sleep(10)"))
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].poll())
        self.assertTrue(created[0].stdout.closed and created[0].stderr.closed)
        selector.close.assert_called_once()
        self.assertEqual(capture.read_status(self.logs, "probe")["reason"], "capture_error")

    def test_python_interruption_cleans_group_even_if_leader_already_exited(self):
        proc = mock.Mock(returncode=0)
        proc.poll.return_value = 0
        proc.stdout, proc.stderr = io.BytesIO(b'{}'), io.BytesIO(b'diagnostic')
        selector = mock.Mock()
        selector.register.side_effect = KeyboardInterrupt()
        with mock.patch.object(capture.subprocess, "Popen", return_value=proc), \
             mock.patch.object(capture.selectors, "DefaultSelector", return_value=selector), \
             mock.patch.object(runner.process, "terminate_group", return_value=False) as cleanup, \
             self.assertRaisesRegex(base.GateError, "^metadata_capture_failed$"):
            self.collect(["never-executed"])
        cleanup.assert_called_once_with(proc)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertEqual(capture.read_status(self.logs, "probe")["reason"], "capture_error")

    def test_status_rejects_tampered_private_prefix_or_unknown_fields(self):
        self.collect(self.args())
        (self.logs / "probe.stderr.log").write_text("changed")
        with self.assertRaises(base.GateError):
            capture.read_status(self.logs, "probe")
        status_path = self.logs / "probe.json"
        status = json.loads(status_path.read_text())
        status["raw"] = "PRIVATE_DIAGNOSTIC"
        status_path.write_text(json.dumps(status))
        with self.assertRaises(base.GateError):
            capture.read_status(self.logs, "probe")

    def test_receipt_must_bind_the_actual_parser_input_not_just_changed_logs(self):
        for memory, changed in ((b'{}', b'{"other":1}'), (b'{"a":1}', b'{"b":2}')):
            def fake(command, cwd, env, attempt, label, budget, timeout):
                (attempt / (label + ".stdout.log")).write_bytes(changed)
                (attempt / (label + ".stderr.log")).write_bytes(b'')
                status = {"returncode": 0, "reason": None, "capture_mode": capture.MODE,
                          "stdout_private": {"bytes": len(changed), "sha256": base.sha(changed)},
                          "stderr_private": {"bytes": 0, "sha256": base.sha(b'')}}
                (attempt / (label + ".json")).write_text(json.dumps(status))
                return 0, memory.decode()
            with self.subTest(memory=memory), mock.patch.object(capture, "capture", side_effect=fake), \
                 self.assertRaisesRegex(base.GateError, "^metadata_stdout_identity$"):
                runner.command(self.state, "prepare-baseline-graph", ["never-execute"], {}, runner.process.Budget(5))

    def test_failure_progress_has_only_separate_stream_hashes(self):
        with self.assertRaises(base.GateError):
            runner.command(self.state, "prepare-baseline-graph", self.args(stderr=b'\xff'), {}, runner.process.Budget(5))
        value = json.loads((self.state / "progress.json").read_text())
        safe = runner.export_progress(value, None)
        failure = safe["command_failure"]
        self.assertEqual(failure["reason"], "invalid_utf8")
        self.assertEqual(failure["capture_mode"], capture.MODE)
        self.assertNotIn(str(self.state), json.dumps(safe))
        bad = json.loads(json.dumps(value))
        bad["command_failure"]["stderr_private"]["raw"] = "PRIVATE_DIAGNOSTIC"
        with self.assertRaises(base.GateError):
            runner.export_progress(bad, None)


if __name__ == "__main__":
    unittest.main()
