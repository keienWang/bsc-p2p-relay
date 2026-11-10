# Local verification records

Results are specific to the source and scope stated in each record. Historical
failures or passes below are not claims about an untested later commit.

## Latest cache integration record

The indexed-cache and conservative bulk-expiry implementation was locally
integrated at `2dd88ac3cbbf09ed3234173139ff667c9bb63c17` on 2026-11-10. The
[complete integration record](CACHE_BULK_EXPIRY.md#independent-review-and-integrated-local-validation)
binds the independent review, six serial local verification phases, exact command
scopes, durations and log hashes. Default race/vet/build/smoke, module/guard checks,
10 Python tests, actual official-txpool tagged race/vet/guard, and the default plus
tagged test-graph vulnerability scans passed. All scanners reported no findings.
This is not hosted-CI confirmation, public-network delivery, modern BLS safety or
production certification. The unchanged third research job still needs to pass
on the exact eventual public commit with the two other jobs.

## Historical initial implementation

This section records the initial implementation. For the subsequent dependency remediation and currently passing local security gate, see [CI_SECURITY_FIX.md](CI_SECURITY_FIX.md).

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


## Submission statistics verification, 2026-11-10

The [full submission-statistics verification record](OBSERVABILITY_VERIFICATION_2026-11-10.md)
binds frozen implementation/regression source `0c8bdd864adcdd65fa74cdfd28922878eec3177c`
and the later process-smoke-only source `7b56311f6ae4b29e1915afe5399aab1d552cda3e`
to default, actual officialpool and canonical-isolation checks, four successful
scanner scopes, a new ten-second fuzz run, and the real binary stats smoke.
The record preserves the original fuzz outer-timeout, offline scanner-launch
failure and fixture-output preparation failure alongside explicitly authorized
corrections. It does not call those initial attempts successful, overwrite earlier
measurements, claim hosted CI, or close production/BLS/verified-head gates.


### Stats smoke framing follow-up

The [framing-fault follow-up](OBSERVABILITY_VERIFICATION_2026-11-10.md#smoke-framing-fault-follow-up)
records the test-only correction at `d22aacc03f2e07bf74c9b6fe6fd92d4d2ee803c6`:
explicit bounded Content-Length agreement and conservative transfer-coding
rejection, real in-memory HTTPResponse negative fixtures, 43 passing Python tests
and one passing actual-binary smoke. The earlier binary observations remain
valid, but did not exercise this false-green framing fault. Runtime sources and
prior full-suite/failed-attempt evidence are unchanged.
