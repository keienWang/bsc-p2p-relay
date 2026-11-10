# Official genesis fixtures

These two RLP-encoded **headers**, not full blocks, are generated offline from
BSC v1.8.2 commit `54afaff4da31ac9e49ba646a4f65104edac5a2f3`:

- `mainnet-genesis.rlp`: `core.DefaultBSCGenesisBlock().ToBlock().Header()`,
  hash `0x0d21840abff46b96c84b2ac9e10e4f5cdaeb5693cb665db62a2f3b02d2d57b5b`.
- `chapel-genesis.rlp`: `core.DefaultChapelGenesisBlock().ToBlock().Header()`,
  hash `0x6d3c66c5357ec91d5c43af47e234a939b22557cbb552dc45bebbceeed90fbe34`.

Encoding: `rlp.EncodeToBytes(header)`. `TestFixtureProvenance` regenerates both
from the pinned official source and compares exact bytes on every test run.
`TestOfficialGenesisAnchors` separately checks the pinned chain hash constants.

Source: https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/core/genesis.go

## Signed mainnet block 1

`mainnet-block1.json` contains only the header fields from the example response in
[the official Greenfield BSC Archiver README](https://github.com/bnb-chain/greenfield-bsc-archiver/blob/d616dedb2f62c64f1d5f68d0e2d3c3e3d1e8beca/README.md).
Retrieved read-only on 2026-11-10. The source commit
`d616dedb2f62c64f1d5f68d0e2d3c3e3d1e8beca` was independently resolved with
GitHub commit metadata, and the README was fetched at that exact ref. README Git blob:
`02c716579d479bce4837b62c1bcf6cfd5c9db499`. Its fields were extracted directly
from the file, without making the example RPC request. Transaction contents,
claimed TD and RPC metadata were not included. The `hash` field is source
metadata, not trusted input: the test independently derives and checks
`0x04055304e432294a65ff31069c4d3092ff8b58f009cdb50eba5351e0332ad0f6`, then verifies
the proposer signature and progression from the official mainnet genesis.

`TestGenuineMainnetBlockOne` passes both the official engine and exported
constructor/Append path, and rejects a mutated state-root commitment. This is a
real signed public-chain successor, but not evidence of execution or finality.
Other signed successor tests use deliberately synthetic validators and anchors,
which the exported constructor rejects. No external RPC or private chain
material was used. No validator/fork-transition sequence, BLS fixture, current
checkpoint or complete public-chain body/receipt pair is included. The block 2
fixture below adds a complete transaction list, but no receipts.

Independent cross-check: the block 2 test in
[ethers-io/ancillary-bsc](https://github.com/ethers-io/ancillary-bsc/blob/559783304776d82da943fba2be43bfe99e93afc3/src.ts/index.tests.ts)
uses this same block 1 hash as its parent. That source omits roots and the full
bloom needed to reconstruct block 2, so it is only corroboration, not another
verified header fixture; missing values are not guessed.

License note: these files contain public chain header and transaction facts, not copied client
implementation code. The genesis generator is from BSC's LGPL-3.0-or-later client
library; its license remains upstream. The Archiver README source retains its
upstream provenance and notices; no additional license grant for that repository
is asserted here. New verifier and test implementation code uses this project's
GPL-3.0-or-later SPDX declaration.

## Signed mainnet block 2 and complete transaction list

- `mainnet-block2.json`: complete header fields, projected without alteration
  from the raw archival object. Optional post-fork fields in the source are null
  and omitted. `hash` is expected source metadata, checked by local recomputation.
- `mainnet-block2-transactions.json`: the complete one-transaction list, retaining
  all canonical legacy transaction fields and its expected hash. Location fields
  (`from`, block hash/number and index) are omitted and not used as evidence.
- `mainnet-block2-provenance.json`: exact URL, recorded retrieval time, HTTP status,
  original byte count and SHA-256. The raw object's unrelated metadata is not
  committed to this repository.

Source: ordinary static HTTP GET of
https://gnfd-mainnet-bundle.nodereal.io/v1/view/mainnet-bsc-blocks/blocks_s0_e99/block_h2
(final URL unchanged; HTTP 200; 2,328 bytes; no truncation). Recorded retrieval
UTC: `2026-11-10T16:06:13.153796+00:00`. Original raw-byte SHA-256:
`a61a8b178c5ceebebde293b94eca29719c72731149b17f9b8a6b381224356bce`.
The bundle name was checked using the static metadata endpoint recorded in the
provenance file. No JSON-RPC call, browser UI, authentication or transaction
submission was used. This test work consumes the previously downloaded object
without further network requests.

Locally recomputed block hash:
`0xe9296d312a937cdefc201a8fe80dbfa8a9c958ada9e863ddbb324804722f1de0`.
Locally recomputed transaction hash:
`0x1d06a9d52255a2a4385d55093aec7671f3d7f6d83d4cd438991be8b6588e9b91`.
Both also appear in the pinned independent ethers-io source linked above. The
complete header's roots and bloom come from this archival object, not guessed
values added to ethers' partial fixture. Archive metadata and claimed TD are not
trust anchors: the header passes official Parlia checks from the authenticated
mainnet genesis→block 1→block 2 chain.

The sole signed legacy transaction is decoded without signing or changing its
fields. Binary round-trip and complete-list trie reconstruction match the
header's `transactionsRoot` commitment. The large gas limit belongs to the
historical system transaction as supplied; no normal-pool or execution-validity
claim is made. Tests mutate committed header fields and transaction payload,
remove the transaction and duplicate it; none passes the relevant authentication
or commitment check.

There are **no receipt details** in the downloaded object. The receipt root is
only a sealed header commitment; it does not disclose success, failure or logs.
Tests ensure absent or empty receipt lists do not produce a result through the
existing complete-list evidence checker. No receipt is invented. This fixture
proves a transaction-list commitment under a consensus-authenticated header, not
execution, canonicality, confirmations, BLS finality, or permission to broadcast
this historical transaction. No production source code or package scope changes.

The static object contains public blockchain facts. Its provider retains any
applicable source/service rights; this repository asserts no additional license
grant over the provider's material. Only the necessary factual fixture fields
are retained, with provenance. Test implementation remains GPL-3.0-or-later.
