# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

import check_operations_docs
from check_operations_docs import ROOT, check_contracts, check_links, heading_anchors, table_names


class OperationsDocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths = ("docs/OPERATIONS.md", "cmd/bsc-relay/main.go", "internal/relay/node.go",
                 "internal/relay/http.go", "internal/relay/store.go")
        cls.contract = [(ROOT / path).read_text() for path in paths]

    def test_current_source_contract(self):
        self.assertEqual(check_contracts(*self.contract), [])

    def test_wrong_flag_and_missing_health_field_fail(self):
        changed = self.contract.copy()
        changed[0] = changed[0].replace("| `--cache-ttl` |", "| `--cache-life` |")
        changed[0] = changed[0].replace("| `ready_peers` |", "| `ready_nodes` |")
        errors = check_contracts(*changed)
        self.assertTrue(any(error.startswith("flags:") for error in errors))
        self.assertTrue(any(error.startswith("health:") for error in errors))

    def test_changed_source_field_or_error_fails(self):
        changed = self.contract.copy()
        changed[2] = changed[2].replace('json:"queued_peers"', 'json:"queued_count"')
        changed[3] = changed[3].replace('"invalid JSON request"', '"changed JSON error"')
        errors = check_contracts(*changed)
        self.assertTrue(any(error.startswith("submission:") for error in errors))
        self.assertIn("undocumented application error: changed JSON error", errors)

    def test_missing_markers_and_duplicate_rows_fail(self):
        with self.assertRaises(ValueError):
            table_names("no table", "flags", "--")
        doc = "<!-- operations-flags:start -->\n| `--api` |\n| `--api` |\n<!-- operations-flags:end -->"
        with self.assertRaises(ValueError):
            table_names(doc, "flags", "--")

    def test_unrecognized_flag_registration_fails_closed(self):
        changed = self.contract.copy()
        changed[1] += '\nflag.Bool("new-flag", false, "new flag")\n'
        with self.assertRaises(ValueError):
            check_contracts(*changed)

    def test_supported_flag_method_with_unparsed_name_fails_closed(self):
        for declaration in ('flag.StringVar(&cfg.Network, flagName, "mainnet", "network")',
                            'flag.StringVar(&cfg.Network, "BadName", "mainnet", "network")'):
            with self.subTest(declaration=declaration):
                changed = self.contract.copy()
                changed[1] += "\n" + declaration + "\n"
                with self.assertRaises(ValueError):
                    check_contracts(*changed)

    def test_flag_receivers_and_aliases_fail_closed(self):
        registrations = (
            'flag.CommandLine.String("new-flag", "", "new flag")',
            'flag . CommandLine . String("new-flag", "", "new flag")',
            'flag /* registration */ .String("new-flag", "", "new flag")',
            'flag . /* registration */ String("new-flag", "", "new flag")',
            'flags := flag.CommandLine; flags.String("new-flag", "", "new flag")',
            'register := flag.String; register("new-flag", "", "new flag")',
        )
        for registration in registrations:
            with self.subTest(registration=registration):
                changed = self.contract.copy()
                changed[1] = changed[1].replace('flag.Parse()', registration + '\nflag.Parse()')
                with self.assertRaises(ValueError):
                    check_contracts(*changed)
        changed = self.contract.copy()
        changed[1] = changed[1].replace('"flag"', 'otherflag "flag"').replace('flag.', 'otherflag.')
        with self.assertRaises(ValueError):
            check_contracts(*changed)

    def test_flag_concatenated_names_and_grouped_admission_errors_fail(self):
        for index, old, new in (
            (1, 'flag.String("api",', 'flag.String("api" + "2",'),
            (1, '"network", cfg.Network,', '"network" + "2", cfg.Network,'),
            (2, 'var ErrNoPeers = errors.New("no compatible transaction-receiving peer is ready")',
             'var (\n ErrNoPeers = errors.New("changed no peers error")\n)'),
            (2, 'var ErrNoPeers = errors.New("no compatible transaction-receiving peer is ready")',
             'var ErrNoPeers = errors.New("changed" + " no peers error")'),
        ):
            with self.subTest(mutation=new):
                changed = self.contract.copy()
                self.assertIn(old, changed[index])
                changed[index] = changed[index].replace(old, new)
                with self.assertRaises(ValueError):
                    check_contracts(*changed)
        changed = self.contract.copy()
        changed[2] += '\nfunc init() { ErrNoPeers = errors.New("changed error") }\n'
        with self.assertRaises(ValueError):
            check_contracts(*changed)

    def test_constant_status_errors_checked_and_unknown_calls_rejected(self):
        changed = self.contract.copy()
        original = 'apiError(w, 400, "invalid JSON request")'
        changed[3] = changed[3].replace(original, 'apiError(w, http.StatusBadRequest, "invalid JSON request")')
        self.assertEqual(check_contracts(*changed), [])
        changed[3] = changed[3].replace('"invalid JSON request"', '"changed JSON error"')
        self.assertIn("undocumented application error: changed JSON error", check_contracts(*changed))
        for unsupported in ('apiError(w, unknownStatus, "new error")',
                            'apiError(w, http.StatusBadRequest, fmt.Sprint("new error"))',
                            'emit := apiError; emit(w, 400, "new error")'):
            with self.subTest(call=unsupported):
                changed = self.contract.copy()
                changed[3] = changed[3].replace(original, unsupported)
                with self.assertRaises(ValueError):
                    check_contracts(*changed)

    def test_command_returns_nonzero_on_unrecognized_source(self):
        original_read = pathlib.Path.read_text
        mutations = (
            ("cmd/bsc-relay/main.go", 'flag.Parse()',
             'flag.CommandLine.String("new-flag", "", "new flag")\nflag.Parse()'),
            ("internal/relay/http.go", 'apiError(w, 400, "invalid JSON request")',
             'apiError(w, http.StatusBadRequest, "changed JSON error")'),
            ("cmd/bsc-relay/main.go", 'flag.String("api",', 'flag.String("api" + "2",'),
            ("cmd/bsc-relay/main.go", 'flag.Parse()',
             'flag /* registration */ .String("new-flag", "", "new flag")\nflag.Parse()'),
            ("internal/relay/node.go", 'var ErrNoPeers = errors.New("no compatible transaction-receiving peer is ready")',
             'var (\n ErrNoPeers = errors.New("changed no peers error")\n)'),
        )
        for relative, old, new in mutations:
            with self.subTest(source=relative):
                def read_with_change(path, *args, **kwargs):
                    text = original_read(path, *args, **kwargs)
                    return text.replace(old, new) if path == ROOT / relative else text

                output = io.StringIO()
                with mock.patch.object(pathlib.Path, "read_text", read_with_change), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(check_operations_docs.main(), 1)
                self.assertIn("FAIL: operations documentation check", output.getvalue())

    def test_numeric_names_are_not_silently_ignored(self):
        changed = self.contract.copy()
        changed[1] += '\nflag.StringVar(&cfg.Network, "network_2", "mainnet", "network")\n'
        changed[2] = changed[2].replace('json:"ready_peers"', 'json:"status2"')
        errors = check_contracts(*changed)
        self.assertTrue(any("network_2" in error for error in errors))
        self.assertTrue(any("status2" in error for error in errors))
        changed[0] = changed[0].replace("<!-- operations-flags:end -->", "| `--network_2` | example | example |\n<!-- operations-flags:end -->")
        changed[0] = changed[0].replace("| `ready_peers` |", "| `status2` |")
        self.assertEqual(check_contracts(*changed), [])

    def test_duplicate_source_names_fail_closed(self):
        for index, old, new in ((1, 'flag.Parse()', 'flag.String("api", "", "duplicate")'),
                                (2, 'json:"ready_peers"', 'json:"connected_peers"')):
            with self.subTest(source=index):
                changed = self.contract.copy()
                changed[index] = changed[index].replace(old, new)
                with self.assertRaises(ValueError):
                    check_contracts(*changed)

    def test_unknown_json_tag_or_untagged_field_fails_closed(self):
        for tag in ('json:"ready_peers,omitempty"', 'json:""', 'xml:"ready_peers"',
                    'json:"ready_peers" unsupported-tag'):
            with self.subTest(tag=tag):
                changed = self.contract.copy()
                changed[2] = changed[2].replace('json:"ready_peers"', tag)
                with self.assertRaises(ValueError):
                    check_contracts(*changed)
        changed = self.contract.copy()
        changed[2] = changed[2].replace('`json:"ready_peers"`', '')
        with self.assertRaises(ValueError):
            check_contracts(*changed)

    def test_command_returns_nonzero_on_corrupted_document(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_operations_docs.main(), 0)
        original_read = pathlib.Path.read_text
        for old, new in (("| `--api` |", "| `--api-typo` |"),
                         ("| `ready_peers` |", "| `ready_peers_typo` |"),
                         ("`invalid JSON request`", "`misspelled JSON error`"),
                         ("(../cmd/bsc-relay/main.go)", "(../cmd/missing.go)")):
            with self.subTest(change=old):
                def read_with_change(path, *args, **kwargs):
                    text = original_read(path, *args, **kwargs)
                    return text.replace(old, new) if path == ROOT / "docs/OPERATIONS.md" else text

                output = io.StringIO()
                with mock.patch.object(pathlib.Path, "read_text", read_with_change), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(check_operations_docs.main(), 1)
                self.assertIn("FAIL: operations documentation check", output.getvalue())

    def test_local_links_and_external_links_are_not_fetched(self):
        with tempfile.TemporaryDirectory() as work:
            root = pathlib.Path(work)
            (root / "target.md").write_text("# An `API` guide\n")
            page = root / "index.md"
            page.write_text("[local](target.md#an-api-guide) [external](https://invalid.invalid/missing)")
            self.assertEqual(check_links(root, "index.md"), [])
            page.write_text("[file](missing.md) [anchor](target.md#missing) [escape](../outside.md)")
            self.assertEqual(len(check_links(root, "index.md")), 3)

    def test_duplicate_heading_suffix(self):
        self.assertEqual(heading_anchors("# Repeat\n## Repeat\n"), {"repeat", "repeat-1"})


if __name__ == "__main__":
    unittest.main()
