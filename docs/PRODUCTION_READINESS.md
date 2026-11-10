# Production acceptance checklist

Status: **NOT PRODUCTION READY**. This is a Stage 1 broadcast-core prototype. The checklist is a gate, not a claim that a date or deployment has been approved.

## Implemented and locally exercised

- Replay-protected signed transaction decoding, chain ID checks, canonical encoding and signer recovery.
- Legacy, access-list and dynamic-fee transactions. Other envelopes deliberately rejected.
- Official BSC eth/68 dual-handshake compatibility, including opt-out and synthetic pre/post-Jenner fork schedules.
- Local RLPx transport interoperability. No external RPC client or historical chain database.
- Loopback-only HTTP with Host/Origin checks, body/concurrency limits and HTTP timeouts.
- Count/byte/TTL cache bounds; per-peer bounded queue and deduplication; reconnection retry behavior.
- Queue deadlines prevent beginning a transaction write after expiry. An already-started network write is subject to the upstream transport's timeout, not an interruptible transaction deadline.
- Unit/regression tests and race detector; exact current results belong in `VERIFICATION.md`.
- GPL/LGPL notices and pinned BSC module, with explicit security-patch overrides of selected transitive dependencies.

## Required before a production broadcast service

- [ ] Public-network handshake/retention against multiple independent current BSC nodes. Current build environment cannot reach direct external BSC TCP endpoints.
- [ ] Full official-client txpool interoperability, including rejection/ignore cases and reconnects. Library handshake/RLPx tests do not establish txpool acceptance.
- [ ] Long-duration network soak under disconnects, slow readers/writers, packet loss, malformed data and realistic blocks; memory/goroutine stability and operational SLOs measured on target hardware.
- [ ] Load/backpressure tests and capacity planning, including non-responsive peers and concurrent API clients.
- [ ] Vulnerability scan reviewed at the exact release commit. Known transitive-library findings must not be hidden.
- [ ] Full dependency license audit and distribution-ready corresponding source. The Go module inventory is not a standards-compliant SBOM.
- [ ] Reproducible, checksummed release artifacts; complete corresponding source and dependency notices for redistributed binaries; hosted CI passes on the same commit.
- [ ] Operational runbook, alert thresholds, rollout/rollback procedure and an explicit owner.
- [ ] Signed-testnet transaction tests only with an authorized test account/budget. Do not send mainnet funds as a QA shortcut.

## Separate Stage 2: verified head and transaction outcome

The authenticated live capabilities below remain unimplemented. An offline commitment checker now exists, but its results are conditional on an unauthenticated supplied header and it completes none of these live verification gates:

- [ ] Explicit trust/bootstrap model: verified ancestry from genesis or an independently trusted checkpoint and validator snapshot. An unverified peer's head is not a checkpoint.
- [ ] Fork-aware Parlia header/seal/validator transition and finality verification, including adversarial fixtures.
- [ ] Bounded header/reorganization tracking and isolation from dishonest or eclipsing peers.
- [ ] Transaction-root inclusion checks, receipt-root proof verification and receipt execution status anchored to a verified header.
- [ ] Clear distinctions among observed, included, executed, reverted, reorged and finalized.
- [ ] State-proof validation for nonce/balance, if later added. Eth P2P does not provide an unconditional arbitrary-account RPC substitute.

A process with successful P2P writes is still not an independently validating node. No readiness endpoint in this milestone changes that fact.
