# Security boundaries

This experimental Stage 1 relay is not a wallet, validating node, or financial execution assurance service. Do not rely on its response to decide that a payment has succeeded.

## Keys and signed transactions

Sign transactions outside this process. The relay requires no wallet private key, seed phrase, password, or signing authority. Its ephemeral devp2p node key is a networking identity only.

A signed transaction can be broadcast by anyone who obtains it. Submitting one authorizes the relay to expose it to public peers; it is not a private order-flow channel. Even if the relay forgets it after two minutes, other parties may retain or rebroadcast it. Do not submit a transaction you are not ready to make public.

## Local API only

The default API address is `127.0.0.1:8787`. This milestone permits loopback listeners only and has no authentication. Do not publish it using reverse proxies, tunnels, container port forwarding, or shared-host routing. Loopback does not protect against other processes or users on the same machine. Restrict access to the operating-system account and host.

Untrusted callers can consume bandwidth, populate the cache, or submit valid signatures for transactions that are invalid against current chain state. Resource bounds reduce abuse but do not eliminate denial-of-service risk.

## Untrusted peers

Discovery and static peers are transport mechanisms, not trust anchors. A peer may drop transactions, lie about its chain, censor traffic, send malformed data, or observe your originating IP and timing. A successful handshake or local socket write does not establish mempool acceptance.

The relay cannot check account balances, current nonces, current execution rules, contract effects, fee sufficiency against live state, receipts, reorgs, or finality. Local canonical/signature/chain-ID checks are necessary but insufficient for on-chain validity.

Peer input and requests must be bounded, and queued data must remain bounded in both count and bytes. Monitor the process for disconnections, queue pressure, and resource use. Do not treat healthy metrics as consensus verification.

The loopback [statistics endpoint](OBSERVABILITY.md) exposes only fixed aggregate
Submit outcomes and HTTP concurrency refusals. Its random instance ID is public
correlation metadata, not an authentication token or P2P identity. It takes no
transaction slots or state locks; fixed O(1) metric work does not bound total HTTP
resource use. Existing HTTP timeouts and local-access restrictions still apply.
It adds no transaction/peer identity history, exporter, authentication or public
listener, and it cannot prove delivery or consensus state.

## Scope restrictions

Only replay-protected transaction types 0, 1, and 2 for chain ID 56 or 97, according to the selected network, are supported. Blob and set-code transactions are rejected. This restriction is an implementation scope decision, not a statement that those transaction types are universally invalid on BSC.

No live transaction test is required to run the local test suite. Use controlled fixtures and local protocol mocks. Any future network test with a signed transaction can cause real execution and fees and needs an explicitly approved transaction and network.

## Deployment and maintenance

Keep dependency versions and fork schedules reviewed and pinned. This project currently uses official BSC v1.8.2. Review upstream security updates and protocol changes before deployment. Do not blindly replace BSC with upstream go-ethereum: handshake and protocol behavior differ.

Use ordinary operating-system isolation and resource controls appropriate to your deployment. This milestone has no remote administrative interface and no production security audit claim.

## Reporting issues

The public repository is [keienWang/bsc-p2p-relay](https://github.com/keienWang/bsc-p2p-relay). No dedicated security contact or private reporting mechanism is documented here. Share vulnerability details privately with the project owner through an already verified channel; do not assume public issues are a confidential reporting channel. Never include private keys, seed phrases, or unbroadcast valuable signed transactions in reports, logs, fixtures, or public issues.
