# Local verification record

Date: 2026-11-10. Source implementation: `82db9454e094a8eb0f9c0e993335ab11c2e2f57d`; subsequent verification-document commits do not change that implementation. Environment: Linux amd64, Go 1.26.10. This is not a production certification.

## Passed

- `go vet ./...`
- `go test -race -count=1 -timeout=120s ./...`
- `go build -trimpath -buildvcs=false -o bin/bsc-relay ./cmd/bsc-relay`
- `go mod verify` (all resolved modules verified)
- `python3 scripts/smoke.py bin/bsc-relay`: process start, health, no-peer readiness 503, malformed submission 400, graceful SIGTERM, no persistent files in the process working directory.
- Core Go statement coverage: **80.5%**. CLI Go coverage is 0%; process smoke is separate and is not counted by Go coverage instrumentation.
- Ten-second bounded fuzz run: 497 executions, no failure. This short run is a smoke test of the fuzz harness, not exhaustive fuzzing.
- Official library compatibility: BSC eth/68 status/upgrade handshake, transaction encodings, opt-out, malformed handshakes, matching request IDs, synthetic pre/post-Jenner fork IDs, loopback RLPx, and large-block connection survival.
- Reconnection retry, full queue/cache handling, concurrent duplicates, TTL deadlines, late pooled-response resolution, and Host/Origin guards.

Validation-only microbenchmarks on an Intel Xeon Platinum 8573C instance were approximately 53–55 microseconds per transaction for types 0/1/2 (1.5–1.7 KiB allocations). These measure decoding/signature checks, **not end-to-end broadcast throughput, latency, or production capacity**.

## Remaining failed or blocked gates

- `govulncheck` is **nonzero** with remaining GO-2026-4479 in pion/dtls/v2. See SECURITY_REVIEW.md. CI intentionally preserves this failure rather than suppressing it.
- Public BSC bootnode TCP probe returned `Network is unreachable` in this environment. No public-peer retention, full official-node txpool acceptance, mainnet transaction, or testnet transaction success is claimed.
- Hosted CI has not been verified on GitHub.
- Stage 2 consensus/head/receipt/finality verification is not implemented.
- Extended load, soak, formal dependency-license review, and release signing/distribution are pending.

## Binary reachability evidence

The local validation binary, built with `-trimpath -buildvcs=false`, has SHA-256:

`b74d6c32d469a209d97e362ea4b414ea8ded29f74357fc6761a7ad72c62d4981`

`go tool nm` was searched for `github.com/pion/dtls/v2.Client`, `.Dial`, `(*Conn).handshake`, `(*Conn).Read`, `(*Conn).Write`, and `github.com/pion/stun/v2.DialURI`; no matching symbols appeared. This supports the source-level observation that the examined binary does not contain those DTLS session entry points. It is not proof of absence of every exploitable path, does not make the dependency scan pass, and is not a substitute for review of a final distributed binary.

The binary is a local QA artifact, not a published release. Rebuild and revalidate the exact release commit before distributing it.
