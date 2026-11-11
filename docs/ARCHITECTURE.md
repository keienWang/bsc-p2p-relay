# Architecture: Stage 1

## Data flow

1. A local caller signs a transaction outside the relay.
2. The loopback HTTP endpoint decodes a bounded request and validates the signed transaction's canonical encoding, supported type, replay protection, selected chain ID, and recoverable sender.
3. The transaction enters a bounded, TTL-based memory cache. Its hash supplies the deduplication key.
4. Ready peers receive bounded local queue entries for transaction broadcast.
5. The peer writer sends eth transaction messages. A completed write is not a peer acknowledgment or an inclusion result.

HTTP 202 with `stage: queued` means at least one local peer queue accepted the item. HTTP 202 with `stage: cached` has zero new admissions because currently eligible peers already received a queue entry for that hash. No ready peers produces HTTP 503; lack of cache or eligible queue capacity produces HTTP 429. These are local transport states, not propagation guarantees. Cache contents and queues are not durable.

## Dependency and protocol boundary

The Go module uses the `github.com/ethereum/go-ethereum` import path with an explicit replacement to official BSC `v1.8.2`, reviewed at commit `54afaff4da31ac9e49ba646a4f65104edac5a2f3`. Upstream Ethereum and BSC are not interchangeable at this protocol boundary.

The relay advertises **eth/68 only**. BSC v1.8.2 also supports eth/70, but this milestone does not implement or advertise it.

For eth/68:

- Status carries version, network ID, total difficulty, head hash, genesis hash, and fork ID.
- A second BSC UpgradeStatus exchange, message `0x0b`, carries the transaction-broadcast preference.
- The honest local status is based on the upstream in-memory genesis header, not a fabricated current head or a downloaded chain.
- Remote fork compatibility is checked using `forkid.NewStaticFilter` against the selected official chain configuration.
- No remote head is accepted as locally verified consensus state.

An unsynced genesis-based identity may be accepted by compatible peers under fork-ID subset/superset rules. That does not establish a reliable long-lived peer relationship. Static fork configuration must be maintained as the network evolves.

## Message handling

Ordinary broadcasts use Transactions (`0x02`) and transaction-aware RLP encoding. Legacy transaction lists and typed transaction byte envelopes must remain distinct.

GetPooledTransactions (`0x09`) receives a PooledTransactions (`0x0a`) response with the matching request ID and available cached transactions. Missing or expired items are not fabricated.

GetBlockHeaders, GetBlockBodies, and GetReceipts receive empty responses with the matching request IDs because the relay has no chain database. In the reviewed BSC client, explicit required-block challenges allow empty headers for unsynced peers; ignoring them instead would cause timeout/disconnection. Other peer policies may still reject or drop the relay.

Untrusted input, requests, and outbound queues are bounded. A remote peer's transaction-broadcast opt-out must be respected. The node identity key is ephemeral and is not a transaction-signing key.

## Discovery and networks

Mainnet defaults to official bootstrap discovery. Static enode peers can be added repeatedly, and discovery can be disabled. Testnet requires explicit peers. An enode identifies a network peer, not a trusted chain authority.

The HTTP listener defaults to `127.0.0.1:8787` and is restricted to loopback for this milestone. The distinct P2P listener defaults to `:30311`.

## Resource and failure model

- Cache: maximum 1,024 entries, 16 MiB, TTL two minutes.
- Per-peer outbound queue: 64 entries.
- No database, durable retry journal, or chain synchronization.
- Process restarts lose cache and pending work.
- Disconnects, full queues, expiration, and malicious or unsynced peers can prevent propagation.
- Duplicate detection is limited to the local cache window.

Health and readiness are transport diagnostics. Neither proves a valid chain view or transaction delivery.

Node instances are single-use: Close and failed Start are terminal, and restart
requires a fresh Node. See [the lifecycle contract and scoped verification](NODE_LIFECYCLE.md).

## Stage 2 is separate work

Verified transaction inclusion requires more than observing gossip. A future design needs an independently trusted checkpoint or genesis-derived ancestry, Parlia header/validator-transition verification, explicit fork rules, reorganization handling, and evidence linking transaction and receipt roots to verified headers. Finality requires its own consensus evidence and trust model. Peer voting or copying the largest advertised head/TD is not a replacement.

Without these components, receipt responses and peer-reported heads must remain untrusted observations. This milestone deliberately does not label transactions confirmed, successful, or final.

## Official reference sources

All links below pin the reviewed release commit:

- [Protocol packets](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/eth/protocols/eth/protocol.go)
- [BSC handshake](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/eth/protocols/eth/handshake.go)
- [Message handlers](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/eth/protocols/eth/handlers.go)
- [Required-block challenges](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/eth/handler.go)
- [Fork-ID algorithm](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/core/forkid/forkid.go)
- [Chain configurations](https://github.com/bnb-chain/bsc/blob/54afaff4da31ac9e49ba646a4f65104edac5a2f3/params/config.go)
