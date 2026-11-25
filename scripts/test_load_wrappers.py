# SPDX-License-Identifier: GPL-3.0-or-later
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("bash"), "load wrapper argument checks require Bash")
class LoadWrapperArgumentTests(unittest.TestCase):
    def test_help_and_argument_errors_never_start_load(self):
        common = ((("--help",), 0), (("-h",), 0), (("",), 2),
                  (("--unknown",), 2), (("--help", "extra"), 2))
        for wrapper, extra in (
            ("run_relay_soak.sh", (("unexpected",),)),
            ("run_unique_capacity.sh", (("--short", "--help"),
                                        ("--default-ttl", "extra"),
                                        ("--failure-probe", "--short"))),
        ):
            for args, code in (*common, *((invalid, 2) for invalid in extra)):
                with self.subTest(wrapper=wrapper, args=args), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    stubs = root / "bin"
                    stubs.mkdir()
                    for name in ("flock", "mktemp", "go"):
                        stub = stubs / name
                        stub.write_text("#!/bin/bash\nprintf '%s\\n' \"$0\" >> \"$TEST_TOUCHED\"\nexit 99\n",
                                        encoding="utf-8")
                        stub.chmod(0o700)
                    env = dict(os.environ, PATH=str(stubs) + os.pathsep + os.environ["PATH"],
                               TMPDIR=directory, TEST_TOUCHED=str(root / "touched"))
                    result = subprocess.run(["bash", str(SCRIPTS / wrapper), *args], env=env,
                                            capture_output=True, text=True, timeout=5, check=False)
                    self.assertCountEqual(root.iterdir(), [stubs],
                                          "help/error created a lock/output or invoked a workload tool")
                    self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
                    output, empty = (result.stderr, result.stdout) if code else (result.stdout, result.stderr)
                    self.assertEqual(empty, "")
                    self.assertIn(f"Usage: {wrapper}", output)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("bash", "flock", "sha256sum", "tee")),
                     "load wrappers require Linux shell utilities")
class LoadWrapperLogTests(unittest.TestCase):
    def test_failed_log_capture_cannot_report_success(self):
        for wrapper in ("run_relay_soak.sh", "run_unique_capacity.sh"):
            for test_status, tee_status in ((0, 0), (17, 0), (0, 23), (17, 23)):
                with self.subTest(wrapper=wrapper, test=test_status, tee=tee_status):
                    self.check_result(wrapper, test_status, tee_status)

    def test_successful_pipeline_still_needs_a_generated_report(self):
        for wrapper in ("run_relay_soak.sh", "run_unique_capacity.sh"):
            for report_kind in ("missing", "empty", "directory", "symlink"):
                with self.subTest(wrapper=wrapper, report_kind=report_kind):
                    self.check_result(wrapper, 0, 0, report_kind=report_kind)

    def test_original_failure_takes_precedence_when_report_missing(self):
        for wrapper in ("run_relay_soak.sh", "run_unique_capacity.sh"):
            for test_status, tee_status in ((17, 0), (0, 23), (17, 23)):
                with self.subTest(wrapper=wrapper, test=test_status, tee=tee_status):
                    self.check_result(wrapper, test_status, tee_status, report_kind="missing")

    def check_result(self, wrapper, test_status, tee_status, *, report_kind="valid"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stubs = root / "bin"
            stubs.mkdir()
            files = {
                "go": """#!/bin/bash
case "$1" in
    version) echo 'fixture go version';;
    test)
        report=${RELAY_UNIQUE_REPORT:-${RELAY_SOAK_REPORT:-}}
        test -n "$report" || exit 98
        case "$TEST_REPORT_KIND" in
            valid) printf '{"passed":true}\n' > "$report";;
            missing) :;;
            empty) touch "$report";;
            directory) mkdir "$report";;
            symlink)
                printf '{"passed":true}\n' > "$report.other"
                ln -s "$report.other" "$report";;
            *) exit 97;;
        esac
        echo 'fixture test result'
        exit "$TEST_GO_STATUS";;
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
                       TEST_TEE_STATUS=str(tee_status), TEST_REAL_TEE=shutil.which("tee"),
                       TEST_REPORT_KIND=report_kind)
            result = subprocess.run(["bash", str(SCRIPTS / wrapper)], env=env, capture_output=True,
                                    text=True, timeout=10, check=False)
            reports = [path for path in root.glob("bsc-relay-*") if path.is_dir()]
            self.assertEqual(len(reports), 1, (result.stdout, result.stderr))
            report = reports[0]
            invalid_report = not test_status and not tee_status and report_kind != "valid"
            expected = test_status or tee_status or int(invalid_report)
            self.assertEqual(result.returncode, expected, (result.stdout, result.stderr))
            self.assertEqual((report / "exit-code.txt").read_text(), f"{expected}\n")
            if invalid_report:
                self.assertIn("required report.json is missing, empty, or not a regular non-symlink file", result.stderr)
                self.assertIn("Exit=1;", result.stderr)
                self.assertNotIn("Exit=0;", result.stdout)
            else:
                self.assertNotIn("required report.json", result.stderr)
            if tee_status:
                self.assertIn(f"log capture failed (tee exit 23); test exit={test_status}", result.stderr)
                self.assertIn(f"Exit={expected};", result.stderr)
                self.assertNotIn("Exit=0;", result.stdout)
            else:
                self.assertIn("fixture test result", (report / "test.log").read_text())


if __name__ == "__main__":
    unittest.main()
