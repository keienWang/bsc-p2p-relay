# Soak HTTP validation correction: v2

Status: **NOT PRODUCTION READY**. This change closes an acceptance-harness
false-positive, not a production runtime or network-readiness gap.

## Why the original gate was insufficient

Independent review of baseline `25565b313fc53e51f8ebc49792c19fedcbc16b0b`
injected legal JSON HTTP 500 responses after prefill and before final probes.
The original harness recorded 42 / 61 such responses in the 900 / 1,024-entry
scenarios but returned exit 0 with both scenario and top-level `passed=true`.
Read/decode errors and unexpected statuses were not all checked, and expiry did
not share a complete HTTP error gate.

The [historical normal-run results](SOAK_RESULTS_2026-11-10.md) are retained
unchanged as observations. The defect was an incomplete acceptance condition,
not altered historical samples. Those PASS labels must not be used as the
current HTTP failure gate. The old injected-500 JSON/log and old normal-run
artifacts are preserved separately from v2 results.

## Corrected contract

- All phases use one response validator and one final scenario audit. Prefill
  failures stop immediately; occupancy and expiry both invoke the final audit.
- Required response fields have exact types, expected hash/sender, bounded
  queue counts, and consistent stage/duplicate meanings. Missing, extra,
  duplicate or null fields, invalid/trailing JSON, wrong content type,
  truncated/oversized bodies and read/close errors fail acceptance.
- Status 202 is allowed only during admission/load. The exact 429 reason must
  match active backpressure or the specific queue/cache probe. The sole allowed
  503 is the explicit all-peers-disconnected probe, with the exact no-peer error.
  Active reconnect windows never authorize 503. Status 400/500/redirects fail.
- Both all-phase and measured-only attempt/response/status/error denominators
  must conserve. Each response has exactly one valid/invalid classification;
  any client error or invalid response fails even if the arithmetic balances.
- Full counters and up to eight bounded diagnostic messages are retained.
  Invalid responses cannot contribute queue admissions. Expiry must decode
  exactly one transaction, in addition to the existing 1-write/64-skip checks.
- Warmup writer completion has a one-second deadline. Source fingerprints cover
  every soak test file. JSON/environment output identifies
  `harness_revision=http-fail-closed-v2`.

See [SOAK.md](SOAK.md) for the exact allowed phase/reason matrix, metric definitions
and conservation equations. No production validation or limits were weakened.

## Deterministic fault regressions

The test-only RoundTripper does not open a network connection. It injects HTTP
500, unexpected 400/503/200/302, truncated body after otherwise valid JSON,
body-close error, malformed JSON, missing/extra/duplicate/null/wrong-type fields,
wrong hash/sender, negative/excessive queue counts, incorrect stage/duplicate
semantics, malformed rejection objects, mismatched errors, wrong content type,
oversized body and client failure. Prefill and expiry failures are included.

Each fault must produce both a submission validation error and a failing final
scenario audit, with zero admitted-queue contribution. Positive phase-specific
responses are separately tested. Counter corruption tests verify that missing
outcomes cannot leave a green gate. Mixed-distribution quantile regressions cover
1, 100, 65,536, 65,537 and 100,000 observations across the exact/histogram boundary.

These tests have their own clients and counters. They run separately from the
measured load and never appear as performance samples.

```sh
go test -race ./internal/relay \
  -run '^TestSoakHTTP|^TestSoakMixed|^TestSoakQuantiles' \
  -count=1 -timeout=30s -v
./scripts/run_relay_soak.sh
RELAY_SOAK_RACE=1 ./scripts/run_relay_soak.sh
```

## Replacement normal-run evidence

Baseline `25565b3` plus this uncommitted test-only change; Go 1.26.10, official
BSC v1.8.2, GOMAXPROCS=2, seed 20261110, 16 clients, 5s per occupancy scenario.
Shared-container starting load average: `0.18 0.55 0.78`. Default cache/queue/TTL
limits and the isolated short-TTL expiry probe are unchanged.

The normal v2 runner passed in **11.21s**, process exit 0. All samples maintained
900 or 1,024 entries. Latencies below are milliseconds, exact nearest-rank values.

| Measurement | 900 entries | 1,024 entries |
| --- | ---: | ---: |
| All attempts = responses = valid responses, including prefill | 4,901 | 5,041 |
| Measured attempts = responses = valid responses | 4,001 | 4,017 |
| Measured 202 / 429 / 503 | 2,702 / 1,298 / 1 | 2,936 / 1,080 / 1 |
| Client errors / invalid responses | 0 / 0 | 0 / 0 |
| Active responses/s, excluding final probes | 798.79 | 798.83 |
| HTTP RTT p50 / p95 / p99 | 1.185 / 2.229 / 3.590 | 1.141 / 2.021 / 3.263 |
| Handler p50 / p95 / p99 | 0.097 / 0.264 / 0.697 | 0.089 / 0.231 / 0.702 |
| Successful fast pipe writes, post-prefill samples | 2,613 | 2,850 |
| Fast pipe-write p50 / p95 / p99 | 0.0087 / 0.0339 / 0.2174 | 0.0089 / 0.0386 / 0.1703 |
| Successful slow pipe writes, samples | 66 | 66 |
| Slow pipe-write p50 / p95 / p99 | 75.510 / 76.081 / 76.245 | 75.537 / 76.181 / 76.645 |
| Queue admissions = write attempts + disconnect-abandoned | 3,839 = 3,583 + 256 | 4,200 = 3,944 + 256 |
| Write attempts / completed / failed | 3,583 / 3,579 / 4 | 3,944 / 3,940 / 4 |
| Maximum sampled RSS, whole process (bytes) | 62,328,832 | 63,639,552 |
| Goroutines before / max sampled / after cleanup | 24 / 85 / 9 | 24 / 85 / 9 |

Expiry: 66 attempts = 66 valid responses = 65 admissions + 1 queue-full 429;
65 queue admissions = 1 write completed after TTL + 64 expired skips; exactly
one local fixture decode; zero HTTP/fixture/observer errors.

The strict-validation work runs after the timed body read, but still adds shared
CPU/allocation overhead. Do not interpret differences from v1 as a runtime
performance improvement. Exact queue-only latency, TCP/RLPx writes, remote
acceptance, long-duration stability and modern consensus remain unverified.

## Independent rejection check

The same intermittent-500 mutation was independently repeated against copies of
the three exact v2 source files. It produced 48 / 70 HTTP 500 responses and
48 / 70 invalid-response counts. **Both scenario results and top-level passed
were false, and go test exited 1.** This is the expected result of a deliberately
broken handler. The mutation exists only in the independent review checkout;
its log, JSON and exit code are stored separately from all normal measurements.
No fault was injected into the source tree or normal v2 runner.

Validated source fingerprints:

- `soak_test.go`: `72da0470c88c045a99bd423c4cec1dc8beef86953253280334b56e3d6a9b67da`
- `soak_http_test.go`: `4acf16f31a9e4133ba4c15444dfccbab7278c693aaa39f82c193b81a08c5d687`
- `soak_statistics_test.go`: `a0cc4a73b295931784f4d86f6c0d8b12cfedcbd44395518f614d218ccfcb7675`
- `run_relay_soak.sh`: `20582445cc5c73e4832c25570477fde916dc2e10f368e2602c423d62887f3073`

## Final checks for this source snapshot

- Deterministic HTTP faults, valid responses, denominator corruption, mixed
  quantiles and original quantile regression under race: PASS, process exit 0,
  1.428s. Faults are expected rejections inside isolated tests, not load samples.
- Normal timed runner: PASS, exit 0, 11.21s timed / 11.234s process.
- Race-enabled normal timed runner: PASS, exit 0, 13.22s timed / 14.301s process.
  Both scenarios and expiry passed the same strict HTTP audit. Race timing is
  retained separately and not mixed with the normal percentile table above.
- `go vet ./internal/relay`, `gofmt`, runner `bash -n`, and `git diff --check`:
  PASS. No production source, module dependency, network capability or test
  capacity was changed.
- A full repository release check, live-network validation and long-duration
  soak were not rerun as part of this focused correction. All production gates
  listed in PRODUCTION_READINESS.md remain in effect.

An independent review checkout then passed the unchanged v2 normal race workload
at 3s per occupancy scenario (process 10.074s, exit 0), after the developer's run
released the shared lock. Its HTTP/schema/denominator/quantile regressions also
passed under race for three repetitions (2.657s). The three core source hashes
still matched. These shorter independent results are additional validation, not
substitutions for or additions to the 5s normal-run percentile samples.

## Final integration check

The unchanged v2 harness was integrated with local source `b4eaed0c`, whose tree
is published at [3ff8999562d2eeb04ef769cbb0d3887913f1fbde](https://github.com/keienWang/bsc-p2p-relay/commit/3ff8999562d2eeb04ef769cbb0d3887913f1fbde).
The existing official-txpool integration, refreshed supply-chain documents and
checkpoint wire-boundary regression were retained. The new diff contains only
this opt-in harness, its small ordinary regression tests, runner and documentation;
production code, module files and CI job definitions are unchanged.

A single final `GOFLAGS='-buildvcs=false -p=1' make check smoke vuln` passed with
Go 1.26.10. The default five-package race suite included the HTTP fault and
statistics regressions, but skipped the opt-in timed workload. Checkpoint proof
took 20.591s, first-epoch tests 41.717s and relay tests 1.834s. Build, 10 Python
tests, formatting, module verification, dependency guard and process smoke all
passed; `govulncheck@v1.9.0 ./...` reported `No vulnerabilities found.` The timed
v2 and independent negative/positive checks above remain separate evidence.

The explicitly failing `blscacherace` diagnostic is not resolved by any of these
checks. The final published revision still needs its exact hosted two-job CI
result; earlier green baseline workflows do not validate this new change.
