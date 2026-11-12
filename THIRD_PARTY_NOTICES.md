# Third-party notices

## New relay source

New source written for this relay is licensed under the GNU General Public License, version 3 or (at your option) any later version: **GPL-3.0-or-later**. The GPL version 3 text is included in `LICENSE`.

## BNB Smart Chain / go-ethereum

This project depends on the official [bnb-chain/bsc](https://github.com/bnb-chain/bsc) fork of go-ethereum, pinned to **v1.8.2**, reviewed at commit **54afaff4da31ac9e49ba646a4f65104edac5a2f3**. The dependency retains the module/import name `github.com/ethereum/go-ethereum`; the relay's Go module explicitly replaces it with `github.com/bnb-chain/bsc v1.8.2`.

The upstream repository describes library code outside `cmd` as LGPL version 3 and command/binary code inside `cmd` as GPL version 3. Reviewed library source headers grant LGPL version 3 or later. Upstream copyright and license notices remain applicable and are not replaced by the relay's source license.

`COPYING.LESSER` contains the official GNU Lesser General Public License version 3 text. `LICENSE` contains the official GNU General Public License version 3 text, which the LGPL supplements. These license texts have been copied verbatim from the official BSC repository.

Official pinned references:

- [Upstream license description](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/README.md#license)
- [Upstream GPL text](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/COPYING)
- [Upstream LGPL text](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/COPYING.LESSER)
- [Release v1.8.2](https://github.com/bnb-chain/bsc/releases/tag/v1.8.2)

## Other dependencies and distribution

BSC and the Go module graph include additional third-party components with their own licenses. This notice is not an exhaustive license inventory or a substitute for the notices in those modules. Before distributing a built binary, inspect the complete resolved dependency graph, preserve applicable notices, and meet corresponding-source and other license obligations. No binary-distribution compliance audit is claimed for this milestone.

## Pion STUN compatibility update

The project-owned `compat/stunv2` adapter is GPL-3.0-or-later and forwards the subset of the STUN v2 public API consumed by pinned BSC to `github.com/pion/stun/v3 v3.1.7`, using DTLS v3.1.5. No Pion implementation is copied into the adapter. The Pion implementation retains its MIT license, reproduced for reference in `compat/stunv2/UPSTREAM_LICENSE.txt`. Other module notices remain applicable. This adapter is not a complete STUN v2 implementation.

## Experimental Go vulnerability-scanner patches

`research/scanner-synthetic` contains patch-only experimental work based on
[`golang/vuln e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5`](https://github.com/golang/vuln/tree/e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5),
not an official scanner release or endorsed fix. The modified upstream files are
`internal/vulncheck/source.go` (Copyright 2021 The Go Authors),
`internal/vulncheck/packages.go` (Copyright 2023 The Go Authors), and
`internal/scan/run.go` (Copyright 2022 The Go Authors).
Their original BSD-3-Clause `LICENSE` and `PATENTS` are retained verbatim in that
directory. New explicitly BSD-marked scanner contributions are Copyright 2026
The bsc-p2p-relay contributors, under the complete terms in
`research/scanner-synthetic/LICENSE.CONTRIBUTIONS`. This is an explicit scoped
exception to the main relay source license, not a relicensing of the relay.
No complete upstream fork or scanner binary is distributed by this directory;
complete dependency and binary-license auditing remains outstanding.
