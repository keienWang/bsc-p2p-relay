# Stage 2: conditional evidence, then authenticated headers

Status: **live header verification and confirmation APIs remain disabled**. The
new `internal/evidence` package is offline and has no network, RPC, persistence,
or relay integration. Passing its checks does not authenticate the header.

See the separate [checkpoint bootstrap proposal and compatibility proof](CHECKPOINT_BOOTSTRAP_DESIGN.md)
for the next research step. Its bundle schema and resource policies are proposals,
not an implemented importer or enabled live-head verification.

## Implemented boundary

`CheckCompleteLists` accepts a supplied header plus two complete RLP lists: the
block's transaction list and its eth/68 receipt list. These are not entire P2P
response packets. It checks encoding, count, matching transaction/receipt types,
and transaction and receipt trie roots using the pinned BSC types and stack trie.
The output explicitly says `HeaderAuthentication: "not_checked"`. Receipt
outcomes are conditional on the supplied header, with historical post-state-only
receipts reported as unknown. It never reports canonicality or finality.

The API deliberately takes serialized receipts: upstream encoding of an arbitrary
in-memory receipt normalizes nonzero status to success, whereas decoding rejects
invalid status encodings. RPC-only transaction hash/index/block fields are never
trusted. The transaction hash and list index are derived locally. Gas usage is
reported as the committed cumulative value; no ordinary-transaction gas rules are
invented for BSC system transactions. Logs, bloom, gas execution, signatures,
sender validity, uncles and withdrawals are not separately validated by this
commitment-only module. It does not reexecute the EVM.

Resource limits: at most 8 MiB per encoded list, 32,768 entries per list, and
64 KiB of header extra data. Encoded limits are checked before decoding and list
counts before allocating decoded objects. These are local policies, not protocol
maxima; a valid larger block is unsupported. Total memory includes bounded decoded
object/trie overhead and is not limited to the encoded byte count. Callers must
also bound concurrent invocations and incoming transport buffering. Nil lists are
not empty blocks: use explicit RLP empty lists. Errors return no partial result.
Callers must not mutate inputs concurrently.

## First-epoch prototype

The separate offline `internal/firstepoch` experiment now verifies direct
successors 1–199 from hash-pinned official genesis. It includes genuine mainnet
blocks 1–2 and official genesis fixtures, plus signed synthetic/differential tests.
The complete block 2 transaction list reproduces its authenticated header
commitment; receipt details remain unavailable and no outcome is inferred.
It stops before epoch transitions and cannot track current heads. See
[FIRST_EPOCH_HEADERS.md](FIRST_EPOCH_HEADERS.md) for its narrower trust model,
resource proof and remaining bootstrap blockers. Relay/API integration remains
disabled.

## Trust model and bootstrap blocker

The practical next stage is a **trusted-checkpoint consensus light client** with
bounded rolling state, not a full independently executing node. A peer's highest
head, largest claimed TD, or matching peer opinions cannot establish trust.

Require an operator-authenticated bootstrap bundle with chain/genesis identity,
pinned fork configuration, checkpoint header/hash, complete corresponding Parlia
snapshot, supporting ancestors/epoch headers, and provenance/freshness policy.
The snapshot must include validator addresses and ordered BLS vote keys/indexes,
recent signers, recent fork hashes, epoch/block/turn parameters, and justification
and finality state. A checkpoint hash alone does not authenticate an arbitrary
snapshot. Authenticating the bundle or deriving it from trusted history is
mandatory. Include checkpoint TD before advertising an absolute TD to peers.

No production bundle or genuine recorded transition/body/receipt corpus is
provided here. Until these exist, production head authentication stays disabled.
An operator-provided offline bundle does not require external RPC during relay
operation. Streaming all headers from genesis is an alternative bootstrap cost,
but does not remove the distinction between header consensus and execution.

## Reusing Parlia safely

At the reviewed pin, `Parlia.VerifyHeader` checks proposer seals, authorization,
recent-signing rules, fork fields, timing/difficulty and applicable BLS vote
attestations. `verifyValidators` and `verifyTurnLength` instead run in `Finalize`
and consult system-contract state through `ethAPI`. Header-only verification is
not independent validation of those state-derived rules or EVM results.

A narrow adapter can use `parlia.New(config, db, nil, genesisHash)` and implement
`consensus.ChainHeaderReader` on a bounded header DAG. Test this nil-state-API path
without calling stateful methods. Use `VerifyHeader` sequentially first and expose
only previously authenticated history to snapshot application. Snapshot internals
are not an exported stable API. An experimental adapter can preload the pinned
`parlia-<hash>` JSON DB record at a 1024-aligned checkpoint, but that private
contract requires version-locked compatibility tests and strict bundle validation.

Do not use upstream's long-ancestry trusted-checkpoint fallback for untrusted peer
data: it can initialize recent-signing state empty. Missing trusted context must
fail closed. Upstream periodic snapshot DB writes also need pruning; an ordinary
memory DB alone is not bounded memory.

Use `GetJustifiedNumberAndHash` and `GetFinalizedHeader` only with authenticated
history. Leave `VotePool` nil initially and verify header-contained attestations.
Their quorum keys come from the target-parent snapshot, not automatically the
current head's validator list. BLS verification requires correct source/target
ancestry and fork-specific snapshot update rules.

## Retention, forks and outcomes

Retain an authenticated anchor, genesis, bounded branch DAG and snapshots,
dependency ancestors/epoch headers, justified/finalized references, request state,
temporary bodies/receipts, and bounded watched transaction records. Signer history
length is `(validators/2 + 1) * turnLength - 1`; fork history is
`validators * turnLength`. Validator switching is delayed after epoch headers.
Attestation checks require additional target-parent/source dependencies. A fixed
arbitrary short ring is insufficient, and finality can stall indefinitely.

When required context exceeds resource budgets, suspend advancement and request
resynchronization/new authenticated bootstrap. Do not silently drop dependencies
and continue claiming security. Compare verified justification first, then locally
accumulated TD and upstream tie handling. A conflict below the finalized/trusted
anchor is a halt condition, not an ordinary reorg.

For each authenticated block, verify its full transaction list, find watched
hashes, obtain and verify the full receipt list against that same header, then
associate results by index. Track inclusion, outcome evidence, branch-dependent
depth, and BLS finality separately. Reorgs retract canonical inclusion/outcomes.
Unavailable receipts or expired watches remain unknown, never failed or absent.

## Wire and fork compatibility

Keep eth/68 initially. This pin's consensus receipt root uses typed receipt RLP
with status/poststate, cumulative gas, bloom and logs. eth/70 compact wire receipts
omit bloom and support partial responses; reconstruct consensus encoding and
complete the list before root verification. Raw compact bytes are not trie values.

Amsterdam introduces a separate security boundary: upstream documents that
`BlockAccessListHash` and `SlotNumber` are not covered by the proposer seal. They
are forbidden before activation, but their later validity requires other checks.
Mainnet/testnet Amsterdam activation is nil in this pin. Fail closed for unsupported
future fork semantics rather than promoting a seal-only result to an authenticated
block hash. Configuration and release updates require review and new fixtures.

## Verification required before live claims

Current synthetic tests exercise mixed legacy/access-list/dynamic-fee transactions,
additional blob/set-code envelope commitments and pool-sidecar rejection,
success/failure and post-state-only receipts, empty lists, invalid status, missing
and reordered receipts, mutated data/roots, malformed/trailing RLP, nil inputs and
resource bounds. Fuzzing must exercise decoding with the same conditional-output
invariant. Synthetic unsigned transactions are not broadcast or execution fixtures.

Before enabling authenticated heads, add signed header and BLS fixtures, real
public-chain snapshots and sequences crossing validator/fork boundaries, genuine
body/receipt pairs, differential checks against the pinned official engine, and
adversarial stale-snapshot, invalid-quorum/source/target, missing-ancestor, reorg,
resource exhaustion and restart tests. Cover every supported transaction type and
wire version before claiming that coverage. Unit tests alone do not establish
production readiness.

## Pinned official sources

Reviewed release: BSC v1.8.2, commit `54afaff4da31ac9e49ba646a4f65104edac5a2f3`.

- [Parlia engine and fork/header checks](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/consensus/parlia/parlia.go)
- [Snapshot application and validator transitions](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/consensus/parlia/snapshot.go)
- [Consensus reader interfaces](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/consensus/consensus.go)
- [Fast-finality fork choice](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/core/forkchoice.go)
- [Receipt consensus encoding](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/core/types/receipt.go)
- [eth/68 receipt encoding](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/eth/protocols/eth/receipt68.go)
- [Compact receipt encoding](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/eth/protocols/eth/receipt.go)
- [Packet and partial-receipt definitions](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/eth/protocols/eth/protocol.go)
- [Official chain configurations](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/params/config.go)
