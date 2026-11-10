# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from check_stats_docs import ROOT, check_contract


class StatsDocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = [(ROOT / path).read_text() for path in (
            "docs/OBSERVABILITY.md", "internal/relay/stats.go", "internal/relay/http.go")]

    def test_current_fixed_schema(self):
        self.assertEqual(check_contract(*self.contract), [])

    def test_missing_or_renamed_field(self):
        for old, new in (("| `instance_id` |", "| `host_id` |"),
                         ("| `other_total` |", "| `surprise_total` |")):
            changed = self.contract.copy()
            changed[0] = changed[0].replace(old, new)
            self.assertTrue(check_contract(*changed))

    def test_undocumented_source_field(self):
        changed = self.contract.copy()
        changed[1] = changed[1].replace('json:"concurrency_rejected_total"', 'json:"new_total"')
        self.assertTrue(check_contract(*changed))

    def test_v1_cannot_silently_bump_version_or_move_route(self):
        for replacement in ("2", "nextVersion", "1 + 1"):
            changed = self.contract.copy()
            # Preserve arbitrary gofmt alignment rather than rely on whitespace.
            import re
            changed[1] = re.sub(r"(SchemaVersion\s*:\s*)1", r"\g<1>" + replacement, changed[1])
            self.assertTrue(check_contract(*changed))
        changed = self.contract.copy()
        changed[2] = changed[2].replace('case "/v1/stats":', 'case "/v2/stats":')
        self.assertTrue(check_contract(*changed))

    def test_missing_or_duplicate_table_fails_closed(self):
        for replace in ("<!-- absent:start -->", "<!-- stats-submit:end -->"):
            changed = self.contract.copy()
            changed[0] = changed[0].replace("<!-- stats-submit:start -->", replace)
            with self.assertRaises(ValueError):
                check_contract(*changed)
        changed = self.contract.copy()
        changed[0] = changed[0].replace("| `other_total` |", "| `rejected_validation_total` |")
        with self.assertRaises(ValueError):
            check_contract(*changed)
