# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure Python contract tests. No subprocess, Go or namespace is executed."""
import copy
import io
import hashlib
from contextlib import redirect_stdout
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
import scanner_synthetic_ci as h


class ScannerSyntheticContract(unittest.TestCase):
    def test_bundle_hashes_and_exact_scope(self):
        self.assertRegex(h.verify_bundle(), r"^[0-9a-f]{64}$")
        manifest = json.loads((h.BUNDLE / 'MANIFEST.json').read_text())
        for entry in manifest['files']:
            if entry['path'].startswith('patches/'):
                for line in h.file_bytes(h.BUNDLE, entry['path']).decode().splitlines():
                    if line.startswith('+++ b/'):
                        self.assertIn(line[6:], h.CHANGED)

    def test_fifth_patch_only_pins_existing_generic_fixture(self):
        self.assertEqual(len(h.PATCHES), 5)
        self.assertEqual([p[:4] for p in h.PATCHES], ["0001", "0002", "0003", "0004", "0005"])
        payload = (h.BUNDLE / "patches" / h.PATCHES[-1]).read_text()
        self.assertEqual([line for line in payload.splitlines() if line.startswith("+++ b/")],
                         ["+++ b/internal/vulncheck/source_identity_test.go"])
        self.assertNotIn("+func Test", payload)
        self.assertIn('module.GoVersion!="1.18"', payload)
        self.assertIn('filepath.Join(e.Temp(),"generic","go.mod")', payload)
        self.assertIn('info.Mode().IsRegular()', payload)

    def test_format_diff_matches_actual_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"; source.mkdir()
            state = Path(tmp) / "state"; state.mkdir()
            (source / "x.go").write_bytes(b"package x;\n")
            def formatter(cmd, **kwargs):
                self.assertNotIn("-d", cmd)
                if "-w" in cmd:
                    (source / "x.go").write_bytes(b"package x\n")
                return ""
            stdout = io.StringIO()
            with patch.object(h, "CHANGED", ("x.go",)), patch.object(h, "run", side_effect=formatter), redirect_stdout(stdout):
                before, after = h.normalize_sources(source, state, "/bin/go", {})
            self.assertEqual(stdout.getvalue().encode("utf-8"), (state / "format.diff").read_bytes())
            self.assertEqual(before, {"x.go": hashlib.sha256(b"package x;\n").hexdigest()})
            self.assertEqual(after, {"x.go": hashlib.sha256(b"package x\n").hexdigest()})
            self.assertEqual((state / "before-gofmt/x.go").read_bytes(), b"package x;\n")
            self.assertEqual((state / "format.diff").read_bytes(),
                             b"--- before/x.go\n+++ after/x.go\n@@ -1 +1 @@\n-package x;\n+package x\n")

    def test_normalization_command_failures_are_not_ignored(self):
        for outputs in ([ValueError("syntax error")], [ValueError("write error")],
                        ["", ValueError("listing error")], ["", "x.go\n"]):
            with self.subTest(outputs=outputs), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "source"; source.mkdir()
                state = Path(tmp) / "state"; state.mkdir()
                (source / "x.go").write_bytes(b"package x;\n")
                with patch.object(h, "CHANGED", ("x.go",)), patch.object(h, "run", side_effect=outputs):
                    with self.assertRaises(ValueError):
                        h.normalize_sources(source, state, "/bin/go", {})
                self.assertTrue((state / "before-gofmt/x.go").exists())
                self.assertFalse((state / "format.diff").exists())
                self.assertFalse((state / "prepared.json").exists())

    def test_format_errors_abort_without_receipt(self):
        for failure in ("syntax error", "write failure"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "source"; source.mkdir()
                state = Path(tmp) / "state"
                def command(cmd, **kwargs):
                    if "GOVERSION" in cmd: return h.GO_VERSION
                    if "rev-parse" in cmd: return h.UPSTREAM
                    return ""
                with patch.object(h.os, "geteuid", return_value=1001), \
                        patch.object(h.shutil, "which", return_value="/bin/go"), \
                        patch.object(h, "verify_bundle", return_value="digest"), \
                        patch.object(h, "file_bytes", return_value=json.dumps({"upstream_commit": h.UPSTREAM, "references": []}).encode()), \
                        patch.object(h, "CHANGED", ()), patch.object(h, "PATCHES", ()), \
                        patch.object(h, "run", side_effect=command), \
                        patch.object(h, "normalize_sources", side_effect=ValueError(failure)):
                    with self.assertRaisesRegex(ValueError, failure): h.prepare(source, state)
                self.assertFalse((state / "prepared.json").exists())

    def test_missing_changed_hash_input_fails(self):
        with self.assertRaises(ValueError):
            h.file_bytes(h.BUNDLE, '../LICENSE')
        with self.assertRaises(ValueError):
            h.file_bytes(h.BUNDLE, '/etc/passwd')
        with self.assertRaises(ValueError):
            h.file_bytes(h.BUNDLE, 'missing')

    def test_clean_environment_is_not_inherited(self):
        with patch.dict('os.environ', {'GITHUB_TOKEN': 'not-a-real-token', 'GOPROXY': 'https://invalid.example'}):
            env = h.clean_env('/pinned/bin/go', Path('/temporary/state'))
        self.assertNotIn('GITHUB_TOKEN', env)
        self.assertNotIn('GOPROXY', env)
        for name in ('GOENV', 'GOTELEMETRY', 'GOWORK'):
            self.assertEqual(env[name], 'off')
        self.assertEqual(env['GOTOOLCHAIN'], 'local')
        self.assertTrue(env['GOVULNDB'].startswith('file://'))

    def test_readonly_applies_only_to_outer_build(self):
        env = h.clean_env("/pinned/bin/go", Path("/state"))
        self.assertEqual(env["GOFLAGS"], "-buildvcs=false -p=1")
        cmd = h.selected_test_command("/pinned/bin/go", "./internal/scan", ["TestA"])
        self.assertEqual(cmd[:3], ["/pinned/bin/go", "test", "-mod=readonly"])
        self.assertEqual(cmd[-3:], ["-run", "^(TestA)$", "./internal/scan"])
        isolated = h.isolated_command(Path("/source"), Path("/state"),
                                      {"go": "/pinned/bin/go", "uid": 1001, "gid": 1001}, "net:[111]")
        self.assertIn("GOFLAGS=-buildvcs=false -p=1", isolated)
        self.assertFalse(any("-mod=readonly" in arg for arg in isolated))

    def test_namespace_command_has_no_online_fallback(self):
        cmd = h.isolated_command(Path('/source'), Path('/state'),
                                 {'go': '/pinned/bin/go', 'uid': 1001, 'gid': 1001}, 'net:[111]')
        self.assertEqual(cmd[:6], ['sudo', '-n', 'timeout', '--signal=TERM', '--kill-after=10s', '360s'])
        for arg in ('--net', '--pid', '--fork', '--mount-proc', '--kill-child=KILL',
                    '--clear-groups', '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all',
                    '--no-new-privs', '--reuid=1001', '--regid=1001', '-i', 'GOPROXY=off', 'GOSUMDB=off'):
            self.assertIn(arg, cmd)
        self.assertEqual(cmd[-3:], ['inner', '/source', '/state'])
        self.assertLess(cmd.index('unshare'), cmd.index('setpriv'))
        self.assertLess(cmd.index('setpriv'), cmd.index('env'))

    def isolation(self, **overrides):
        args = dict(status={'NoNewPrivs': '1', **{k: '0000000000000000' for k in
                     ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')}},
                    current_netns='net:[2]', host_netns='net:[1]',
                    links=[{'ifname': 'lo', 'flags': ['LOOPBACK']}], routes4=[], routes6=[], uid=1001, groups=[])
        args.update(overrides)
        return args

    def test_isolation_positive(self):
        h.verify_isolation(**self.isolation())

    def test_isolation_negative_boundaries(self):
        changes = ({'uid': 0}, {'groups': [998]}, {'current_netns': 'net:[1]'}, {'host_netns': ''},
                   {'routes4': [{'dst': 'default'}]}, {'routes6': [{'dst': 'default'}]},
                   {'links': [{'ifname': 'eth0', 'flags': []}]},
                   {'links': [{'ifname': 'lo', 'flags': ['UP']}]})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                h.verify_isolation(**self.isolation(**change))
        for field in ('NoNewPrivs', 'CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
            status = copy.deepcopy(self.isolation()['status'])
            status[field] = '0' if field == 'NoNewPrivs' else '1'
            with self.subTest(field=field), self.assertRaises(ValueError):
                h.verify_isolation(**self.isolation(status=status))

    def events(self, events):
        return '\n'.join(json.dumps(e) for e in events)

    def test_requires_every_selected_test_and_completion(self):
        good = [{'Action': 'pass', 'Test': 'TestA'}, {'Action': 'pass', 'Test': 'TestB'}, {'Action': 'pass'}]
        self.assertEqual(h.verify_test_events(self.events(good), ['TestA', 'TestB']), ['TestA', 'TestB'])
        for bad in (good[1:], good[:-1], [{'Action': 'skip', 'Test': 'TestA'}],
                    [{'Action': 'fail'}], [{'Action': 'pass'}]):
            with self.assertRaises(ValueError):
                h.verify_test_events(self.events(bad), ['TestA', 'TestB'])
        with self.assertRaises(ValueError):
            h.verify_test_events('not json', ['TestA'])

    def test_explicit_new_test_selection(self):
        names = [name for group in h.TESTS.values() for name in group]
        self.assertEqual(len(names), 11)
        self.assertEqual(len(set(names)), 11)
        self.assertEqual(set(h.TESTS), {'./internal/vulncheck', './internal/scan'})
        patch_text = '\n'.join((h.BUNDLE / 'patches' / p).read_text() for p in h.PATCHES)
        for name in names:
            self.assertEqual(patch_text.count('+func ' + name + '('), 1)

    def test_attempt_marker_prevents_failed_attempt_reuse(self):
        receipt = {'go': '/pinned/bin/go', 'uid': 1001, 'gid': 1001,
                   'bundle_manifest_sha256': 'test-manifest'}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with patch.object(h, 'read_receipt', return_value=receipt), \
                    patch.object(h, 'verify_changed_scope'), \
                    patch.object(h.shutil, 'which', return_value='/usr/bin/tool'), \
                    patch.object(h.os, 'readlink', return_value='net:[1]'), \
                    patch.object(h, 'run', side_effect=ValueError('simulated isolation failure')) as runner:
                with self.assertRaises(ValueError):
                    h.execute(Path('/source'), state)
                self.assertEqual(json.loads((state / 'outer-result.json').read_text()), {'passed': False})
                self.assertTrue((state / 'started.json').is_file())
                with self.assertRaisesRegex(ValueError, 'already started'):
                    h.execute(Path('/source'), state)
                self.assertEqual(runner.call_count, 1)

    def test_drift_outside_allowlist_is_rejected(self):
        with patch.object(h, 'run', side_effect=[h.UPSTREAM, '\n'.join(h.CHANGED), 'unexpected.go']):
            with self.assertRaisesRegex(ValueError, 'allowlist'):
                h.verify_changed_scope(Path('/source'), {})
        with patch.object(h, 'run', side_effect=['wrong-head']):
            with self.assertRaisesRegex(ValueError, 'HEAD changed'):
                h.verify_changed_scope(Path('/source'), {})

    def test_workflow_separate_job_and_no_scanner_execution(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text()
        job = workflow.split('\n  scanner_synthetic:', 1)[1].split('\n  bls_stage0_inventory:', 1)[0]
        self.assertIn('ref: ' + h.UPSTREAM, job)
        self.assertEqual(job.count('persist-credentials: false'), 2)
        self.assertIn('timeout-minutes: 15', job)
        self.assertNotIn('govulncheck"', job)
        self.assertNotIn('continue-on-error', job)
        self.assertIn('scanner_synthetic_ci.py prepare', job)
        self.assertIn('scanner_synthetic_ci.py run', job)


if __name__ == '__main__':
    unittest.main()
