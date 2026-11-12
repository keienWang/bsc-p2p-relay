# SPDX-License-Identifier: GPL-3.0-or-later
"""Fake tool outputs and synthetic ELF bytes; no Go/native commands execute."""
import contextlib
import hashlib
import io
import json
import pathlib
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

import noexecstack_candidate as candidate
from test_elf_noexecstack import elf_image


def buildinfo(binary, *, trimpath):
    # Relay settings reflect the first hosted Go 1.26.11 -trimpath output.
    # Non-trimmed test settings follow pinned load/pkg.go and load/test.go.
    path = 'cmd/bsc-relay' if trimpath else 'internal/nativecompat.test'
    settings = ['-buildmode=exe', '-compiler=gc', 'CGO_ENABLED=1', 'GOOS=linux', 'GOARCH=amd64', 'GOAMD64=v1']
    settings += ['-trimpath=true'] if trimpath else ['-ldflags=' + candidate.LINK_POLICY, '-race=true', '-tags=noexecstack',
                  'CGO_CFLAGS="-O2 -g"', 'CGO_CPPFLAGS=', 'CGO_CXXFLAGS="-O2 -g"', 'CGO_LDFLAGS="-O2 -g"']
    return f'{binary}: go1.26.11\n\tpath\tgithub.com/keienWang/bsc-p2p-relay/{path}\n' + ''.join(
        '\tbuild\t' + setting + '\n' for setting in settings)


class FakeRunner(candidate.Runner):
    def __init__(self, root, baseline_stack=7, candidate_stack=6, fail='', empty='', mutate='', untracked='', buildinfo_replace=None):
        super().__init__(root)
        self.calls = []
        self.baseline_stack = baseline_stack
        self.candidate_stack = candidate_stack
        self.fail, self.empty, self.mutate = fail, empty, mutate
        self.untracked = untracked
        self.buildinfo_replace = buildinfo_replace or {}
        self.archive = root / 'module/bls/lib/linux/amd64/libbls384_256.a'
        self.archive.parent.mkdir(parents=True)
        self.archive.write_bytes(b'fake archive only')

    def run(self, label, args, seconds=15):
        args = [str(x) for x in args]
        self.calls.append((label, args, seconds))
        if label == self.fail: raise ValueError('deliberate command failure: ' + label)
        if label == self.empty: return ''
        if label in ('source-commit', 'source-commit-after'): return '1' * 40 + '\n'
        if label in ('source-tree', 'source-tree-after'): return '2' * 40 + '\n'
        if label in ('untracked-before', 'untracked-after'): return self.untracked
        if label == 'go-environment':
            env = dict.fromkeys(candidate.ENV_KEYS, '')
            env.update(GOVERSION='go1.26.11', GOOS='linux', GOARCH='amd64', GOHOSTOS='linux',
                       GOHOSTARCH='amd64', CGO_ENABLED='1', CC='gcc', CXX='g++', AR='ar')
            return json.dumps(env)
        if label == 'cc-selected-linker': return '/fake/ld\n'
        if label == 'herumi-module':
            return json.dumps({'Path': candidate.HERUMI, 'Version': 'v1.31.0',
                               'Sum': candidate.HERUMI_SUM, 'Dir': str(self.root / 'module')})
        if label == 'herumi-archive-members': return '\n'.join(candidate.MEMBERS) + '\n'
        if label in ('baseline-build', 'candidate-build', 'herumi-test-build'):
            output = pathlib.Path(args[args.index('-o') + 1])
            output.write_bytes(elf_image(stack_flags=self.baseline_stack if label == 'baseline-build' else self.candidate_stack))
        if label == self.mutate:
            output = pathlib.Path(args[-1]); data = bytearray(output.read_bytes())
            data[-1] ^= 1; output.write_bytes(data)
        if label.endswith('-buildinfo'):
            text = buildinfo(args[-1], trimpath=label != 'herumi_fixture-buildinfo')
            if label in self.buildinfo_replace:
                text = text.replace(*self.buildinfo_replace[label])
            return text
        if label == 'herumi-public-vectors': return '=== RUN   TestHerumiPublicVectors\n--- PASS: TestHerumiPublicVectors (0.01s)\nPASS\n'
        return 'mock output\n'


class HarnessNoexecstackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.work = self.root / 'outputs'; self.work.mkdir()

    def exercise(self, **options):
        runner = FakeRunner(self.root, **options)
        with mock.patch.object(candidate, 'ARCHIVE_SHA', hashlib.sha256(runner.archive.read_bytes()).hexdigest()), \
             mock.patch.object(candidate, 'ARCHIVE_SIZE', runner.archive.stat().st_size), \
             mock.patch.object(candidate, 'tool_version', side_effect=lambda r, label, args: r.run(label + '-version', args + ['--version'])), \
             mock.patch.object(candidate.platform, 'system', return_value='Linux'), \
             mock.patch.object(candidate.platform, 'machine', return_value='x86_64'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            candidate.experiment(runner, self.work)
        return runner, output.getvalue()

    def test_exact_link_policy_on_both_regression_builds_and_default_suite(self):
        runner, output = self.exercise()
        calls = {label: (args, limit) for label, args, limit in runner.calls}
        baseline = calls['baseline-build'][0]
        variant = calls['candidate-build'][0]
        self.assertEqual(baseline[:4], ['go', 'build', '-trimpath', '-buildvcs=false'])
        self.assertNotIn('-ldflags=' + candidate.LINK_POLICY, baseline)
        self.assertEqual(variant[:5], baseline[:4] + ['-ldflags=' + candidate.LINK_POLICY])
        self.assertEqual(variant[5:], ['-o', str(self.work / 'relay-noexecstack'), './cmd/bsc-relay'])
        self.assertEqual(calls['default-race-same-policy'], (['go', 'test', '-race', '-ldflags=' + candidate.LINK_POLICY,
                         '-count=1', '-timeout=180s', './...'], 240))
        self.assertEqual(calls['herumi-test-build'][0], ['go', 'test', '-c', '-race', '-tags=noexecstack',
                         '-ldflags=' + candidate.LINK_POLICY, '-o', str(self.work / 'herumi-public-vectors.test'), './internal/nativecompat'])
        self.assertEqual(calls['candidate-loopback-smoke'][0][-1], str(self.work / 'relay-noexecstack'))
        labels = [x[0] for x in runner.calls]
        self.assertLess(labels.index('herumi_fixture-elf-programs'), labels.index('herumi-public-vectors'))
        self.assertIn('"canonical_child_worker_covered": false', output)
        receipt = json.loads(next(line[len('result='):] for line in output.splitlines() if line.startswith('result=')))
        self.assertEqual(receipt['build_commands'], {'baseline': baseline, 'candidate': variant,
                         'herumi_fixture': calls['herumi-test-build'][0]})
        self.assertTrue(all(limit <= 240 for _, _, limit in runner.calls))
        self.assertFalse(any('canonicalisolation' in ' '.join(args) for _, args, _ in runner.calls))

    def test_trimmed_buildinfo_uses_observable_fields_not_an_omitted_ldflags_string(self):
        text = buildinfo('relay', trimpath=True)
        self.assertNotIn('-ldflags', text)
        result = candidate.validate_buildinfo(text, 'relay', trimpath=True)
        self.assertEqual(result['settings']['-trimpath'], 'true')
        self.assertIn('trimpath omits ldflags', result['linker_policy_evidence'])
        fixture = candidate.validate_buildinfo(buildinfo('fixture', trimpath=False), 'fixture', trimpath=False)
        self.assertEqual(fixture['settings']['-ldflags'], candidate.LINK_POLICY)
        self.assertEqual(fixture['settings']['CGO_CPPFLAGS'], '')

    def test_buildinfo_identity_settings_and_trimpath_must_match(self):
        text = buildinfo('relay', trimpath=True)
        invalid = [text.replace('relay: go1.26.11', 'other: go1.26.11'),
                   text.replace('go1.26.11', 'go1.26.10'), text.replace('cmd/bsc-relay', 'other'),
                   text.replace('\tpath\t', '\tignored\t'), text + '\tpath\tduplicate\n',
                   text.replace('\tbuild\t-compiler=gc\n', ''), text.replace('CGO_ENABLED=1', 'CGO_ENABLED=0'), text.replace('CGO_ENABLED=1', 'CGO_ENABLED='),
                   text.replace('GOARCH=amd64', 'GOARCH=arm64'), text.replace('GOOS=linux', 'GOOS=darwin'),
                   text.replace('-buildmode=exe', '-buildmode=pie'), text.replace('-trimpath=true', '-trimpath=false'),
                   text.replace('\tbuild\t-trimpath=true\n', ''), text + '\tbuild\tGOOS=linux\n',
                   text + '\tbuild\tbroken\n', text + '\tbuild\t-ldflags=' + candidate.LINK_POLICY + '\n']
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate.validate_buildinfo(value, 'relay', trimpath=True)

    def test_native_buildinfo_requires_exact_policy_not_a_substring_or_trimpath(self):
        text = buildinfo('fixture', trimpath=False)
        invalid = [text.replace('\tbuild\t-ldflags=' + candidate.LINK_POLICY + '\n', ''),
                   text.replace(candidate.LINK_POLICY, candidate.LINK_POLICY + ',execstack'),
                   text.replace('-race=true', '-race=false'), text.replace('-tags=noexecstack', '-tags=other'),
                   text + '\tbuild\t-trimpath=true\n', text + '\tbuild\t-ldflags=' + candidate.LINK_POLICY + '\n',
                   text.replace('\tbuild\t-ldflags=', '\tdep\t-ldflags=')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate.validate_buildinfo(value, 'fixture', trimpath=False)

    def test_invalid_candidate_or_fixture_buildinfo_stops_before_execution(self):
        for label, replacement in [('candidate-buildinfo', ('GOOS=linux', 'GOOS=darwin')),
                                   ('herumi_fixture-buildinfo', ('-ldflags=' + candidate.LINK_POLICY, '-unrelated=value'))]:
            with self.subTest(label=label):
                runner = FakeRunner(self.root / label, buildinfo_replace={label: replacement})
                with mock.patch.object(candidate, 'ARCHIVE_SHA', hashlib.sha256(runner.archive.read_bytes()).hexdigest()), \
                     mock.patch.object(candidate, 'ARCHIVE_SIZE', runner.archive.stat().st_size), \
                     mock.patch.object(candidate, 'tool_version'), \
                     mock.patch.object(candidate.platform, 'system', return_value='Linux'), \
                     mock.patch.object(candidate.platform, 'machine', return_value='x86_64'), \
                     contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(ValueError):
                    candidate.experiment(runner, self.work)
                labels = [call[0] for call in runner.calls]
                self.assertNotIn('herumi-public-vectors', labels)
                self.assertNotIn('candidate-loopback-smoke', labels)
                self.assertNotIn('result=', output.getvalue())
                if label == 'candidate-buildinfo':
                    self.assertNotIn('default-race-same-policy', labels)

    def test_baseline_already_nonexecutable_is_not_a_failure(self):
        _, output = self.exercise(baseline_stack=6)
        self.assertIn('result=', output)

    def test_executable_candidate_fails_before_regressions(self):
        with self.assertRaisesRegex(ValueError, 'GNU_STACK'):
            self.exercise(candidate_stack=7)

    def test_missing_metadata_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'missing metadata'):
            self.exercise(empty='candidate-elf-programs')

    def test_hash_drift_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'changed during metadata'):
            self.exercise(mutate='candidate-elf-dynamic')

    def test_command_failure_does_not_produce_result(self):
        with self.assertRaisesRegex(ValueError, 'deliberate command failure'):
            self.exercise(fail='default-race-same-policy')

    def test_native_tool_versions_record_resolved_bytes(self):
        tool = self.root / 'fake-tool'
        tool.write_bytes(b'fixture tool, never executed')
        runner = mock.Mock(env={'PATH': 'fake'})
        runner.run.return_value = 'fake version\n'
        with mock.patch.object(candidate.shutil, 'which', return_value=str(tool)), contextlib.redirect_stdout(io.StringIO()) as output:
            candidate.tool_version(runner, 'cc', ['gcc'])
        runner.run.assert_called_once_with('cc-version', ['gcc', '--version'])
        self.assertIn(hashlib.sha256(tool.read_bytes()).hexdigest(), output.getvalue())
        with mock.patch.object(candidate.shutil, 'which', return_value=None), self.assertRaisesRegex(ValueError, 'missing native tool'):
            candidate.tool_version(runner, 'cc', ['gcc'])

    def test_missing_native_test_pass_marker_is_not_success(self):
        with self.assertRaisesRegex(ValueError, 'named RUN then PASS'):
            self.exercise(empty='herumi-public-vectors')

    def test_native_event_contract_rejects_skips_failures_zero_matches_and_duplicates(self):
        valid = '=== RUN   TestHerumiPublicVectors\n--- PASS: TestHerumiPublicVectors (0.01s)\nPASS\n'
        candidate.validate_native_output(valid)
        invalid = ['', 'PASS\n', valid.replace('=== RUN   TestHerumiPublicVectors\n', ''),
                   valid.replace('--- PASS:', '--- SKIP:'), valid.replace('--- PASS:', '--- FAIL:'),
                   valid.replace('TestHerumiPublicVectors', 'OtherTest'),
                   valid + 'PASS\n', valid.replace('PASS\n', 'FAIL\n'),
                   'testing: warning: no tests to run\n' + valid,
                   '=== RUN   OtherTest\n' + valid, '=== RUN   TestHerumiPublicVectors\n' + valid,
                   '--- SKIP: OtherTest (0.00s)\n' + valid,
                   '--- PASS: TestHerumiPublicVectors (0.01s)\n=== RUN   TestHerumiPublicVectors\nPASS\n']
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(ValueError):
                candidate.validate_native_output(output)

    def test_untracked_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'untracked files before'):
            self.exercise(untracked='unexpected.go\n')

    def test_runner_environment_has_no_toolchain_or_module_downloads(self):
        runner = candidate.Runner(self.root)
        for key, value in [('GOTOOLCHAIN', 'local'), ('GOPROXY', 'off'), ('GOSUMDB', 'off'),
                           ('GOENV', 'off'), ('GOWORK', 'off'), ('GOFLAGS', '-mod=readonly')]:
            self.assertEqual(runner.env[key], value)
        process = mock.Mock(returncode=0, pid=12345)
        with mock.patch.object(candidate.subprocess, 'Popen', return_value=process) as popen, \
             mock.patch.object(candidate, 'capture_output', return_value=b'version output\n') as capture, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run('fake', ['not-executed'], 3), 'version output\n')
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(capture.call_args.args[2], candidate.MAX_COMMAND_OUTPUT)
        self.assertEqual(runner.output_used, len(b'version output\n'))

    def test_timeout_stops_only_created_process_group_with_bounded_reaping(self):
        process = mock.Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired('fake', 5), 0]
        with mock.patch.object(candidate.subprocess, 'Popen', return_value=process), \
             mock.patch.object(candidate, 'capture_output', side_effect=candidate.CaptureFailure('deadline exceeded', b'prefix')), \
             mock.patch.object(candidate.os, 'killpg') as kill, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, 'owned group; command reaped'):
                candidate.Runner(self.root).run('fake', ['not-executed'], 1)
        self.assertEqual(kill.call_args_list, [mock.call(12345, signal.SIGTERM), mock.call(12345, signal.SIGKILL)])
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=5), mock.call(timeout=5)])
        self.assertIn('prefix', output.getvalue())

    def test_output_limit_preserves_only_bounded_prefix_and_fails(self):
        process = mock.Mock(pid=12345)
        process.stdout.fileno.return_value = 42
        selector = mock.Mock()
        selector.select.return_value = [(object(), 1)]
        with mock.patch.object(candidate.os, 'set_blocking'), \
             mock.patch.object(candidate.os, 'read', return_value=b'abcdef'), \
             mock.patch.object(candidate.selectors, 'DefaultSelector') as factory:
            factory.return_value.__enter__.return_value = selector
            with self.assertRaises(candidate.CaptureFailure) as raised:
                candidate.capture_output(process, candidate.time.monotonic() + 1, 5)
        self.assertEqual(raised.exception.prefix, b'abcde')
        runner = candidate.Runner(self.root)
        with mock.patch.object(candidate.subprocess, 'Popen', return_value=process), \
             mock.patch.object(candidate, 'capture_output', side_effect=raised.exception), \
             mock.patch.object(candidate, 'stop_process_group', return_value='bounded cleanup') as stop, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, 'output cap exceeded'):
                runner.run('fake', ['not-executed'])
        stop.assert_called_once_with(process)
        self.assertEqual(runner.output_used, 5)
        self.assertIn('abcde', output.getvalue())
        self.assertNotIn('abcdef', output.getvalue())

    def test_bounded_stream_reads_eof_and_reaps_with_timeout(self):
        process = mock.Mock(pid=12345)
        process.stdout.fileno.return_value = 42
        process.poll.return_value = 0
        selector = mock.Mock(); selector.select.return_value = [(object(), 1)]
        with mock.patch.object(candidate.os, 'set_blocking') as blocking, \
             mock.patch.object(candidate.os, 'read', side_effect=[b'hello', b'']), \
             mock.patch.object(candidate.selectors, 'DefaultSelector') as factory:
            factory.return_value.__enter__.return_value = selector
            self.assertEqual(candidate.capture_output(process, candidate.time.monotonic() + 1, 5), b'hello')
        blocking.assert_called_once_with(42, False)
        self.assertGreater(process.wait.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(process.wait.call_args.kwargs['timeout'], 5)

    def test_unconfirmed_cleanup_is_bounded_and_not_called_success(self):
        process = mock.Mock(pid=12345)
        process.wait.side_effect = subprocess.TimeoutExpired('fake', 5)
        with mock.patch.object(candidate.os, 'killpg'):
            self.assertIn('not confirmed', candidate.stop_process_group(process))
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(all(c.kwargs == {'timeout': 5} for c in process.wait.call_args_list))

    def test_shared_output_cap_prevents_additional_commands(self):
        runner = candidate.Runner(self.root); runner.output_used = candidate.MAX_SHARED_OUTPUT
        with mock.patch.object(candidate.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'shared output cap exhausted'):
                runner.run('fake', ['not-executed'])
        popen.assert_not_called()

    def test_deadline_prevents_any_command(self):
        runner = candidate.Runner(self.root); runner.deadline = 0
        with mock.patch.object(candidate.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'deadline exhausted'): runner.run('fake', ['not-executed'])
        popen.assert_not_called()

    def test_public_fixture_has_no_key_creation_or_signing_api(self):
        source = (candidate.ROOT / 'internal/nativecompat/herumi_test.go').read_text()
        self.assertTrue(source.startswith('//go:build noexecstack && linux && amd64 && cgo\n'))
        for forbidden in ('SecretKey', 'SetByCSPRNG', 'SignByte(', '.Sign(', 't.Parallel', 'prysmaticlabs'):
            self.assertNotIn(forbidden, source)
        for api in ('Deserialize(', 'Serialize()', 'IsValidOrder()', '.Aggregate(', '.Add(', '.VerifyByte(', '.FastAggregateVerify('):
            self.assertIn(api, source)
        vectors = json.loads((candidate.ROOT / 'internal/nativecompat/testdata/herumi_public_vectors.json').read_text())
        self.assertEqual(len(vectors), 2)
        for vector in vectors:
            self.assertEqual(set(vector), {'pub', 'msg', 'sig', 'out'})
            self.assertEqual(vector['out'], 'true')
            self.assertEqual([len(bytes.fromhex(vector[k])) for k in ('pub', 'msg', 'sig')], [48, 32, 96])
        self.assertEqual(vectors[0]['msg'], vectors[1]['msg'])
        self.assertNotEqual(vectors[0]['pub'], vectors[1]['pub'])

    def test_workflow_is_separate_bounded_linux_research(self):
        workflow = (candidate.ROOT / '.github/workflows/ci.yml').read_text()
        old, rest = workflow.split('  noexecstack_candidate:\n')
        job, scanner = rest.split('  scanner_synthetic:\n', 1)
        self.assertIn('go build -trimpath -buildvcs=false ./cmd/bsc-relay', old)
        self.assertNotIn(candidate.LINK_POLICY, old)
        for text in ('runs-on: ubuntu-24.04', 'timeout-minutes: 15', "go-version: '1.26.11'",
                     "test_*noexecstack*.py", 'timeout-minutes: 3', 'run: go mod download',
                     'timeout-minutes: 12', 'run: bash scripts/run_noexecstack_candidate.sh'):
            self.assertIn(text, job)
        for forbidden in ('upload-artifact', 'sudo', 'unshare', 'curl', 'wget'):
            self.assertNotIn(forbidden, job)


if __name__ == '__main__':
    unittest.main()
