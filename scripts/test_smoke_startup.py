# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import subprocess
import unittest
from unittest import mock

from smoke import check_api_bind_failure


class APIStartupSmokeTest(unittest.TestCase):
    def check(self, code=1, output="listen tcp 127.0.0.1:19001: bind: address already in use", failure=None):
        sockets = [mock.MagicMock(), mock.MagicMock()]
        for sock, port in zip(sockets, (19001, 19002)):
            sock.__enter__.return_value = sock
            sock.getsockname.return_value = ("127.0.0.1", port)
        execute = mock.Mock(return_value=subprocess.CompletedProcess([], code, output), side_effect=failure)
        try:
            with mock.patch("smoke.socket.socket", side_effect=sockets), \
                    mock.patch("smoke.subprocess.run", execute), \
                    contextlib.redirect_stdout(io.StringIO()):
                check_api_bind_failure("/fixture/bsc-relay", "/fixture/work")
        finally:
            for sock in sockets:
                sock.bind.assert_called_once_with(("127.0.0.1", 0))
                sock.listen.assert_called_once_with(1)
                sock.__exit__.assert_called_once()
        return execute

    def test_owned_loopback_ports_and_bounded_process(self):
        execute = self.check()
        execute.assert_called_once_with(
            ["/fixture/bsc-relay", "--no-discovery", "--api", "127.0.0.1:19001", "--p2p", "127.0.0.1:19002"],
            cwd="/fixture/work", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=5, check=False,
        )

    def test_wrong_error_exit_or_startup_log_fails(self):
        api_error = "listen tcp 127.0.0.1:19001: bind: address already in use"
        for code, output in (
            (0, api_error), (2, api_error), (1, ""),
            (1, "listen tcp 127.0.0.1:19002: bind: address already in use"),
            (1, api_error + "\nexperimental relay started"),
            (1, api_error + "\nlisten tcp 127.0.0.1:19002: bind failed"),
        ):
            with self.subTest(code=code, output=output), self.assertRaises(AssertionError):
                self.check(code, output)

    def test_timeout_fails_and_releases_test_ports(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.check(failure=subprocess.TimeoutExpired("fixture", 5))


if __name__ == "__main__":
    unittest.main()
