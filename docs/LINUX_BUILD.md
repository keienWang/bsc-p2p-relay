# Checked Linux/amd64 build entry

Status: the checked delivery entry and same-policy official test package passed
hosted validation at public source `c27438bf46969dd224db5cbaa49f65b747537787`.
The [acceptance record](HOSTED_ACCEPTANCE_2026_11_12.md) binds that result to its
exact source, artifacts, commands and CI run. It does not confer acceptance on
later code changes. No production readiness, full native compatibility or
executable-stack exploit is claimed.

## User and CI entry points

From a checkout, prepare the public pinned modules, then build:

```sh
go mod download
make build
# Explicit alias for the same supported path:
make build-linux-amd64
```

The output remains `bin/bsc-relay`. Default CI calls the very same
[helper](../scripts/linux_build.py) with the fixed `bsc-relay` output name so its
existing metadata collector and loopback smoke inspect/use that same artifact.
No relay HTTP endpoint, CLI flag, wire behavior, native archive, Go module or
runtime source is changed.

`make build` dispatches using effective `go env GOOS/GOARCH`, not `uname` or an
assumed host. A Linux/amd64 target must pass the supported checks; CGO disabled,
unsupported settings or cross-compilation do not fall back to a bare build.
Other targets retain their original development build without Linux linker
flags. That route, including non-POSIX hosts, is not a new platform qualification
or ELF guarantee. A direct `go build` is an unchecked developer bypass.

The supported lane is deliberately narrow:

- Native host **and** target Linux/amd64, Go **1.26.11**, CGO=1, GOAMD64=v1,
  and no GOEXPERIMENT. Newer toolchains require a separately reviewed lane update.
- Default CGO flags (`-O2 -g`, empty CPPFLAGS); compiler/archiver commands have
  one executable argument, without wrapper arguments. Tool paths, hashes and
  versions are recorded, not granted a general supply-chain trust certificate.
- No active Go workspace. Effective GOFLAGS permits only `-mod=readonly`,
  `-buildvcs=false`, and one `-p=1` through `-p=8`. Duplicate, unknown, quoted or
  conflicting options fail. `-ldflags`, `-overlay`, `-toolexec`, extra tags and
  alternative modfiles are not accepted for the delivery/default-test graph.
- The official test operation additionally accepts only its existing exact
  `-modfile=integration/officialtxpool/go.mod` and `-tags=officialtxpool`.
- Python 3, Git, the C/C++ toolchain, `ar`, `file` and `readelf` must exist.
  Missing tools, failed commands or incomplete metadata fail explicitly.

The first configuration query already sets GOTOOLCHAIN=local. It reads the
inherited effective GOENV/GOFLAGS/GOWORK before normalizing anything. Raw query
output and rejected GOFLAGS values are not printed. Only approved necessary
configuration fields are subsequently logged; no full environment or proxy
credentials are dumped. Validated effective compiler, target and cache settings
are preserved while GOENV/GOWORK and module-resolution downloads are disabled.
Missing cached dependencies require the preparation step above, not an automatic
network fallback. This is not OS network isolation.

## Artifact policy and failure behavior

The checked build uses fixed argv including:

```text
go build -mod=readonly -trimpath -buildvcs=false -ldflags=-extldflags=-Wl,-z,noexecstack -o <temporary-output> ./cmd/bsc-relay
```

No additional PIE, forced external link mode, native archive modification or
platform-wide linker setting is introduced. The Herumi module version/sum and
linux/amd64 archive hash/size must match the recorded research inputs; archive
identity/hash is checked again before installation or a successful test receipt. The existing bounded
[ELF checker](../scripts/elf_stack_policy.py) must find exactly one GNU_STACK
without execute permission and no writable/executable LOAD segment. Missing,
duplicate, unsupported or truncated ELF information fails closed. The binary
must be regular and non-symlink, and its identity/hash must remain stable through
metadata checks. Go buildinfo must match the trimmed relay contract; Go 1.26.11
intentionally omits `-ldflags` under `-trimpath`. Exact build argv, source
commit/tree/status, SHA-256, byte count, build ID and ELF metadata provide the
recorded evidence, not an embedded linker-flag claim or reproducibility proof.
Source commit/tree/status identifies the checkout context, not an attestation
that the commit exactly represents every byte compiled. Dirty source status is
disclosed and is not clean release-source evidence.

Build and checks occur in a private temporary directory on the destination
filesystem. Only after every check passes and the old destination is confirmed
unchanged is the new artifact atomically renamed into place. Build, metadata,
ELF-check or rename failure leaves the old binary available and reports failure;
it does not call that old binary a successful new build. Symlink destinations or
output-parent symlinks are rejected. These are controlled-checkout safeguards,
not protection against hostile concurrent mutation or crash-durable packaging.
The build entry never executes the newly installed relay.

Linux operations reuse bounded process-group capture: 4 MiB per command,
16 MiB cumulative output and bounded TERM/KILL cleanup. Shared operation
deadlines are 210 seconds for builds, 330 seconds for default race and 450
seconds for official tests, leaving 30 seconds before the respective CI limits. Build is limited to 180 seconds; default race to 240 seconds; official
test compilation to 180 seconds and test execution to 200 seconds with a Go
180-second test deadline. Short metadata commands have 15-second ceilings.
Other-target development builds use a 180-second limit. The non-POSIX fixed
configuration query has a 15-second timeout rather than the Linux streaming cap;
its development build inherits output rather than capturing it.

## Regression scope

The default CI race operation is:

```sh
make race-linux-amd64
# Same helper used by default CI:
python3 scripts/linux_build.py race
```

It runs the entire default `./...` graph with explicit identical linker policy,
without research tags. The developer `make test`/`make race` commands remain
ordinary tests, including the race prerequisite of `make check`; they are not
evidence of same-policy coverage. No policy is
injected globally into GOFLAGS.

The official CI operation retains the alternate modfile/tag and the entire
`./internal/relay` package:

```sh
GOFLAGS='-buildvcs=false -mod=readonly -modfile=integration/officialtxpool/go.mod -tags=officialtxpool' go mod download
python3 scripts/linux_build.py official-test
```

It compiles a race test binary with the exact linker policy, inspects its own
`internal/relay.test` buildinfo/tag and ELF, then executes that exact binary
from `internal/relay`, matching the original package working directory, without
any `-test.run` filter. Its verbose output must show one named
`TestOfficialBSCTxPoolLoopback` RUN followed by PASS and a final whole-package
PASS. The existing opt-in skips `TestSoakMsgPipe`, `TestUniqueCapacityDefaultTTL`
and `TestUniqueCapacityFailureReport` remain explicit exclusions, not covered
tests. Unknown skips or a skipped official loopback are rejected. Binary hashes
are checked again after the whole package runs.

The separate research job continues the already exercised public Herumi fixture
and candidate smoke. The canonical-isolation outer test and independently built
child worker are unchanged and **not** covered by this policy. The helper does
not fix Prysm's known cache race or qualify all native paths, CPU dispatch,
dynamic memory permissions, native source closure, reproducible builds, target
hardware, public-network behavior or production operation.

## Prior research result, not this delivery candidate's result

[Run 43250728200, job 131295480435](https://github.com/keienWang/bsc-p2p-relay/actions/runs/43250728200/job/131295480435)
completed on research source `b8a6fa80dcbd05b432b7aa127e87d31ddfad5165`, tree
`292f8e2030cd23965714a7c6301cd2e342b9d7cb`, using Go 1.26.11. Its five jobs passed.
The comparison's three inspected artifacts retained identical before/after
hashes:

| Artifact | Bytes | GNU_STACK | SHA-256 |
| --- | ---: | --- | --- |
| Baseline relay | 50,729,184 | RWE | `afa71849f7d73969e55048218c6737d9044aec58a579ea540dd3d47e66ed4018` |
| Candidate relay | 50,729,184 | RW | `a40432347222aec447eab0a8421f398a9cf4f11fbc8ee6f74c9def91730013e0` |
| Herumi public-vector race fixture | 6,694,464 | RW | `1f585e6951e667ca0b92f99d709f8ad7cbf4ccec71455b501e8afa69a14986f3` |

All three had LOAD flags R / RE / R / RW. The same-policy default race suite,
actual public Herumi RUN/PASS, candidate loopback smoke and final result receipt
completed. Full decoded job log: 308,883 bytes, SHA-256
`a567af19404d7e098ef5c3fb2f4300117e333e6fbbbf3ec492ed45f4a1424422`.
This demonstrates those artifacts and exercised paths, not the new official
alternate graph under the policy or a new default delivery artifact.

The [research history](NOEXECSTACK_CANDIDATE.md) retains the first metadata-contract
failure, older measured RW/RWE observations and their limits. Neither the warning
alone nor the change of Go version establishes a vulnerability or Go regression.
Local tests of this integration use only Python mocks and synthetic ELF bytes;
Go/native execution and hosted validation remain separate required checks.
