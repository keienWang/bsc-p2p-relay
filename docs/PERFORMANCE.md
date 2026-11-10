# Local load and performance checks

Date: 2026-11-10. Go 1.26.10, Linux amd64, Intel Xeon Platinum 8573C, GOMAXPROCS=9. These are in-memory measurements, not network delivery SLOs or production capacity.

## Identified bottleneck and change

The near-capacity fixture holds 900 cached hashes and 20 peers, each remembering 900 hashes. Previously every submission scanned every per-peer map while holding the submission lock, about 18,000 map entries per call. A CPU profile attributed 54.55% cumulative CPU to map iteration.

The revised implementation checks the target hash deadline exactly on every access, sweeps each map at most once per second on ordinary submissions, and forces reclamation when a hard count bound needs room. Periodic maintenance also releases expired peer metadata while idle. Capacity reclamation happens only for a new admission, after checking a valid duplicate, so a full map does not restore per-request scans for duplicates. Physical map size remains bounded by the configured cache count. Transaction payload deadlines and cache validity checks are unchanged.

Three one-second benchmark repetitions:

- Duplicate submission: before 434,680–460,550 ns/op; after 71,604–86,539 ns/op. Approximately 1,600 bytes and 45 allocations per operation.
- Fresh submission with explicit fixture recycling: before 420,192–476,873 ns/op; after 75,645–101,459 ns/op. Approximately 2,241 bytes and 85 allocations per operation. This measurement includes queue/cache/map recycling and is not pure admission cost.

An additional exactly-full-capacity fixture (1,024 hashes × 20 peers) measured duplicate submission at 77,096–82,824 ns/op, 1,593 bytes and 44 allocations over three runs. A regression asserts that a duplicate does not force a capacity sweep.

Do not turn these figures into a claim of public-network transactions per second. No remote node acceptance or propagation latency is measured. Repeat on target hardware with transport enabled, varied cache occupancy, slow peers and realistic concurrent requests.

## Regression/load coverage

The load suite exercises full and partial queues, byte/count cache bounds, replacement peers, repeated submissions, simultaneous Health/pruning/submission, expired entries, exact target expiry before the next sweep, forced reclamation at capacity, and idle metadata cleanup. It asserts active deduplication, queue deadlines, cache byte accounting and hard map bounds.

Five race repetitions passed before integration; the full combined race suite also passed. This is bounded deterministic stress, not an extended soak. End-to-end p95/p99 measurements, sustained public-network churn and resource limits under real traffic remain open production gates.

Run:

```
go test -race -count=5 ./internal/relay -run 'TestLoad|TestCached|TestExpired|TestConcurrent'
go test ./internal/relay -run='^$' -bench=BenchmarkSubmitNearCapacity -benchmem -benchtime=1s -count=3
```
