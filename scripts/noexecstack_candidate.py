# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded Linux/amd64-only hosted experiment, not the delivery build."""
import hashlib
import json
import os
import pathlib
import platform
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from elf_stack_policy import ELFError, inspect_file, read_regular

ROOT = pathlib.Path(__file__).resolve().parents[1]
LINK_POLICY = '-extldflags=-Wl,-z,noexecstack'
HERUMI = 'github.com/herumi/bls-eth-go-binary'
HERUMI_SUM = 'h1:9eeW3EA4epCb7FIHt2luENpAW69MvKGL5jieHlBiP+w='
ARCHIVE_SHA = '498c90b7e3f3cf7a819b2bfd0b619162d8c38e1a29bf8d924821eaba616c9873'
ARCHIVE_SIZE = 916592
MEMBERS = ['bls_c384_256.o', 'fp.o', 'static_code.o', 'bint64.o', 'base64.o']
MAX_COMMAND_OUTPUT = 4 * 1024 * 1024
MAX_SHARED_OUTPUT = 16 * 1024 * 1024
ENV_KEYS = ['GOVERSION', 'GOOS', 'GOARCH', 'GOHOSTOS', 'GOHOSTARCH', 'GOAMD64',
            'CGO_ENABLED', 'CC', 'CXX', 'AR', 'CGO_CFLAGS', 'CGO_CPPFLAGS',
            'CGO_CXXFLAGS', 'CGO_FFLAGS', 'CGO_LDFLAGS', 'GOFLAGS', 'GOTOOLCHAIN']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def emit(label, value):
    print(label + '=' + json.dumps(value, sort_keys=True), flush=True)


class CaptureFailure(RuntimeError):
    def __init__(self, reason, prefix):
        super().__init__(reason)
        self.prefix = prefix


def capture_output(process, deadline, limit):
    prefix = bytearray()
    try:
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            opened = True
            while opened or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CaptureFailure('command deadline exceeded', bytes(prefix))
                for key, mask in selector.select(min(0.1, remaining)):
                    try:
                        chunk = os.read(fd, min(65536, limit - len(prefix) + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(fd)
                        opened = False
                        continue
                    room = limit - len(prefix)
                    prefix.extend(chunk[:room])
                    if len(chunk) > room:
                        raise CaptureFailure('command/shared output cap exceeded', bytes(prefix))
            try:
                process.wait(timeout=min(5, max(0.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                raise CaptureFailure('command final wait exceeded deadline', bytes(prefix))
        return bytes(prefix)

    except CaptureFailure:
        raise
    except Exception as error:
        raise CaptureFailure(f'output capture failed: {error}', bytes(prefix)) from error


def stop_process_group(process):
    # This session belongs only to the command started below. Close the reader
    # before bounded waits; do not keep consuming output from a failed command.
    process.stdout.close()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                return 'termination/reaping not confirmed within bounded cleanup'
    return 'termination signals sent to owned group; command reaped'


class Runner:
    def __init__(self, root):
        self.root = root
        self.deadline = time.monotonic() + 660
        self.output_used = 0
        self.env = dict(os.environ, GOTOOLCHAIN='local', GOWORK='off', GOENV='off',
                        GOTELEMETRY='off', GOMAXPROCS='2', GOFLAGS='-mod=readonly',
                        GOPROXY='off', GOSUMDB='off', GOPRIVATE='', GONOPROXY='', GONOSUMDB='')

    def run(self, label, args, seconds=15):
        args = [str(x) for x in args]
        budget = min(seconds, self.deadline - time.monotonic())
        require(budget > 0, 'shared experiment deadline exhausted')
        output_limit = min(MAX_COMMAND_OUTPUT, MAX_SHARED_OUTPUT - self.output_used)
        require(output_limit > 0, 'shared output cap exhausted')
        emit('command', {'label': label, 'argv': args, 'timeout_seconds': round(budget, 3),
                         'combined_output_cap_bytes': output_limit})
        process = subprocess.Popen(args, cwd=self.root, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=0, start_new_session=True)
        try:
            raw = capture_output(process, time.monotonic() + budget, output_limit)
        except BaseException as error:
            prefix = getattr(error, 'prefix', b'')
            self.output_used += len(prefix)
            print(prefix.decode('utf-8', errors='replace'), end='', flush=True)
            cleanup = stop_process_group(process)
            emit('command_failed', {'label': label, 'reason': str(error), 'cleanup': cleanup,
                                   'captured_prefix_bytes': len(prefix),
                                   'captured_prefix_sha256': hashlib.sha256(prefix).hexdigest()})
            raise RuntimeError(f'{label}: {error}; {cleanup}') from error
        finally:
            process.stdout.close()
        self.output_used += len(raw)
        output = raw.decode('utf-8', errors='replace')
        print(output, end='', flush=True)
        emit('command_exit', {'label': label, 'exit': process.returncode, 'captured_bytes': len(raw)})
        require(process.returncode == 0, f'{label}: command failed with exit {process.returncode}')
        return output


def regular_digest(path):
    data, identity = read_regular(path)
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'identity': identity}


def tool_argv(value):
    args = shlex.split(value)
    require(args, 'empty native tool command')
    return args


def tool_version(runner, label, args):
    executable = shutil.which(args[0], path=runner.env.get('PATH'))
    require(executable is not None, f'missing native tool: {args[0]}')
    resolved = pathlib.Path(executable).resolve(strict=True)
    emit('native_tool', {'name': label, 'argv': args, 'resolved_path': str(resolved), **regular_digest(resolved)})
    text = runner.run(label + '-version', args + ['--version'])
    require(text.strip(), f'{label}: missing version output')


def validate_buildinfo(text, binary, *, trimpath):
    """Check observable Go 1.26.11 settings, not flags deliberately omitted by Go."""
    lines = text.splitlines()
    require(lines and lines[0] == f'{binary}: go1.26.11', 'buildinfo: wrong artifact or Go version')
    expected_path = 'github.com/keienWang/bsc-p2p-relay/' + (
        'cmd/bsc-relay' if trimpath else 'internal/nativecompat.test')
    paths = [line[len('\tpath\t'):] for line in lines if line.startswith('\tpath\t')]
    require(paths == [expected_path], 'buildinfo: missing, duplicate or wrong package path')
    settings = {}
    for line in lines:
        if not line.startswith('\tbuild\t'):
            continue
        key, separator, value = line[len('\tbuild\t'):].partition('=')
        require(separator and key and key not in settings, 'buildinfo: malformed or duplicate setting')
        settings[key] = value
    expected = {'-buildmode': 'exe', '-compiler': 'gc', 'CGO_ENABLED': '1', 'GOOS': 'linux', 'GOARCH': 'amd64'}
    for key, value in expected.items():
        require(settings.get(key) == value, 'buildinfo: missing or unexpected ' + key)
    if trimpath:
        require(settings.get('-trimpath') == 'true', 'buildinfo: relay requires trimpath=true')
        # Go 1.26.11 load/pkg.go omits -ldflags under -trimpath (issue 52372).
        # The exact build argv and final ELF/hash, not this omitted field, bind policy.
        require('-ldflags' not in settings, 'buildinfo: trimmed relay unexpectedly records ldflags')
    else:
        require('-trimpath' not in settings, 'buildinfo: native fixture unexpectedly trimmed')
        require(settings.get('-ldflags') == LINK_POLICY, 'buildinfo: native fixture missing exact linker policy')
        require(settings.get('-race') == 'true' and settings.get('-tags') == 'noexecstack',
                'buildinfo: native fixture missing race or exact research tag')
    return {'path': expected_path, 'settings': settings,
            'linker_policy_evidence': 'recorded build argv and checked ELF; trimpath omits ldflags'
            if trimpath else 'exact recorded ldflags plus build argv and checked ELF'}


def metadata(runner, label, binary, enforce, *, trimpath):
    before = inspect_file(binary, enforce=enforce)
    emit(label + '_before', before)
    for name, args in [
        ('file', ['file', '--', binary]),
        ('buildinfo', ['go', 'version', '-m', binary]),
        ('buildid', ['go', 'tool', 'buildid', binary]),
        ('elf-header', ['readelf', '-hW', binary]),
        ('elf-programs', ['readelf', '-lW', binary]),
        ('elf-dynamic', ['readelf', '-dW', binary]),
    ]:
        text = runner.run(label + '-' + name, args)
        require(text.strip(), f'{label}/{name}: missing metadata output')
        if name == 'buildinfo':
            emit(label + '_buildinfo_contract', validate_buildinfo(text, binary, trimpath=trimpath))
    after = inspect_file(binary, enforce=enforce)
    require(after == before, label + ': binary changed during metadata collection')
    emit(label + '_after', after)
    return after


def validate_native_output(output):
    lines = [line for line in output.splitlines() if line]
    events = [line for line in lines if re.match(r'^(=== (RUN|PAUSE|CONT)|--- (PASS|FAIL|SKIP))', line)]
    require(len(events) == 2 and events[0] == '=== RUN   TestHerumiPublicVectors'
            and re.fullmatch(r'--- PASS: TestHerumiPublicVectors \([0-9]+(?:\.[0-9]+)?s\)', events[1]),
            'Herumi fixture requires exactly its named RUN then PASS events')
    require(lines[-1] == 'PASS' and lines.count('PASS') == 1
            and lines.index(events[0]) < lines.index(events[1]) < len(lines) - 1,
            'Herumi fixture missing unique final PASS')
    require(not re.search(r'\b(SKIP|FAIL)\b|no tests to run', output, re.IGNORECASE),
            'Herumi fixture reported skip/fail/zero-match output')


def experiment(runner, work):
    require(platform.system() == 'Linux' and platform.machine() in ('x86_64', 'amd64'), 'host must be Linux/amd64')
    source = runner.run('source-commit', ['git', 'rev-parse', '--verify', 'HEAD']).strip()
    tree = runner.run('source-tree', ['git', 'rev-parse', '--verify', 'HEAD^{tree}']).strip()
    require(len(source) == 40 and len(tree) == 40 and all(c in '0123456789abcdef' for c in source + tree), 'invalid source identity')
    runner.run('clean-source-before', ['git', 'diff', '--exit-code', 'HEAD'])
    require(not runner.run('untracked-before', ['git', 'ls-files', '--others', '--exclude-standard']).strip(), 'untracked files before experiment')
    runner.run('go-version', ['go', 'version'])
    environment = json.loads(runner.run('go-environment', ['go', 'env', '-json', *ENV_KEYS]))
    for key, value in {'GOVERSION': 'go1.26.11', 'GOOS': 'linux', 'GOARCH': 'amd64',
                       'GOHOSTOS': 'linux', 'GOHOSTARCH': 'amd64', 'CGO_ENABLED': '1'}.items():
        require(environment.get(key) == value, f'unsupported {key}: {environment.get(key)}')
    for key in ENV_KEYS:
        require(key in environment, 'missing Go environment field: ' + key)
    cc, cxx, ar = (tool_argv(environment[k]) for k in ('CC', 'CXX', 'AR'))
    for label, args in [('cc', cc), ('cxx', cxx), ('ar', ar), ('readelf', ['readelf']), ('file', ['file'])]:
        tool_version(runner, label, args)
    # Driver-selected linker, not an attestation of a complete link command.
    linker = runner.run('cc-selected-linker', cc + ['-print-prog-name=ld']).strip()
    require(linker and '\n' not in linker, 'missing/ambiguous driver-selected linker')
    tool_version(runner, 'driver-selected-ld', [linker])
    module = json.loads(runner.run('herumi-module', ['go', 'list', '-m', '-json', HERUMI]))
    require(module.get('Path') == HERUMI and module.get('Version') == 'v1.31.0'
            and module.get('Sum') == HERUMI_SUM and not module.get('Replace'), 'unexpected Herumi module identity')
    require(module.get('Dir'), 'missing Herumi module directory')
    archive = pathlib.Path(module['Dir']) / 'bls/lib/linux/amd64/libbls384_256.a'
    archive_before = regular_digest(archive)
    emit('herumi_archive_before', {'path': str(archive), **archive_before})
    require(archive_before['sha256'] == ARCHIVE_SHA and archive_before['bytes'] == ARCHIVE_SIZE, 'Herumi archive differs from recorded pinned bytes')
    members = runner.run('herumi-archive-members', ar + ['t', str(archive)]).splitlines()
    require(members == MEMBERS, 'unexpected Herumi archive members')
    require(runner.run('herumi-archive-sections', ['readelf', '-SW', archive]).strip(), 'missing archive section output')

    baseline = work / 'relay-baseline'
    candidate = work / 'relay-noexecstack'
    common = ['go', 'build', '-trimpath', '-buildvcs=false']
    baseline_command = common + ['-o', str(baseline), './cmd/bsc-relay']
    candidate_command = common + ['-ldflags=' + LINK_POLICY, '-o', str(candidate), './cmd/bsc-relay']
    runner.run('baseline-build', baseline_command, 120)
    baseline_info = metadata(runner, 'baseline', baseline, False, trimpath=True)
    runner.run('candidate-build', candidate_command, 120)
    candidate_info = metadata(runner, 'candidate', candidate, True, trimpath=True)
    runner.run('default-race-same-policy', ['go', 'test', '-race', '-ldflags=' + LINK_POLICY,
               '-count=1', '-timeout=180s', './...'], 240)
    fixture = work / 'herumi-public-vectors.test'
    fixture_command = ['go', 'test', '-c', '-race', '-tags=noexecstack',
                       '-ldflags=' + LINK_POLICY, '-o', str(fixture), './internal/nativecompat']
    runner.run('herumi-test-build', fixture_command, 90)
    fixture_info = metadata(runner, 'herumi_fixture', fixture, True, trimpath=False)
    native_output = runner.run('herumi-public-vectors', [fixture, '-test.v', '-test.run=^TestHerumiPublicVectors$',
               '-test.count=1', '-test.timeout=20s'], 30)
    validate_native_output(native_output)
    require(inspect_file(fixture) == fixture_info, 'Herumi test artifact changed during execution')
    runner.run('candidate-loopback-smoke', ['python3', 'scripts/smoke.py', candidate], 30)
    require(inspect_file(candidate) == candidate_info, 'candidate changed during regressions')
    require(inspect_file(baseline, enforce=False) == baseline_info, 'baseline changed during regressions')
    require(regular_digest(archive) == archive_before, 'Herumi archive changed during experiment')
    emit('herumi_archive_after', archive_before)
    require(runner.run('source-commit-after', ['git', 'rev-parse', '--verify', 'HEAD']).strip() == source, 'source commit changed')
    require(runner.run('source-tree-after', ['git', 'rev-parse', '--verify', 'HEAD^{tree}']).strip() == tree, 'source tree changed')
    runner.run('clean-source-after', ['git', 'diff', '--exit-code', 'HEAD'])
    require(not runner.run('untracked-after', ['git', 'ls-files', '--others', '--exclude-standard']).strip(), 'untracked files after experiment')
    emit('result', {'source_commit': source, 'source_tree': tree, 'link_policy': LINK_POLICY,
                   'baseline': baseline_info, 'candidate': candidate_info, 'herumi_fixture': fixture_info,
                   'build_commands': {'baseline': baseline_command, 'candidate': candidate_command, 'herumi_fixture': fixture_command},
                   'default_race_same_policy': True, 'herumi_public_vectors_same_policy': True,
                   'loopback_smoke_same_candidate': True, 'canonical_child_worker_covered': False,
                   'scope': 'bounded Linux/amd64 experiment; not native safety or production approval'})


def main():
    require(len(sys.argv) == 1, 'no arguments accepted')
    with tempfile.TemporaryDirectory(prefix='relay-noexecstack-', dir=os.environ.get('RUNNER_TEMP')) as temp:
        experiment(Runner(ROOT), pathlib.Path(temp))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, ELFError) as error:
        sys.exit(f'ERROR: noexecstack experiment incomplete or failed: {error}')
