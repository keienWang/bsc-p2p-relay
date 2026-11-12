# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure mock contracts. No Go/native tool or relay/test binary executes."""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import linux_build as build
from test_elf_noexecstack import elf_image
from test_harness_noexecstack import buildinfo


def environment():
    env = dict.fromkeys(build.KEYS, '')
    env.update(GOVERSION='go1.26.11', GOTOOLCHAIN='local', GOOS='linux', GOARCH='amd64', GOHOSTOS='linux',
               GOHOSTARCH='amd64', CGO_ENABLED='1', GOAMD64='v1', CC='gcc', CXX='g++', AR='ar',
               GOENV='/fake/go/env', GOWORK='', GOROOT='/fake/go', GOPATH='/fake/path',
               GOCACHE='/fake/cache', GOMODCACHE='/fake/modules')
    for key in ('CGO_CFLAGS', 'CGO_CXXFLAGS', 'CGO_FFLAGS', 'CGO_LDFLAGS'):
        env[key] = '-O2 -g'
    return env


OFFICIAL_PASS = ('=== RUN   TestOther\n--- PASS: TestOther (0.01s)\n'
                 '=== RUN   TestOfficialBSCTxPoolLoopback\n--- PASS: TestOfficialBSCTxPoolLoopback (35.12s)\n'
                 '=== RUN   TestSoakMsgPipe\n--- SKIP: TestSoakMsgPipe (0.00s)\nPASS\n')


class FakeRunner(build.BuildRunner):
    def __init__(self, root, env=None, *, failure='', stack=6, mutate=False, event=OFFICIAL_PASS):
        super().__init__(root)
        self.environment = env or environment()
        self.calls = []
        self.cwds = {}
        self.archive = root / 'module/bls/lib/linux/amd64/libbls384_256.a'
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_bytes(b'fake native archive')
        self.failure, self.stack, self.mutate, self.event = failure, stack, mutate, event

    def run(self, label, argv, seconds=15):
        argv = [str(arg) for arg in argv]
        self.calls.append((label, argv, seconds))
        self.cwds[label] = self.root
        if label == self.failure:
            raise RuntimeError('mock command failure')
        if label == 'effective-go-environment':
            print(json.dumps(self.environment))
            return json.dumps(self.environment)
        if label == 'cc-selected-linker': return 'ld\n'
        if label == 'herumi-module': return json.dumps({'Path': build.HERUMI, 'Version': 'v1.31.0', 'Sum': build.HERUMI_SUM, 'Dir': str(self.root / 'module')})
        if label == 'source-commit': return '1' * 40 + '\n'
        if label == 'source-tree': return '2' * 40 + '\n'
        if label == 'source-status': return ''
        if label in ('qualified-relay-build', 'official-package-build'):
            binary = pathlib.Path(argv[argv.index('-o') + 1]); binary.write_bytes(elf_image(stack_flags=self.stack)); binary.chmod(0o755)
        if label == 'artifact-buildinfo':
            official = argv[-1].endswith('relay.test')
            result = buildinfo(argv[-1], trimpath=not official)
            if official:
                result = result.replace('internal/nativecompat.test', 'internal/relay.test').replace('-tags=noexecstack', '-tags=officialtxpool')
            return result
        if label == 'artifact-dynamic' and self.mutate:
            binary = pathlib.Path(argv[-1]); binary.write_bytes(binary.read_bytes()[:-1] + b'X')
        if label == 'official-package-all-tests': return self.event
        return 'mock metadata\n'


class LinuxBuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.old = self.root / 'bsc-relay'; self.old.write_bytes(b'previous good binary')
        for key, value in [('ARCHIVE_SHA', hashlib.sha256(b'fake native archive').hexdigest()), ('ARCHIVE_SIZE', len(b'fake native archive'))]:
            patch = mock.patch.object(build, key, value); patch.start(); self.addCleanup(patch.stop)

    def exercise(self, action='build', **options):
        runner = FakeRunner(self.root, **options)
        with mock.patch.object(build, 'tool_version'), contextlib.redirect_stdout(io.StringIO()) as output:
            build.dispatch(runner, action, 'bsc-relay')
        return runner, output.getvalue()

    def test_initial_query_keeps_conflicting_settings_until_checked_and_never_downloads_toolchain(self):
        with mock.patch.dict(os.environ, {'GOFLAGS': '-overlay=private-value', 'GOENV': '/private/env',
                                         'GOWORK': '/private/work', 'GOTOOLCHAIN': 'auto'}, clear=True):
            runner = build.BuildRunner(self.root)
        self.assertEqual(runner.env['GOTOOLCHAIN'], 'local')
        self.assertEqual(runner.env['GOFLAGS'], '-overlay=private-value')
        self.assertEqual(runner.env['GOENV'], '/private/env')
        self.assertEqual(runner.env['GOWORK'], '/private/work')
        self.assertNotIn('GOPROXY', runner.env)

    def test_raw_goenv_and_rejected_flag_values_are_not_printed(self):
        env = environment(); env['GOFLAGS'] = '-ldflags=do-not-print-secret'
        runner = FakeRunner(self.root, env)
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(ValueError) as raised:
            build.dispatch(runner, 'build', 'bsc-relay')
        self.assertNotIn('do-not-print-secret', str(raised.exception) + output.getvalue())
        self.assertEqual([x[0] for x in runner.calls], ['effective-go-environment'])

    def test_environment_query_rejects_missing_or_unrequested_fields_without_dumping(self):
        for changed in ('missing', 'extra'):
            env = environment()
            if changed == 'missing': del env['GOENV']
            else: env['UNREQUESTED_SECRET'] = 'do-not-print'
            runner = FakeRunner(self.root, env)
            with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(ValueError):
                build.read_environment(runner)
            self.assertNotIn('do-not-print', output.getvalue())

    def test_goflags_closed_allowlist_rejects_wrappers_overlays_conflicts_and_unknowns(self):
        for value in ('', '-mod=readonly -buildvcs=false -p=2'):
            build.validate_goflags(value, official=False)
        build.validate_goflags('-mod=readonly -modfile=' + build.MODFILE + ' -tags=officialtxpool', official=True)
        for value in ('-ldflags=-extldflags=-zexecstack', '-overlay=anything', '-toolexec=wrapper',
                      '-mod=mod', '-modfile=other', '-tags=officialtxpool', '-race', '-p=99',
                      '-p=1 -p=2', '-gcflags=all=-N', "'-mod=readonly'", '-tags=canonicalisolation'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build.validate_goflags(value, official=False)
        for value in ('-modfile=other', '-tags=officialtxpool,extra'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build.validate_goflags(value, official=True)

    def test_unsupported_native_environment_fails_before_build_and_does_not_override(self):
        for key, value in [('GOVERSION', 'go1.26.10'), ('GOHOSTOS', 'darwin'), ('GOHOSTARCH', 'arm64'),
                           ('CGO_ENABLED', '0'), ('GOAMD64', 'v3'), ('GOEXPERIMENT', 'experiment'),
                           ('GOWORK', '/other/go.work'), ('CGO_LDFLAGS', '-Wl,-z,execstack'),
                           ('CGO_CPPFLAGS', '-Doverride'), ('CC', 'ccache gcc')]:
            env = environment(); env[key] = value
            runner = FakeRunner(self.root, env); before = dict(runner.env)
            with self.subTest(key=key), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                build.dispatch(runner, 'build-linux-amd64', 'bsc-relay')
            self.assertEqual(runner.env, before)
            self.assertEqual(self.old.read_bytes(), b'previous good binary')

    def test_normalization_preserves_reviewed_values_and_disables_module_resolution(self):
        env = environment(); env['GOFLAGS'] = '-mod=readonly -p=2'
        runner = FakeRunner(self.root, env)
        with mock.patch.object(build, 'tool_version'), contextlib.redirect_stdout(io.StringIO()):
            build.qualify(runner, env)
        for key in ('CC', 'CGO_CFLAGS', 'GOFLAGS', 'GOCACHE', 'GOMODCACHE'):
            self.assertEqual(runner.env[key], env[key])
        for key in ('GOENV', 'GOWORK', 'GOPROXY', 'GOSUMDB'):
            self.assertEqual(runner.env[key], 'off')
        self.assertEqual(runner.env['GOTOOLCHAIN'], 'local')

    def test_linux_default_and_explicit_alias_build_check_then_atomically_replace(self):
        for action in ('build', 'build-linux-amd64'):
            self.old.write_bytes(b'previous good binary')
            actual_replace = os.replace
            def replace(src, dst):
                self.assertEqual(pathlib.Path(dst).read_bytes(), b'previous good binary')
                self.assertEqual(pathlib.Path(src).parent.parent, self.root)
                actual_replace(src, dst)
            with mock.patch.object(build.os, 'replace', side_effect=replace) as renamed:
                runner, output = self.exercise(action)
            renamed.assert_called_once()
            self.assertEqual(self.old.read_bytes(), elf_image())
            labels = [x[0] for x in runner.calls]
            self.assertLess(labels.index('qualified-relay-build'), labels.index('artifact-buildinfo'))
            command = next(x[1] for x in runner.calls if x[0] == 'qualified-relay-build')
            self.assertEqual(command[:6], ['go', 'build', '-mod=readonly', '-trimpath', '-buildvcs=false', '-ldflags=' + build.LINK_POLICY])
            self.assertIn('installed=', output)
            self.assertIn('not an exact compiled-source attestation', output)
            self.assertFalse(list(self.root.glob('.relay-build-*')))

    def test_build_metadata_and_elf_failures_preserve_previous_binary_and_cleanup(self):
        for options in ({'failure': 'qualified-relay-build'}, {'failure': 'artifact-buildinfo'},
                        {'failure': 'artifact-programs'}, {'stack': 7}, {'mutate': True}):
            with self.subTest(options=options), self.assertRaises((ValueError, RuntimeError)):
                self.exercise(**options)
            self.assertEqual(self.old.read_bytes(), b'previous good binary')
            self.assertFalse(list(self.root.glob('.relay-build-*')))

    def test_native_archive_mismatch_or_drift_rejects_install(self):
        with mock.patch.object(build, 'ARCHIVE_SHA', '0' * 64), self.assertRaisesRegex(ValueError, 'archive differs'):
            self.exercise()
        original = build.metadata
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.root / 'module/bls/lib/linux/amd64/libbls384_256.a').write_bytes(b'changed archive')
            return result
        with mock.patch.object(build, 'metadata', side_effect=changed), self.assertRaisesRegex(ValueError, 'archive changed'):
            self.exercise()
        self.assertEqual(self.old.read_bytes(), b'previous good binary')

    def test_failed_atomic_replace_preserves_old_artifact(self):
        with mock.patch.object(build.os, 'replace', side_effect=OSError('mock rename failure')), self.assertRaises(OSError):
            self.exercise()
        self.assertEqual(self.old.read_bytes(), b'previous good binary')
        self.assertFalse(list(self.root.glob('.relay-build-*')))

    def test_symlink_output_and_parent_are_rejected(self):
        target = self.root / 'other'; target.write_bytes(b'not touched')
        self.old.unlink(); self.old.symlink_to(target)
        with self.assertRaises(ValueError): self.exercise()
        self.assertEqual(target.read_bytes(), b'not touched')
        (self.root / 'bin').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): build.build(FakeRunner(self.root), 'bin/bsc-relay', {}, None)

    def test_concurrently_changed_destination_is_not_overwritten(self):
        original = build.metadata
        def altered(*args, **kwargs):
            result = original(*args, **kwargs)
            self.old.write_bytes(b'newer independently built binary')
            return result
        with mock.patch.object(build, 'metadata', side_effect=altered), self.assertRaisesRegex(ValueError, 'concurrently'):
            self.exercise()
        self.assertEqual(self.old.read_bytes(), b'newer independently built binary')

    def test_other_targets_keep_development_build_without_linux_flags(self):
        for goos, goarch in [('darwin', 'arm64'), ('windows', 'amd64'), ('linux', 'arm64')]:
            env = environment(); env.update(GOOS=goos, GOARCH=goarch)
            runner, output = self.exercise(env=env)
            argv = next(x[1] for x in runner.calls if x[0] == 'development-build')
            self.assertFalse(any('ldflags' in arg for arg in argv))
            self.assertNotIn('artifact-buildinfo', [x[0] for x in runner.calls])
            self.assertIn('no ELF hardening guarantee', output)
            with self.assertRaises(ValueError): self.exercise('build-linux-amd64', env=env)

    def test_default_race_uses_explicit_policy_without_research_tag_or_global_injection(self):
        runner, output = self.exercise('race')
        argv = next(x[1] for x in runner.calls if x[0] == 'default-race-same-policy')
        self.assertEqual(argv, ['go', 'test', '-race', '-mod=readonly', '-ldflags=' + build.LINK_POLICY, '-count=1', '-timeout=180s', './...'])
        self.assertNotIn('ldflags', runner.env['GOFLAGS'])
        self.assertIn('default_race_result=', output)

    def test_official_runs_whole_alternate_graph_binary_after_elf_check(self):
        env = environment(); env['GOFLAGS'] = '-buildvcs=false -mod=readonly -modfile=' + build.MODFILE + ' -tags=officialtxpool'
        runner, output = self.exercise('official-test', env=env)
        calls = {name: argv for name, argv, _ in runner.calls}
        argv = calls['official-package-build']
        for token in ('-c', '-race', '-modfile=' + build.MODFILE, '-tags=officialtxpool', '-ldflags=' + build.LINK_POLICY):
            self.assertIn(token, argv)
        self.assertEqual(argv[-1], './internal/relay')
        execution = calls['official-package-all-tests']
        self.assertEqual(execution[1:], ['-test.v', '-test.count=1', '-test.timeout=180s'])
        self.assertFalse(any('run=' in item for item in argv + execution))
        labels = [x[0] for x in runner.calls]
        self.assertLess(labels.index('artifact-dynamic'), labels.index('official-package-all-tests'))
        self.assertEqual(runner.cwds['official-package-all-tests'], self.root / 'internal/relay')
        self.assertEqual(runner.root, self.root)
        self.assertIn('official_result=', output)
        self.assertIn('TestSoakMsgPipe', output)

    def test_official_buildinfo_has_its_own_package_and_tag_contract(self):
        text = buildinfo('fixture', trimpath=False).replace('internal/nativecompat.test', 'internal/relay.test').replace('-tags=noexecstack', '-tags=officialtxpool')
        build.official_buildinfo(text, 'fixture')
        for invalid in (text.replace('internal/relay.test', 'internal/nativecompat.test'),
                        text.replace('-tags=officialtxpool', '-tags=noexecstack'),
                        text.replace(build.LINK_POLICY, build.LINK_POLICY + ',execstack'),
                        text + '\tbuild\tGOOS=linux\n', text + '\tbuild\t-trimpath=true\n'):
            with self.assertRaises(ValueError): build.official_buildinfo(invalid, 'fixture')

    def test_official_events_require_named_pass_whole_package_pass_and_only_known_opt_in_skips(self):
        self.assertEqual(build.validate_official_output(OFFICIAL_PASS), ['TestSoakMsgPipe'])
        # Existing OTel regression logs an indented child-test PASS. It is not
        # a second top-level package PASS and must not break full-package runs.
        self.assertEqual(build.validate_official_output('    otel_log_security_test.go:67: child output\n        PASS\n' + OFFICIAL_PASS), ['TestSoakMsgPipe'])
        for output in ('PASS\n', OFFICIAL_PASS.replace('--- PASS: TestOfficial', '--- SKIP: TestOfficial'),
                       OFFICIAL_PASS.replace('TestOfficialBSCTxPoolLoopback', 'UnrelatedTest'),
                       OFFICIAL_PASS.replace('TestSoakMsgPipe', 'UnexpectedSkip'), OFFICIAL_PASS + 'PASS\n',
                       'testing: warning: no tests to run\n' + OFFICIAL_PASS, '--- FAIL: Other (0s)\n' + OFFICIAL_PASS):
            with self.subTest(output=output), self.assertRaises(ValueError): build.validate_official_output(output)
        with self.assertRaises(ValueError): self.exercise('official-test', event='PASS\n')

    def test_missing_metadata_and_command_failure_remain_fail_closed(self):
        runner = mock.Mock(); runner.run.return_value = ''
        binary = self.root / 'fixture'; binary.write_bytes(elf_image())
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'missing artifact'):
            build.metadata(runner, binary)

    def test_make_readme_and_ci_use_shared_entry_and_preserve_other_jobs(self):
        make = (build.ROOT / 'Makefile').read_text()
        self.assertIn('build:\n\tpython3 scripts/linux_build.py build\n', make)
        self.assertIn('build-linux-amd64:\n\tpython3 scripts/linux_build.py build-linux-amd64\n', make)
        self.assertIn('race-linux-amd64:\n\tpython3 scripts/linux_build.py race\n', make)
        self.assertIn('race:\n\tgo test -race -count=1 -timeout=$(TEST_TIMEOUT) ./...\n', make)
        workflow = (build.ROOT / '.github/workflows/ci.yml').read_text()
        default, remainder = workflow.split('  official_txpool:', 1)
        official, remainder = remainder.split('  canonical_isolation:', 1)
        self.assertIn('python3 scripts/linux_build.py race', default)
        self.assertIn('python3 scripts/linux_build.py build-linux-amd64 --output bsc-relay', default)
        self.assertLess(default.index('build-linux-amd64 --output bsc-relay'), default.index('collect_binary_metadata.sh'))
        self.assertIn('python3 scripts/linux_build.py official-test', official)
        self.assertIn('run: go mod download', official)
        self.assertNotIn('linux_build.py', remainder)
        readme = (build.ROOT / 'README.md').read_text()
        self.assertIn('make build', readme)
        self.assertNotIn('go build -o bsc-p2p-relay ./cmd/bsc-relay', readme)


if __name__ == '__main__':
    unittest.main()
