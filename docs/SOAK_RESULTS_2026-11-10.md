# Local load evidence, 2026-11-10

**Historical v1 run.** Its normal-run measurements remain recorded unchanged,
but the original HTTP acceptance gate missed some unexpected failures. It is
not the current acceptance result. The independently reproduced false-green
case, correction and replacement run are in [SOAK_HTTP_VALIDATION.md](SOAK_HTTP_VALIDATION.md).

**NOT PRODUCTION READY.** These results cover the in-process official BSC
MsgPipe fixture and loopback HTTP described in [SOAK.md](SOAK.md). They do not
measure public peers, real txpools, TCP/RLPx transaction writes, inclusion or
modern consensus verification.

## Reproducibility

- Baseline: `af68f2e9098e9100af1bae946aa4c00fbe665fdb`.
- Independent checkout; no production code modifications, commit or publication.
- Go `go1.26.10 linux/amd64`, pinned official BSC `v1.8.2`.
- `GOMAXPROCS=2`, seed `20261110`, 16 clients, 5s per occupancy scenario.
- Final command: `./scripts/run_relay_soak.sh`, exit 0, 11.20s timed test.
- Harness SHA-256:
  `49a25140daed46d272b2223ee56c2d69b9e310730de8933feffb82f228236da1`.
- Runner SHA-256:
  `d8b1f23d59eed8f504b6dc0dd1a413e3c2fcddfa1d984eb87f78000711793bdf`.
- Shared Linux container, not a dedicated performance machine. Starting host
  load average was `0.12 0.55 1.06`; scheduling noise remains part of the result.
- Full JSON, per-second resource samples, environment metadata and logs accompany
  this deliverable. Two earlier successful sanity runs and a development compile
  failure (unused import, corrected before load) were retained separately.

## Final sanity results

All latency values below are milliseconds, exact nearest-rank over the stated
observations. HTTP counters include final probes; active throughput excludes
prefill and final probes. Transport counters include prefill, while transport
latency excludes prefill.

| Measurement | 900 cached | 1,024 cached |
| --- | ---: | ---: |
| Cache count at every resource sample | 900 | 1,024 |
| Active HTTP responses/second | 799.07 | 773.23 |
| HTTP response samples including probes | 4,001 | 3,888 |
| HTTP RTT p50 / p95 / p99 | 1.102 / 2.187 / 3.628 | 1.167 / 2.945 / 5.460 |
| HTTP handler p50 / p95 / p99 | 0.083 / 0.358 / 0.917 | 0.084 / 0.619 / 1.775 |
| Fast successful pipe writes, latency samples | 2,603 | 2,766 |
| Fast pipe-write p50 / p95 / p99 | 0.0084 / 0.0357 / 0.1877 | 0.0082 / 0.0468 / 0.3123 |
| Slow successful pipe writes, latency samples | 66 | 63 |
| Slow pipe-write p50 / p95 / p99 | 75.519 / 76.062 / 76.290 | 75.513 / 77.470 / 169.632 |
| HTTP 202 / 429 / 503 | 2,696 / 1,304 / 1 | 2,850 / 1,037 / 1 |
| Queue admissions, including prefill | 3,833 | 4,113 |
| Write attempts / completed / failed | 3,574 / 3,569 / 5 | 3,857 / 3,853 / 4 |
| Queued entries abandoned on disconnect | 259 | 256 |
| Writer expiry skips in normal-TTL scenario | 0 | 0 |
| Same-ID reconnect cycles | 2 | 2 |
| Maximum sampled RSS (whole process, bytes) | 57,516,032 | 67,346,432 |
| Maximum sampled Go heap allocated (bytes) | 23,286,344 | 37,342,560 |
| Goroutines: before load / max sampled / after cleanup | 24 / 86 / 9 | 24 / 86 / 9 |

The 1,024-entry case contains 16 explicit cache-full rejections. Other 429s were
queue-full (1,297 / 981) or HTTP concurrency-limit rejections (7 / 40). These
failures are part of the workload, not omitted from success summaries. No client,
read, fixture decode, hash, unexpected-code or observer errors were observed.

The slow-reader p99 of **169.632ms** in the full-cache case is retained, despite a
75ms configured reader delay. Small sample counts and shared-container scheduling
make this unsuitable for an SLO claim. Percentiles contain only completed writes;
failed writes and abandoned queues have separate counts and failure-duration
series in the full JSON. No abandoned queue entry became a success sample.

The cache-age series is also in the JSON, but intentionally not presented here as
queue latency: reconnect retries can have several seconds of existing cache age
before a new enqueue. Exact enqueue-to-write timing remains uninstrumented.

## Expiry and complete-queue backpressure

The separate 250ms-TTL probe observed:

- 65 successful queue admissions, then one queue-full HTTP 429.
- One already-started write completed after the cache TTL and was decoded locally.
- Exactly 64 queued entries skipped after expiry, zero abandoned queued entries.
- Conservation: `65 = 1 attempted + 0 abandoned + 64 expired`.

This explicitly confirms the existing limitation that transaction expiry does not
interrupt a write already in progress. It is not a hidden success or a new TTL
relaxation.

## Checks and interpretation

- Final short harness: PASS, exit 0, 11.20s timed test.
- Focused race-enabled harness + quantile regression: PASS, exit 0, 13.80s timed
  load / 14.894s Go test process. Race timings are not mixed with normal timings.
- Entire `internal/relay` race suite with the opt-in load disabled: PASS.
- `go vet ./internal/relay`, `gofmt` check and runner `bash -n`: PASS.
- No multi-minute/hour soak, dedicated capacity run, live network test or full
  repository release verification was performed by this change.

The observed cache and queue bounds held; worker goroutines returned below the
pre-load count after connections closed. RSS and heap fluctuated and did not
prove a long-term plateau. Fixed telemetry buffers and Go allocator retention
remain in the process after load. This is useful short sanity evidence, not a
long-duration memory-stability or production-readiness claim. Follow-up gates
and interpretation limits are in [SOAK.md](SOAK.md).
