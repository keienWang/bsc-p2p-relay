# Production acceptance checklist

Status: **NOT PRODUCTION READY**. This is a Stage 1 broadcast-core prototype. The checklist is a gate, not a claim that a date or deployment has been approved.

## Implemented and locally exercised

- Replay-protected signed transaction decoding, chain ID checks, canonical encoding and signer recovery.
- Legacy, access-list and dynamic-fee transactions. Other envelopes deliberately rejected.
- Official BSC eth/68 dual-handshake compatibility, including opt-out and synthetic pre/post-Jenner fork schedules.
- Local RLPx transport interoperability. No external RPC client or historical chain database.
- [Isolated official BSC backend/txpool integration](../integration/officialtxpool/README.md): real pending-pool admission, insufficient-funds rejection, deduplication and transient reconnects over loopback P2P, with no RPC endpoints.
- Loopback-only HTTP with Host/Origin checks, body/concurrency limits and HTTP timeouts.
- Count/byte/TTL cache bounds; per-peer bounded queue and deduplication; reconnection retry behavior.
- Queue deadlines prevent beginning a transaction write after expiry. An already-started network write is subject to the upstream transport's timeout, not an interruptible transaction deadline.
- Unit/regression tests and race detector; exact current results belong in `VERIFICATION.md`.
- GPL/LGPL notices and pinned BSC module, with explicit security-patch overrides of selected transitive dependencies.

## Required before a production broadcast service

- [ ] Public-network handshake/retention against multiple independent current BSC nodes. Current build environment cannot reach direct external BSC TCP endpoints.
- [ ] Official-binary/deployment txpool interoperability under current production fork and state conditions, including rejection/ignore cases. The isolated embedded official backend test establishes local txpool acceptance and reconnect behavior only; it does not establish public mainnet/testnet acceptance.
- [ ] Long-duration network soak under disconnects, slow readers/writers, packet loss, malformed data and realistic blocks; memory/goroutine stability and operational SLOs measured on target hardware.
- [ ] Load/backpressure tests and capacity planning, including non-responsive peers and concurrent API clients.
- [ ] Vulnerability scan reviewed at the exact release commit. Known transitive-library findings must not be hidden.
- [ ] Full dependency license audit and distribution-ready corresponding source. A target-specific CycloneDX 1.6 SBOM is recorded in SBOM.md; this does not complete the dependency-license audit or corresponding-source packaging.
- [ ] Reproducible, checksummed release artifacts; complete corresponding source and dependency notices for redistributed binaries; hosted CI passes on the same commit.
- [ ] Operational acceptance of the [experimental runbook](OPERATIONS.md), target-specific alert thresholds/SLOs, tested rollout/rollback procedure and an explicit owner. The documented local rehearsal and incident procedures are preparation, not completion of this gate.
- [ ] Signed-testnet transaction tests only with an authorized test account/budget. Do not send mainnet funds as a QA shortcut.

Prepared evidence includes the [targeted Herumi archive source and notice appendix](native-herumi/README.md) and a [no-transaction connection plan](native-herumi/NO_TRANSACTION_CONNECTION_PLAN.md). These are partial preparation, not completion of the gates above or authorization to run a network experiment.

[Read-only binary metadata collection](BINARY_METADATA.md) binds future ELF observations to the exact built file. Historical Go 1.26.10 properties do not establish the current Go 1.26.11 executable's properties, and metadata collection alone does not complete a production gate.

The [Linux/amd64 build policy](LINUX_BUILD.md) integrates the checked linker policy
into the recommended build path. Its default artifact and alternate official-txpool
graph passed the [source-bound hosted acceptance](HOSTED_ACCEPTANCE_2026_11_12.md)
on `c27438bf46969dd224db5cbaa49f65b747537787`. This is a checked build/test result,
not completion of the remaining production checklist or all native qualification.

## Separate Stage 2: verified head and transaction outcome

The authenticated live capabilities below remain unimplemented. The offline commitment checker remains conditional on an unauthenticated supplied header. A separate first-epoch experiment authenticates genesis successors only through block 199, including a real mainnet block 1 fixture; it cannot follow the current chain. Neither completes these live verification gates:

- [ ] Explicit trust/bootstrap model: verified ancestry from genesis or an independently trusted checkpoint and validator snapshot. An unverified peer's head is not a checkpoint.
- [ ] Fork-aware Parlia header/seal/validator transition and finality verification, including adversarial fixtures.
- [ ] Resolve or independently isolate the [upstream BLS cache concurrency/resource issue](BLS_CACHE_KNOWN_ISSUE.md) before any concurrent importer or modern BLS verification is enabled.
- [ ] Bounded header/reorganization tracking and isolation from dishonest or eclipsing peers.
- [ ] Transaction-root inclusion checks, receipt-root proof verification and receipt execution status anchored to a verified header.
- [ ] Clear distinctions among observed, included, executed, reverted, reorged and finalized.
- [ ] State-proof validation for nonce/balance, if later added. Eth P2P does not provide an unconditional arbitrary-account RPC substitute.

A process with successful P2P writes is still not an independently validating node. No readiness endpoint in this milestone changes that fact.
