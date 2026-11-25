# SPDX-License-Identifier: GPL-3.0-or-later
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from check_runtime_dependencies import forbidden_modules, forbidden_packages

RELAY_MODULE = "github.com/keienWang/bsc-p2p-relay"
RUNTIME = RELAY_MODULE + "/cmd/bsc-relay"
TEST = RELAY_MODULE + "/internal/relay.test"
SCRIPT = pathlib.Path(__file__).with_name("check_runtime_dependencies.py").resolve()


class RuntimeDependencyGuardTest(unittest.TestCase):
    def test_rejects_exact_packages_and_descendants(self):
        bad = ["github.com/pion/dtls/v2", "github.com/pion/dtls/v2/pkg/crypto",
               "golang.org/x/crypto/openpgp", "golang.org/x/crypto/openpgp/packet",
               "golang.org/x/crypto/openpgp [example.test]"]
        allowed = ["github.com/pion/dtls/v3", "golang.org/x/crypto/sha3",
                   "golang.org/x/crypto/openpgp-other"]
        self.assertEqual(forbidden_packages(bad + allowed), bad)

    def test_split_openpgp_module_is_never_an_advisory_workaround(self):
        self.assertEqual(forbidden_modules(["github.com/pion/dtls/v2", "golang.org/x/crypto"]), [])
        self.assertEqual(forbidden_modules(["golang.org/x/crypto/openpgp"]),
                         ["golang.org/x/crypto/openpgp"])

    def invoke(self, runtime=RUNTIME, tests=TEST, modules=RELAY_MODULE):
        with tempfile.TemporaryDirectory() as directory:
            fake = pathlib.Path(directory, "go")
            fake.write_text(f"#!{sys.executable}\n" + """\
import os, sys
graph = {
    ('list', '-deps', '-buildvcs=false', './cmd/bsc-relay'): 'RUNTIME',
    ('list', '-deps', '-test', '-buildvcs=false', './...'): 'TESTS',
    ('list', '-m', '-f', '{{.Path}}', 'all'): 'MODULES',
}
print(os.environ[graph[tuple(sys.argv[1:])]])
""", encoding="utf-8")
            fake.chmod(0o700)
            return subprocess.run(
                [sys.executable, "-O", str(SCRIPT)], text=True, capture_output=True, check=False, timeout=5,
                env=dict(os.environ, PATH=directory, RUNTIME=runtime, TESTS=tests, MODULES=modules),
            )

    @unittest.skipUnless(os.name == "posix", "requires a POSIX executable stub")
    def test_cli_requires_each_named_root_before_reporting_success(self):
        for key, expected, diagnostic in (
            ("runtime", RUNTIME, "relay command"),
            ("tests", TEST, "relay test binary"),
            ("modules", RELAY_MODULE, "relay module"),
        ):
            for bad in ("", expected + "-another", "example.com/other"):
                with self.subTest(graph=key, bad=bad):
                    result = self.invoke(**{key: bad})
                    self.assertEqual((result.returncode, result.stdout), (1, ""))
                    self.assertIn(diagnostic, result.stderr)
        # The package compiled as an ordinary library does not prove a test
        # graph was listed, even when both application package paths exist.
        result = self.invoke(tests=RUNTIME + "\n" + RELAY_MODULE + "/internal/relay")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("relay test binary", result.stderr)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX executable stub")
    def test_cli_still_rejects_forbidden_graph_members_with_valid_roots(self):
        for override, diagnostic in (
            ({"runtime": RUNTIME + "\ngithub.com/pion/dtls/v2"}, "Forbidden packages"),
            ({"tests": TEST + "\ngolang.org/x/crypto/openpgp"}, "Forbidden packages"),
            ({"modules": RELAY_MODULE + "\ngolang.org/x/crypto/openpgp"}, "Unsupported modules"),
        ):
            with self.subTest(graph=override):
                result = self.invoke(**override)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn(diagnostic, result.stderr)
        self.assertEqual(self.invoke().returncode, 0)


if __name__ == "__main__":
    unittest.main()
