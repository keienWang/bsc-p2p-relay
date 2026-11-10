# Offline first-epoch header experiment

Status: **not integrated into the relay, API, peer selection or confirmations**.
`internal/firstepoch` is a deliberately small, fail-closed proof of the official
Parlia public API path. It cannot follow the live BSC chain.

## Supported input and trust

`New(chainID, genesisRLP, maxHeaders)` accepts only chain 56 or 97 and a genesis
header whose hash exactly matches the pinned official mainnet/Chapel constant.
The caller may obtain genesis from an untrusted source: bytes are bounded and
hashed locally before being admitted. No peer head, claimed TD, snapshot JSON,
custom fork config or arbitrary checkpoint is accepted as trust.

`Append(headerRLP)` accepts only the direct successor of the last accepted header,
numbered 1 through 199. It runs BSC v1.8.2 `Parlia.VerifyHeader` with nil `ethAPI`
and retains a header only after success. It freezes the supported era before
block 200 (first validator-update epoch) and before Shanghai (first timestamp
fork on both pinned networks). All optional post-fork header fields are rejected,
including Amsterdam's seal-unbound access-list hash and slot number. The pinned
networks have no block-based fork transition in blocks 1 through 199; mainnet's
Ramanujan/Niels rules are already active at genesis, Chapel's are not.

The official genesis hash authenticates its initial validator list. Success is
**genesis-anchored consensus-header authentication** under that validator set,
not full execution validation. The official engine checks the proposer seal,
authorized proposer, recent-signing restrictions, difficulty, and applicable
header/timing/gas rules. Header-only verification does not run the EVM, verify
transaction/receipt lists or validate state roots. Upstream's `Finalize` checks
state-derived validator and turn changes, which this module never reaches.
The engine's historical rules are reused as-is, not retroactively tightened to
modern chain rules. For example, first-epoch Chapel had not activated Ramanujan
and the official engine accepts equal or decreasing timestamps there; mainnet
had already activated the rule and rejects those timestamps. Regression tests
check both networks. This historical experiment is not modern timestamp validation.
There is no fork choice, canonicality promise, inclusion
claim, confirmation count, or BLS finality output. A malicious authorized
validator can still sign an execution-invalid header.

## Bounds and fail-closed behavior

- Encoded headers, including genesis, are at most 2,048 bytes, checked before RLP
  decoding. Extra data on successors is exactly 97 bytes; genesis is hash-pinned.
- The caller chooses 2–200 retained headers, including genesis. The default is
  not inferred. There is no eviction: at capacity, advancement stops. Retained
  encoded-equivalent input is at most 400 KiB, plus bounded decoded object and
  slice overhead. This is not a hard process RSS limit.
- A private reader exposes only the authenticated linear history. Missing,
  skipped, competing and repeated ancestors fail closed. No candidate is exposed
  to upstream snapshot reconstruction before verification.
- Each append creates a fresh empty in-memory DB and official engine, then closes
  them on success or failure. In this range upstream only stores the genesis
  snapshot (the next periodic snapshot is at 1,024). Genesis validator count is
  fixed by the pinned hash. Temporary snapshot/cache memory and reconstruction
  work are bounded by at most 198 retained ancestors. No snapshots persist across
  attempts, so failed candidates cannot accumulate DB records or engine caches.
- The upstream long-ancestry trust fallback requires more than 600,000 ancestors.
  It is unreachable here; its test asserts the retention bound remains below the
  pinned upstream threshold. There is no private snapshot injection or ability to
  resume from a peer-provided checkpoint.
- Calls on one verifier are serialized. Decoded input is privately owned and
  returned headers are copied. Callers must not mutate input buffers during calls
  and must bound verifier instance count, incoming transport buffers and retries.
  Fresh-engine reconstruction is intentionally slower than a production client.

## Evidence and limits

Tests include deterministic synthetic secp256k1-signed progression through block
199, official-engine differential acceptance/rejection checks, invalid signatures,
wrong chain signing domain, unauthorized/recent signers, incorrect difficulty,
gas, timing, ancestry, fork fields, malformed/trailing RLP, count/byte limits,
copy isolation, concurrent append and failure atomicity. Official mainnet and
Chapel genesis fixtures are regenerated from the pinned source byte-for-byte.
A genuine signed mainnet block 1 header from the official Greenfield BSC Archiver
README passes both the official engine and exported constructor/Append path;
its exact hash is recomputed and mutated-root rejection tested. This static
source was retrieved without RPC; see the fixture README for provenance.
Synthetic anchors are inaccessible through the exported constructor.

These tests establish a bounded bootstrap experiment, not live operation. There
are no validator/fork-transition fixtures, multi-block real successor sequence,
or BLS coverage. Production progression still needs authenticated checkpoint/snapshot
bootstrap, bounded dependency-aware retention, verified reorg/finality handling,
real public-chain fixtures, restart behavior, and independent review. Do not
raise `MaxHeaders` or relax the fork guards to turn this into a live client: that
would invalidate the memory, fallback and execution-boundary arguments.

Official sources at pin `54afaff4da31ac9e49ba646a4f65104edac5a2f3`:

- [Engine](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/consensus/parlia/parlia.go)
- [Snapshots](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/consensus/parlia/snapshot.go)
- [Genesis](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/core/genesis.go)
- [Chain configurations](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/params/config.go)

## Why extension is blocked and what is needed next

Crossing an epoch is not a matter of increasing a constant. `VerifyHeader` checks
a signed validator-list commitment but does not check that the list is the one
returned by the stateful validator system contract; `verifyValidators` and
`verifyTurnLength` are called from `Finalize`. A continued header-only design must
explicitly accept validator-set changes endorsed by the existing consensus as a
light-client assumption, or supply independently execution-authenticated state
and contract results. The latter is outside this module and cannot be replaced
by a nil `ethAPI` or peer assertions.

A bounded recent bootstrap needs an **operator-authenticated complete bundle**:
network/genesis and pinned fork config, checkpoint header/hash, checkpoint TD if
absolute TD will be advertised, full corresponding Parlia snapshot (validator
addresses, ordered BLS keys/indexes, recent signers/fork hashes, epoch/block/turn
parameters and justification/finality state), supporting ancestor/epoch headers,
and provenance/freshness policy. Authenticating the header alone does not
validate the snapshot. A state-free trusted snapshot import/export API is not
available as a stable public constructor here. The existing alternative is a
version-locked private JSON snapshot DB record at a 1,024-aligned checkpoint,
which requires strict bundle validation and compatibility tests before use.

Next implementation therefore needs either a reviewed upstream public snapshot
bootstrap interface or that carefully tested private adapter; genuine recent
bundles and transition/BLS sequences; dependency-aware bounded history/snapshot
retention that halts on missing context; and restart/reorg/finality tests. The
current package deliberately offers no checkpoint constructor, snapshot import,
config override or fallback to bridge this gap. It is a safe stepping stone, not
completion of current-head tracking or confirmations.

## Local verification (2026-11-10)

Using the project's Go 1.26.10 toolchain:

- `go test -count=1 -timeout=120s ./...`: passed.
- `make check`: passed (vet, complete race suite, relay build, gofmt check,
  module verification and runtime dependency guard).
- `go test ./internal/firstepoch -run='^$' -fuzz=FuzzAppend -fuzztime=20s -parallel=1 -timeout=90s`:
  passed, but only 7 executions were reported, so this is a limited fuzz smoke
  check, not broad fuzz coverage. An earlier 10-second/two-worker run ended with
  `context deadline exceeded`, without a reported failing input or crash corpus.
- `git diff --check`: passed.

Exact fork-config fingerprints and a test over all serialized Block/Time fork
fields guard the no-transition assumptions. The bounded snapshot test rebuilds
at the maximum supported height and asserts exactly one genesis snapshot record,
with a value under 64 KiB. Tests do not substitute for an independent review or
real transition/BLS coverage.
