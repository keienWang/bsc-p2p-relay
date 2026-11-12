# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline contract tests: only fake metadata tools, never Go or the relay."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/collect_binary_metadata.sh"
TOOLS = ("git", "sha256sum", "stat", "file", "go", "readelf")
BASH = shutil.which("bash")
SNAPSHOT = [
    ["stat", "-c", "%d:%i:%f:%s:%y:%z", "--", "./bsc-relay"],
    ["stat", "-c", "%s", "--", "./bsc-relay"],
    ["sha256sum", "--", "./bsc-relay"],
]
EXPECTED = SNAPSHOT + [
    ["git", "rev-parse", "--verify", "HEAD"],
    ["git", "rev-parse", "--verify", "HEAD^{tree}"],
    ["file", "--", "./bsc-relay"],
    ["go", "version", "-m", "./bsc-relay"],
    ["go", "tool", "buildid", "./bsc-relay"],
    ["readelf", "--version"],
    ["readelf", "-hW", "./bsc-relay"],
    ["readelf", "-lW", "./bsc-relay"],
    ["readelf", "-dW", "./bsc-relay"],
] + SNAPSHOT

# Every external program in the collector is replaced, including hash/stat.
# Hash/stat emulate their read-only contracts on actual fixture bytes. Unknown
# commands fail closed. No installed Go, linker, ELF reader or network is used.
FAKE = r"""
import hashlib, json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
log = pathlib.Path(os.environ['FAKE_LOG'])
rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
rows.append({'argv': [name] + args, 'cwd': os.getcwd(), 'toolchain': os.getenv('GOTOOLCHAIN')})
log.write_text(''.join(json.dumps(row) + '\n' for row in rows))
if len(rows) == int(os.getenv('FAIL_AT', '0')):
    print('deliberate command failure', file=sys.stderr)
    sys.exit(23)
binary = pathlib.Path('./bsc-relay')
if name == 'stat' and args in (['-c', '%s', '--', './bsc-relay'], ['-c', '%d:%i:%f:%s:%y:%z', '--', './bsc-relay']):
    st = binary.stat()
    if args[1] == '%s':
        print('invalid' if os.getenv('INVALID_SIZE') else st.st_size)
    else:
        print(':'.join(str(x) for x in (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)))
elif name == 'sha256sum' and args == ['--', './bsc-relay']:
    digest = 'invalid' if os.getenv('INVALID_HASH') else hashlib.sha256(binary.read_bytes()).hexdigest()
    print(digest + '  ./bsc-relay')
elif name == 'git' and args in (['rev-parse', '--verify', 'HEAD'], ['rev-parse', '--verify', 'HEAD^{tree}']):
    print(('1' if args[-1] == 'HEAD' else '2') * 40)
elif name == 'file' and args == ['--', './bsc-relay']:
    print('./bsc-relay: mock ELF')
elif name == 'go' and args == ['version', '-m', './bsc-relay']:
    print('./bsc-relay: mock go1.26.11 CGO_ENABLED=1')
elif name == 'go' and args == ['tool', 'buildid', './bsc-relay']:
    print('mock-build-id')
elif name == 'readelf' and args == ['--version']:
    print('mock readelf version')
elif name == 'readelf' and args == ['-hW', './bsc-relay']:
    print('mock ELF header ET_EXEC')
elif name == 'readelf' and args == ['-lW', './bsc-relay']:
    print('mock GNU_STACK ' + os.getenv('STACK_FLAGS', 'RW'))
elif name == 'readelf' and args == ['-dW', './bsc-relay']:
    print('mock dynamic NEEDED libc.so.6')
    change = os.getenv('MUTATE')
    if change == 'content':
        body = bytearray(binary.read_bytes()); body[-1] ^= 1; binary.write_bytes(body)
    elif change == 'identity':
        replacement = pathlib.Path('replacement')
        replacement.write_bytes(binary.read_bytes())
        replacement.replace(binary)
    elif change == 'symlink':
        target = pathlib.Path('target'); target.write_bytes(binary.read_bytes())
        binary.unlink(); binary.symlink_to(target)
else:
    print('unrecognized fake command: ' + repr([name] + args), file=sys.stderr)
    sys.exit(98)
"""


class BinaryMetadataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        self.script = self.root / 'scripts/collect_binary_metadata.sh'
        shutil.copyfile(SCRIPT, self.script)
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        for name in TOOLS:
            tool = self.tools / name
            tool.write_text('#!' + sys.executable + '\n' + FAKE)
            tool.chmod(0o755)
        self.binary = self.root / 'bsc-relay'
        # Executable canary: any mistaken launch creates a marker and fails.
        self.binary.write_text('#!/bin/sh\nprintf executed > EXECUTED\nexit 97\n')
        self.binary.chmod(0o755)
        self.log = self.root / 'commands.jsonl'

    def collect(self, *args, **changes):
        # Do not inherit caller flags or lookup paths; all tools are fixtures.
        env = {'PATH': str(self.tools), 'FAKE_LOG': str(self.log),
               'GOTOOLCHAIN': 'auto', 'LANG': 'C', **changes}
        result = subprocess.run([BASH, str(self.script), *args], cwd=self.tools,
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertFalse((self.root / 'EXECUTED').exists(), 'relay was executed')
        return result

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_exact_read_only_commands_identity_and_output(self):
        digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        size = self.binary.stat().st_size
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r['argv'] for r in self.rows()], EXPECTED)
        self.assertTrue(all(r['cwd'] == str(self.root) and r['toolchain'] == 'local' for r in self.rows()))
        for text in ('1' * 40, '2' * 40, 'mock ELF', 'mock go1.26.11', 'mock-build-id',
                     'mock readelf version', 'mock GNU_STACK RW', 'mock dynamic',
                     f'binary_sha256_before={digest}', f'binary_sha256_after={digest}',
                     f'binary_bytes_before={size}', f'binary_bytes_after={size}', 'PASS: metadata'):
            self.assertIn(text, result.stdout)

    def test_executable_stack_is_recorded_not_classified(self):
        result = self.collect(STACK_FLAGS='RWE')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GNU_STACK RWE', result.stdout)
        self.assertIn('not a vulnerability verdict', result.stdout)

    def test_all_command_failures_are_explicit_and_stop_collection(self):
        for position in range(1, len(EXPECTED) + 1):
            with self.subTest(command=EXPECTED[position - 1]):
                self.log.unlink(missing_ok=True)
                result = self.collect(FAIL_AT=str(position))
                self.assertEqual(result.returncode, 23)
                self.assertIn('ERROR: metadata command failed (exit 23):', result.stderr)
                self.assertEqual(len(self.rows()), position)
                self.assertNotIn('PASS:', result.stdout)

    def test_each_missing_tool_fails_before_collection(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                tool = self.tools / name
                hidden = self.root / name
                tool.rename(hidden)
                try:
                    result = self.collect()
                finally:
                    hidden.rename(tool)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'required tool not found: {name}', result.stderr)
                self.assertEqual(self.rows(), [])

    def test_input_must_be_regular_not_symlink(self):
        self.binary.unlink()
        for kind in ('missing', 'directory', 'symlink', 'broken_symlink', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'directory': self.binary.mkdir()
                elif kind == 'symlink': self.binary.symlink_to(self.script)
                elif kind == 'broken_symlink': self.binary.symlink_to('absent')
                elif kind == 'fifo': os.mkfifo(self.binary)
                result = self.collect()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('regular, non-symlink', result.stderr)
                self.assertEqual(self.rows(), [])
                if kind == 'directory': self.binary.rmdir()
                else: self.binary.unlink(missing_ok=True)

    def test_rejects_input_arguments(self):
        result = self.collect('/another/binary')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no arguments accepted', result.stderr)
        self.assertEqual(self.rows(), [])

    def test_rejects_malformed_hash_or_size(self):
        for setting in ('INVALID_SIZE', 'INVALID_HASH'):
            with self.subTest(setting=setting):
                self.log.unlink(missing_ok=True)
                result = self.collect(**{setting: '1'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('invalid size or SHA256 output', result.stderr)
                self.assertNotIn('PASS:', result.stdout)

    def test_rejects_content_drift(self):
        result = self.collect(MUTATE='content')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('binary SHA256 changed', result.stderr)
        self.assertNotIn('PASS:', result.stdout)

    def test_rejects_same_content_replacement(self):
        result = self.collect(MUTATE='identity')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('binary identity or metadata changed', result.stderr)
        self.assertNotIn('PASS:', result.stdout)

    def test_rejects_final_symlink(self):
        result = self.collect(MUTATE='symlink')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('regular, non-symlink', result.stderr)
        self.assertNotIn('PASS:', result.stdout)

    def test_ci_uses_same_default_build_before_existing_smoke(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        expected = ('      - name: Build and check the Linux delivery artifact\n'
                    '        timeout-minutes: 4\n'
                    '        run: python3 scripts/linux_build.py build-linux-amd64 --output bsc-relay\n'
                    '      - name: Inspect the built relay without executing it\n'
                    '        timeout-minutes: 1\n'
                    '        run: bash scripts/collect_binary_metadata.sh\n'
                    '      - run: python3 scripts/smoke.py ./bsc-relay\n')
        self.assertIn(expected, workflow.split('  official_txpool:', 1)[0])
        self.assertEqual(workflow.count('run: bash scripts/collect_binary_metadata.sh'), 1)


if __name__ == '__main__':
    unittest.main()
