# Proposed no-transaction connection experiment

Status: preparation only. No host has been selected and no connection experiment is authorized by this document.

## Required authorization and host controls

An operator must approve the host, unprivileged process user, network, exact peer enodes and outbound TCP destinations, runtime limit, resource/traffic budget and stop owner. The host must be controlled, without untrusted local API callers. Keep HTTP on a literal loopback address, disable discovery, and do not expose HTTP through a proxy, tunnel or published container port. Do not add RPC, wallet/signing keys, a historical database, persistent remote access or private project inputs.

Use an artifact with `--observe-only` and confirm its startup reports `observe_only=true`. This preserves the observation endpoints but rejects requests to the transaction endpoint before the handler parses or admits a transaction. It does not disable normal P2P protocol replies, replace host controls, or apply to earlier binaries without this flag.

## Proposed process configuration

After approval and artifact/libc compatibility checks, the command shape is:

```sh
bsc-relay --network mainnet --no-discovery --observe-only \
  --p2p 127.0.0.1:0 --api 127.0.0.1:8787 --max-peers 3 \
  --peer '<approved-enode-1>' --peer '<approved-enode-2>'
```

The placeholders are not executable configuration. Approved testnet peers require an explicit testnet selection. No discovery or guessed peers may be substituted to obtain a successful connection.

## Observation and stopping

- Start with an approved, bounded connection window, for example ten minutes. A longer soak requires its own agreed duration and resource budget.
- Use only GET `/healthz`, `/readyz` and `/v1/stats`. Do not POST raw transactions, including test fixtures. Cache occupancy and all submission totals must remain zero.
- Record aggregate endpoint samples, same-instance counter changes, verified process RSS/CPU/thread/fd counts and exit/restart status. If permitted, inspect owned-process memory-map permissions and obtain peer-side handshake/retention diagnostics. OS thread count is not Go goroutine count.
- Do not collect wallet data, credentials, transaction bodies or unrestricted packet captures. Normal P2P handshakes and protocol replies still occur; this is not passive packet capture.
- Stop the verified owned process on an unexpected listener/destination/discovery, nonzero submission/cache observation, sensitive log content, crash/restart, budget expiry or resource-cap breach. Record shutdown and any uncertain observations. Successful writes and clean exits are not transaction-delivery or consensus evidence.

This experiment can provide current connection/retention observations and an initial resource baseline. It cannot establish current-state txpool admission, transaction execution/finality, authenticated head, full security or production readiness. Signed-testnet transaction testing remains a separate authorization and budget decision.
