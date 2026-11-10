# Statistics ledger verification record

Date: 2026-11-10 UTC. Scope: the test-only independent audit described in
[STATS_LEDGER_AUDIT.md](STATS_LEDGER_AUDIT.md). This is not production readiness,
public-network interoperability, remote transaction acceptance or confirmation.

## Exact source and execution scope

- Local baseline: `f8e88cffa7c2d8778531172d609b686526d5eafc`, tree
  `2abfc68d6b70c545ef843aafac8a3e472767fd55`.
- Initial audited test source: `484542b16c77988e0292e605beee43cc47c9be9a`.
  The bounded focused race command `go test -race -count=1 -timeout=90s
  -run '^TestStatsLedger' -v ./internal/relay` passed (8 top-level tests,
  210 PASS lines including subtests, 2.887 s Go package time).
- An independent review used an exact copy of that initial source for six short
  parser/accounting/Handler/writer groups, without repeating MsgPipe/TTL scenes.
  Its bounded race run passed (1.169 s Go package time).
- Review hardening was added as a separate commit. The final **tested Go source**
  is `f4b188d83ff3889d05dd16f2d04ccad907c6902b`, tree
  `62386d0f0b321857c6bd844b43f982cc315fb263`. Exact error-reason and semantic-mutant
  assertions were reviewed statically, then exercised by both aggregate graphs
  below. Earlier focused results are not substituted for this final-source run.
- This verification document and its cross-link are a later documentation-only
  addition. The tested Go source is unchanged.

The baseline-to-candidate change adds only three Go test files and documentation.
No Go runtime implementation, API, CI, dependency, SBOM or default configuration
was changed. Existing HTTP-500 failure checks and Content-Length premature-EOF
smoke rejection remain intact. The smoke parser's fixed-length/EOF support and
Transfer-Encoding refusal are not a product HTTP limitation.

## One serial aggregate on the final tested source

Go 1.26.10, linux/amd64, `GOMAXPROCS=2`, shared cached modules and build cache,
`GOTOOLCHAIN=local`, `GOWORK=off`, `GOENV=off`, `GOPROXY=off`, `GOSUMDB=off`.
Default `GOFLAGS='-buildvcs=false -mod=readonly -p=1'`. All `RELAY_*` and `BSC_*`
opt-in variables were removed from the runner environment. Each stage acquired
the shared nonblocking experiment lock, had a finite external timeout, and checked
the exact clean source before and after. The whole serial batch had a 1,800-second
budget and stopped on any failure. All listed stages exited 0; none was retried.

| Command or check | Budget | Actual outer duration | Result |
| --- | --- | --- | --- |
| `make check` | 480 s outer; Go 300 s | 135.265 s | PASS: vet, default race, build, 43 Python tests, format, module verification and dependency guard |
| `make smoke` | 90 s | 0.656 s | PASS: actual binary lifecycle/API/statistics smoke |
| `make fuzz` | 300 s outer; 10 s campaign, 2 workers | 20.429 s | PASS: 1,951 executions; 29 baseline inputs, 4 new interesting inputs, total 33 |
| `SHA-256 check; go version; go version -m govulncheck` | 30 s | 0.132 s | PASS: Go 1.26.10; scanner v1.9.0 build metadata |
| `govulncheck ./...` | 120 s | 16.754 s | PASS: no vulnerabilities found |
| `govulncheck -test ./...` | 120 s | 13.565 s | PASS: no vulnerabilities found |
| `actual alternate-module/tag dependency guard, vet and relay race` | 240 s outer; Go 180 s | 57.443 s | PASS: officialtxpool graph, including all 9 statistics-ledger top-level tests |
| `govulncheck -test -tags=officialtxpool -show=verbose ./internal/relay` | 120 s | 17.188 s | PASS: no vulnerabilities found |
| `43 Python tests; operations/stats documentation; gofmt; git diff --check` | 90 s | 0.513 s | PASS |

Default relay race package time was 5.292 s. The actual `officialtxpool` relay
race package time was 41.056 s. Its environment used
`GOFLAGS='-buildvcs=false -mod=readonly -p=1 -modfile=integration/officialtxpool/go.mod -tags=officialtxpool'`.
The tagged sequence ran `python3 scripts/check_runtime_dependencies.py`,
`go vet ./internal/relay`, and
`go test -race -count=1 -timeout=180s -v ./internal/relay`; the same alternate
module/tag environment was used for its test-graph scanner. Default and tagged
environments were separate.

The fuzz numbers above are logged executions and interesting-input/corpus counts,
not code coverage percentages, branch counts, a throughput benchmark or TPS.
The built binary used by smoke had SHA-256
`63db65d60fa5f6728f01854053c056c402ea240b453f16d55538f1e9e8366491`.

The literal `make vuln` go-run launcher was **not rerun**: its offline module
metadata startup limitation was already known. The equivalent default `./...`
scan and the additional two test-graph scans used the independently built,
version-checked `golang.org/x/vuln/cmd/govulncheck` v1.9.0 executable, SHA-256
`0108fc96c31b697274b66fc071da4ccc9f81de4079bb016867e3a479796d444c`.
The record claims those scanner commands passed, not that `make vuln` ran.

## Evidence identity

Raw stdout, per-stage intent, exact environment, command, source identity and exit
records are retained in a separate local evidence bundle, not versioned in this
repository. These SHA-256 values identify the stdout logs of this batch:

| Evidence member | SHA-256 |
| --- | --- |
| `03-make-check/output.log` | `1008df6ea8dd971931fc2b607025d14ed1d5e6cc616ba0febccfb6dafec0bdae` |
| `04-make-smoke/output.log` | `859698639ffdd8a4c2ba7e0805f363e0a5a9d752452cdfa3408eb9f53e21ca61` |
| `05-make-fuzz/output.log` | `85b8efe9f22fe06ee18da878deb798bd84a3f0a52e2bbb6413e302aadd56d919` |
| `06-scanner-provenance/output.log` | `3df1f3b9a6fc743297852958fa5075ad9c420f63d799126d944bbf697e6ecbba` |
| `07-default-vulnerability-scan/output.log` | `3016e51e4eac0d421674d2128bbbdefb2924b4646e0c14a1ab034977ad73fae5` |
| `08-test-vulnerability-scan/output.log` | `3016e51e4eac0d421674d2128bbbdefb2924b4646e0c14a1ab034977ad73fae5` |
| `09-official-guard-vet-race/output.log` | `a4d4e7e60332d16b40bfb266c53162501e11215b51682b7eb7013a2bad31b092` |
| `10-official-test-vulnerability-scan/output.log` | `75e6617ec1a00760a0db0c80e0d8145a25417a82e1949c9f6303028b48ab5f0f` |
| `11-final-light-checks/output.log` | `593e439d49facb0c85df600b74594baa9a90ad1cbf9b0832e5e778cd80d1cad2` |

Initial focused race log: `db0fbdbbfc9c0df3bae256f831c136457423e123aaa7f644349da8062f58e20c`.
Independent initial-source review log:
`63d8224084518ec3b2c902440cc88f998af80ecceb45afeb2018d8f8c3f8391c`.

## Deliberately not run or claimed

- No 150-second default-TTL stream, opt-in soak, benchmark, standalone intentional
  failure probe, public P2P/discovery, RPC, real account or real payment.
- No repeat of the unchanged tagged canonical-isolation replay in this local
  batch. Default checkpoint/first-epoch package tests ran as part of `make check`;
  they do not replace that separate tagged replay or public chain verification.
- No BLS known-failure tag and no claim that its process-global race is solved.
- No production TPS, remote mainnet acceptance/confirmation, modern authenticated
  chain head, atomic in-flight snapshot, or production-readiness claim.
- No publication or CI status is established by this local record. Remote commit
  identity and all publication checks must be verified separately.
