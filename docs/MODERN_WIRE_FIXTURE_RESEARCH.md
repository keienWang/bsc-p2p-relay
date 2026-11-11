# Offline synthetic modern-header wire fixture

This is a separate **test-only research fixture**, not a live-chain importer or a
production milestone. It fixes the wire representation of a new synthetic chain;
it does not change, repair in place, or reinterpret the earlier fixture, its
proof, or any earlier digest. No runtime source, dependency version, upstream
cryptography, or validation path is changed.

## Why the earlier fixture remains unchanged

`makeFixture` intentionally enables a partial synthetic schedule: Bohr requires a
present zero `ParentBeaconRoot`, while Cancun is disabled and therefore Parlia
requires absent withdrawals/blob fields. The resulting gap in optional Header
RLP fields cannot round-trip through the official decoder.
`TestSyntheticHeaderWireBoundary` continues to assert this exact limitation for
the old genesis and successor. That is a fixture limitation, not evidence that
real BSC headers fail RLP.

The new constructor is `makeModernWireFixture` in
`internal/checkpointproof/modern_wire_test.go`. It does not modify the legacy
constructor or the separate process-isolation/JSON experiment.

## Coherent configuration and official field construction

The profile is **synthetic all-forks-through-Fermi**, not current BSC mainnet,
Chapel, a historical replay, or a claim about forks after Fermi.

- Deep-copy the pinned official `params.ParliaTestChainConfig`, preserving its
  complete pre-Cancun block/time fork configuration. Assign test chain ID 98765.
- Enable Haber, HaberFix, Bohr, Pascal, Prague, Lorentz, Maxwell and Fermi at zero.
  Shanghai and Cancun are already enabled at zero in the official base.
- Preserve the official Cancun blob configuration and copy
  `DefaultPragueBlobConfigBSC`. Both have target 3, max 6, update fraction 3338477
  in this pin. Do not substitute Ethereum's different Prague schedule.
- Leave Osaka and every later fork disabled. No unnecessary later fork is
  enabled merely to make decoding pass.
- Execute the official `ChainConfig.CheckConfigForkOrder` in the constructor and
  again after reloading the configuration from disk. It requires all intervening
  forks through Fermi. In particular, omitting Prague while enabling Lorentz is
  rejected, and enabling Prague without its blob schedule is rejected. Both
  failures have regression tests. Merely adding Shanghai/Cancun to the legacy
  schedule would be insufficient.

Genesis is constructed by official `core.Genesis.ToBlock` with empty allocation,
fixed past timestamp 1600000000, gas limit 40,000,000, difficulty 1, and the public
test validator extra data. Its London base fee, empty withdrawals commitment,
zero blob fields, zero beacon root, and empty requests commitment come from the
pinned official genesis rules, not guessed optional-field padding.

Every successor uses official `eip1559.CalcBaseFee`,
`eip4844.CalcExcessBlobGas`, empty withdrawals and empty requests constants, a
present zero Bohr beacon root, and zero blob gas for this no-blob fixture.
Post-Fermi optional access-list and slot fields remain nil. Empty state,
transaction and receipt roots are synthetic placeholders, not execution results.
The proposed header is re-signed after setting all final fields.

There are exactly three deterministic public test-only ECDSA scalars 1/2/3 and
BLS scalars 11/12/13, as in the earlier experiment. Every successor is newly
signed; votes commit to the new source/target hashes. The official full
`Parlia.VerifyHeader` path verifies proposer and BLS aggregate signatures. No
unsealed-header-only shortcut, fake crypto, bypassed check, or upstream code edit
is used. Verification is sequential and `VotePool` remains nil.

## What the tests establish

The small test first checked genesis plus twelve successors before attempting
the longer proof. It checks canonical official RLP decode/re-encode bytes,
all Header semantic fields including nil states, identical hashes, and
`CopyHeader` storage independence. Every populated pointer and byte slice is
checked for aliasing; mutating the copy leaves the original unchanged.

The comparator explicitly checks `*big.Int` pointer nilness before `Cmp`.
Numerically equal zero values with different internal word allocation are equal;
absent versus present zero remains different. It does not use `reflect.DeepEqual`
on big-integer internals.

Negative small-chain cases reject wrong withdrawals, non-multiple blob gas,
incorrect excess gas, nonzero beacon root, missing requests hash, forbidden future
optional fields, altered proposer signatures, malformed BLS signatures, and a
well-formed aggregate signature over the wrong message. The latter specifically
reaches and fails aggregate cryptographic verification. Independent controls also
prove the original aggregate verifies the correct vote hash, while the new
aggregate verifies the alternate hash and fails the original one, followed by
the exact official `invalid attestation, signature verify failed` result. Separate
wire negatives reject trailing data, truncation, non-minimal integer encoding and
non-minimal list-length encoding.

The longer proof freshly builds and verifies successors 1 through 3073, with
canonical wire decode before each header is admitted to the reader. It exports
3074 headers including genesis, complete snapshots S=2048 and C=3072, and the
expected post-successor snapshot at 3073. It then:

1. Writes the header list, configuration and snapshots to temporary local files
   and reads them back. The complete RLP list re-encodes byte-for-byte, every
   header field and hash agrees, and all numbers/parent links are checked.
2. Loads only S and derives C using the decoded trusted synthetic suffix. Every
   exported snapshot field agrees with the independently generated C snapshot.
3. Loads S and C into another fresh engine/database, verifies decoded 3073 with
   official `VerifyHeader`, and compares the resulting full snapshot with full
   synthetic replay.
4. Creates another fresh engine **and another fresh memory DB**, reimports only
   the on-disk S/C records, and recovers the identical snapshot at 3073. It does
   not reuse an engine cache or import the nonaligned 3073 snapshot.
5. Confirms anchor-only and a suffix missing epoch ancestor 2000 both reject the
   candidate with `unknown ancestor`.

The retained suffix is [2000,3072] plus genesis before the candidate, then 1075
headers including 3073 afterward. At S/C the epoch is 1000, interval 450 ms, and
turn length 32. The initial snapshot starts at the upstream default epoch 200;
upstream snapshot rules perform the epoch-length changes. The Bohr sentinel is
preserved. Synthetic justified=3072 and finalized=3071 are engine outputs for this
fixture only, not real-world finality evidence.

## Reproduction and recorded results

Research base: local `b4eaed0c8e3ca82b3130e8e7619b4b2d4b282196`, whose tree
matches [public 3ff8999](https://github.com/keienWang/bsc-p2p-relay/commit/3ff8999562d2eeb04ef769cbb0d3887913f1fbde), locally cloned without changing that source.
This candidate is integrated on the later [public d147633f](https://github.com/keienWang/bsc-p2p-relay/commit/d147633f1af87b02fe77d4ef9757b7b3364292fa) tree, preserving its load tests and supply-chain documents. Dependencies remain exactly pinned by its unchanged
`go.mod` and `go.sum`. The default code still uses BSC v1.8.2.

For reproduction, put Go 1.26.11 on PATH and download/verify the pinned
modules before an offline run. Keep the usual checksum-database settings:

```sh
export GOFLAGS=-buildvcs=false
export GOPROXY=off
```

The original isolated runs used a prepopulated module cache and disabled both
proxy and checksum-database network lookup. No dependencies were changed or
fetched. The reproduction commands here retain normal checksum verification.
No network service or live transaction was used.

Initial focused test, exit 0 (package 0.092s):

```sh
go test -count=1 -p=1 -timeout=120s \
  -run '^(TestModernWireSmallChain|TestHeaderSemanticBigIntNilAndZero|TestSyntheticHeaderWireBoundary)$' \
  -v ./internal/checkpointproof
```

Final focused test after adding checkpoint persistence and the well-formed wrong
BLS message case, exit 0 (package 11.403s; checkpoint test 11.29s):

```sh
BSC_MODERN_WIRE_OUTPUT_DIR=/tmp/bsc-modern-wire-new-artifacts \
  go test -count=1 -p=1 -timeout=180s \
  -run '^(TestModernWireSmallChain|TestHeaderSemanticBigIntNilAndZero|TestModernWireCheckpointRoundTrip|TestSyntheticHeaderWireBoundary)$' \
  -v ./internal/checkpointproof
```

The output-directory environment variable is optional and test-only. The test
always verifies temporary file round trips. When export is requested, the output
directory must not already exist; the test refuses to overwrite it. Do not point
it at a live data directory. Full captured logs are retained separately from
source in the delivered research archive.

The exported canonical list is 2,714,112 bytes. SHA-256:
`08d59559eb1dce022f15f345cd858150ec5ce1770b36155cf5604b1291fdea5a`.
New hashes:

- Genesis: `0xfa793de59f8a91705c57e00bc6df547c4ee7c32ea86ab5f2e835b077cb8002b9`
- S=2048: `0x1c9ffc33fb14a73ceec73a18fbd699dedaa55f22faadbda833a61715d7704ae4`
- C=3072: `0x36824e82f259849a640b32ef087b569aac930377e1b9efe6d3060db74115ed65`
- 3073: `0x7070d0125212a3c961b2ef2eeae48a25aebaa7909e13d5815356e4353b14da0c`

The artifact manifest includes SHA-256 for configuration, header RLP and all
three snapshot files. It is a local reproducibility inventory, **not an
operator signature or trusted bundle authentication format**. All hashes,
snapshots and signatures belong to the new chain, not the legacy proof.

One additional focused race run was performed after other workloads finished.
It passed on the first and only attempt, exit 0 (package 23.007s; checkpoint test
21.38s), with no race report:

```sh
go test -race -count=1 -p=1 -timeout=180s \
  -run '^(TestModernWireSmallChain|TestHeaderSemanticBigIntNilAndZero|TestModernWireCheckpointRoundTrip|TestSyntheticHeaderWireBoundary)$' \
  -v ./internal/checkpointproof
```

The output-directory environment variable was unset for this run; existing
artifacts were not overwritten. The generated canonical RLP SHA-256 and all
four recorded header hashes were identical. This is sequential fixture coverage
under the race detector, not a concurrent BLS-cache stress test or a fix for the
known process-global cache race. Full output including the command and explicit
`TEST_EXIT_CODE=0` is in `03-focused-race.log` in the evidence package.

The initial isolated research runs did not include repository-wide tests,
fuzzing, load tests or a vulnerability scan. Their focused results do not imply
those stages passed. The subsequent integration checks are recorded below.

## Final integration verification

The integrated source preserves the prior load/response-validation harness and
all previously published files. It adds no runtime entrypoint or module change.
Independent review reproduced the complete canonical header list, configuration
and all three snapshots byte-for-byte, including the recorded hashes. The added
cryptographic controls and malformed-wire regressions passed race testing;
the full independent focused suite also passed in 21.591s.

A single `GOFLAGS='-buildvcs=false -p=1' make check smoke vuln` then passed on the
integrated tree with Go 1.26.10. Checkpoint tests took 41.488s, first-epoch tests
43.239s and relay tests 1.940s. This included the complete default five-package
race suite, build, 10 Python tests, formatting, module verification, dependency
guard, process smoke and the default vulnerability scan. The timed load harness
and explicitly failing `blscacherace` diagnostic remain opt-in and were not run.
A separately installed `govulncheck` v1.9.0 also scanned
`-test ./internal/checkpointproof`; both scans returned exit 0 and
`No vulnerabilities found.` This does not resolve the known dynamic cache race.

No fuzz campaign, real-network run or production validation was performed. The
exact published revision still requires its hosted two-job CI result; green
baseline workflows do not establish that result for this change.

## Boundaries that remain

- Header-only verification cannot validate executed state roots, receipts,
  transaction effects, system-contract transitions, validator membership or turn
  values derived from execution, or the requests hash's execution-derived content.
  `Finalize` and its stateful validators/turn checks are not called with nil
  ethAPI. The empty successor state root is expressly not an executed chain.
- The new config is coherent according to pinned upstream fork checks, but no
  real chain history, fork activation transition, real checkpoint provenance,
  external peer, or independent full-node differential test is established.
- Snapshot JSON is trusted local synthetic data. There is no strict hostile
  decoder, authentication, resource-bounded importer, rollback policy, reorg
  handling, durable production database, or relay integration here.
- Restart means fresh in-process engines and memory databases reconstructed from
  local artifact files. It is not a process-isolation or crash-recovery proof.
- Sequential verification does not repair or disprove the known upstream
  process-global BLS cache race or establish production memory/concurrency safety.
- The earlier header-only/JSON-only fixture and its boundary test remain valid
  records of their own limited profile. Their old digests must not be replaced
  with these newly generated values.

## Pinned official sources

All references use commit
[`54afaff4da31ac9e49ba646a4f65104edac5a2f3`](https://github.com/bnb-chain/bsc/tree/54afaff4da31ac9e49ba646a4f65104edac5a2f3)
(BSC v1.8.2):

- `params/config.go:381-417`: Parlia test configuration;
  `:620-674`: official blob constants;
  `:1573-1685`: fork order and required blob schedules.
- `core/genesis.go:526-615`: genesis construction and fork-specific fields.
- `consensus/misc/eip1559/eip1559.go`: BSC base-fee calculation/check.
- `consensus/misc/eip4844/eip4844.go:98-191`: blob field checks and derived excess.
- `consensus/parlia/parlia.go:574-799`: full verification and optional fields;
  `:973` onward: proposer/vote verification; `Finalize`, `verifyValidators` and
  `verifyTurnLength`: execution-dependent boundaries.
- `consensus/parlia/snapshot.go:350-401`: interval, epoch and delayed-set rules.
- `core/types/block.go`: Header field order, official CopyHeader, seal commitment.
