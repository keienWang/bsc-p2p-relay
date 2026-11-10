# BSC P2P Relay

A Stage 1, memory-only relay for **already signed BNB Smart Chain transactions**, using devp2p directly. No external RPC service and no chain database are required. This is an experimental transport component, not a full node, wallet, validator, or production transaction-delivery service.

**HTTP acceptance means local queue admission only. It does not mean a peer accepted the transaction, that it propagated, or that it was included or finalized.**

Repository: https://github.com/keienWang/bsc-p2p-relay. Go module: `github.com/keienWang/bsc-p2p-relay`.

The BSC STUN compatibility adapter and its security rationale are documented in [compat/stunv2](compat/stunv2/README.md) and [the CI security fix](docs/CI_SECURITY_FIX.md).

## Scope

- Accept signed raw transactions over a loopback-only HTTP API.
- Validate canonical encoding, replay protection, chain ID, and recoverable signature locally.
- Support transaction types 0 (legacy), 1 (access list), and 2 (dynamic fee). Blob and set-code transactions are rejected.
- Connect to BSC peers using eth/68 and BSC's Status plus UpgradeStatus handshake.
- Keep submitted transactions in a bounded, expiring in-memory cache and queue broadcasts to ready peers.
- Answer requests for cached transactions; answer unavailable chain-data requests with empty results.

The relay process provides no state, nonce, balance, fee-market, execution, authenticated inclusion, or finality verification. Transactions may be invalid against the current chain even after local validation. Peer retention and delivery are not guaranteed. Restarting loses the transaction cache and queues.

## Build and test

Use Go **1.26.10 or a newer supported security-patched version** with normal Go build prerequisites for the target platform. Dependencies come from the public Go module ecosystem. The module replaces `github.com/ethereum/go-ethereum` with `github.com/bnb-chain/bsc v1.8.2`.

```sh
go mod download
go test ./...
go build -o bsc-p2p-relay ./cmd/bsc-relay
./bsc-p2p-relay --help
```

These are commands to run, not a claim that tests have passed. No live transaction broadcast tests are included in this milestone's verification claims.

## Run

```sh
./bsc-p2p-relay --network mainnet
```

Defaults:

- HTTP API: `127.0.0.1:8787`, loopback only.
- P2P listener: `:30311`.
- Mainnet discovery: official BSC mainnet bootnodes.
- P2P identity: ephemeral node key, unrelated to wallet signing keys.

Use `--no-discovery` to disable discovery and repeat `--peer` to supply static enode URLs:

```sh
./bsc-p2p-relay --network mainnet --no-discovery --peer 'enode://PUBLIC_KEY@HOST:PORT'
./bsc-p2p-relay --network testnet --no-discovery --peer 'enode://PUBLIC_KEY@HOST:PORT'
```

Replace those illustrative enode values with actual peers. Testnet requires explicit peers; it does not use mainnet bootnodes. Network selection determines chain ID: mainnet 56 or testnet 97. Use `--help` for the complete implemented flags.

Do not expose the HTTP API through a reverse proxy, tunnel, or public port forward. It has no authentication and this milestone disallows a remote API listener. The P2P listener is a separate network surface.

## HTTP API

### POST /v1/transactions

Body:

```json
{"raw_tx":"0x..."}
```

Supply a canonical, signed transaction for the selected chain. Sign elsewhere. Never send seed phrases, private keys, or wallet passwords to this process.

On accepted submission, HTTP 202 returns `hash`, `sender`, `queued_peers`, `duplicate`, and `stage` (`queued` or `cached`).

- `hash` is the locally calculated transaction hash.
- `sender` is recovered from the signature.
- `queued_peers` counts local peer queue admissions, not acknowledgments from the network. It can be zero.
- `duplicate` identifies an already cached submission; it is not an inclusion check.
- `stage: queued` means at least one local queue admission. `stage: cached` means a duplicate has no new eligible peer.
- With no ready peer, the request returns HTTP 503. A full cache or full eligible peer queues returns HTTP 429.
- Resubmitting a cached transaction can enqueue it to a newly connected peer. The same transaction is not repeatedly queued to the same peer within its TTL.

Request size and resource limits apply. Invalid or unsupported transactions are rejected. A successful HTTP response is not a receipt and provides no execution guarantee.

### GET /healthz

Returns process health and relay metrics. Health does not establish network delivery or a correct chain view.

### GET /readyz

Reports whether peers are ready for relay transport. Readiness does not prove peer honesty, chain synchronization, or transaction acceptance.

## Resource bounds

The transaction cache is bounded by 1,024 entries, 16 MiB, and a two-minute TTL. Each peer has a 64-item outbound queue. These are best-effort transport buffers, not durable delivery guarantees. Expiration, capacity limits, disconnects, and process exit can all prevent delivery.

See [production acceptance gates](docs/PRODUCTION_READINESS.md) before considering deployment.

## Offline evidence helper

`internal/evidence` checks complete transaction and eth/68 receipt lists against a supplied header's roots. Its result explicitly says the header was not authenticated. It is not wired to the relay or any confirmation API. See [the Stage 2 trust design](docs/VERIFIED_HEAD_DESIGN.md).

Near-capacity in-memory load tests and measured limitations are recorded in [performance checks](docs/PERFORMANCE.md).

## Next stage

Stage 2 would require independently anchored ancestry and Parlia consensus verification, plus inclusion/receipt evidence and reorganization handling. Merely copying peer heads or requesting receipts is not independent verification. This is substantial separate work, not an enabled feature or a promised delivery date.

See [architecture](docs/ARCHITECTURE.md), [security](docs/SECURITY.md), and [third-party notices](THIRD_PARTY_NOTICES.md).

## License

New relay source is GPL-3.0-or-later. See [LICENSE](LICENSE). BSC library components retain their upstream LGPL-3.0-or-later notices; see [COPYING.LESSER](COPYING.LESSER) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
