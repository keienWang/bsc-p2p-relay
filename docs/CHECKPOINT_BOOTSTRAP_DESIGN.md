# Operator-authenticated Parlia bootstrap

Status: **proposed, not implemented**. Only synthetic compatibility tests exist.
No bundle decoder/importer, live-head integration or confirmation API is enabled.

Scope: design and test-only compatibility proof against official BSC v1.8.2,
commit `54afaff4da31ac9e49ba646a4f65104edac5a2f3`. Do not extend or modify the
genesis-only first-epoch experiment. No real bundle is supplied here.

## Evidence and review boundary

`internal/checkpointproof/checkpoint_test.go` runs the real Parlia engine using
this repository's unchanged dependency graph. `github.com/ethereum/go-ethereum`
resolves to the published `github.com/bnb-chain/bsc v1.8.2` module; the existing
security-patched transitive versions, Greenfield/Tendermint replacements and
`./compat/stunv2` adapter remain in force. No BSC source-directory replacement,
new module dependency, or alternative prototype module graph is used.

The synthetic proof covers private **Snapshot** JSON round-trip, checkpoint alignment,
full-snapshot reconstruction/restart equality, BLS/finality, missing target-parent
and delayed-epoch dependencies, Fermi depth 3 versus 4, corrupt BLS aggregates,
and one complete modern validator replacement with recency clearing. It also
checks quorum boundaries and that non-adjacent votes preserve accumulated
finality rather than replacing it with the latest vote data. Private loader
tests demonstrate acceptance of forged recency and mismatched number/hash fields,
and silent defaults of zero epoch/interval/turn values; these are trust hazards
the future adapter must reject before inserting any DB record.
A deliberately unsafe test reader also demonstrates that snapshot replay can
consume an invalid BLS vote even when `VerifyHeader` rejects the same header.
These tests record the pinned dependency boundary, not behavior a future upstream
version should preserve. Authentication of history and verify-before-reader
ordering are mandatory for any future adapter.

The synthetic fork schedule is not a realistic wire-header fixture: it activates
Bohr and supplies `ParentBeaconRoot`, but leaves Cancun inactive and the preceding
`WithdrawalsHash`, `BlobGasUsed` and `ExcessBlobGas` fields absent. Official
in-memory `VerifyHeader` accepts this artificial combination. Encoding and then
decoding its genesis or verified successor with official Header RLP fails at
`WithdrawalsHash` because of that optional-field gap. `wire_boundary_test.go`
records the observed limitation, not a requirement that real BSC headers fail.
Official Header JSON round-trip preserves these two fixture hashes and successor
verification, but is not a strict untrusted-header decoder or proof of P2P wire
compatibility. This proof has not validated canonical serialized modern headers.
Future wire/importer work needs a coherent fork schedule and canonical RLP
round-trip plus authentic real-chain fixtures, separately from this snapshot test.

These observations do not prove the proposed schema's completeness or all
membership/fork-transition invariants. The suggestions below need independent
review and adversarial/real-chain fixtures **before** implementing a strict
bundle decoder. No real BSC checkpoint or execution-derived transition fixture
is supplied. Tests construct deterministic synthetic keys in memory and perform
no external RPC or live transaction broadcast.

Run the focused proof with `go test -race -count=1 -timeout=180s ./internal/checkpointproof`.
Run the project's `make check` and `make vuln` as well. These commands are not
claims of production validation.

## 1. Trust and supported scope

Bootstrap is a trusted-checkpoint consensus light client, not execution validation.
Authenticate the **entire bundle**, including both snapshots and their finality
state, against an operator-provided out-of-band SHA-256 digest. Alternatively use
a separately reviewed signed-manifest format and operator-pinned public key. A
digest/signature distributed alongside an arbitrary peer's bundle is not trust.
Never accept a peer checkpoint, peer agreement, claimed TD, or header hash alone
as authentication of snapshot fields.

The operator must attest that snapshot seeds came from fully verified history
with no long-ancestry reconstruction shortcut. Local shape checks cannot prove a
seed's recent-signing map is complete or that its validator/finality state is true.
An authenticated checkpoint header authenticates hash-linked preceding header
bytes, but not an unrelated snapshot JSON record.
The digest test only demonstrates mutation detection with an already trusted
expected digest. It does not establish operator identity, implement signed
manifests, or make an operator-approved but incorrect snapshot true. Real bundle
authentication, provenance and rollback policy remain proposed work.

Start with a deliberately narrow modern profile: pinned network 56 or 97, after
Luban/Plato/Bohr/Lorentz/Maxwell/Fermi, with stable epoch length 1000 and block
interval 450 ms throughout the bootstrap suffix. Use the compiled official full
chain configuration, not user-supplied fork overrides. Freeze supported successor
fork semantics explicitly; reject Amsterdam and any unaudited future change.
The synthetic test configuration is never a supported network configuration.

This profile accepts consensus-endorsed validator and turn-length transitions as
a light-client assumption. `VerifyHeader` checks header structure, signer,
authorization, recency, timing, difficulty, fork fields and applicable BLS votes.
It does **not** call `verifyValidators` or `verifyTurnLength`: those are in
`Finalize`, consult system-contract state through ethAPI, and require execution.
Never call stateful preparation/finalization with nil ethAPI. BLS quorum cannot
replace validating the execution-derived membership/turn rules.

## 2. Bundle envelope and exact snapshot schema

Use a small versioned format with length-prefixed canonical bytes, or strict JSON
with a digest over the exact received bytes. For strict JSON, reject duplicate
members at every depth, unknown members, missing required members, trailing input,
noncanonical integer keys, floats, negative numbers, overflow and null where not
allowed. `DisallowUnknownFields` alone does not reject duplicate members.

Required envelope fields:

- Format ID/version; exact engine commit and private snapshot codec ID.
- Chain ID, official genesis hash and encoded genesis header; compiled config
  fingerprint, including all block/time forks, Parlia and blob schedule parameters.
- Main checkpoint `(C, hash, canonical header RLP)` with C positive and divisible
  by 1024. Include authenticated absolute TD only if the relay will advertise it;
  otherwise disable any absolute-TD claim. Verify a bounded nonnegative integer.
- Complete main snapshot C and complete support snapshot S = C - 1024, each with
  canonical header/hash binding. Require S > 0 for this modern-only profile.
- A sorted, contiguous, hash-linked supporting header suffix `[L, C]`, plus the
  official genesis header. Require strictly increasing numbers and exact parent
  hash links. Source/target finality references and required epoch ancestors must
  be in that suffix or be genesis. No disconnected untrusted number lookup.
- Issuer/provenance, issue time, expiry, supported profile and operator rollback
  policy. Validate against locally trusted identity, minimum checkpoint and age
  policy. Peers cannot update the operator's trust, freshness or rollback policy.

The versioned raw DB snapshot has **exactly** these keys:

```
number: uint64
hash: 0x-prefixed 32-byte hash
epoch_length: uint64
block_interval: uint64
turn_length: uint8
validators: {canonical 20-byte address: {
  "index:omitempty": integer,
  "vote_address": [48 integer bytes]
}}
recents: {canonical decimal uint64 string: canonical 20-byte address}
recent_fork_hashes: {canonical decimal uint64 string: 8 lowercase hex digits}
"attestation:omitempty": null | {
  "SourceNumber": uint64, "SourceHash": 32-byte hash,
  "TargetNumber": uint64, "TargetHash": 32-byte hash
}
```

The colon-containing names are literal upstream tags, not typos to repair. Fixed
48-byte arrays serialize as JSON arrays, not base64 or hex. The hash/address Go
types use hex strings. Marshal a strictly validated owned DTO into the exact
pinned record format; do not pass arbitrary peer JSON directly to upstream.

Proposed checks for a future decoder, not implemented acceptance rules:

- `number/hash` exactly match its specified header and key; numbers fit the
  adapter's supported range, e.g. below 2^63. Both seed heights are 1024-aligned.
- Require every field explicitly, including non-null maps. Reject zero epoch,
  block interval and turn length; upstream silently defaults these otherwise.
  For this stable modern profile epoch=1000, interval=450. Check fork predicates
  on headers and reject a suffix spanning an unsupported transition.
- Require 1..64 distinct nonzero validator addresses. Treat this as a local policy
  limit, not the header format's count maximum. Sorted ascending address order
  must give indexes exactly 1..V, with non-null validator records. Require 48-byte
  BLS keys to pass the pinned public-key parser/subgroup check; reject duplicate
  keys as a conservative supported-profile restriction. Older zero-key Luban
  activation records are unsupported, not silently upgraded.
- Require turn length 1..32 as a local resource/support policy. Reject a future
  header's announced set or turn that exceeds policy *before* it can enter engine
  snapshot application. Use checked arithmetic for all lengths and heights.
- Compute H=(floor(V/2)+1)*T-1 and F=V*T using checked arithmetic. Require
  H < epochLength for this proposed conservative profile. The individual V/T
  limits alone are insufficient: V=64 and T=32 gives H=1055, beyond the
  supported epoch length 1000. Upstream uses a modulo-epoch switch position,
  so this adapter must reject that unaudited combination rather than assume
  the delayed switch will be reachable. This is a local unsupported-profile
  decision, not a claim that the upstream consensus rules are wrong.
- Ordinary `recents` entries must have heights
  in [N-H,N], nonzero addresses and match the authenticated header's recovered
  signer. Whether every entry may be required to belong to the *current*
  validator set needs the membership analysis below; do not yet implement that
  as a general importer rule. Stored map length can be H+1, although active
  `countRecents` excludes the left endpoint. Maxwell finality pruning and Bohr
  switches legitimately make this map sparse; do not fill missing entries.
- **Preserve the epoch sentinel.** Bohr writes zero address at
  `MaxUint64 - (switchHeight / epochLengthAtSwitch)`. There may be one such
  high-numbered entry in addition to ordinary history. Its index is not a block
  height. Reject all other future-height entries and zero-address ordinary
  entries. For the trusted seed, validate the sentinel's shape/range and preserve
  it exactly; its exact switch provenance is part of operator trust. Check exact
  sentinel evolution when replaying the authenticated suffix. Inferring it only
  from the current validator count can be wrong after a delayed set-size change.
- `recent_fork_hashes` must be bounded by F, keyed inside [N-F+1,N], and match the
  last four vanity bytes of the corresponding authenticated headers. Sparse
  history can occur after an expanding validator set. Do not invent entries.
- Attestation may be null if no attestation ever established state. If present,
  require SourceNumber < TargetNumber < N and exact referenced hashes/headers on
  the retained authenticated branch (genesis allowed). Source is the finalized
  reference and target the latest justified reference. The object is accumulated
  state, **not necessarily the vote data in the checkpoint header** and contains
  no BLS signature. Do not replace it with that header's attestation or reset it.
- These checks establish type, bounds and cross-field consistency, not the
  completeness/truth of seed history. Compare reconstruction from S with the
  entire supplied C snapshot as an additional consistency/codec check.

### Recent-signer membership: what is and is not established

At a Bohr-active validator switch, `Snapshot.apply` verifies the switch header
against the old set, adds its signer, then replaces the entire `Recents` map
with a zero-address epoch sentinel before replacing `Validators`. The included
replacement test announces three wholly new validators at epoch 2000, retains
old-validator history through block 2062 without new finality pruning, checks
that old-set-signed block 2063 clears it, and accepts the new set at 2064.
This test establishes that particular modern delayed-switch path.

Source inspection suggests an induction for a correctly initialized, stable
post-Bohr snapshot: ordinary entries are added only for the active set, and every
actual set replacement clears them. A repeated-switch sentinel skips the entire
replacement, not only the clear. But this is **not yet a general validated schema
invariant**. Pre-Bohr replacement does not clear all history, so departed members
can legally remain; being merely after the Bohr activation timestamp is not proof
that inherited history has drained. Set growth/shrinkage, turn changes, sentinel
collision/guard behavior, activation boundaries and complete official fixtures
still need review. An authenticated seed is a trust assertion, not automatically
proof of those conditions. Until that scope is established, preserve and bound
its history rather than rejecting entries solely for current-set nonmembership.

## 3. Exact dependencies and public/private adapter boundary

`parlia.New(config, db, nil, genesisHash)` is usable for header-only methods.
Public `Parlia.APIs(reader)` exposes a `*parlia.API` with `GetSnapshotAtHash` and
`GetSnapshot`. It can export an existing/reconstructed snapshot in process, without
external RPC, but there is no exported constructor/import setter for complete
engine state. Exported Snapshot fields alone do not initialize private fields or
the engine's cache. Avoid unsafe/linkname/reflection mutation.
The returned snapshot can alias engine cache state: export an owned serialized
copy while calls are serialized, and never expose the mutable pointer to callers.

The pinned private import is `db.Put("parlia-" || rawHash32, jsonBlob)`.
`snapshot()` only attempts DB loads at heights divisible by 1024. Its loader does
not validate number/hash binding, map contents, indexes or complete recency and
defaults zero fields. The adapter, not upstream, must perform the checks above.
Lock compatibility tests to the exact source pin and reject unknown codec pins.

For candidate n=C+1:

- Parent header and snapshot C are needed for ordinary checks.
- Before Fermi the only eligible vote target is n-1; after Fermi targets are
  n-1, n-2 or n-3. The quorum snapshot is **target-1**, hence C-1 through C-3 may
  be needed immediately. Snapshot C plus three headers does not supply those
  earlier snapshots. S=C-1024 supplies a bounded reconstruction base.
- For applying S+1 through C (or later), the delayed validator switch at height
  k needs the epoch header found by walking H ancestors, using the pre-switch
  snapshot's V and T. If that epoch predates S, include every intermediate header
  down to it. No header-number-only shortcut is safe. Set L to the minimum of
  these epoch dependencies, all ordinary recency/fork-history keys in both seeds,
  and both seeds' non-genesis source/target references. If dependency closure
  exceeds budget, reject rather than truncate. After set/turn changes recompute
  closure; do not assume an arbitrary fixed short ring always suffices.

Authenticate/shape-check the full bundle first, then construct a reader exposing
only its trusted history. In a fresh engine with **only S imported**, call the
public snapshot export for C and compare every exported field with supplied C.
This uses `snapshot.apply`, not full header verification; its input suffix must
already be operator-authenticated. It does not prove execution or historical BLS
validation. Then build the operational engine with both verified seed records.
Verify new headers sequentially. Only after `VerifyHeader` succeeds may a candidate
enter the reader or snapshot application. Do not use batch candidate parents or
snapshot getters on unverified candidate headers.

Keep VotePool nil. On authenticated history, `GetJustifiedNumberAndHash` returns
the accumulated target; `GetFinalizedHeader` follows the accumulated source.
`updateAttestation` keeps the previous finalized source when a new vote has a
non-adjacent source/target, and Fermi permits non-parent targets. Before Plato,
upstream may accept malformed attestations; that era is outside this profile.
Keep authenticated genesis available: nil-attestation justification uses it and
can dereference a nil genesis if the reader is incomplete. Never turn nil/error
finality into a successful or newly finalized result.

## 4. Bounded runtime and fail-closed behavior

Suggested initial **local policy**, not consensus limits: 8 MiB bundle, 4096
supporting headers, 64 KiB per header, 32 KiB extra data, 512 KiB per snapshot,
64 validators, turn<=32. Bound encoded bytes *before* allocation, all counts,
retained branch count, candidate concurrency and decoded/cache overhead separately.
If a real valid bundle exceeds policy, report unsupported and require a newer
checkpoint or explicitly reviewed limits.

For the first adapter, use a fresh engine and in-memory DB per operation, populated
only with owned authenticated base snapshots and the bounded history. Enforce a
header-lookup work budget and maximum ancestry depth safely below upstream's
600,000-header fallback threshold. Expose no on-demand old history fetch from the
reader. Missing DB record, malformed record, missing header, exhausted counter or
missing finality dependency must abort, not select another trust mode. The ordinary
fallback cannot be reached with a <=4096-height contiguous suffix and no outside
ancestors, even if the private DB record fails to load.

Fresh engines bound only Parlia-instance cache lifetime, including failed
candidates. The fixed instance capacities are 1280 snapshots, 4096 signatures
and 86400 double-sign keys;
DB checkpoint writes occur every 1024 and are **not** automatically pruned.
An ordinary memory DB with a long-lived engine is therefore not a bounded design.
Prysm v5.3.2 also maintains a **process-global** BLS public-key cache capped at
2,000,000 keys, with a 200,000-entry channel and a permanent goroutine. Engine
closure neither clears nor stops it. Consequently, fresh engines are not a
complete process-memory or concurrency boundary. A concurrent cold-key parse
race has been reproduced; see [the known issue and diagnostic](BLS_CACHE_KNOWN_ISSUE.md).
Its memory/concurrency/lifecycle audit or independently verified process
isolation is a blocker before any importer or live BLS verification feature.
Authenticate the digest and enforce byte/count limits before public-key parsing,
and account for every BLS caller in the process, not just a verifier-instance lock.
For efficient long-lived operation, implement a reviewed namespace/key/byte/count
bounded DB and dependency-aware pruning, including per-branch snapshots and cache
lifetime. Do not evict a base until its replacement is independently derived from
already authenticated history. Restart exports must authenticate the whole saved
state or replay from the operator bundle; a saved header hash is not sufficient.

Initially stop when the bounded suffix fills. Later roll to locally derived
aligned checkpoint pairs, preserving epoch, vote, justified/finalized and branch
dependencies. Finality can stall indefinitely; halt and request a fresh trusted
bootstrap when it exceeds budget. No silently discarded finality references.
Reorgs below the trusted/finalized anchor halt. Fork choice and transaction outcome
APIs remain separate work and must not be enabled by this prototype.

## 5. Delivery sequence and required tests

1. Review the test-only compatibility proof and this proposal independently in
   the normal repository, especially the seed-trust boundary and unproved schema
   invariants. Do not treat the proposal as a ready-to-implement specification.
2. Only after that review, implement a pure bundle decoder/authentication layer,
   exact JSON/private codec and network/config pin checks. No network or live-head
   interface.
3. Bounded reader/DB wrapper, dependency closure, transactional import, restart
   consistency check S->C, then offline successor verification only.
4. Genuine operator-authenticated public-chain bundles and sequences crossing
   delayed validator/BLS-key/turn changes and supported block/time forks. Obtain
   them separately; synthetic replay is not a substitute.
5. Independent security review before live integration, rolling state, reorgs,
   transaction evidence and confirmation claims.

Required negative tests: every missing/unknown/duplicate/null field; defaults,
overflow, malformed RLP/JSON, truncated/trailing bytes, wrong chain/config/pin,
unknown operator, wrong digest/signature, stale/rollback bundle, mismatched header
and DB key, wrong indexes/keys/recents/sentinel/fork history/finality refs, oversized
maps, and forged but correctly shaped state with unchanged header hash.
Dependency tests must remove each required ancestor/snapshot, omit genesis, corrupt
or misalign the DB record, exceed budgets and prove fallback stays unreachable.
Vote tests must cover target-parent membership across a change, quorum boundaries,
invalid bitsets/key/subgroup/signature/source/target, Fermi depth 1/2/3 versus 4,
stalled and non-adjacent finality and conflicting branches. Differential tests
must compare full snapshot fields and header acceptance against an independently
executing official node/recorded fixture, plus fuzzing, failure atomicity, restart,
bounded long-run memory and DB pruning.

The included test-only proof covers the evidence list above. It does not implement
the proposed schema validator, authenticated file importer or resource wrapper.

## Local verification (2026-11-10)

The optional `blscacherace` diagnostic is a known **failing** concurrent-cache
scenario, not part of the enabled sequential proof suite. A default passing
suite or static vulnerability scan does not cover or resolve that finding.

Using Go 1.26.10 and the dependency baseline published at
[`37f461ad`](https://github.com/keienWang/bsc-p2p-relay/commit/37f461ad0af5bbbf50c38861f9220476b5cc04a8):

- An intermediate full-suite run exceeded the old 120-second test deadline in
  the existing first-epoch reconstruction test during concurrent local work.
  No assertion failure or race in that test was reported. The failure was
  retained, not treated as a pass. `TEST_TIMEOUT` is now explicitly configurable
  with a finite 300-second default for these expensive offline tests; this does
  not change any production/network timeout. The final suite passed with serial
  package scheduling and less concurrent local load.
- Integrated `GOFLAGS="-buildvcs=false -p=1" make check smoke vuln`: passed.
  The checkpoint and first-epoch race packages took 25.750s and 43.035s.
  This includes vet, the complete
  five-package race suite, relay build, 10 Python tests, formatting/module
  verification, the command/test dependency guard and process smoke.
- The integrated `govulncheck@v1.9.0 ./...` result is `No vulnerabilities found.`
  with no additional package/module notices in its analyzed graph. Earlier
  prototype runs used an older dependency baseline and are not this record.
  The scan is not a claim that the entire upstream module graph is advisory-free.
- This test/docs batch changes no `go.mod`, `go.sum` or replacement. The
  first-epoch source and scope are unchanged. No genuine modern-checkpoint
  fixtures or live-network tests were run; this is not production validation.

## Pinned source references

- `consensus/parlia/snapshot.go`: Snapshot/ValidatorInfo tags and load/store;
  countRecents; updateAttestation; apply, delayed switches and FindAncientHeader.
- `consensus/parlia/parlia.go`: snapshot (around 805), verifyVoteAttestation (472),
  verifyCascadingFields (699), verifySeal (973), stateful verifyValidators (1244),
  verifyTurnLength (1289), APIs (1943), justified/finalized getters (2262/2286),
  epochLength (2443), GetAncestorGenerationDepth (2507).
- `consensus/parlia/api.go`: in-process snapshot export surface.
- `core/types/vote.go`, `core/types/block.go`, `params/config.go`: vote schema,
  real proposer SealHash, immutable network/fork configuration.

All paths refer to [the official pinned source](https://github.com/bnb-chain/bsc/tree/54afaff4da31ac9e49ba646a4f65104edac5a2f3).
