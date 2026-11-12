# Stage1: private BLST cache comparison candidate

Status: Python/mock verification only. No Stage1 Go build, namespace launch,
race test, module download or hosted run has occurred locally. This integration
adds a separate experimental hosted job, not a result from that job. Production
dependencies and public main remain unchanged pending publication decisions.

## Inputs and identity

The candidate inherits reviewed local stage0 0caec95560d5486264195b9d1f035832446bf4e3,
source-equivalent to published research 4c524e90. Public main baseline remains
560ddfc85e05f9a22d16d3b420396d10605e00c8. The successful stage0 run 43271313587
produced 468460 bytes of safe inventory, SHA-256
76938fd9d47a5552f310d52827e7e76f99675e2eca07000a278e4a7261a1067f.
That inventory contains 289 package instances and 25 module records, but is an
import-only NON-RACE probe, not the graph accepted by this candidate.

The six licensed research materials from 936f61dbad170dccaeecd445cb1dec9c36d6b2d6
are inherited unchanged. See [materials](../research/bls-cache-race/README.md),
[NOTICE](../research/bls-cache-race/NOTICE.md) and root [LICENSE](../LICENSE).
They must not be distributed as a standalone folder without their license text.
No upstream reference directory, complete fork, vendored module, binary or
private bundle is added to this repository.

`bls_cache_race_ci.py` checks the fixed root LICENSE/mod/sum/compat inputs and
all six materials. It uses Go 1.26.11, Linux/amd64, GOAMD64=v1, CGO=1,
GOMAXPROCS=2 and `-mod=readonly -buildvcs=false -p=1`. It binds the Go executable,
selected Go tool executables, VERSION, Python, platform tool executables and its
imported Python guard sources before/after. This is not complete native compiler
or toolchain attestation.

Both sides are private TEST-DERIVED Prysm modules, not official unmodified
module binaries. Preparation verifies the official Prysm v5.3.2 module receipt
against root go.sum, its two original file hashes, and `go mod verify` before
copying. The original module cache is never patched. Private copies reject
symlinks/path escapes and bind every file in a directory manifest. Both receive
the exact same six-test source. Only candidate receives patch0001; the complete
file-set/content comparison allows exactly its two production-file changes.
The original download h1 and the derived manifest SHA are separate identities.

Each private modfile is an exact relay-root go.mod copy plus ONLY one relative
Prysm replacement; its .sum is the exact relay lock. Existing BSC/STUN/other
replacements remain unchanged. All commands run from the temporary RELAY root,
using the fully qualified Prysm cachepatchtests package, never from Prysm root.
That retains relay's selected BLST a3c15d6 pin rather than Prysm's default.

## Preparation, graph gate and isolation

The reviewed experimental invocation is `python3 scripts/bls_cache_race_ci.py`, with no
public arguments. `--inner` is an internal handoff and independently requires
namespace/identity verification before compilation. No toolchain is installed.

Preparation may use ONLY https://proxy.golang.org without fallback, GOAUTH=off,
GOSUMDB=off and locked module checksums. It downloads the three explicit pins;
exact package metadata may fetch additional locked ordinary module sources.
There is no vulnerability DB, external checkout, tidy or broad upstream test.

Each side collects its own `go list -race -deps -test -json` plus authoritative
module receipts. Package instances, ForTest and ImportMap are preserved. The
same exact six-test SOURCE/compilation graph is used on both sides. Local Prysm
identity, all effective module pins (including geth -> BSC), selected source
hashes, whole original/derived Prysm-tree digests, locked checksums for other
modules and discovered license paths are recorded. Baseline must actually select nonblocking, candidate Hashicorp LRU.
This is observed from each new graph, not inferred from patch text or stage0.
The baseline's nonblocking code has its own MPL/BSD provenance even though the
original graph did not select the Hashicorp module.

Before compilation the runner enters fresh Linux network AND PID namespaces,
with mount-proc and kill-child, then drops to the original non-root UID/GID,
clears supplementary groups and all capabilities, and sets no-new-privileges.
It verifies zero capabilities, different namespace identities, a sole DOWN
loopback and empty IPv4/IPv6 routes. GOPROXY=off is additional defense, not the
network isolation itself. Namespace setup has no fallback to online execution.
This is network/PID isolation and privilege reduction, NOT a complete filesystem
sandbox. The same-UID work still uses the host filesystem. It is not a facility
for executing arbitrary untrusted code, only these fixed reviewed inputs.

The exact graphs are recollected offline and compared before either binary is
built; both binaries must compile successfully before either is run. The fixed
link policy is `-extldflags=-Wl,-z,noexecstack`, with bounded ELF policy inspection
before execution. Graph/static-source identities, binaries and private/source
inputs are checked after execution. Generated testmain materialization is
recorded separately; changes in that status are not silently called static-source
changes. This is selected Go declarations plus whole module-source digests,
NOT a complete preprocessed native include/assembly-selection inventory.

## Asymmetric execution and strict acceptance

The baseline runs ONLY `TestCacheConcurrentFirstUse`, at most three independent
OS processes, each `-test.count=1`. A complete quiet pass permits the next fresh
process; three quiet passes mean NOT REPRODUCED, not race freedom. Compile error,
OOM, timeout, signal, skip, zero-match, malformed stream or unrelated failure
stops without being relabeled as expected red. Candidate executes all SIX exact
tests once. The baseline and candidate runtime COVERAGE IS NOT SYMMETRIC, even
though their test source and compilation test set are identical.

Fixed GORACE is `halt_on_error=0 exitcode=66 atexit_sleep_ms=0 history_size=1`;
caller GORACE is never inherited. Target red requires the named cold-test FAIL,
package failure and matching process status, plus a complete race block whose
read Get and write Add access stacks belong to the fixed nonblocking lru.go and
both pass through publicKeyFromBytes/PublicKeyFromBytes. Frame and test-event
classification is fail-closed. Additional unrelated races/assertions fail the
gate. Candidate requires exact six RUN/PASS events, matching text ownership,
package PASS, exit0, and no race. Public evidence contains fixed frame classes,
counts/actions/test names, never raw stack paths or test2json.

## Bounds, output and research CI integration

- Preparation: six-minute total deadline; metadata commands normally 90 seconds,
  explicit pin download 120 seconds and patch operations 20 seconds each.
- Isolated phase: fifteen-minute total deadline; each compile at most five
  minutes, each test process 60 seconds with its internal timeout at 50 seconds.
  The privileged namespace watchdog is 910 seconds; outer capture is 925
  seconds within a 930-second budget.
- Each process group is bounded using the reviewed scanner capture helper,
  with bounded TERM/KILL cleanup; namespace lifetime catches remaining children.
- Per-command raw output cap: 16 MiB. Prepare and inner budgets: 64 MiB each.
  JSON event and exported safe evidence limits apply independently.
- Each copied/hashed module tree: at most 20000 files / 512 MiB; individual
  source reads at most 32 MiB. Test binaries at most 256 MiB. Core dumps disabled;
  process/file-descriptor/file-size limits are set before inner execution.
  NPROC is a per-UID limit, not a namespace-only count; FSIZE bounds each file,
  not total filesystem usage. Neither is a total-disk or hard-memory budget.
- No low address-space limit is imposed on Go/race. Memory is NOT hard-bounded;
  this is not a two-million-entry RSS, contention or throughput benchmark.

The separate `experimental BLS cache race comparison` job has a 25-minute
outer limit and a 23-minute wrapper step. The existing six jobs remain byte-for-byte
unchanged. This new job uses cache:false, no cache actions or artifact export,
and a disposable Ubuntu 24.04 runner. Source trees,
module/build caches, raw logs and test binaries remain temporary and are deleted
before ordinary success/failure return. A killed runner may skip Python cleanup,
so disposal is the final boundary.

Stdout contains a BLS_STAGE1_SAFE_RECEIPT line followed by the exact pretty-printed
safe JSON bytes it counts and hashes. Both success and failure use this format.
A failed gate may export typed safe
progress, command labels, local-output lengths/hashes and a fixed error code,
not raw tool errors or environment. Success and failure/progress are projected
through exact nested schemas; graph/origin data must match the original host
preparation receipt, and unknown sentinel fields are rejected. ELF evidence
retains only its validated integer/class/permission fields, no private path.
No claim is made that hashed raw logs were
published. The workflow runs the wrapper directly so failed progress remains in
the failing step. The publisher must independently reconstruct the following safe JSON and
verify its printed bytes/SHA against the receipt. No raw-log unmasking, huge
one-line JSON output or source artifact is required.

The four whitebox tests, full consumer graphs, independent cryptographic vectors,
negative-crypto breadth, scanner identities, native-selection audit, performance
and RSS acceptance remain separate gates. A successful comparison would show
this target race reproduced and this candidate passing these six tests, not a
production fix or blanket license/source-distribution clearance.

## Integration verification boundary

The runner preparation revision c6738744 passed 218 Python contracts and two
documentation checks locally. Independent reviews covered the classifier,
source/isolation contracts, safe export and final small changes. These are
Python/mock/static checks, not real Go/native or namespace execution.

While integrating the seventh job, the existing timeout test exposed an invalid
scheduling assumption: a child Python interpreter is not guaranteed to print
within 150 ms. The production capture function and its timeout were NOT changed.
The test now separates a deterministic received-prefix/timeout/cleanup contract
from a real child timeout/reap contract that correctly permits empty startup
output. The initial failure is retained, along with an intermediate missing-import
error in the test edit and its correction. No failed hosted attempt was rerun.

The combined tree passed 221 Python tests in 12.407 seconds; its 25 focused
workflow/timeout contracts also passed independent review. The first actual
seven-job hosted run and its exact source/binary/race receipts remain required. A green preparation job or Python suite cannot satisfy those
gates. The initial publication target is the BLS research branch, not main;
there is no deployment or release authorization implied by this document.
