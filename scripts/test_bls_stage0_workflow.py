# SPDX-License-Identifier: GPL-3.0-or-later
"""Static workflow boundary; no Go, process, network or BLS execution."""
import hashlib
from pathlib import Path
import unittest


class StageZeroWorkflow(unittest.TestCase):
    def test_original_five_jobs_remain_byte_identical(self):
        data = (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_bytes()
        prefix, job = data.split(b'  bls_stage0_inventory:\n', 1)
        self.assertEqual(hashlib.sha256(prefix).hexdigest(), '35bf325dbd1a09a8d335d7cfcb527a2efada12f5ca1af188619640e515878e1d')
        self.assertEqual(data.count(b'  bls_stage0_inventory:\n'), 1)
        self.assertNotIn(b'\n  test:', job)

    def test_only_bounded_metadata_job_is_enabled(self):
        data = (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text()
        job = data.split('  bls_stage0_inventory:\n', 1)[1]
        self.assertIn('timeout-minutes: 12', job)
        self.assertIn('timeout-minutes: 11', job)
        self.assertEqual(job.count('cache: false'), 1)
        self.assertEqual(job.count('persist-credentials: false'), 1)
        self.assertIn('go-version: \'1.26.11\'', job)
        self.assertIn('python3 scripts/bls_stage0_inventory.py >', job)
        self.assertIn("obj['tests_executed'] is False", job)
        self.assertIn("obj['patch_applied'] is False", job)
        self.assertIn("obj['license_approval'] is False", job)
        self.assertIn("hashlib.sha256(data).hexdigest()", job)
        for forbidden in ('actions/cache', 'upload-artifact', 'continue-on-error',
                          'if: always()', 'go test', 'go run', 'go build',
                          'govulncheck', 'cache: true', 'SecretKey'):
            self.assertNotIn(forbidden, job)


if __name__ == '__main__':
    unittest.main()
