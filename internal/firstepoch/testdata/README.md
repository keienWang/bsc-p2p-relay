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
checkpoint or public-chain body/receipt fixture is included.

Independent cross-check: the block 2 test in
[ethers-io/ancillary-bsc](https://github.com/ethers-io/ancillary-bsc/blob/559783304776d82da943fba2be43bfe99e93afc3/src.ts/index.tests.ts)
uses this same block 1 hash as its parent. That source omits roots and the full
bloom needed to reconstruct block 2, so it is only corroboration, not another
verified header fixture; missing values are not guessed.

License note: these files contain public chain header facts, not copied client
implementation code. The genesis generator is from BSC's LGPL-3.0-or-later client
library; its license remains upstream. The Archiver README source retains its
upstream provenance and notices; no additional license grant for that repository
is asserted here. New verifier and test implementation code uses this project's
GPL-3.0-or-later SPDX declaration.
