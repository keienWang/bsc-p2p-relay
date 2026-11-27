# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from check_flag_defaults import compare_defaults, documented_defaults, help_defaults


DOC = """<!-- operations-flags:start -->
| Flag | Default | Notes |
| --- | --- | --- |
| `--network` | `mainnet` | string |
| `--p2p` | `:30311` | string |
| `--check-config` | `false` | bool |
| `--explicit` | `true` | bool |
| `--peer` | none | repeated |
| `--http-drain-timeout` | `5s` | duration |
<!-- operations-flags:end -->
"""
HELP = (
    "Usage of ./relay:\n"
    '  -network string\n    \tnetwork (default "mainnet")\n'
    '  -p2p string\n    \taddress (default ":30311")\n'
    "  -check-config\n    \tcheck configuration\n"
    "  -explicit\n    \texplicit (default true)\n"
    "  -peer value\n    \trepeatable\n"
    "  -http-drain-timeout duration\n    \thttp (1ms..30s) (default 5s)\n"
)


class FlagDefaultsTest(unittest.TestCase):
    def test_formats_and_exact_agreement(self):
        self.assertEqual(compare_defaults(DOC, HELP), [])

    def test_drift_without_name_change(self):
        self.assertEqual(compare_defaults(DOC, HELP.replace("default 5s", "default 7s")),
                         ["--http-drain-timeout: documented '5s', binary default '7s'"])

    def test_names_must_match(self):
        self.assertEqual(compare_defaults(DOC, HELP.replace("-p2p ", "-p2p-listen ")),
                         ["flag names differ: undocumented=['p2p-listen']; not in binary=['p2p']"])

    def test_unsupported_or_duplicate_input_fails_closed(self):
        for bad_help in (HELP + "  -peer value\n    \trepeatable\n",
                         HELP.replace("  -peer value", "  -peer unknown"),
                         HELP.replace(" (default 5s)", ""),
                         "failed to start"):
            with self.subTest(bad_help=bad_help), self.assertRaises(ValueError):
                help_defaults(bad_help)
        with self.assertRaises(ValueError):
            documented_defaults(DOC.replace("| none |", "| empty |"))


if __name__ == "__main__":
    unittest.main()
