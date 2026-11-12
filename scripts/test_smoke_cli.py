# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import subprocess
import unittest
from unittest import mock

from smoke import check_cli_rejections


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


if __name__ == "__main__":
    unittest.main()
