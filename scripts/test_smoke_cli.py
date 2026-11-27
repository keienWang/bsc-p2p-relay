# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import errno
import io
import os
import pathlib
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from smoke import (MAX_STARTUP_LOG_BYTES, check_cli_rejections, check_observation,
                   check_occupied_ports_cli,
                   ipv6_loopback_available, running_local_relay, startup_endpoint,
                   wait_startup_endpoint)


ERROR = "unexpected positional arguments; use --help for supported flags"


class OptimizedSmokeTest(unittest.TestCase):
    def test_optimized_runtime_rejects_wrong_counts_and_failed_shutdown(self):
        cases = {
            "unexpected Submit outcomes": """
                from smoke import assert_stats_counts
                assert_stats_counts({"submit": {}, "http": {"concurrency_rejected_total": 0}}, 0)
            """,
            "relay exited with status 7 after SIGTERM": """
                from unittest.mock import Mock, patch
                from smoke import running_local_relay
                child = Mock()
                child.wait.return_value = 7
                child.poll.return_value = 7
                with patch("smoke.subprocess.Popen", return_value=child), patch(
                    "smoke.wait_startup_endpoint", return_value=("127.0.0.1:12345", 12345)
                ):
                    with running_local_relay("/fixture/bsc-relay", "/fixture/work",
                                             "127.0.0.1", "127.0.0.1:0", True):
                        pass
            """,
        }
        for message, code in cases.items():
            with self.subTest(message=message):
                result = subprocess.run([sys.executable, "-O", "-c", textwrap.dedent(code)],
                                        cwd=os.path.dirname(__file__), capture_output=True,
                                        text=True, timeout=5, check=False)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn(message, result.stderr)


class SmokeEntrypointTest(unittest.TestCase):
    def invoke(self, work, *args):
        script = pathlib.Path(__file__).resolve().with_name("smoke.py")
        return subprocess.run([sys.executable, "-O", str(script), *args], cwd=work,
                              capture_output=True, text=True, check=False, timeout=5)

    def test_help_and_invalid_arguments_do_not_launch(self):
        with tempfile.TemporaryDirectory() as work:
            marker = pathlib.Path(work) / "called"
            binary = pathlib.Path(work) / "canary"
            binary.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(marker)) + "\n")
            binary.chmod(0o700)
            result = self.invoke(work, "--help")
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assertIn("locally built Relay executable", result.stdout)
            for args in ((), ("canary", "extra")):
                with self.subTest(args=args):
                    result = self.invoke(work, *args)
                    self.assertEqual((result.returncode, result.stdout), (2, ""))
                    self.assertIn("usage:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(marker.exists())

    def test_bad_binary_and_stage_failure_have_actionable_diagnostics(self):
        with tempfile.TemporaryDirectory() as work:
            root = pathlib.Path(work)
            (root / "not-executable").write_text("fixture")
            for path, expected in (("missing", "missing"), (".", "executable regular file"),
                                   ("not-executable", "executable regular file")):
                with self.subTest(path=path):
                    self.assert_smoke_failure(self.invoke(work, path), expected)
            fake = root / "guarded"
            # A harmless executable that does not honor the Relay CLI contract.
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o700)
            self.assert_smoke_failure(self.invoke(work, "./guarded"),
                                      "CLI positional arguments did not fail before startup")

    def assert_smoke_failure(self, result, detail):
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("relay smoke failed:", result.stderr)
        self.assertIn(detail, result.stderr)
        self.assertNotIn("Traceback", result.stderr)


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


class OccupiedPortSmokeTest(unittest.TestCase):
    def run_fixture(self, bad=None):
        attempts = []

        def execute(command, **kwargs):
            self.assertEqual(command[:4], ["/fixture/bsc-relay", "--no-discovery", "--observe-only", "--p2p"])
            self.assertEqual(command[5], "--api")
            addresses = {"--p2p": command[4], "--api": command[6]}
            self.assertEqual(len(command), 7)
            flag, stage = (("--api", b"bind HTTP API:") if not attempts else ("--p2p", b"start P2P node:"))
            address = addresses.pop(flag)
            self.assertEqual(list(addresses.values()), ["127.0.0.1:0"])
            host, port = address.split(":")
            self.assertEqual(host, "127.0.0.1")
            self.assertNotEqual(port, "0")
            # The first socket must still be held when each binary runs.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as contender:
                with self.assertRaises(OSError) as raised:
                    contender.bind((host, int(port)))
                self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
            attempts.append(flag)
            self.assertEqual(kwargs["timeout"], 5)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            status, detail = 1, b""
            if bad == "status":
                status = 0
            if bad == "stage":
                stage = b"wrong bind stage:"
            if bad == "address":
                address = "127.0.0.1:0"
            if bad == "startup":
                detail = b"experimental relay started\n"
            if bad == "budget":
                detail = b"x" * MAX_STARTUP_LOG_BYTES
            record = (b'ERROR relay stopped error="' + stage + b" listen tcp " + address.encode("ascii")
                      + b': bind: address already in use"\n')
            os.write(kwargs["stdout"].fileno(), record + detail)
            return subprocess.CompletedProcess(command, status)

        with mock.patch("smoke.subprocess.run", side_effect=execute), contextlib.redirect_stdout(io.StringIO()) as output:
            if bad is None:
                check_occupied_ports_cli("/fixture/bsc-relay", "/fixture/work")
                self.assertIn("PASS: occupied loopback", output.getvalue())
                self.assertEqual(attempts, ["--api", "--p2p"])
            else:
                with self.assertRaisesRegex(AssertionError, "did not report|announced startup|log budget"):
                    check_occupied_ports_cli("/fixture/bsc-relay", "/fixture/work")
                self.assertEqual(attempts, ["--api"])
                self.assertEqual(output.getvalue(), "")

    def test_actual_reservation_covers_both_cli_stages(self):
        self.run_fixture()

    def test_never_accepts_wrong_stage_address_status_startup_or_excess_logs(self):
        for bad in ("stage", "address", "status", "startup", "budget"):
            with self.subTest(bad=bad):
                self.run_fixture(bad)


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
    def test_fixed_rebind_requires_owned_endpoint_before_probe(self):
        for endpoint, failure, poll in (
            (("127.0.0.1:12346", 12346), None, None),
            (None, RuntimeError("relay exited before smoke observation: bind HTTP API"), 1),
        ):
            with self.subTest(endpoint=endpoint):
                child = mock.Mock()
                child.poll.return_value = poll
                with mock.patch("smoke.subprocess.Popen", return_value=child) as launch, \
                        mock.patch("smoke.wait_startup_endpoint", return_value=endpoint, side_effect=failure):
                    with self.assertRaisesRegex((AssertionError, RuntimeError),
                                                "requested fixed loopback address|bind HTTP API"):
                        with running_local_relay("/fixture/bsc-relay", "/fixture/work",
                                                 "127.0.0.1", "127.0.0.1:12345", True):
                            self.fail("a failed or mismatched bind exposed an endpoint")
                self.assertEqual(launch.call_args.args[0], [
                    "/fixture/bsc-relay", "--no-discovery", "--p2p", "127.0.0.1:0",
                    "--api", "127.0.0.1:12345", "--observe-only",
                ])
                child.send_signal.assert_not_called()
                if poll is None:
                    child.kill.assert_called_once_with()
                    child.wait.assert_called_once_with(timeout=10)
                else:
                    child.kill.assert_not_called()

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
