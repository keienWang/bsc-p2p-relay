# CI security fix: maintained STUN dependency

Date: 2026-11-10. Baseline: public main commit `594dcb9c07652cca7f6e70d4bf7b051ed06ae8ea`.

## What failed

The initial hosted workflow passed formatting, vet, race tests, compilation and process smoke. Its final govulncheck step failed on GO-2026-4479 in `github.com/pion/dtls/v2@v2.2.12`. The numerous annotations were call paths for that one finding. The failure was not suppressed.

## Actual dependency change

BSC v1.8.2 imports STUN v2 in one production file, `p2p/nat/stun.go`. STUN v2 has no newer patched release and brings in obsolete DTLS v2. The project now replaces that STUN module with a small, explicit adapter for only the APIs BSC consumes. All STUN work is delegated to maintained STUN v3.1.7, whose DTLS dependency is v3.1.5. No cryptography is reimplemented, and no dummy successful response replaces network behavior.

`go list -deps ./cmd/bsc-relay` contains DTLS v3 and no DTLS v2 package. The complete upstream module graph may still list obsolete DTLS v2 even though it is not in this executable’s compiled package graph. `scripts/check_runtime_dependencies.py` guards against its reintroduction. The replacement and nested module are recorded in go.mod/go.sum and the dependency inventory. BSC itself remains v1.8.2. An upstream BSC expansion of the STUN API must compile against and be reviewed with this deliberately limited adapter.

## Local verification

- `make check smoke`: vet, full race suite, build, module verification and process startup/shutdown tests passed.
- `go test -race -count=3 ./internal/compatcheck`: the real official BSC NAT/STUN caller exchanges a request/response with a loopback UDP server and recovers the expected XOR-mapped address. The responder uses independently encoded RFC 8489 bytes, not the same STUN library. Missing or malformed mapped-address information is rejected; wrong transaction IDs are ignored; no-response timeout and rejection of new requests after close are covered.
- `go test -race github.com/ethereum/go-ethereum/p2p/nat -run TestParse -count=1`: official BSC parsing tests passed.
- `go run golang.org/x/vuln/cmd/govulncheck@v1.9.0 ./...`: exit 0, no called-code vulnerabilities found. The tool still reports 1 imported-package finding and 3 module findings without identified call paths. Nothing is hidden or exempted.

The normal relay still has no automatic NAT/STUN configuration. Tests do not contact public STUN services, BSC nodes, or send transactions.

Hosted CI must be verified on the new pushed commit before claiming GitHub is green. A passing scan does not complete the broader production checklist, real-network tests, formal audit or Stage 2 verification.

The adapter does not add a cancellation contract: upstream STUN v2/v3 do not guarantee that concurrent Close wakes an already-blocked Do. The BSC caller performs synchronous Do before its deferred Close, and this relay does not enable NAT/STUN during normal operation. Any future cancellation-based use requires separate design and tests.

The local post-fix validation binary has SHA-256 `60dc306eb2e1b8a80c83c5ae477817d0fa7fa261ac7d76941731deb6ba684a7d`. Its build metadata includes DTLS v3.1.5 and no DTLS v2 module; ELF GNU_STACK is RW, not executable. This binary is a QA artifact, not a release.
