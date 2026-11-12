#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed Linux/amd64 build/test entry points; not a general release tool."""
import argparse
import contextlib
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import time

from elf_stack_policy import ELFError, inspect_file
from noexecstack_candidate import (ARCHIVE_SHA, ARCHIVE_SIZE, ENV_KEYS, HERUMI, HERUMI_SUM, LINK_POLICY,
                                  Runner, emit, regular_digest,
                                  require, tool_argv, tool_version, validate_buildinfo)

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODFILE = 'integration/officialtxpool/go.mod'
EXTRA_ENV = ['GOENV', 'GOWORK', 'GOEXPERIMENT', 'GOROOT', 'GOPATH', 'GOCACHE', 'GOMODCACHE']
KEYS = list(dict.fromkeys(ENV_KEYS + EXTRA_ENV))
OUTPUTS = ('bin/bsc-relay', 'bsc-relay')
OPT_IN_SKIPS = {'TestSoakMsgPipe', 'TestUniqueCapacityDefaultTTL', 'TestUniqueCapacityFailureReport'}


class BuildRunner(Runner):
    def __init__(self, root):
        super().__init__(root)
        # Preserve inherited GOENV/GOFLAGS/GOWORK for the first effective query.
        # Only prohibit implicit toolchain downloads before reading configuration.
        self.env = dict(os.environ, GOTOOLCHAIN='local')
        self.deadline = time.monotonic() + 600


def read_environment(runner):
    args = ['go', 'env', '-json', *KEYS]
    if os.name == 'posix':
        # GOFLAGS may contain arbitrary user strings. Do not print the raw
        # query or its failure output before the closed allowlist is checked.
        with contextlib.redirect_stdout(io.StringIO()):
            text = runner.run('effective-go-environment', args)
    else:
        # Non-POSIX is an unqualified development route. Inherit stdout for its
        # build, rather than using the Linux-only bounded process-group runner.
        # This fixed, short metadata query has a time limit, not a streaming cap.
        text = subprocess.check_output(args, cwd=runner.root, env=runner.env, timeout=15, text=True, stderr=subprocess.STDOUT)
    environment = json.loads(text)
    require(isinstance(environment, dict) and set(environment) == set(KEYS)
            and all(isinstance(environment.get(k), str) for k in KEYS),
            'incomplete effective Go environment')
    return environment


def validate_goflags(value, *, official):
    tokens = value.split()
    seen = set()
    for token in tokens:
        key, separator, argument = token.partition('=')
        require(separator and key not in seen, 'unsupported or duplicate GOFLAGS option (value withheld)')
        seen.add(key)
        allowed = token in ('-mod=readonly', '-buildvcs=false') or bool(re.fullmatch(r'-p=[1-8]', token))
        if official:
            allowed = allowed or token in ('-modfile=' + MODFILE, '-tags=officialtxpool')
        require(allowed, 'unsupported GOFLAGS option (value withheld)')
    return value


def qualify(runner, environment, *, official=False):
    expected = {'GOVERSION': 'go1.26.11', 'GOTOOLCHAIN': 'local', 'GOOS': 'linux', 'GOARCH': 'amd64',
                'GOHOSTOS': 'linux', 'GOHOSTARCH': 'amd64', 'CGO_ENABLED': '1', 'GOAMD64': 'v1', 'GOEXPERIMENT': ''}
    for key, value in expected.items():
        require(environment[key] == value, f'unsupported {key}: {environment[key]!r}; qualified lane is native Go 1.26.11 Linux/amd64 CGO=1 GOAMD64=v1')
    require(environment['GOWORK'] in ('', 'off'), 'active Go workspace is not supported')
    validate_goflags(environment['GOFLAGS'], official=official)
    for key in ('CGO_CFLAGS', 'CGO_CXXFLAGS', 'CGO_FFLAGS', 'CGO_LDFLAGS'):
        require(environment[key] == '-O2 -g', 'unsupported custom ' + key)
    require(environment['CGO_CPPFLAGS'] == '', 'unsupported custom CGO_CPPFLAGS')
    for key in ('CC', 'CXX', 'AR'):
        require(len(tool_argv(environment[key])) == 1, 'compiler/archiver wrapper arguments are not supported: ' + key)
    emit('effective_configuration_before_normalization', environment)
    # Preserve the reviewed effective values, including configured local caches,
    # before disabling further GOENV/workspace/module-resolution changes.
    for key in ('GOOS', 'GOARCH', 'CGO_ENABLED', 'GOAMD64', 'GOEXPERIMENT', 'CC', 'CXX', 'AR',
                'CGO_CFLAGS', 'CGO_CPPFLAGS', 'CGO_CXXFLAGS', 'CGO_FFLAGS', 'CGO_LDFLAGS',
                'GOROOT', 'GOPATH', 'GOCACHE', 'GOMODCACHE', 'GOFLAGS'):
        runner.env[key] = environment[key]
    runner.env.update(GOTOOLCHAIN='local', GOENV='off', GOWORK='off', GOTELEMETRY='off',
                      GOPROXY='off', GOSUMDB='off', GOPRIVATE='', GONOPROXY='', GONOSUMDB='')
    emit('module_resolution', 'disabled; prepare pinned modules with go mod download before this entry point')
    runner.run('go-version', ['go', 'version'])
    for label, args in [('cc', tool_argv(environment['CC'])), ('cxx', tool_argv(environment['CXX'])),
                        ('ar', tool_argv(environment['AR'])), ('readelf', ['readelf']), ('file', ['file'])]:
        tool_version(runner, label, args)
    linker = runner.run('cc-selected-linker', tool_argv(environment['CC']) + ['-print-prog-name=ld']).strip()
    require(linker and '\n' not in linker, 'missing or ambiguous driver-selected linker')
    tool_version(runner, 'driver-selected-ld', [linker])
    module = json.loads(runner.run('herumi-module', ['go', 'list', '-mod=readonly', '-m', '-json', HERUMI]))
    require(module.get('Path') == HERUMI and module.get('Version') == 'v1.31.0'
            and module.get('Sum') == HERUMI_SUM and not module.get('Replace') and module.get('Dir'),
            'unexpected Herumi module identity')
    archive = pathlib.Path(module['Dir']) / 'bls/lib/linux/amd64/libbls384_256.a'
    archive_before = regular_digest(archive)
    require(archive_before['sha256'] == ARCHIVE_SHA and archive_before['bytes'] == ARCHIVE_SIZE,
            'Herumi archive differs from the recorded pinned bytes')
    emit('herumi_archive_before', archive_before)
    return archive, archive_before


def check_archive(native):
    archive, before = native
    require(regular_digest(archive) == before, 'Herumi archive changed during the operation')
    emit('herumi_archive_after', before)


def source_identity(runner):
    identity = {}
    for key, arg in [('commit', 'HEAD'), ('tree', 'HEAD^{tree}')]:
        value = runner.run('source-' + key, ['git', 'rev-parse', '--verify', arg]).strip()
        require(bool(re.fullmatch('[0-9a-f]{40}', value)), 'invalid source ' + key)
        identity[key] = value
    identity['status'] = runner.run('source-status', ['git', 'status', '--porcelain', '--untracked-files=normal'])
    identity['clean_before_build'] = not identity['status'].strip()
    identity['binding'] = 'checkout context only; not an exact compiled-source attestation'
    emit('source', identity)
    return identity


def official_buildinfo(text, binary):
    lines = text.splitlines()
    require(lines and lines[0] == f'{binary}: go1.26.11', 'official buildinfo: wrong artifact or version')
    require([line for line in lines if line.startswith('\tpath\t')] ==
            ['\tpath\tgithub.com/keienWang/bsc-p2p-relay/internal/relay.test'], 'official buildinfo: wrong package')
    settings = {}
    for line in lines:
        if line.startswith('\tbuild\t'):
            key, separator, value = line[len('\tbuild\t'):].partition('=')
            require(separator and key and key not in settings, 'official buildinfo: malformed/duplicate setting')
            settings[key] = value
    expected = {'-buildmode': 'exe', '-compiler': 'gc', 'CGO_ENABLED': '1', 'GOOS': 'linux',
                'GOARCH': 'amd64', '-race': 'true', '-tags': 'officialtxpool', '-ldflags': LINK_POLICY}
    for key, value in expected.items():
        require(settings.get(key) == value, 'official buildinfo: missing or unexpected ' + key)
    require('-trimpath' not in settings, 'official test binary unexpectedly trimmed')
    return settings


def metadata(runner, binary, *, official=False):
    before = inspect_file(binary)
    emit('artifact_before', before)
    for name, argv in [('file', ['file', '--', binary]), ('buildinfo', ['go', 'version', '-m', binary]),
                       ('buildid', ['go', 'tool', 'buildid', binary]), ('header', ['readelf', '-hW', binary]),
                       ('programs', ['readelf', '-lW', binary]), ('dynamic', ['readelf', '-dW', binary])]:
        output = runner.run('artifact-' + name, argv)
        require(output.strip(), 'missing artifact metadata: ' + name)
        if name == 'buildinfo':
            contract = official_buildinfo(output, binary) if official else validate_buildinfo(output, binary, trimpath=True)
            emit('buildinfo_contract', contract)
    require(inspect_file(binary) == before, 'artifact changed during metadata collection')
    emit('artifact_after', before)
    return before


def existing_output(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(mode), 'existing output must be a regular non-symlink file')
    return regular_digest(path)


def build(runner, output, source, native):
    target = runner.root / output
    require(output in OUTPUTS, 'unsupported output path')
    # Only fixed root/bin destinations are supported. Do not follow a bin symlink.
    require(not target.parent.is_symlink(), 'output parent must not be a symlink')
    target.parent.mkdir(exist_ok=True)
    old = existing_output(target)
    # A private temporary directory on the destination filesystem keeps the
    # install atomic. No build/check failure replaces the previous good artifact.
    with tempfile.TemporaryDirectory(prefix='.relay-build-', dir=target.parent) as temp:
        staged = pathlib.Path(temp) / 'bsc-relay'
        argv = ['go', 'build', '-mod=readonly', '-trimpath', '-buildvcs=false',
                '-ldflags=' + LINK_POLICY, '-o', str(staged), './cmd/bsc-relay']
        runner.run('qualified-relay-build', argv, 180)
        artifact = metadata(runner, staged)
        require(staged.stat().st_mode & 0o111, 'built artifact is not executable')
        require(existing_output(target) == old, 'output changed concurrently; not replacing it')
        require(inspect_file(staged) == artifact, 'staged artifact changed before install')
        check_archive(native)
        # All policy checks precede replacement. There is no fallback to an old
        # binary or unchecked build on failure. A rename is not a runtime test.
        os.replace(staged, target)
    emit('installed', {'source': source, 'output': output, 'sha256': artifact['sha256'], 'bytes': artifact['bytes'],
                       'gnu_stack_flags': artifact['gnu_stack_flags'], 'load_flags': artifact['load_flags'],
                       'build_argv': argv, 'link_policy': LINK_POLICY, 'runtime_executed': False})


def validate_official_output(output):
    lines = [line for line in output.splitlines() if line]
    name = 'TestOfficialBSCTxPoolLoopback'
    starts = [i for i, line in enumerate(lines) if line == '=== RUN   ' + name]
    passes = [i for i, line in enumerate(lines) if re.fullmatch(r'--- PASS: ' + name + r' \([0-9]+(?:\.[0-9]+)?s\)', line)]
    require(len(starts) == len(passes) == 1 and starts[0] < passes[0], 'official loopback requires one RUN then PASS')
    require(lines[-1] == 'PASS' and lines.count('PASS') == 1, 'official test package missing final PASS')
    require(not re.search(r'(?m)^\s*--- FAIL:|^FAIL(?:\s|$)|no tests to run', output), 'official test package reported failure or zero match')
    skipped = re.findall(r'(?m)^\s*--- SKIP: (\S+)', output)
    require(set(skipped) <= OPT_IN_SKIPS, 'unexpected skipped test in official package')
    return skipped


def official_test(runner, source, native):
    with tempfile.TemporaryDirectory(prefix='relay-official-', dir=runner.env.get('RUNNER_TEMP')) as temp:
        binary = pathlib.Path(temp) / 'relay.test'
        argv = ['go', 'test', '-c', '-race', '-mod=readonly', '-modfile=' + MODFILE, '-tags=officialtxpool',
                '-ldflags=' + LINK_POLICY, '-o', str(binary), './internal/relay']
        runner.run('official-package-build', argv, 180)
        artifact = metadata(runner, binary, official=True)
        # No -test.run filter: retain the original entire tagged relay package.
        execute = [str(binary), '-test.v', '-test.count=1', '-test.timeout=180s']
        root = runner.root
        try:
            # go test executes this package from its source directory.
            runner.root = root / 'internal/relay'
            emit('official_run_directory', str(runner.root))
            output = runner.run('official-package-all-tests', execute, 200)
        finally:
            runner.root = root
        skipped = validate_official_output(output)
        require(inspect_file(binary) == artifact, 'official test artifact changed during execution')
        check_archive(native)
        emit('official_result', {'source': source, 'artifact': artifact, 'build_argv': argv, 'run_argv': execute,
                                'link_policy': LINK_POLICY, 'opt_in_skips': skipped, 'whole_relay_package': True,
                                'canonical_child_covered': False})


def dispatch(runner, action, output):
    # Leave at least 30 seconds before each CI outer step deadline for cleanup.
    budget = {'build': 210, 'build-linux-amd64': 210, 'race': 330, 'official-test': 450}
    require(action in budget and output in OUTPUTS, 'unsupported operation or output')
    runner.deadline = min(runner.deadline, time.monotonic() + budget[action])
    environment = read_environment(runner)
    linux_target = (environment['GOOS'], environment['GOARCH']) == ('linux', 'amd64')
    if action == 'build' and not linux_target:
        emit('development_build', 'target is outside Linux/amd64; no ELF hardening guarantee or Linux linker flags')
        target = runner.root / output
        target.parent.mkdir(exist_ok=True)
        argv = ['go', 'build', '-trimpath', '-buildvcs=false', '-o', str(target), './cmd/bsc-relay']
        if os.name == 'posix':
            runner.run('development-build', argv, 180)
        else:
            subprocess.run(argv, cwd=runner.root, env=runner.env, check=True, timeout=180)
        return
    require(os.name == 'posix', 'qualified entry requires a native Linux host')
    native = qualify(runner, environment, official=action == 'official-test')
    source = source_identity(runner)
    if action in ('build', 'build-linux-amd64'):
        build(runner, output, source, native)
    elif action == 'race':
        runner.run('default-race-same-policy', ['go', 'test', '-race', '-mod=readonly', '-ldflags=' + LINK_POLICY,
                   '-count=1', '-timeout=180s', './...'], 240)
        check_archive(native)
        emit('default_race_result', {'source': source, 'link_policy': LINK_POLICY, 'canonical_child_covered': False})
    elif action == 'official-test':
        official_test(runner, source, native)
    else:
        raise ValueError('unsupported action')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'build-linux-amd64', 'race', 'official-test'))
    parser.add_argument('--output', choices=OUTPUTS, default=OUTPUTS[0])
    args = parser.parse_args()
    dispatch(BuildRunner(ROOT), args.action, args.output)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ELFError) as error:
        sys.exit(f'ERROR: Linux build/test entry incomplete or failed; no unchecked fallback: {error}')
