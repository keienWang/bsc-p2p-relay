# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from check_runtime_dependencies import forbidden_modules, forbidden_packages


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


if __name__ == "__main__":
    unittest.main()
