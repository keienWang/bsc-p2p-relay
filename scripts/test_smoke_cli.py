# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import errno
import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from smoke import (MAX_STARTUP_LOG_BYTES, check_cli_rejections, check_observation,
                   ipv6_loopback_available, startup_endpoint, wait_startup_endpoint)


ERROR = "unexpected positional arguments; use --help for supported flags"


class CLISmokeTest(unittest.TestCase):
    def run_check(self, result):
        with mock.patch("smoke.subprocess.run", return_value=result) as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            check_cli_rejections("/fixture/bsc-relay", "/fixture/work")
        return execute

    def test_all_cases_keep_invalid_api_before_positional_arguments(self):
        execute = self.run_check(subprocess.CompletedProcess([], 1, ERROR + "\n"))
        self.assertEqual(execute.call_count, 6)
        suffixes = []
        for call in execute.call_args_list:
            self.assertEqual(call.args[0][:6], ["/fixture/bsc-relay", "--api",
                             "not-an-address", "--no-discovery", "--p2p",
                             "127.0.0.1:0"])
            suffixes.append(tuple(call.args[0][6:]))
            self.assertEqual(call.kwargs, {
                "cwd": "/fixture/work", "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                "text": True, "timeout": 5, "check": False,
            })
        self.assertEqual(set(suffixes), {
            ("unrecognized-subcommand",),
            ("unrecognized-subcommand", "--network", "testnet"),
            ("--network", "testnet", "unexpected-value"),
            ("--no-discovery", "false", "--network", "testnet"),
            ("--", "--network", "testnet"),
            ("-", "--network", "testnet"),
        })

    def test_old_address_error_and_wrong_status_or_startup_fail(self):
        for code, output in (
            (1, 'address not-an-address: missing port in address'),
            (0, ERROR),
            (2, ERROR),
            (1, ERROR + '\nexperimental relay started'),
            (1, ""),
        ):
            with self.subTest(code=code, output=output), self.assertRaises(AssertionError):
                self.run_check(subprocess.CompletedProcess([], code, output))

    def test_timeout_is_not_accepted(self):
        with mock.patch("smoke.subprocess.run", side_effect=subprocess.TimeoutExpired("fixture", 5)), \
                self.assertRaises(subprocess.TimeoutExpired):
            check_cli_rejections("/fixture/bsc-relay", "/fixture/work")


class LoopbackPortSmokeTest(unittest.TestCase):
    def test_missing_ipv6_loopback_is_reported_before_launch(self):
        with mock.patch("smoke.socket.socket", side_effect=OSError(errno.EAFNOSUPPORT, "fixture")), \
                mock.patch("smoke.subprocess.Popen") as launch, contextlib.redirect_stdout(io.StringIO()) as output:
            check_observation("/fixture/bsc-relay", observe_only=True, ipv6=True)
        launch.assert_not_called()
        self.assertIn("SKIP: IPv6 loopback", output.getvalue())

    def test_ipv6_permission_errors_are_not_swallowed(self):
        with mock.patch("smoke.socket.socket", side_effect=OSError(errno.EACCES, "fixture")):
            with self.assertRaises(OSError):
                ipv6_loopback_available()


class StartupEndpointSmokeTest(unittest.TestCase):
    @staticmethod
    def record(api, mode=b"true"):
        return (b"2026/11/21 00:00:00 INFO experimental relay started; no chain/state verification or confirmations "
                b'network=mainnet api=' + api + b' observe_only=' + mode + b' enode=fixture\n')

    def test_only_complete_own_bound_endpoint(self):
        v4 = self.record(b"127.0.0.1:12345", b"false")
        v6 = self.record(b"[::1]:54321")
        self.assertIsNone(startup_endpoint(v4[:-1], "127.0.0.1", False))
        self.assertIsNone(startup_endpoint(b"unrelated api=127.0.0.1:12345\n", "127.0.0.1", False))
        self.assertEqual(startup_endpoint(b"earlier log\n" + v4, "127.0.0.1", False), ("127.0.0.1:12345", 12345))
        self.assertEqual(startup_endpoint(v6, "::1", True), ("[::1]:54321", 54321))
        for api in (b"127.0.0.1:0", b"127.0.0.1:65536", b"127.0.0.1:80/path", b"[::1]:12345", b"127.0.0.2:12345"):
            with self.subTest(api=api), self.assertRaisesRegex(ValueError, "expected loopback"):
                startup_endpoint(self.record(api), "127.0.0.1", True)
        with self.assertRaisesRegex(ValueError, "wrong observation mode"):
            startup_endpoint(v4, "127.0.0.1", True)

    def test_exited_process_and_log_budget_are_not_success(self):
        with tempfile.TemporaryFile() as log:
            process = mock.Mock()
            process.poll.return_value = 1
            log.write(self.record(b"127.0.0.1:12345"))
            log.flush()
            with self.assertRaisesRegex(RuntimeError, "exited before smoke"):
                wait_startup_endpoint(process, log, "127.0.0.1", True)
            log.write(b"x" * MAX_STARTUP_LOG_BYTES)
            log.flush()
            with self.assertRaisesRegex(RuntimeError, "log budget"):
                wait_startup_endpoint(process, log, "127.0.0.1", True)


class StartupCleanupSmokeTest(unittest.TestCase):
    def test_startup_timeout_kills_and_reaps_real_child_without_http(self):
        real_popen = subprocess.Popen
        children = []

        def fallback_cleanup(child):
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

        def launch_fixture(command, **kwargs):
            self.assertEqual(command, ["/fixture/bsc-relay", "--no-discovery", "--p2p",
                                       "127.0.0.1:0", "--api", "127.0.0.1:0", "--observe-only"])
            # No sockets or descendants: this test-only process never produces a
            # startup record and cannot exit by itself during the observation.
            child = real_popen([sys.executable, "-c", "import signal; signal.pause()"], **kwargs)
            children.append(child)
            self.addCleanup(fallback_cleanup, child)
            return child

        with mock.patch("smoke.STARTUP_TIMEOUT_SECONDS", 0.05), \
                mock.patch("smoke.subprocess.Popen", side_effect=launch_fixture), \
                mock.patch("smoke.urllib.request.build_opener") as build_opener:
            with self.assertRaisesRegex(RuntimeError, "did not report a bound API"):
                check_observation("/fixture/bsc-relay", observe_only=True)
            build_opener.return_value.open.assert_not_called()

        self.assertEqual(len(children), 1)
        child = children[0]
        # Inspect the cached return code before polling, which could itself reap
        # the process and mask a missing wait() in the smoke cleanup.
        self.assertEqual(child.returncode, -signal.SIGKILL)
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)


if __name__ == "__main__":
    unittest.main()
