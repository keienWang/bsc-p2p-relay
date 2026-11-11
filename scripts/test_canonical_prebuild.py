# SPDX-License-Identifier: GPL-3.0-or-later
"""Small, deliberately shape-specific check for the CI-only worker prebuild."""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prebuild_canonical_worker.sh"
HELPER = ROOT / "internal/canonicalisolation/isolation_test.go"


# Intentionally pin this small helper's entire known shape. Any refactor needs
# review against the shell prebuild; unknown Go expressions are not interpreted.
EXPECTED_HELPER = r'''
func worker(t *testing.T) string {
	t.Helper()
	buildOnce.Do(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, "go", "build", "-race", "-tags=canonicalisolation", "-o", workerPath, "./worker")
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		start := time.Now()
		t.Logf("worker build start: argv=%q", cmd.Args)
		buildErr = cmd.Run()
		t.Logf("worker build finished: elapsed=%s err=%v", time.Since(start), buildErr)
	})
	if buildErr != nil {
		t.Fatalf("worker build: %v", buildErr)
	}
	return workerPath
}
'''.strip()


def check_helper(source):
    match = re.search(r"^func worker\(t \*testing\.T\) string \{\n.*?^\}",
                      source, re.MULTILINE | re.DOTALL)
    return (source.count("func worker(") == 1 and match is not None
            and match[0] == EXPECTED_HELPER)


class CanonicalPrebuildTest(unittest.TestCase):
    def test_helper_contract(self):
        source = HELPER.read_text()
        self.assertTrue(check_helper(source))
        for old, new in (
            ('"-race",', ''),
            ('"./worker")', '"./other")'),
            ('cmd.Stdout = os.Stdout', 'cmd.Dir = "elsewhere"'),
            ('cmd.Stdout = os.Stdout', 'cmd.Env = os.Environ()'),
            ('120*time.Second', '180*time.Second'),
            ('exec.CommandContext(ctx, "go", "build",', 'exec.CommandContext(ctx, "go", "build", "-trimpath",'),
        ):
            with self.subTest(change=new):
                self.assertFalse(check_helper(source.replace(old, new)))

    def test_script_uses_bounded_matching_build(self):
        source = SCRIPT.read_text()
        builds = [line for line in source.splitlines() if line.startswith("timeout ")]
        self.assertEqual(builds, ['timeout --signal=TERM --kill-after=5s 175s go build -race -tags=canonicalisolation -o "$temp_dir/worker" ./worker || status=$?'])

    def test_mock_build_exit_cleanup_and_inherited_environment(self):
        # PATH points only to a fake go and ordinary OS tools. No Go compilation,
        # fixtures, worker execution, downloads or external services are used.
        for status in (0, 7, 124):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                tools = root / "bin"
                tools.mkdir()
                fake = tools / "go"
                fake.write_text('''#!/bin/bash
set -eu
case "$1" in
version|env) exit 0 ;;
build)
  printf '%s\\n' "$PWD" "$GOFLAGS" "$GOMAXPROCS" "$GOTOOLCHAIN" "$GOWORK" "$GOCACHE" "$GOMODCACHE" "$@" > "$FAKE_LOG"
  shift
  while [ "$1" != '-o' ]; do shift; done
  shift
  printf '#!/bin/bash\\nexit 99\\n' > "$1"
  chmod +x "$1"
  exit "$FAKE_STATUS"
  ;;
*) exit 98 ;;
esac
''')
                fake.chmod(0o755)
                keep = root / "unrelated"
                keep.write_text("keep")
                env = dict(os.environ, PATH=f"{tools}:/usr/bin:/bin", RUNNER_TEMP=temp,
                           FAKE_LOG=str(root / "build.log"), FAKE_STATUS=str(status),
                           GOFLAGS="-buildvcs=false -mod=readonly -p=1 -tags=canonicalisolation",
                           GOMAXPROCS="2", GOTOOLCHAIN="local", GOWORK="off",
                           GOCACHE="same-build-cache", GOMODCACHE="same-module-cache")
                result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                fields = (root / "build.log").read_text().splitlines()
                self.assertEqual(fields[:7], [str(ROOT / "internal/canonicalisolation"),
                    env["GOFLAGS"], "2", "local", "off", "same-build-cache", "same-module-cache"])
                self.assertEqual(fields[7:11], ["build", "-race", "-tags=canonicalisolation", "-o"])
                self.assertEqual(fields[12:], ["./worker"])
                output = pathlib.Path(fields[11])
                self.assertEqual(output.name, "worker")
                self.assertEqual(output.parent.parent, root)
                self.assertFalse(output.parent.exists())
                self.assertEqual(list(root.glob("canonical-prebuild.*")), [])
                self.assertEqual(keep.read_text(), "keep")
                self.assertIn(f"exit={status}", result.stdout)


if __name__ == "__main__":
    unittest.main()
