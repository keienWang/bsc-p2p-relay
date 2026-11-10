# Canonical RLP one-shot Parlia research

## Scope and status

`internal/canonicalisolation` is an **opt-in, test-only experiment**, behind the
`canonicalisolation` build tag. There is no relay API, importer, network client,
worker pool service, dependency change, upstream patch or production integration.
The supervisor test package and its protocol dependency use only the Go standard
library. BSC/Prysm/BLST/Herumi are linked into a separate worker command, never
into that supervising test binary. This separation includes package initializers,
not merely the location of explicit verification calls.

This experiment uses the independently reproduced synthetic modern-wire fixture
from [MODERN_WIRE_FIXTURE_RESEARCH.md](MODERN_WIRE_FIXTURE_RESEARCH.md). It does not
reuse the old JSON Header transport, change the legacy fixture, or reinterpret its
hashes. Empty state/transaction/receipt roots remain synthetic placeholders.
Header verification does not validate block execution, system contracts, validator
state transitions derived from execution, or a real BSC checkpoint or chain head.

The known upstream concurrent cold-cache race remains a blocker. The existing
`blscacherace` diagnostic and every production-readiness gate remain unchanged.
A successful bounded test is not a library race fix, native-C race audit, hostile
context RSS bound, sandbox proof or evidence of remote exploitability.

## Trust and wire boundaries

The compiled SHA-256 constants bind the exact configuration, full canonical RLP
list, and S=2048/C=3072 snapshot files previously reproduced independently.
The request cannot substitute its own manifest, digest or trust root. The local
`manifest.json` is a reproducibility inventory, not a signature or an operator
checkpoint-authentication protocol. The pins attest only to this known synthetic
fixture; a general bundle provenance scheme is still absent.

The request contains:

- The **trusted fixture bundle**: `config.json`, `headers.rlp`,
  `snapshot-2048.json`, `snapshot-3072.json`. Every byte is authenticated before
  the worker decodes any of these structures.
- An ascending unique selection of retained headers from [2000,3072], plus
  genesis, and a unique selection of the two seed snapshots. Deliberately missing
  selections exercise official historical-dependency failures without allowing
  unauthenticated replacement context.
- A separate **untrusted candidate** encoded as one official canonical Header
  RLP, constrained to height 3073. The authenticated list's reference 3073 is
  never added to the candidate reader. The separately decoded candidate is added
  only after successful official `Parlia.VerifyHeader`.
- A full-replay flag for a separate reference experiment. This initializes the
  reader with genesis, imports **no seed snapshots**, and calls official
  `VerifyHeader` on every header 1..3072 before admitting it, then verifies the
  untrusted 3073. Replaying `Snapshot.apply` alone is not BLS verification and is
  never counted as such.

The ordinary checkpoint mode trusts already-authenticated support history. Its
single `VerifyHeader` call is full proposer/attestation verification of the
candidate; it does not reverify all retained support signatures on each request.
Full replay is deliberately separate, slower evidence. The complete authenticated
bundle includes 3074 raw headers, even though ordinary reader retention is only
1073 support headers plus genesis. This is not a general or minimum-history
transport format.

### Bounds in this fixed profile

- Request envelope: 4 MiB at worker input, read with `LimitReader(max+1)`; the
  supervising test's `run` checks the same limit. The measured valid JSON/base64
  envelope is 3,635,278 bytes.
- Authenticated RLP list: exactly 2,714,112 bytes and 3074 headers. Maximum allowed
  per-header RLP: 8192 bytes. The fixture maximum is 1089; candidate 3073 is 883.
- Configuration and each snapshot: at most 8192 bytes. Actual sizes are 1088 and
  3101/3101 bytes. At most 1073 retained header numbers and two snapshot selections.
- Exactly three distinct trusted validators/keys; unique validator indexes 1..3.
  The candidate's fixed height and authenticated context cannot choose arbitrary
  historical key sets. These are fixture constraints, not a hostile general
  validator-set resource analysis.
- Output: at most 4096 bytes per stdout/stderr stream, with overflow drained but
  rejected. Error strings are capped at 512 bytes. Any stderr is a failed call.
- At most two active child processes **per supervisor instance**. Every request
  has a 60-second context including time waiting for a slot. Cancellation asks
  the OS to kill the child; a slot is released only after `Wait` returns. Failed
  starts, cancellation while queued, exit failures and invalid output fail closed.

The `execute(raw []byte)` caller already holds the complete input in parent memory;
this is not an incoming-service memory limit. JSON decoding, base64 copies, header
objects, native allocations, runtime stacks, BLS caches and the fixed 200,000-entry
cache channel also use memory. There is no cgroup, RSS ceiling, RLIMIT, seccomp,
network namespace or universal hard wall-clock guarantee. `GOMAXPROCS=2` is set
before exec and checked by the worker; it is not an OS-thread limit. The fixed
worker starts no descendants. Cleanup of arbitrary process trees is not proven.

### Strict representations

The bounded envelope rejects unknown fields, duplicate keys (also nested/escaped
keys), more than one JSON value and nesting beyond 16 levels. It is not a general
canonical-JSON format.

After provenance checks, the worker uses official RLP decoding and byte-for-byte
re-encoding for every header and the complete list. It checks count, numbers,
parent links, genesis identity and candidate height. The candidate also rejects
trailing bytes, truncation and nonminimal integer/list-length encodings.

Snapshots use duplicate/unknown-key rejection and exact official
`json.Marshal` round trips. This intentionally rejects semantically similar but
noncanonical encodings and preserves upstream's literal `attestation:omitempty`
and `index:omitempty` tags. It catches missing fields, case aliases, normalized map
keys and short fixed arrays. Explicit checks bind snapshot number/hash, epoch
1000, interval 450 ms, turn length 32, three unique validators/keys, three recent
entries, the **different exact Bohr sentinel at S and C**, 96 recent fork hashes,
and source/target attestation numbers/hashes. No zero/default-field substitution
is accepted. The direct decoder negative tests re-marshal typed snapshots so
profile failures are not accidentally masked by JSON field-order rejection.

### Result binding and failure shape

The worker binds its result to SHA-256 of the exact request bytes and candidate
RLP, plus the compiled fixture identity. On success it emits the official header
hash, resulting snapshot digest, justified/finalized heights and successful
`VerifyHeader` count. The parent accepts only after `Wait`, exit 0, correct binding,
empty error, exact accepted shape and expected count. The fixture tests additionally
compare the candidate hash and snapshot digest to independently reproduced values.

Failures use nonzero exit and never carry success-only candidate hash, snapshot
digest or finality fields. They report admission/restore/replay/verify/postverify
stage and a bounded error. A crash, malformed/extra output, output overflow,
unbound response, deadline or inconsistent success-shaped result is a failure.
These are results computed inside a trusted local executable, not independently
verifiable third-party cryptographic proofs. Successful stdio/exit alone proves
neither executable provenance nor correctness.

## Exact wrong-message BLS control

A separate fresh generator process authenticates the fixture and constructs only
two small candidate RLP records. It uses the same documented public test scalars
(ECDSA 1/2/3, BLS 11/12/13); no private user keys are involved.

It verifies that the original aggregate verifies the correct vote hash. It then
signs a deliberately different hash, parses the resulting valid curve-point
aggregate with subgroup validation, checks that it verifies the wrong hash and
fails the original hash, replaces the attestation and correctly re-signs the
proposer seal. ECDSA recovery must equal the candidate coinbase. Both candidates
must pass official canonical RLP round trips.

The independent verification worker must reject the changed candidate with exactly
`invalid attestation, signature verify failed`, after the ordinary context and
claimed-signer authorization checks. In this upstream version `verifySeal` checks
BLS before recovering the proposer signature: the failed BLS branch does **not**
execute that later recovery. Correct re-signing is established by the generator
control; the accepted control exercises the complete official path. A malformed point or `signature not in group` is not an adequate
substitute. The observed candidate RLP SHA-256 values are:

- Original: `f3fd2c2ed52a77a2714f3378ff1169b15bb8ed5b2b142b7790bb315b7e5c5d92`
- Wrong message: `08959ca9448beceb32b48945c274204c36ab3d69def52389bb42a72ca71aa80b`

## Source audit: cache and background activity

BSC is pinned to v1.8.2, commit
[`54afaff4da31ac9e49ba646a4f65104edac5a2f3`](https://github.com/bnb-chain/bsc/tree/54afaff4da31ac9e49ba646a4f65104edac5a2f3).
Prysm is v5.3.2; the unchanged module graph selects BLST
`v0.3.17-0.20260401165437-a3c15d6a6c68`. Source hashes and line-numbered excerpts
are retained with research evidence. No dependency or cryptographic code was edited.

1. Prysm `blst/init.go` initializes a process-global two-million-key LRU.
   `cache/nonblocking.NewLRU` allocates its 200,000-entry pointer channel and
   starts the persistent `handleGetRequests` goroutine. `Parlia.Close` is a no-op;
   fresh engines do not reset this cache. Process exit followed by parent `Wait`
   is the lifetime boundary used here.
2. **`PublicKeyFromBytes` calls `pubkeyCache.Add` synchronously**, at
   `blst/public_key.go:52`. It is not a queued asynchronous Add. The background
   worker moves list nodes for `Get` operations under the list lock. The known
   concurrent callers can still race on `Add`'s existing-entry `ent.value` write
   and `Get`'s read. These facts must not be conflated.
3. RLP/config/snapshot decoding and snapshot replay store/copy fixed key bytes;
   they do not call `PublicKeyFromBytes`. The ordinary candidate's attestation
   loop calls it exactly three times, once per distinct fixture key. The generator
   derives public keys directly from secret scalars and uses already parsed points;
   it makes no `PublicKeyFromBytes` calls. These are **source-derived counts**, not
   runtime call instrumentation.
4. Full replay has attested headers 3..3073, so its source-derived expectation is
   3071 x 3 = 9213 `PublicKeyFromBytes` calls. This repeats only three distinct keys.
   No context decode calls are omitted from that analysis. Its explicit
   `VerifyHeader` counter is runtime evidence of full calls, not BLS-call tracing.
5. Whole verification is **not single-threaded**. BLST `FastAggregateVerify`
   aggregates already parsed points, then calls `Verify` and
   `coreAggregateVerifyPkInG1`, which launches pairing work and collects its
   results before returning. BLST point aggregation also has internal worker
   paths. These paths do not call Prysm's parser/cache. With the exec-time Go
   compute limit, Prysm sets BLST maxProcs to 1, but internal goroutines/native
   work still exist and native-C races are not assessed by Go's race detector.
   In particular, P1 point aggregation uses `min(GOMAXPROCS, number of points)`,
   which is two workers for these three points, independently of the pairing
   maxProcs setting.
6. Prysm's public initializer calls Herumi's BLS12-381/ETH-mode initializer with
   public-key/signature order checks enabled. The two imported ants pools start
   maintenance goroutines; Prometheus registration can start transient descriptor
   goroutines. These are real initializers, not additional BLS parser callers.
   Ordinary Go runtime/GC work remains. No total goroutine count is asserted.
7. `Parlia.New` initializes synchronous engine state. The worker invokes only
   `VerifyHeader` plus synchronous snapshot/finality accessors, with `VotePool`
   nil. It never calls asynchronous `VerifyHeaders`, `Seal`, execution/finalize
   assembly, vote-envelope verification, VM BLS precompiles, services, dialing or
   network/RPC servers. Compiled but uncalled code is not the same as unlinked code;
   the worker dependency inventory records the broader graph. HTTP handler
   registration at import is not a listening server.

The fixed three-key path cannot reach two-million-key eviction. Combined with
source inspection and a bounded race-instrumented run, this supports only this
specific executable/context experiment. It does not prove arbitrary contexts,
reused workers, unrelated BLS callers, new dependencies, multiple services,
large key sets or malicious RSS behavior safe.

## Reproduction and evidence

Use Go 1.26.10 with the already pinned modules available locally. No dependencies
need to be fetched by this experiment. Keep normal checksum-verification settings:

```sh
export GOFLAGS='-buildvcs=false -p=1'
export GOMAXPROCS=2 GOPROXY=off GOTOOLCHAIN=local
export BSC_CANONICAL_FIXTURE_DIR=/absolute/path/to/reproduced-modern-wire-artifacts
```

The four pinned files must be generated by the existing
`TestModernWireCheckpointRoundTrip` export described in the modern-wire report,
or supplied from its independently reproduced artifacts. This change stores no
multi-megabyte request or duplicate fixture blob. The generator emits the two
small candidate records into test memory; child binaries use temporary directories.

The first minimal gate passed strict JSON, input bounds, small canonical Header
RLP and supervisor cancellation/failure/output tests. Its binary-symbol check
failed because the default Go 1.26 temporary test executable had **no symbol
section**, not because it contained a BSC dependency. The complete original log
and exit 1 remain retained. A single corrected invocation preserved symbols and
passed dependency graph, actual executable build metadata and `go tool nm` checks:

```sh
go test -race -ldflags='-s=false -w=false' -tags=canonicalisolation \
  -count=1 -timeout=30s -run='^TestSupervisorDependencyBoundary$' \
  -v ./internal/canonicalisolation
```

That corrected boundary run exited 0 (1.172s). To retain its full graph/metadata/
symbol output, set `BSC_CANONICAL_EVIDENCE_DIR` to an existing empty directory;
creation of each evidence file is exclusive and refuses overwrite.

The first snapshot-admission run exited 0 (1.508s), checking both pinned snapshots,
all 3074 canonical RLP records, typed semantic mutations and strict JSON negatives:

```sh
go test -race -tags=canonicalisolation -count=1 -timeout=30s \
  -run='^TestSnapshotStrictSemantics$' -v ./internal/canonicalisolation/worker
```

The first limited official run used one generator and five sequential fresh
workers. It exited 0 (26.370s), with exactly the following selected cases:

```sh
go test -race -tags=canonicalisolation -count=1 -timeout=90s \
  -run='^TestCanonicalOfficialCases$/(valid|wrong_message_correct_proposer|wrong_config|missing_epoch|candidate_trailing)$' \
  -v ./internal/canonicalisolation
```

- Valid: official candidate hash and post-candidate snapshot digest matched the
  independent reference; justified=3072 and finalized=3071.
- Wrong message: exact `invalid attestation, signature verify failed`, 1151
  header-reader accesses and zero misses.
- Missing epoch 2000: exact `unknown ancestor`, 1088 accesses and one miss.
- Wrong config: provenance rejection in admission, zero reader accesses.
- Candidate trailing byte: exact `rlp: input contains more than one value` in
  admission, zero reader accesses.

The first full-replay run used one generator followed by one verifying worker and
exited 0 (22.485s). The runtime counter recorded 3073 successful `VerifyHeader`
calls, with no seed snapshot import, and the final header/snapshot/finality matched
the independent reference. The 9213 BLS-parser call figure above is still a static
expectation, not runtime instrumentation.

```sh
go test -race -tags=canonicalisolation -count=1 -timeout=90s \
  -run='^TestCanonicalFullReplay$' -v ./internal/canonicalisolation
```

The first concurrent batch exited 0 (24.614s): one generator, then eight fresh
verification workers, four valid and four wrong-message candidates. All eight
PIDs differed, valid results matched the reference, and all negatives returned the
exact official signature-verification error. Observed active-child peak was **2**,
with zero active children/occupied slots after all `Wait` calls. That run
asserted peak <= 2 rather than peak == 2, so overlap was an observation rather
than a required assertion. After this run, the final review candidate strengthens
the assertion to peak == cap(slots) == 2, with zero active children/slots. The
stronger assertion had **not** been rerun at the initial handoff; the independent
review below subsequently exercised it successfully. No additional
post-batch request or automatic retry was performed in the initial run.

```sh
go test -race -tags=canonicalisolation -count=1 -timeout=90s \
  -run='^TestCanonicalConcurrentProcesses$' -v ./internal/canonicalisolation
```

The cancellation test kills a deliberately sleeping helper executable, not a
hung Parlia/BLST C call. The observed failure tests cover start failure, nonzero
exit, output overflow, unbound success-shaped output, deadline, queued cancellation
and slot release. They do not prove cleanup of arbitrary process trees or forced
termination of every native-code failure mode.

After review, the earlier input smoke negatives were strengthened to start from
the complete valid envelope, changing exactly one property. The first strengthened
run exited 0: parent package 12.262s, worker package 1.169s, with one generator and
four sequential admission-only workers. Oversize (4,194,305 bytes), trailing JSON,
unknown field and duplicate key each returned its exact admission error with zero
reader accesses. Four direct bundle tests kept every other field authenticated
and enlarged only config, header-list, support or anchor bytes; each returned
exactly `bundle record size exceeds bound`. No full verification/replay/concurrency
run was repeated for these test-only improvements.

```sh
go test -race -tags=canonicalisolation -count=1 -timeout=60s \
  -run='^(TestCanonicalInputBounds|TestAuthenticatedBundleLimits)$' \
  -v ./internal/canonicalisolation ./internal/canonicalisolation/worker
```

The early `TestBundleBoundsBeforeAuthentication` uses empty companion fields and
is only a rejection smoke test; it does not independently isolate the byte bounds.
That coverage comes from the later complete-fixture mutation tests above.

These other official-case subtests were implemented but **not run** in the initial
staged investigation:
missing immediate parent, missing support/all snapshots, wrong snapshot/raw context,
duplicate/excess selections and candidate oversize. Some related primitives were
covered directly in the small decoder/protocol tests; that is not end-to-end
coverage of those subprocess cases at that stage. The independent complete tagged
suite below subsequently covered them. No default whole-repository suite, fuzz
campaign, vulnerability scan, real network run or production validation was
performed in that initial investigation. Earlier baseline CI/scans do not
substitute for this change.

## Independent review and actual relay boundary

The seven frozen source/document files were copied without changes onto local
`683a9e5a56dd4c890234d3e02aa1125c66bb49a4`, the exact tree equivalent of public
`ac4ffde7f64724881f683b3d1946f366faaa70be`. Existing capacity, modern-wire, SBOM,
official-txpool and security files were preserved. The resulting review candidate
was `249ba731b0a90c873bf3874621b88a56495c982c`.

Independent review used a separately reproduced fixture whose four compiled pins
all matched. One complete, serial tagged race run on that exact candidate exited
0: supervisor 89.416s, protocol 1.138s, worker 1.491s. It covered all 14 official
cases, the full 3073-call replay, eight distinct verification children with the
strengthened **peak == 2** assertion and zero remaining active children/slots,
all admission/decoder/failure tests, and the actual supervisor dependency graph,
binary build metadata and symbol boundary. No failing run was retried.

```sh
go test -race -ldflags='-s=false -w=false' -tags=canonicalisolation \
  -count=1 -timeout=180s -v ./internal/canonicalisolation/...
```

The complete independent test log SHA-256 is
`b08a8f97d3406163e055fa7fba63365e208cb4e9e6677f3cdc5cc9d4b56babfe`.
This review did not run the default whole-repository suite, fuzzing, the known
failing upstream cache diagnostic, vulnerability scanning or real networking.

**The real relay executable is not the standard-library-only research supervisor.**
Read-only inspection of the current relay command dependency graph includes
Prysm, BLST and Herumi. The checked local `bsc-relay` binary has SHA-256
`5bd8e0647707f2931dd315b8e4f83cb341d6deb7d909fc6372988c7f367b0b03`;
its symbols include Prysm `blst.init.0`, the process-global nonblocking LRU and
native BLST code. The research supervisor's unlinked-library result therefore
cannot be applied to the relay process. This does not establish a new race,
runtime parser reachability or remote exploitability; symbol presence alone is
not an execution trace, and absence of a standalone parser symbol is not proof
of no call through inlining.

Any future relay integration must separately audit the actual parent command's
initializers, reachable BLS callers, memory use and concurrency boundary, then
test that integration. Passing this fixed-context subprocess experiment does not
perform that work or remove the production BLS blocker.

## Dedicated CI coverage

The ordinary default and official-txpool jobs do not execute this opt-in package.
The separate `canonical_isolation` job, displayed as **synthetic canonical Parlia
isolation**, generates the fixture from the checked-out source in a fresh runner
temporary directory, then checks the compiled pins in the research tests. It
does not download a prebuilt checkpoint or reuse a previous job's artifact.

That job fixes Go 1.26.10 and `GOMAXPROCS=2`, uses readonly module resolution and
serial package builds, vets the tagged packages, and runs the complete tagged
race suite with symbols retained for the actual supervisor-binary checks. It
also runs the existing dependency guard under the tag and scans the exact tagged
test graph using pinned `govulncheck@v1.9.0`. The scanner is installed independently
of the research build flags. No failure is ignored or converted to success.

The job has a 15-minute outer limit; fixture generation and the tagged suite each
have a 180-second Go test timeout, in addition to individual child deadlines.
This checks the bounded synthetic research profile, not real networking, OS-level
resource isolation, native-C race freedom or a production-integrated relay.
The job definition alone is not an execution result: its exact published-commit
Actions conclusion must be checked before reporting hosted CI success.

A local CI-equivalent preparation run on the unchanged reviewed Go sources
successfully generated the fixture in 13.395s, then passed the tagged dependency
guard, tagged-package vet and exact tagged test-graph vulnerability scan (exit 0,
`No vulnerabilities found`). All four generated files independently matched the
compiled pins. The full log SHA-256 is
`927fa5e56a83798d159ea19eb566fd743b4a9105f3aaccb5e3b011462186ad8c`.
This run did not repeat the independently completed tagged race suite or the
default whole-repository tests. YAML parsing and step/context inspection are
static checks, not a substitute for the hosted job.

The existing application SBOM describes the relay command at its recorded public
source commit. It is not a binary SBOM for this additional test-only worker.

## Frozen delivery

Research started from local `6bdefbb03eb5cb1528e7fb0ac8a2a8fa3e2595db`, tree
`b717c2583a6305e4c4054256001bcc44f4379b30`, equivalent to public
`921b9a865e0cf4317e11f0061d826ba428cccb20`. Later capacity/SBOM/publication work
was deliberately not reset or modified. The initial review contained this new
document and six tagged Go source/test files. Integration additionally links this
report from the README and adds the dedicated CI job above. Existing production
sources, tests, dependency manifests and the dynamic race diagnostic are unchanged.
Raw logs, source-audit excerpts, binary symbol inventories and versioned source
fingerprints are separate research evidence, not automatically committed assets.
No publication or production integration is part of this delivery.
