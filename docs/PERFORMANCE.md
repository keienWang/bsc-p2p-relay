# Local load and performance checks

Date: 2026-11-10. Go 1.26.10, Linux amd64, Intel Xeon Platinum 8573C, GOMAXPROCS=9. These are in-memory measurements, not network delivery SLOs or production capacity.

The measurements below predate the subsequent [bounded cache expiry index](CACHE_EXPIRY_INDEX.md).
That document describes its invariants, regression coverage and benchmark plan;
these historical numbers are not measurements of the new index.

## Identified bottleneck and change

The near-capacity fixture holds 900 cached hashes and 20 peers, each remembering 900 hashes. Previously every submission scanned every per-peer map while holding the submission lock, about 18,000 map entries per call. A CPU profile attributed 54.55% cumulative CPU to map iteration.

The revised implementation checks the target hash deadline exactly on every access, sweeps each map at most once per second on ordinary submissions, and forces reclamation when a hard count bound needs room. Periodic maintenance also releases expired peer metadata while idle. Capacity reclamation happens only for a new admission, after checking a valid duplicate, so a full map does not restore per-request scans for duplicates. Physical map size remains bounded by the configured cache count. Transaction payload deadlines and cache validity checks are unchanged.

Three one-second benchmark repetitions:

- Duplicate submission: before 434,680–460,550 ns/op; after 71,604–86,539 ns/op. Approximately 1,600 bytes and 45 allocations per operation.
- Fresh submission with explicit fixture recycling: before 420,192–476,873 ns/op; after 75,645–101,459 ns/op. Approximately 2,241 bytes and 85 allocations per operation. This measurement includes queue/cache/map recycling and is not pure admission cost.

An additional exactly-full-capacity fixture (1,024 hashes × 20 peers) measured duplicate submission at 77,096–82,824 ns/op, 1,593 bytes and 44 allocations over three runs. A regression asserts that a duplicate does not force a capacity sweep.

Do not turn these figures into a claim of public-network transactions per second. No remote node acceptance or propagation latency is measured. Repeat on target hardware with transport enabled, varied cache occupancy, slow peers and realistic concurrent requests.

Successful writes do not free the cache, and the fresh-submit benchmark explicitly
recycles entries. Its operation rate is therefore not sustained new-hash capacity.
The separate [unique-hash capacity tests and retention budget](UNIQUE_CAPACITY.md)
exercise real count/byte rejection, deadline recovery, and first-offer versus retry
accounting without recycling live cache entries.

## Regression/load coverage

The load suite exercises full and partial queues, byte/count cache bounds, replacement peers, repeated submissions, simultaneous Health/pruning/submission, expired entries, exact target expiry before the next sweep, forced reclamation at capacity, and idle metadata cleanup. It asserts active deduplication, queue deadlines, cache byte accounting and hard map bounds.

Five race repetitions passed before integration; the full combined race suite also passed. This is bounded deterministic stress, not an extended soak. End-to-end p95/p99 measurements, sustained public-network churn and resource limits under real traffic remain open production gates.

Run:

```
go test -race -count=5 ./internal/relay -run 'TestLoad|TestCached|TestExpired|TestConcurrent'
go test ./internal/relay -run='^$' -bench=BenchmarkSubmitNearCapacity -benchmem -benchtime=1s -count=3
```

## Opt-in HTTP and transport-pressure harness

See [the local soak protocol](SOAK.md) for separately measured 900/full-1,024
occupancy, concurrent loopback HTTP, official BSC MsgPipe readers, slow-reader
backpressure, same-ID replacement, expiry conservation, percentiles and resource
snapshots. It is excluded from ordinary CI load and does not measure TCP/RLPx or
remote txpool acceptance. Exact queue-only latency and long-duration production
stability remain open gates.

A [dated short-run evidence report](SOAK_RESULTS_2026-11-10.md) records the exact
configuration, denominators, rejection/failure counts and successful latency
percentiles. It does not close the long-duration production gate.

The [v2 HTTP gate correction](SOAK_HTTP_VALIDATION.md) supersedes the original
soak acceptance result: unexpected statuses, malformed responses and missing
error denominators now fail both occupancy and expiry scenarios. Fault injections
are isolated from performance samples.

## Statistics observation cost

`BenchmarkStatsObservation` separates direct `Stats()` snapshots from the
in-process observation handler's JSON encoding plus `httptest.ResponseRecorder`
cost. Each has serial and parallel variants, on empty and 1,024-entry cache
fixtures with 20 synthetic session records. Fixture construction and signing are
outside timing. Counters stay fixed during timing; this does not measure atomic
write contention. No session connects, no transaction is submitted, and there
are no network requests. The handler benchmark deliberately includes each new
recorder's allocations but excludes request construction and real HTTP transport.

On a qualified Linux/amd64 development host with the reviewed toolchain and
native archive, use the repository's noexecstack linker policy explicitly:

```sh
go test -ldflags=-extldflags=-Wl,-z,noexecstack ./internal/relay \
  -run='^TestStatsSnapshotAllocations$' \
  -bench='^BenchmarkStatsObservation$' -benchmem -benchtime=1s -count=3
```

See [the Linux build policy](LINUX_BUILD.md) before using that platform-specific
flag; it is not a portable command for other linkers. Record Go version, CPU,
GOMAXPROCS and repeated measurements when comparing revisions. Do not use race
instrumentation for performance numbers; run race separately for correctness.
Parallel `ns/op` is aggregate work per operation, not per-request latency or a
p95/p99. These measurements are not HTTP capacity, transaction TPS, remote
acceptance, an SLO, or production readiness. CPU timing is informational: there
is no hardware-dependent CI timing threshold.

`TestStatsSnapshotAllocations` runs in the ordinary test suite and guards the
snapshot core's zero heap-allocation property. That narrow regression does not
claim zero allocations for JSON encoding, HTTP, metadata initialization or the
relay process. The existing lock-independence and concurrency tests remain the
correctness checks; benchmarks do not replace them.
