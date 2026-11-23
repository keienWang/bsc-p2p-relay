# SPDX-License-Identifier: GPL-3.0-or-later
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parent


@unittest.skipUnless(all(shutil.which(tool) for tool in ("bash", "flock", "sha256sum", "tee")),
                     "load wrappers require Linux shell utilities")
class LoadWrapperLogTests(unittest.TestCase):
    def test_failed_log_capture_cannot_report_success(self):
        for wrapper in ("run_relay_soak.sh", "run_unique_capacity.sh"):
            for test_status, tee_status in ((0, 0), (17, 0), (0, 23), (17, 23)):
                with self.subTest(wrapper=wrapper, test=test_status, tee=tee_status):
                    self.check_result(wrapper, test_status, tee_status)

    def check_result(self, wrapper, test_status, tee_status):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stubs = root / "bin"
            stubs.mkdir()
            files = {
                "go": """#!/bin/bash
case "$1" in
    version) echo 'fixture go version';;
    test) echo 'fixture test result'; exit "$TEST_GO_STATUS";;
    *) exit 99;;
esac
""",
                "tee": """#!/bin/bash
if [[ "$TEST_TEE_STATUS" != 0 ]]; then
    cat >/dev/null
    echo 'fixture log write failed' >&2
    exit "$TEST_TEE_STATUS"
fi
exec "$TEST_REAL_TEE" "$@"
""",
            }
            for name, body in files.items():
                path = stubs / name
                path.write_text(body, encoding="utf-8")
                path.chmod(0o700)
            env = dict(os.environ, PATH=str(stubs) + os.pathsep + os.environ["PATH"],
                       TMPDIR=directory, TEST_GO_STATUS=str(test_status),
                       TEST_TEE_STATUS=str(tee_status), TEST_REAL_TEE=shutil.which("tee"))
            result = subprocess.run(["bash", str(SCRIPTS / wrapper)], env=env, capture_output=True,
                                    text=True, timeout=10, check=False)
            reports = [path for path in root.glob("bsc-relay-*") if path.is_dir()]
            self.assertEqual(len(reports), 1, (result.stdout, result.stderr))
            report = reports[0]
            expected = test_status or tee_status
            self.assertEqual(result.returncode, expected, (result.stdout, result.stderr))
            self.assertEqual((report / "exit-code.txt").read_text(), f"{expected}\n")
            if tee_status:
                self.assertIn(f"log capture failed (tee exit 23); test exit={test_status}", result.stderr)
                self.assertIn(f"Exit={expected};", result.stderr)
                self.assertNotIn("Exit=0;", result.stdout)
            else:
                self.assertIn("fixture test result", (report / "test.log").read_text())


if __name__ == "__main__":
    unittest.main()
