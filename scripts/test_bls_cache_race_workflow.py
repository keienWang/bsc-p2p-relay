# SPDX-License-Identifier: GPL-3.0-or-later
"""Static CI boundary contracts, not a Go or namespace execution."""
import hashlib
from pathlib import Path
import unittest


class StageOneWorkflow(unittest.TestCase):
    def workflow(self):
        return (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_bytes()

    def test_all_six_existing_jobs_are_byte_identical(self):
        data = self.workflow()
        self.assertEqual(data.count(b'  bls_cache_race:\n'), 1)
        prefix, job = data.split(b'  bls_cache_race:\n', 1)
        self.assertEqual(hashlib.sha256(prefix).hexdigest(),
                         'fc390f2d710157d5fa5404c00c3b13dd2905c376cb03cb3a36395b3b9141fb1d')
        self.assertNotIn(b'\n  test:', job)

    def test_candidate_job_is_pinned_bounded_and_nonexporting(self):
        job = self.workflow().decode().split('  bls_cache_race:\n', 1)[1]
        for text in ('runs-on: ubuntu-24.04', 'timeout-minutes: 25', 'timeout-minutes: 23',
                     "go-version: '1.26.11'", 'GOTOOLCHAIN: local', "GOENV: 'off'", "GOWORK: 'off'",
                     "GOTELEMETRY: 'off'", "GOMAXPROCS: '2'", 'test_bls_cache_race*.py',
                     'test "$(go env GOVERSION)" = go1.26.11',
                     'run: python3 scripts/bls_cache_race_ci.py'):
            self.assertIn(text, job)
        self.assertEqual(job.count('cache: false'), 1)
        self.assertEqual(job.count('persist-credentials: false'), 1)
        self.assertIn('actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803', job)
        self.assertIn('actions/setup-go@924ae3a1cded613372ab5595356fb5720e22ba16', job)
        for text in ('actions/cache', 'upload-artifact', 'continue-on-error', 'if: always()',
                     'govulncheck', 'go test', 'go run', 'go build', 'cache: true', 'SecretKey',
                     'curl ', 'wget ', 'cat ', 'tee ', 'RUNNER_TEMP', '> '):
            self.assertNotIn(text, job)
        # Go compilation/execution belongs inside the runner's checked namespace,
        # not a convenient online workflow fallback.
        self.assertEqual(job.count('python3 scripts/bls_cache_race_ci.py'), 1)


if __name__ == '__main__':
    unittest.main()
