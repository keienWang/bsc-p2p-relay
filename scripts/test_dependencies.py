# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = {"Path": "example.org/relay", "Main": True, "Dir": "/ignored/path"}
REPLACED = {"Path": "example.org/lib", "Version": "v1.0.0", "Sum": "h1:fixture",
            "Replace": {"Path": "./compat", "Dir": "/ignored/adapter"}}


@unittest.skipUnless(os.name == "posix" and shutil.which("make"), "requires POSIX make fixture")
class DependencyInventoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        for directory in ("scripts", "docs", "bin"):
            (self.root / directory).mkdir()
        shutil.copy(ROOT / "Makefile", self.root / "Makefile")
        for name in ("dependencies.py", "local_json.py"):
            source = ROOT / "scripts" / name
            if source.exists():
                shutil.copy(source, self.root / "scripts" / name)
        self.existing = self.root / "docs/DEPENDENCIES.json"
        self.previous = b'{"previous": true}\n'
        self.existing.write_bytes(self.previous)
        self.existing.chmod(0o640)
        self.env = dict(os.environ, PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"])
        fake = self.root / "bin/go"
        fake.write_text(f"#!{sys.executable}\n" + """\
import os, sys
if sys.argv[1:] != ["list", "-m", "-json", "all"]:
    sys.exit(77)
sys.stdout.write(os.environ["FIXTURE_LIST"])
sys.exit(int(os.environ["FIXTURE_EXIT"]))
""", encoding="utf-8")
        fake.chmod(0o700)

    def run_tool(self, *command, payload="", status=0):
        return subprocess.run(command, cwd=self.root, timeout=5, capture_output=True, text=True,
                              env=dict(self.env, FIXTURE_LIST=payload, FIXTURE_EXIT=str(status)), check=False)

    def test_make_never_truncates_previous_inventory_on_failed_collection(self):
        for payload, status in ((json.dumps(MODULE), 42), (json.dumps(MODULE) + "\n{", 0),
                                ("", 0), (json.dumps({"Dir": "/wrong"}), 0)):
            with self.subTest(payload=payload, status=status):
                result = self.run_tool("make", "dependencies", payload=payload, status=status)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.existing.read_bytes(), self.previous)
                self.assertEqual(list(self.existing.parent.iterdir()), [self.existing])

    def test_make_installs_same_json_as_stdout_without_machine_paths(self):
        payload = json.dumps(MODULE) + "\n" + json.dumps(REPLACED)
        stdout = self.run_tool(sys.executable, "scripts/dependencies.py", payload=payload)
        self.assertEqual(stdout.returncode, 0, stdout.stderr)
        result = self.run_tool("make", "dependencies", payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.existing.read_bytes(), stdout.stdout.encode())
        self.assertEqual(self.existing.stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(stdout.stdout)["modules"], [
            {"Path": "example.org/relay", "Main": True},
            {"Path": "example.org/lib", "Version": "v1.0.0", "Sum": "h1:fixture",
             "Replace": {"Path": "./compat"}},
        ])


if __name__ == "__main__":
    unittest.main()
