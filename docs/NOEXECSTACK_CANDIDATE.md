# Linux/amd64 non-executable-stack research candidate

Status: candidate source and local mock checks only until an independently
reviewed hosted run supplies actual results. Default build flags, the existing
four CI jobs, dependencies, prebuilt archives and runtime code are unchanged.

## Planned scope and budget

A separate Ubuntu x64 job has a 15-minute total ceiling. Module preparation is
limited to 3 minutes. The research harness has an 11-minute shared deadline:
120 seconds for each of two relay builds, 240 seconds for the default race
suite, 90 seconds to compile the Herumi fixture, 30 seconds for its execution,
30 seconds for the existing loopback smoke, and short metadata commands.
The remaining shared budget takes precedence over any per-command limit.
Timeouts and excess output terminate the harness-owned process group.
Combined command stdout/stderr is capped at 4 MiB per command and 16 MiB
across the harness. A failure preserves its bounded output prefix and hash in
the job log. TERM and KILL cleanup waits are each limited to 5 seconds; failure
to confirm reaping is reported, never treated as success. A timeout is a failed experiment,
not an automatic budget increase or a passing partial result.

The two relay builds use the same source, platform and environment. The
candidate adds only `-ldflags=-extldflags=-Wl,-z,noexecstack` to the baseline
build, apart from its output filename. The baseline's stack flags are observed,
not required to be executable. The candidate must have exactly one GNU_STACK
without execute permission and no writable/executable LOAD segment. Missing,
ambiguous, unsupported or truncated ELF data fails closed.

The default race suite explicitly receives that same linker policy. A separate
build-tagged Herumi test uses only public keys, signatures and messages from
pinned upstream vectors. Its compiled test executable is inspected before use.
The existing smoke uses loopback with discovery disabled; it includes the
existing malformed `0x00` HTTP POST rejection check, not a signed transaction.
No wallet key, account, public peer, server or transaction broadcast is involved.

This does not cover the canonical-isolation child worker's independent linker
flags, tagged official-txpool tests, every native path, runtime memory maps,
target-hardware CPU dispatch, or the known Prysm cache race. An ELF noexecstack
flag is a linker policy override, not proof that every native path is compatible
or that the underlying missing assembly annotation has been corrected.

## Recorded observations, kept separate

The earlier [Herumi appendix](native-herumi/HERUMI_ARCHIVE_EVIDENCE.json) records
Go 1.26.10 executable SHA-256
`63db65d60fa5f6728f01854053c056c402ea240b453f16d55538f1e9e8366491`
with `GNU_STACK RW`. That observation is retained and is not applied to a new
compiler, linker, runner or artifact.

The later [default job 131261007984 in run 43240991880](https://github.com/keienWang/bsc-p2p-relay/actions/runs/43240991880/job/131261007984)
measured source commit
`ff92513cebf6b3bc27e3cc612654fa1f4fcd4d0d`, tree
`be18efcef86ceb2ff1525fda18260fd88772b67a`, on Ubuntu 24.04 runner image
`20261101.360.1`, using Go 1.26.11, CGO=1 and readelf 2.42. The exact original
command was `go build -trimpath -buildvcs=false ./cmd/bsc-relay`.

- Binary SHA-256: `afa71849f7d73969e55048218c6737d9044aec58a579ea540dd3d47e66ed4018`.
- Binary size: 50,729,184 bytes; before/after metadata digests matched.
- ELF: ET_EXEC, **GNU_STACK RWE**, LOAD flags R / RE / R / RW, GNU_RELRO present,
  no BIND_NOW/FLAGS/FLAGS_1, and NEEDED libc.so.6.
- Full default job log: 47,165 decoded bytes, SHA-256
  `a7d07b336bd48bf3bf18f30d4bfefa5c709b902617138ae7fac87775960b3399`.

The final RWE header was measured, not inferred from the missing
`.note.GNU-stack` warning. Actual host gcc/ld versions and runtime memory maps
were not captured in that run. Readelf's version is not a substitute for the
linker's version. These two historical observations do not isolate a Go
regression, demonstrate exploitability or establish the current main artifact's
permissions. The new candidate has no measured result merely because this
experiment has been added.

## What the new experiment records and enforces

The [harness](../scripts/noexecstack_candidate.py) records source commit/tree,
selected Go environment, compiler/C++ compiler/archiver/reader paths, binary
hashes and versions, and the linker's path/version reported by the compiler
driver. The last item is driver selection, not a captured complete link command.
The module identity and current linux/amd64 Herumi archive must match the pinned
v1.31.0 sum and the appendix's 916,592-byte archive SHA-256. Archive member and
section tables are printed without modifying or executing the archive.

For both relay outputs and the dedicated Herumi test executable, it records
SHA-256, size, Go buildinfo/build ID, `file`, and full `readelf -hW/-lW/-dW`
output. The candidate buildinfo must contain the exact linker policy. Regular,
non-symlink input checks and before/after file identity/hash checks reject drift.
The baseline is not executed. A baseline that already has RW stack permissions
is a valid observation, not a failed experiment.

The independent [ELF checker](../scripts/elf_stack_policy.py) directly parses
bounded ELF64, little-endian, x86-64 EXEC/DYN program headers. It rejects missing,
duplicate or extended/unsupported tables, truncated records, invalid file
extents and unknown relevant permission bits. It requires at least one LOAD and
exactly one GNU_STACK, then rejects any executable stack or W+X LOAD in a
candidate. It is a limited permission checker, not a complete ELF loader or
malicious-file security validator. Endpoint file checks are not an atomic
snapshot against a hostile writer.

Module preparation occurs in a separate, bounded ordinary CI step. The harness
uses GOTOOLCHAIN=local, GOENV/GOWORK=off, GOPROXY/GOSUMDB=off, an empty private
module override, GOFLAGS=-mod=readonly and GOMAXPROCS=2. It does not install tools
or run a downloader. This is not an OS-level network sandbox. Both compared
builds share these settings. Outputs are temporary and are not uploaded.

The default `go test -race` receives the same explicit linker policy, with no
research build tag. The separate Herumi test is compiled with `-race`,
`-tags=noexecstack` and the same policy, independently checked before execution,
and required to report exactly its named RUN then PASS events and final PASS,
without skip/fail/zero-match output. Existing unmodified jobs and
canonical subprocesses do not inherit this policy and are not counted as
candidate coverage. No default delivery build is changed.

## Public Herumi fixture provenance

The fixture calls the actual `github.com/herumi/bls-eth-go-binary/bls` API,
not Prysm's BLST adapter. Init uses BLS12-381 / EthModeDraft07; public-key and
signature order checks remain enabled. It checks byte round trips, valid
individual signatures, native point/signature aggregation and verification,
wrong-key/wrong-message rejection and truncated-point rejection. Initialization
and all operations are sequential in a dedicated process.

Two public records (1 and 5, lines 1-4 and 17-20) are reproduced as
[test data](../internal/nativecompat/testdata/herumi_public_vectors.json) from
[Herumi verify.txt at ecb2d6d6d613faa5de749699432721b8342a1798](https://github.com/herumi/bls-eth-go-binary/blob/ecb2d6d6d613faa5de749699432721b8342a1798/bls/tests/verify.txt).
Only `pub`, `msg`, `sig` and expected `out` fields are included. No private
scalar, signing API, random key generation or account data is used.

The fixed source files read to design this fixture were:

- [bls.go](https://github.com/herumi/bls-eth-go-binary/blob/ecb2d6d6d613faa5de749699432721b8342a1798/bls/bls.go),
  18,670 bytes, SHA-256 `8e4d7b669db47bf71ab9188dd7dcb0bd8073a05aea3cdc6b9ebf8ae3582f1b60`.
- [bls_test.go](https://github.com/herumi/bls-eth-go-binary/blob/ecb2d6d6d613faa5de749699432721b8342a1798/bls/bls_test.go),
  16,224 bytes, SHA-256 `ab9bf11b41670fd00721150e8a6af6e3aa3eeabd6fe586bf56da34e288a7c72d`.
- The complete upstream `verify.txt`, 10,547 bytes, SHA-256
  `bc16f646335bbe4f5c6b2c1414f334351729b31debbc7d493c13dc21f606ad58`.

These public test fields are attributed to the pinned Herumi distribution;
its [scoped source and notice evidence](native-herumi/README.md) retains its
stated limits. This fixture does not assign a new blanket license to upstream
files or complete the native distribution review.

## Verification limits

Local Python tests use synthetic ELF bytes and mocked tools, including failures,
unsupported/truncated inputs, executable permissions, timeouts and hash drift.
The Go test source and API relationships can be statically reviewed, but require
real hosted compilation/execution before they can be called passing. A successful
candidate run would establish only the recorded artifacts and exercised paths.
It would not repair the prebuilt archive's missing annotation, prove every
native path safe under non-executable stack policy, or close other production
gates. No execution result is promised when a resource deadline is reached.

Tracked-source changes and non-ignored untracked files are rejected before and
after the experiment. Ignored build/cache files are not described as verified
source. Module-resolution downloads are disabled during comparison; the
explicitly reviewed test logic uses loopback, without namespace isolation.
